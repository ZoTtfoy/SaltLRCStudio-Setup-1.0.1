# SaltLRC Studio —— 一条命令做完发布（Windows PowerShell 5.1 兼容）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File packaging\release.ps1                 # 用当前版本
#   powershell -ExecutionPolicy Bypass -File packaging\release.ps1 -Version 0.21.0 # 先改版本号，再发布
#   powershell -ExecutionPolicy Bypass -File packaging\release.ps1 -SkipTests      # 跳过测试（不推荐）
#   powershell -ExecutionPolicy Bypass -File packaging\release.ps1 -MakeInstaller  # 顺便编译安装包（需要 Inno Setup）
#
# 它把"每次手做的那一串"按顺序做掉，任何一步失败就停（绝不允许"半成品被当成发布版"）：
#   1) 版本号三处同步（唯一权威 saltlrc\__init__.py → pyproject.toml、使用说明首行）
#   2) 全量测试（**必须全绿**，允许 skip；v0.22.2 起不再给"缺 numpy"开特例）
#   3) 打包 exe（build.ps1）
#   4) exe 冒烟（smoke-exe.ps1，要求没有 FAIL）
#   5) 资源时间早于构建时间（否则 exe 里是旧页面）
#   6) 新建交付目录 Desktop\SaltLRCStudio-v<版本>，复制产物 + 源码（清 pycache）
#   7) 交付前后比对哈希（残留进程锁住 exe 时复制会静默失败 —— 第 1 条老坑）
#   8) 从**交付路径**启动 exe（--no-browser，绝不弹窗），跑页面自检，再 /api/exit
#   9) 打印"还需要人工做的"（几份手写文档）
#
# 手写文档不在脚本里：目录说明.txt / 修改记录.md 的"这一版改了什么"是写给用户的，
# 每次内容都不一样 —— 脚本只保证"机械的那部分不会漏、不会拿旧的充数"。

param(
  [string]$Version = '',
  [switch]$SkipTests,
  [switch]$MakeInstaller
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path (Join-Path $here '..')).Path
Set-Location $root

$py = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$node = 'C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe'
$nodeModules = 'C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules'

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "  PASS $text" -ForegroundColor Green }
# 自己产生的东西要收掉 —— 不然每跑一次发布就往 %TEMP% 里留一份。
# 这和 v0.23.1 修的"测试漏临时目录"是同一类毛病。
# （v0.23.5 起 Run-Native 不再写 .out/.err 了 —— 那 11 个文件连同这份清理代码
#   一起没了：**让东西不产生，比记得删它更可靠**。）
$script:ScratchDir = ''
function Cleanup-Temp {
  # 先收掉自己启动的那个 exe：交付目录里的 exe 被进程锁住时，
  # `Copy-Item` 会**静默失败**（第 1 条老坑），下一次发布就会复制到一个旧文件。
  Get-Process SaltLRCStudio -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  $dir = $script:ScratchDir
  $script:ScratchDir = ''
  # 只删本脚本自己在 %TEMP% 下建的那一个（名字前缀 + 必须在 TEMP 里，两道都满足才动手）
  if ($dir -and (Test-Path -LiteralPath $dir) -and ($dir -like (Join-Path $env:TEMP 'saltlrc-release-*'))) {
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
  }
}
function Die($text)  { Write-Host "  FAIL $text" -ForegroundColor Red; Cleanup-Temp; exit 1 }
# 写文件一律**不带 BOM**：Set-Content -Encoding UTF8 在 PowerShell 5.1 下会塞一个 BOM，
# 那会悄悄改掉交付文件的头几个字节（使用说明.txt 现在是没有 BOM 的）。
function Write-NoBom($path, $text) {
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}
# 读文本**必须**显式按 UTF-8。`Get-Content -Raw` 在 PS 5.1 下对没有 BOM 的文件按系统
# ANSI（本机 GBK）解码 → 中文变乱码；再写回去就是双重编码，整份文件报废。
# （v0.20.0 第一次跑这个脚本时就这么把 dist\使用说明.txt 搞坏了，
#   靠交付目录里上一版的副本才恢复。这条是那次踩出来的。）
function Read-Utf8($path) {
  return [System.IO.File]::ReadAllText($path, (New-Object System.Text.UTF8Encoding($false)))
}
# 跑外部程序：**带超时**，并且**不经过 cmd.exe**。
#
# 为什么要超时（v0.23.5，这次真的踩了）：
#   原来这里是 `& cmd.exe /c '"exe" "a" "b" > out 2> err'`，它**没有超时**。
#   子进程一旦卡住（等一个打不开的网络地址、等一个弹不出来的系统对话框），
#   整条发布流程就**永久停在那里，而且一个字都不输出** ——
#   实测卡了 13 分钟，日志停在"从交付路径启动"之后，看起来和"正在慢慢跑"一模一样。
#   这正是本项目最讨厌的那一类毛病：**失败没有声音**。
#   现在每次调用都带上限；超时就杀掉子进程，并把"超时"当成一次**失败**报出来。
#
# 为什么不再用 cmd.exe 重定向：
#   改成 .NET 直接起进程 + **异步**读 stdout/stderr。既不用往 %TEMP% 里写
#   .out/.err（少一份要清理的东西），也绕开了"PS 5.1 把原生程序的 stderr
#   包成 ErrorRecord、而 $ErrorActionPreference='Stop' 又把它升成终止错误"的老毛病。
#   两条流必须都异步读：只读一条时，另一条写满管道缓冲区就会互相死等（超时都救不了）。
#   CreateNoWindow=$true：这台机器上不许开窗口。
# 工作目录跟着**当前目录**走，不固定成 $root —— 第 6 步要在"交付的 源码\"里跑测试。
# 参数拼接一律走下面的 `Quote-Arg` —— 引号不转义的话参数会被截断。

