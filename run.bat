@echo off
rem Starts LocationSpoof with a tray icon (no console window). The first run sets everything up.
rem Quit from the tray icon; quitting restores the iPhone's real location.
rem For a console with live logs instead:  run.bat --console
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\pythonw.exe (
  call setup.bat || (pause & exit /b 1)
)

if /i "%~1"=="--console" (
  .venv\Scripts\python.exe -m backend.launcher --no-tray
) else (
  start "" .venv\Scripts\pythonw.exe -m backend.launcher %*
)
