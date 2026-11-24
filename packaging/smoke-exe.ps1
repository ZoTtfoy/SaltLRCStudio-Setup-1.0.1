# Smoke-tests the packaged, windowless executable end to end.
param([int]$Port = 8899, [string]$Exe)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $root 'dist\SaltLRCStudio.exe' }
$base = "http://127.0.0.1:$Port"
$log = @()

function note($ok, $name, $detail) {
  $script:log += ("{0} {1}{2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $name, $(if (-not $ok) { " :: $detail" }))
}

if (-not (Test-Path $Exe)) { throw "exe not found: $Exe" }
# 调试接口默认关闭（口令只从环境变量来），本次冒烟自带一个专属口令，
# 让 exe 的子进程继承 —— 冒烟顺带把"设了口令就能用"这条也验掉。
$DebugPass = 'smoke-debug-pass'
$env:SALTLRC_DEBUG_PASSWORD = $DebugPass
$p = Start-Process -FilePath $Exe -ArgumentList "--port=$Port", '--no-browser' -PassThru
note $true "started exe (pid $($p.Id))" ''

$timer = [Diagnostics.Stopwatch]::StartNew()
$up = $false
while ($timer.Elapsed.TotalSeconds -lt 90) {
  Start-Sleep -Milliseconds 700
  try { $null = Invoke-WebRequest -Uri "$base/api/capabilities" -UseBasicParsing -TimeoutSec 3; $up = $true; break }
  catch { }
}
note $up "server answers on $base" 'no response within 90s'
if (-not $up) { $log | Set-Content -Encoding UTF8 (Join-Path $root '_exe_smoke.log'); exit 1 }

# 1. wrong password
try { $null = Invoke-RestMethod -Uri "$base/api/debug/auth" -Method Post -Body '{"password":"nope"}' -ContentType 'application/json'; note $false 'wrong password rejected' 'accepted' }
catch { note ($_.Exception.Response.StatusCode.value__ -eq 403) 'wrong password rejected' $_.Exception.Message }

# 2. right password -> token
$token = ''
try { $auth = Invoke-RestMethod -Uri "$base/api/debug/auth" -Method Post -Body ('{"password":"' + $DebugPass + '"}') -ContentType 'application/json'; $token = $auth.token; note ($token.Length -gt 10) 'password returns token' '' }
catch { note $false 'password returns token' $_.Exception.Message }

# 3. gated state
try { $null = Invoke-RestMethod -Uri "$base/api/debug/state"; note $false 'state requires token' 'served without token' }
catch { note ($_.Exception.Response.StatusCode.value__ -eq 401) 'state requires token' $_.Exception.Message }

# 4. state with token keeps errors + requests + log dir
$state = Invoke-RestMethod -Uri "$base/api/debug/state" -Headers @{ 'X-Debug-Token' = $token }
note ($null -ne $state.log_dir) 'state exposes log dir' ''
note ($null -ne $state.requests) 'state records requests' ''
note ($state.requests.Count -gt 0) 'last action recorded' ''
$lastPath = $state.requests[-1].path
note ($lastPath -like '/api/debug*') "last action is the newest call ($lastPath)" ''

# 5. front-end report channel + bundle
$null = Invoke-RestMethod -Uri "$base/api/debug/client-error" -Method Post -Body '{"message":"ui blew up","source":"/library"}' -ContentType 'application/json'
$bundle = Invoke-RestMethod -Uri "$base/api/debug/bundle" -Method Post -Body '{}' -ContentType 'application/json' -Headers @{ 'X-Debug-Token' = $token }
note ($bundle.bytes -gt 0) "diagnostics bundle written ($($bundle.path))" ''
$state2 = Invoke-RestMethod -Uri "$base/api/debug/state" -Headers @{ 'X-Debug-Token' = $token }
$matched = @($state2.errors | Where-Object { $_.message -like '*ui blew up*' })
note ($matched.Count -gt 0) "front-end error kept in journal ($($matched.Count) match)" ''

# 6. exit through the footer endpoint
try { $null = Invoke-RestMethod -Uri "$base/api/exit" -Method Post -Body '{}' -ContentType 'application/json'; note $true 'exit endpoint answered' '' }
catch { note $false 'exit endpoint answered' $_.Exception.Message }
# A onefile build is a bootloader plus a child process, and shutting down means
# the child tears down Python and the bootloader then removes the unpacked
# temp directory. A flat 3s wait was too short and reported "still running" for
# a process that had in fact exited correctly, so poll instead of guessing.
$gone = $false
$timer = [Diagnostics.Stopwatch]::StartNew()
while ($timer.Elapsed.TotalSeconds -lt 30) {
  Start-Sleep -Milliseconds 500
  $live = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
  if (-not $live) { $gone = $true; break }
  if ($live.HasExited) { $gone = $true; break }
}
note $gone 'process exited after /api/exit' 'still running after 30s'
if (-not $gone) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }

$log | Set-Content -Encoding UTF8 (Join-Path $root '_exe_smoke.log')
if ($log -match '^FAIL') { exit 1 }
exit 0