# 拼命令行参数：每个参数单独引号化，**并且处理参数里本来就有的引号**。
#
# 为什么必须处理（v0.23.5，新写的探针当场撞出来的）：
#   直接 `'"' + $arg + '"'` 的话，参数里含 `"` 时 Windows 会在那个引号处
#   **提前结束这个参数** —— 实测 `python -c "…sys.stdout.write("OUT")"` 里的
#   引号被吃掉，子进程收到的是 `…write(OUT)`，报 `NameError: name 'OUT' is not defined`。
#   （原来的 cmd.exe 写法把这件事糊过去了，换成 .NET 直接起进程才露出来。）
# 规则照 Windows 的 CommandLineToArgvW：引号写成 `\"`，
#   而**引号前**和**结尾**的反斜杠都要翻倍，否则它们会被当成转义符吃掉收尾引号。
function Quote-Arg($arg) {
  $text = [string]$arg
  if ($text -eq '') { return '""' }              # 空参数也得占个位置，不能凭空消失
  if ($text -notmatch '[\s"]') { return $text }  # 没有空格也没有引号 → 原样给
  $text = $text -replace '(\\*)"', '$1$1\"'      # 引号前的反斜杠翻倍，再把引号转义
  $text = $text -replace '(\\+)$', '$1$1'        # 结尾的反斜杠翻倍
  return '"' + $text + '"'
}
function Run-Native($exe, [string[]]$argv, $tag, $timeoutSec = 180) {
  $info = New-Object System.Diagnostics.ProcessStartInfo
  $info.FileName = $exe
  $info.Arguments = (($argv | ForEach-Object { Quote-Arg $_ }) -join ' ')
  $info.UseShellExecute = $false
  $info.CreateNoWindow = $true
  $info.RedirectStandardOutput = $true
  $info.RedirectStandardError = $true
  $info.WorkingDirectory = (Get-Location).Path
  $proc = [System.Diagnostics.Process]::Start($info)
  $outTask = $proc.StandardOutput.ReadToEndAsync()
  $errTask = $proc.StandardError.ReadToEndAsync()
  if (-not $proc.WaitForExit($timeoutSec * 1000)) {
    try { $proc.Kill() } catch {}
    $proc.WaitForExit()
    Die ($tag + ' 超过 ' + $timeoutSec + ' 秒还没结束（已杀掉）—— ' +
         '卡住的子进程通常是在等一个打不开的网络地址或一个弹不出来的系统对话框。')
  }
  [void]$proc.WaitForExit()          # 让两个异步读取把剩余内容收完
  $code = $proc.ExitCode
  $text = $outTask.Result + $errTask.Result
  return @{ Code = $code; Text = $text }
}

