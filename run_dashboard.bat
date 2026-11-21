@echo off
setlocal
cd /d "%~dp0"
set NOPAUSE=
echo %* | find /i "--scheduled" >nul && set NOPAUSE=1

if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Could not create the Python environment. Install Python 3.10+ from python.org and tick "Add Python to PATH".
  goto :end_fail
)
set PY=".venv\Scripts\python.exe"

%PY% -c "import yfinance, pandas, numpy, requests" 2>nul
if errorlevel 1 (
  echo Installing required packages ^(first run only, takes a few minutes^)...
  %PY% -m pip install --upgrade pip
  %PY% -m pip install -r requirements.txt
  %PY% -c "import yfinance, pandas, numpy, requests"
  if errorlevel 1 (
    echo.
    echo Package install failed - see the messages above.
    goto :end_fail
  )
)

%PY% asx_dashboard.py %*
if errorlevel 1 goto :end_fail
echo.
echo Done. Open output\asx_dashboard_latest.html
if not defined NOPAUSE pause
exit /b 0

:end_fail
echo.
echo The run did not finish - the messages above say why.
if not defined NOPAUSE pause
exit /b 1
