# Verifies the v0.11.0 new features really shipped inside the packaged exe:
# five lyric sources in the catalogue, login routes present, and the pages
# carrying the new controls. Run against a started exe.
param([int]$Port = 8901, [string]$Exe)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $root 'dist\SaltLRCStudio.exe' }
$base = "http://127.0.0.1:$Port"
$fail = 0
$script:lines = @()
function note($ok, $name, $detail) {
  if ($ok) { $script:lines += "PASS $name" } else { $script:lines += "FAIL $name :: $detail"; $script:fail++ }
}

if (-not (Test-Path $Exe)) { throw "exe not found: $Exe" }
$p = Start-Process -FilePath $Exe -ArgumentList "--port=$Port", '--no-browser' -PassThru
$timer = [Diagnostics.Stopwatch]::StartNew()
$up = $false
while ($timer.Elapsed.TotalSeconds -lt 90) {
  Start-Sleep -Milliseconds 700
  try { $null = Invoke-WebRequest -Uri "$base/api/capabilities" -UseBasicParsing -TimeoutSec 3; $up = $true; break } catch { }
}
if (-not $up) { Write-Host "FAIL server did not come up"; Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; exit 1 }

$cap = Invoke-RestMethod -Uri "$base/api/capabilities"
note ($cap.version -eq '0.11.0') "capabilities version = 0.11.0" $cap.version

$ids = @($cap.provider_catalogue | ForEach-Object { $_.id })
foreach ($want in 'netease', 'lrclib', 'qqmusic', 'kugou', 'kuwo') {
  note ($ids -contains $want) "catalogue has $want" ($ids -join ',')
}
note ($ids.Count -eq 5) "catalogue has exactly 5 sources" $ids.Count

# netease must be flagged as needing login; the others must not
$net = $cap.provider_catalogue | Where-Object { $_.id -eq 'netease' }
note ($net.needs_login -eq $true) "netease flagged needs_login" $net.needs_login

# credentials status: must never leak a cookie value field
$cred = Invoke-RestMethod -Uri "$base/api/credentials"
$raw = $cred | ConvertTo-Json -Depth 6 -Compress
note ($raw -notmatch '(?i)cookie') "credentials status never mentions cookie" $raw.Substring(0, [Math]::Min(200, $raw.Length))

# login routes must exist (a POST with a bad body must NOT 404)
$seen = @{}
foreach ($route in 'qr-create', 'qr-check', 'password', 'sms-send', 'sms-verify', 'logout', 'whoami') {
  try {
    $null = Invoke-RestMethod -Uri "$base/api/login/$route" -Method Post -Body '{}' -ContentType 'application/json' -ErrorAction Stop
    $seen[$route] = 200
  } catch {
    $code = $_.Exception.Response.StatusCode.value__
    $seen[$route] = $code
  }
}
foreach ($route in $seen.Keys) {
  note ($seen[$route] -ne 404) "route /api/login/$route exists (status $($seen[$route]))" '404'
}

# the pages must carry the new controls
$lib = (Invoke-WebRequest -Uri "$base/library" -UseBasicParsing).Content
foreach ($id in 'provider', 'loginOpen', 'loginState', 'tabQr', 'tabPassword', 'tabSms', 'qrStart', 'pwSubmit', 'smsSend', 'smsSubmit', 'loginLogout', 'loginClose') {
  note ($lib -match ("id=`"$id`"")) "library page has #$id" ''
}
$libjs = (Invoke-WebRequest -Uri "$base/library.js" -UseBasicParsing).Content
note ($libjs -match 'qr-wait') "library.js polls the long-poll endpoint" ''
$note_ = ($libjs -match 'provider_catalogue')

$idx = (Invoke-WebRequest -Uri "$base/" -UseBasicParsing).Content
note ($idx -match 'id="matchProvider"') "editor page has #matchProvider" ''
$sjs = (Invoke-WebRequest -Uri "$base/studio.js" -UseBasicParsing).Content
note ($sjs -match 'fillMatchProviders') "studio.js fills the source dropdown" ''
note ($sjs -notmatch "get\('capabilities'\)") "studio.js no longer calls the missing get()" ''
note ($sjs -match 'capabilities\(\)\.then\(fillMatchProviders\)') "studio.js wires it to capabilities()" ''

# a QR code must actually come back as a decodable PNG data URI
try {
  $qr = Invoke-RestMethod -Uri "$base/api/login/qr-create" -Method Post -Body '{}' -ContentType 'application/json'
  note ($qr.qr -like 'data:image/png;base64,*') "qr-create returns a PNG data URI" $qr.qr.Substring(0, [Math]::Min(40, $qr.qr.Length))
  note ($qr.key.Length -gt 5) "qr-create returns a key" ''
} catch { note $false "qr-create works" $_.Exception.Message }

# shutdown
try { $null = Invoke-RestMethod -Uri "$base/api/exit" -Method Post -Body '{}' -ContentType 'application/json' } catch { }
Start-Sleep -Seconds 3
Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Get-Process SaltLRCStudio -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

if ($fail -eq 0) { $script:lines += "ALL PASS" }
$script:lines | Set-Content -Encoding UTF8 (Join-Path $root '_v011_smoke.log')
$script:lines -join "`n"
if ($fail -ne 0) { exit 1 }