# ---------------------------------------------------------------- 1) 版本号
Step '1/9 版本号'
$initPath = Join-Path $root 'saltlrc\__init__.py'
$current = ([regex]::Match((Get-Content $initPath -Raw), '__version__ = "([^"]+)"')).Groups[1].Value
if (-not $current) { Die '读不到 saltlrc\__init__.py 里的 __version__' }
if ($Version -and $Version -ne $current) {
  Write-NoBom $initPath ((Get-Content $initPath -Raw) -replace '__version__ = "[^"]+"', ('__version__ = "' + $Version + '"'))
  $current = $Version
  Ok "saltlrc\__init__.py -> $current"
}
# pyproject.toml 与使用说明首行跟着唯一权威走（都用显式 UTF-8 读）
$pyproject = Join-Path $root 'pyproject.toml'
$text = Read-Utf8 $pyproject
$text = $text -replace 'version = "[^"]+"', ('version = "' + $current + '"')
Write-NoBom $pyproject $text
# 安装包脚本里的默认版本号也要跟（单独用 ISCC 编译时用的就是它）
$issPath = Join-Path $here 'installer.iss'
if (Test-Path $issPath) {
  $iss = Read-Utf8 $issPath
  $iss = $iss -replace '#define AppVersion "[^"]+"', ('#define AppVersion "' + $current + '"')
  Write-NoBom $issPath $iss
}
$usagePath = Join-Path $root 'dist\使用说明.txt'
if (Test-Path $usagePath) {
  $usage = Read-Utf8 $usagePath
  $usage = $usage -replace '# 使用说明（v[^）]+）', ('# 使用说明（v' + $current + '）')
  Write-NoBom $usagePath $usage
  # 写完读回来验首行形状 —— 只要出现乱码（例如按 ANSI 解码后写回的双重编码），
  # 首行就不再是这个形状，发布立刻停在这里，而不是把坏文件送出去。
  $firstLine = (Read-Utf8 $usagePath).Split("`n")[0].Trim()
  if ($firstLine -ne ('# 使用说明（v' + $current + '）')) {
    Die ('《使用说明》首行不对（很可能被按 ANSI 解码后写回了）：' + $firstLine +
         '  ← 从上一版交付目录取回 dist\使用说明.txt 再重跑')
  }
}
# 交付目录的索引也跟着版本号走（它随交付一起发出，首行必须对得上当前版本）
$tocPath = Join-Path $root 'dist\目录说明.txt'
if (Test-Path $tocPath) {
  $toc = Read-Utf8 $tocPath
  $toc = $toc -replace '^SaltLRC Studio v[^\s]+ —— 目录说明', ('SaltLRC Studio v' + $current + ' —— 目录说明')
  Write-NoBom $tocPath $toc
  $tocFirst = (Read-Utf8 $tocPath).Split("`n")[0].Trim()
  if ($tocFirst -ne ('SaltLRC Studio v' + $current + ' —— 目录说明')) {
    Die ('《目录说明》首行不对（很可能被按 ANSI 解码后写回了）：' + $tocFirst)
  }
}
Ok "版本 = $current（三处已同步）"

# ---------------------------------------------------------------- 2) 测试
if (-not $SkipTests) {
  Step '2/9 全量测试'
  $run = Run-Native $py @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-t', 'tests') 'tests' 900
  $out = $run.Text
  # **必须全绿**（允许 skip），不再有"已知失败"这一档。
  # v0.22.2 之前这里给"缺 numpy 的 test_adapters"开了特例，理由是可选的 ASR 依赖。
  # 那个特例现在已经不需要了：ASR 契约对三方库打桩，缺依赖时的**可读报错**另有断言
  # （tests/test_adapters.py），所以在装没装可选依赖的机器上都跑得出结论。
  # 留着特例的坏处很实在：闸门"允许一个失败"就等于给所有失败留了一扇门。
  if ($out -notmatch '(?m)^OK') {
    Write-Host $out
    Die '测试未通过（本闸门只认全绿；可选依赖缺失已不该再造成失败）'
  }
  Ok ([regex]::Match($out, 'Ran \d+ tests').Value +
      $(if ($out -match 'skipped') { '（含 skip）' } else { '' }))
}

