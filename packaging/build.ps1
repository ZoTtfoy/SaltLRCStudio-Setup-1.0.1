# Builds the single-file SaltLRC Studio executable.
# Usage: powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here '..')
Set-Location $root

$py = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\pyinstaller.exe'
if (-not (Test-Path $py)) { $py = 'pyinstaller' }

& $py `
  --noconfirm --clean --onefile --noconsole `
  --name SaltLRCStudio `
  --icon "packaging\icons\SaltLRCStudio-icon.ico" `
  --add-data "saltlrc\web;saltlrc\web" `
  --exclude-module tkinter `
  --exclude-module numpy `
  --exclude-module PIL `
  --log-level WARN `
  app_entry.py

# NOTE: the icon must be passed HERE. This script drives PyInstaller with CLI
# flags and never reads SaltLRCStudio.spec — that file is a by-product this very
# command regenerates, so editing it has no effect on the build (and PyInstaller
# overwrites the edit on the next run).

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
$exe = Join-Path $root 'dist\SaltLRCStudio.exe'
$size = (Get-Item $exe).Length / 1MB
"Built: $exe ({0:N1} MB) — windowed, no console" -f $size
