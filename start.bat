@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m saltlrc serve
  pause
  exit /b
)
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 -m saltlrc serve
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    for /f "delims=" %%P in ('dir /b /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
      if not defined WBPY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%P\python.exe" set "WBPY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%P\python.exe"
    )
    if defined WBPY (
      "%WBPY%" -m saltlrc serve
    ) else (
    if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
      "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m saltlrc serve
    ) else (
      echo Python 3.10+ is required. Please install Python and try again.
    )
    )
  ) else (
    python -m saltlrc serve
  )
)
pause