# ---------------------------------------------------------------- 3) 打包
Step '3/9 打包 exe'
$assetNewest = Get-ChildItem (Join-Path $root 'saltlrc\web') -Recurse -File |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
$build = Run-Native 'powershell' @('-ExecutionPolicy', 'Bypass', '-File', (Join-Path $here 'build.ps1')) 'build' 900
if ($build.Code -ne 0) { Write-Host $build.Text; Die '打包失败（build.ps1 退出码非 0）' }
$exe = Join-Path $root 'dist\SaltLRCStudio.exe'
if (-not (Test-Path $exe)) { Die '打包没有产出 dist\SaltLRCStudio.exe' }
$built = (Get-Item $exe).LastWriteTime
if ($assetNewest.LastWriteTime -gt $built) {
  Die ('资源比 exe 新：' + $assetNewest.FullName + ' —— exe 里会是旧页面')
}
Ok ('exe ' + [math]::Round((Get-Item $exe).Length / 1MB, 1) + ' MB，构建于 ' + $built.ToString('HH:mm:ss'))

# ---------------------------------------------------------------- 4) 冒烟
Step '4/9 exe 冒烟'
$port = 8912
$smoke = Run-Native 'powershell' @('-ExecutionPolicy', 'Bypass', '-File', (Join-Path $here 'smoke-exe.ps1'), '-Port', $port) 'smoke' 300
if ($smoke.Code -ne 0) { Write-Host $smoke.Text; Die '冒烟脚本退出码非 0' }
$log = Get-Content (Join-Path $root '_exe_smoke.log') -Raw
if ($log -match 'FAIL') { Write-Host $log; Die '冒烟里有 FAIL' }
$pass = ([regex]::Matches($log, 'PASS')).Count
if ($pass -lt 13) { Write-Host $log; Die "冒烟只过了 $pass 项（预期 13）" }
Ok "$pass/13"

# ---------------------------------------------------------------- 5-7) 交付
Step '5/9 新建交付目录并复制'
$dest = Join-Path ([Environment]::GetFolderPath('Desktop')) ('SaltLRCStudio-v' + $current)
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
New-Item -ItemType Directory -Path $dest | Out-Null
Copy-Item $exe (Join-Path $dest 'SaltLRCStudio.exe')
# 工程文件的图标：`.saltproj` 的关联把 DefaultIcon 直接指向这个文件
# （见 saltlrc\assoc.py），所以它**必须和 exe 在同一个目录**。
# 忘了复制的话，图标会静默退回程序自身的图标 —— 下面第 6 步会当场拦下来。
Copy-Item (Join-Path $here 'icons\SaltLRCStudio-project.ico') $dest
Copy-Item (Join-Path $root 'dist\使用说明.txt') $dest
Copy-Item (Join-Path $root 'dist\目录说明.txt') $dest
# 交付里只放**发行用**文档：`RELEASE_NOTES.md` 是「每版改了什么」，`CHANGELOG.md` 是
# 开发用的完整档案（含排查过程），不随交付发出 —— 交付物要能直接给外人看。
Copy-Item (Join-Path $root 'docs\RELEASE_NOTES.md')   (Join-Path $dest '更新日志.md')
Copy-Item (Join-Path $root 'docs\ARCHITECTURE.md')    (Join-Path $dest '分层结构.md')
Copy-Item (Join-Path $root 'docs\WINDOW_STRUCTURE.md') (Join-Path $dest '窗口结构.md')
Copy-Item (Join-Path $root 'docs\AUDIO_ALIGNMENT.md') (Join-Path $dest '音频对齐说明.md')
Copy-Item (Join-Path $root 'saltlrc\web\debug-panel.js') (Join-Path $dest '调试面板插件.js')
Copy-Item (Join-Path $root 'README.md') $dest
Copy-Item (Join-Path $root 'THIRD_PARTY_NOTICES.md') $dest
New-Item -ItemType Directory -Path (Join-Path $dest '图标源文件') | Out-Null
Copy-Item (Join-Path $here 'icons\*') (Join-Path $dest '图标源文件')
New-Item -ItemType Directory -Path (Join-Path $dest '源码') | Out-Null
foreach ($item in 'saltlrc', 'tests', 'docs', 'examples', 'packaging', 'app_entry.py',
                  'pyproject.toml', 'THIRD_PARTY_NOTICES.md', 'SaltLRCStudio.spec',
                  'launcher.cs', 'setup-ai.ps1', 'start.bat') {
  Copy-Item (Join-Path $root $item) (Join-Path $dest '源码') -Recurse -Force
}
Get-ChildItem (Join-Path $dest '源码') -Recurse -Directory -Filter '__pycache__' |
  Remove-Item -Recurse -Force
