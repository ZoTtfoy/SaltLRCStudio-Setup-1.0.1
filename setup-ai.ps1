param([ValidateSet('faster','heart')][string]$Engine = 'faster')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonCandidate = Get-Command python -ErrorAction SilentlyContinue
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if ($pythonCandidate) { $pythonExe = $pythonCandidate.Source }
elseif (Test-Path -LiteralPath $bundledPython) { $pythonExe = $bundledPython }
else { throw 'Install Python 3.10 or later first.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $pythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$extra = if ($Engine -eq 'heart') { 'heart' } else { 'asr' }
& $venvPython -m pip install "-e" ".[${extra}]" huggingface-hub
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network connection.' }
& $venvPython scripts/download_models.py --engine $Engine
if ($LASTEXITCODE -ne 0) { throw 'Model download failed. Existing partial downloads can be resumed.' }
Write-Host 'Ready. Double-click start.bat.'
