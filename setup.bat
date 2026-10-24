@echo off
rem One-time setup on Windows: creates .venv and installs dependencies.
rem Set PYTHON to choose the interpreter, e.g.  set PYTHON=py -3.13
rem Python 3.13 is required on Windows:
rem  - 3.14: pymobiledevice3's pylzss has no prebuilt Windows package for it yet.
rem  - 3.12 and older: the iOS 17.4+ tunnel needs sslpsk_pmd3, whose Windows build is broken
rem    (it links an OpenSSL 1.1 DLL that Python doesn't ship). 3.13 uses Python's own ssl instead.
setlocal
cd /d "%~dp0"

if not defined PYTHON (
  py -3.13 -c "" >nul 2>nul && set "PYTHON=py -3.13"
)
if not defined PYTHON (
  python -c "import sys; sys.exit(sys.version_info[:2] != (3, 13))" >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
  echo Python 3.13 is required. Install it from https://www.python.org/downloads/ ^(or: winget install Python.Python.3.13^)
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