Get-ChildItem (Join-Path $dest '源码') -Recurse -File -Filter '*.pyc' | Remove-Item -Force
Ok $dest

Step '6/9 比对哈希 + 交付源码自洽'
$a = (Get-FileHash $exe -Algorithm SHA256).Hash
$b = (Get-FileHash (Join-Path $dest 'SaltLRCStudio.exe') -Algorithm SHA256).Hash
if ($a -ne $b) { Die '复制后的 exe 与 dist 的不一致（残留进程会锁住 exe 让复制静默失败）' }
Ok 'SHA256 一致'
# `.saltproj` 的图标必须跟着 exe 一起走：关联里写的是"exe 旁边那个 .ico"。
# 少了它，程序不会报错，只会静默退回程序自身的图标 —— 那种失败没人会注意到。
if (-not (Test-Path (Join-Path $dest 'SaltLRCStudio-project.ico'))) {
  Die '交付目录里没有 SaltLRCStudio-project.ico —— .saltproj 的图标会退回程序图标'
}
Ok '工程图标 SaltLRCStudio-project.ico 已随 exe 交付'
# 交付的 源码\ 必须**自己**能跑通测试：既证明拷贝完整，也证明它不依赖工作区。
# （有一次漏拷了 tests\ 下的文件，就是这条查出来的。）
Set-Location (Join-Path $dest '源码')
$srcRun = Run-Native $py @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-t', 'tests') 'srccopy2' 900
Set-Location $root
$srcOk = $srcRun.Text -match '(?m)^OK'      # 与第 2 步同一条判据：只认全绿
if (-not $srcOk) { Write-Host $srcRun.Text; Die '交付的 源码\ 单独跑测试不通过' }
Ok ([regex]::Match($srcRun.Text, 'Ran \d+ tests').Value + '（交付源码目录里跑的）')

