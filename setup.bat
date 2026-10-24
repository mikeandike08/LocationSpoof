@echo off
rem One-time setup on Windows: creates .venv and installs dependencies.
rem Set PYTHON to choose the interpreter, e.g.  set PYTHON=py -3.12
rem Python 3.14 doesn't work yet: pymobiledevice3's pylzss has no prebuilt Windows package for it.
setlocal
cd /d "%~dp0"

if not defined PYTHON (
  for %%v in (3.12 3.13 3.11 3.10) do (
    if not defined PYTHON (
      py -%%v -c "" >nul 2>nul && set "PYTHON=py -%%v"
    )
  )
)
if not defined PYTHON (
  python -c "import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 13))" >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
  echo Python 3.10 to 3.13 is required ^(3.12 recommended^). Install it from https://www.python.org/downloads/
  goto :fail
)

if not exist .venv\Scripts\python.exe (
  echo Creating virtualenv with %PYTHON%...
  %PYTHON% -m venv .venv || goto :fail
)
.venv\Scripts\python.exe -m pip install -q -U pip || goto :fail
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail
.venv\Scripts\python.exe -c "import pymobiledevice3, fastapi, pystray" || goto :fail
echo Setup complete. Start LocationSpoof with run.bat
exit /b 0

:fail
echo.
echo Setup failed.
exit /b 1
