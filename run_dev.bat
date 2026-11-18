@echo off
rem Dev backend launcher for Windows terminal (cmd / double-click).
rem
rem   run_dev.bat             foreground, Ctrl+C to stop
rem   run_dev.bat --detach    background, survives closing this window
rem   run_dev.bat --status    is it running and healthy?
rem   run_dev.bat --stop      stop the background one
rem
rem All real logic lives in scripts\run_dev.py; this file only fixes the
rem interpreter and working directory. ASCII only on purpose: cmd reads .bat
rem in the OEM code page, and non-ASCII text here gets mangled.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [run_dev] .venv not found. Create it first:
  echo            py -3.12 -m venv .venv
  exit /b 2
)

".venv\Scripts\python.exe" scripts\run_dev.py %*
set "RC=%ERRORLEVEL%"
rem Keep the window open only for the double-click case (no arguments), so a
rem failure can be read. With arguments we are inside a real terminal - pausing
rem there would just block the prompt.
if "%~1"=="" if not "%RC%"=="0" pause
exit /b %RC%