# ---------------------------------------------------------------- 8) 交付物自证
Step '7/9 从交付路径启动并自检'
Get-Process SaltLRCStudio -ErrorAction SilentlyContinue | Stop-Process -Force
$probe = 8913
$env:SALTLRC_CONFIG_DIR = Join-Path $env:TEMP ('saltlrc-release-' + $current)
$script:ScratchDir = $env:SALTLRC_CONFIG_DIR      # 收尾时由 Cleanup-Temp 删掉
# 起交付的 exe：**用 .NET 直接起，别用 `cmd /c start /B`，也别用 Start-Process。**
#
# 为什么不能用 `& cmd.exe /c 'start "" /B "app.exe" …'`（这一版卡死 13 分钟的真因）：
#   **那一句根本不会返回。** exe 起来了、输出也进了文件，但 PowerShell 一直等它，
#   于是脚本永久停在那一行、**一个字都不输出**；日志停在 exe 打的那两行上，
#   看起来和"正在慢慢跑"一模一样。
#   证据（探针 v1，进度用无缓冲落盘汇报，不经过管道）：把 app 杀掉的那一瞬间，
#   那一条命令才结束。加 `> "文件" 2>&1` **并不能**救它 —— 试过了。
# 为什么不能用 `Start-Process`（本机第二次撞上）：
#   PS 5.1 的 Start-Process 会拼一个环境变量字典，而本机同时存在 `https_proxy` 与
#   `HTTPS_PROXY`（大小写不同的同一个变量）→ 直接抛
#   `已添加项。字典中的关键字:"https_proxy"所添加的关键字:"HTTPS_PROXY"`。
# 所以：.NET 起进程，两条流都是**新建的管道**（不会继承脚本自己的 stdout，
# 因此不会攥住脚本的输出管道），并且**立刻异步排空**（不排空的话，
# 子进程写满管道缓冲区就会自己卡住）。CreateNoWindow：这台机器上不许开窗口。
$appLog = Join-Path $root '_exe_run.log'
$appInfo = New-Object System.Diagnostics.ProcessStartInfo
$appInfo.FileName = (Join-Path $dest 'SaltLRCStudio.exe')
$appInfo.Arguments = '--no-browser --port=' + $probe
$appInfo.UseShellExecute = $false
$appInfo.CreateNoWindow = $true
$appInfo.RedirectStandardOutput = $true
$appInfo.RedirectStandardError = $true
$appInfo.WorkingDirectory = (Get-Location).Path
$app = [System.Diagnostics.Process]::Start($appInfo)
$appOut = $app.StandardOutput.ReadToEndAsync()
$appErr = $app.StandardError.ReadToEndAsync()
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 500
  try { Invoke-WebRequest ("http://127.0.0.1:$probe/") -UseBasicParsing -TimeoutSec 2 | Out-Null; $ready = $true; break } catch {}
}
if (-not $ready) {
  try { $app.Kill() } catch {}
  [System.IO.File]::WriteAllText($appLog, $appOut.Result + $appErr.Result)
  Die ('交付的 exe 没能在 20 秒内起来（端口 ' + $probe + '）。先看它自己的启动输出：' +
       $appLog + ' —— 里面通常是"端口被占"或"配置目录不可写"这类原因。')
}
Ok "已启动，正在等它答话"
$caps = Invoke-WebRequest ("http://127.0.0.1:$probe/api/capabilities") -UseBasicParsing -TimeoutSec 15
$served = ($caps.Content | ConvertFrom-Json).version
if ($served -ne $current) { Die ("交付的 exe 报的版本是 $served，不是 $current") }
Ok "已就绪并答话（版本 $served）"
if ($node -and (Test-Path $node)) {
  $env:NODE_PATH = $nodeModules
  $pageCheck = Run-Native $node @('tests\web_dom_check.js', ("http://127.0.0.1:$probe")) 'page' 300
  if ($pageCheck.Text -notmatch '0 failed') { Write-Host $pageCheck.Text; Die '交付 exe 上的页面接线检查没全过' }
  Ok ([regex]::Match($pageCheck.Text, '\d+ checks, 0 failed').Value)
}
Invoke-WebRequest ("http://127.0.0.1:$probe/api/exit") -Method POST -Body '{}' -ContentType 'application/json' -UseBasicParsing -TimeoutSec 15 | Out-Null
# **确认它真的退了。** 以前这里只是 `Start-Sleep 3` 再报一句"已退出"——
# 那是在**猜**，不是在验。现在拿进程句柄等它，等不到就算失败
# （`/api/exit` 这条路走不通时，交付物会留一个听着的端口在用户机器上）。
$exited = $app.WaitForExit(30000)
[System.IO.File]::WriteAllText($appLog, $appOut.Result + $appErr.Result)
if (-not $exited) {
  try { $app.Kill() } catch {}
  Die '交付的 exe 收到 /api/exit 之后 30 秒还没退出（端口还听着）—— 见 ' + $appLog
}
Ok "交付 exe 版本 $served，已自己退出"

# ---------------------------------------------------------------- 9) 剩下的人工活
Step '8/9 安装包（可选）'
$iscc = @('C:\Program Files (x86)\Inno Setup 6\ISCC.exe', 'C:\Program Files\Inno Setup 6\ISCC.exe') |
  Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  Write-Host '  跳过：本机没装 Inno Setup 6。装好之后这一条也可以自动：'
  Write-Host ('    "ISCC.exe" packaging\installer.iss "/DAppVersion=' + $current + '"')
  Write-Host '  （packaging\installer.iss 已经写好并被 tests\test_packaging.py 静态守着）'
} elseif ($MakeInstaller) {
  & $iscc (Join-Path $here 'installer.iss') ('/DAppVersion=' + $current) | Out-Null
  $setup = Join-Path $root ('dist\SaltLRCStudio-Setup-' + $current + '.exe')
  if (-not (Test-Path $setup)) { Die '编译安装包失败' }
  Copy-Item $setup $dest
  Ok ('安装包 ' + [math]::Round((Get-Item $setup).Length / 1MB, 1) + ' MB')
} else {
  Write-Host '  检测到 Inno Setup，但这次没传 -MakeInstaller：加这个开关就会一起编译。'
}

Step '9/9 还需要人工做的'
Write-Host '  · 交付前肉眼过一遍桌面上的目录'
Write-Host '  · 发布说明与使用说明已自动带版本号；若某版有面向使用者的特别提示，写进 docs\RELEASE_NOTES.md'
Cleanup-Temp
Ok '已清掉本脚本自己在 %TEMP% 下的临时文件'
Write-Host ''
Write-Host ('发布准备完成：' + $dest) -ForegroundColor Green
