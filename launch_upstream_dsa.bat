@echo off
setlocal
set "ROOT=C:\Users\z09432\WorkBuddy\2026-07-09-09-57-01\daily_stock_analysis"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "NODEBIN=C:\Users\z09432\.workbuddy\binaries\node\versions\22.22.2"
set "LOG=C:\tmp\upstream_dsa.log"

REM skip if webui.py already running
powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*webui.py*' }; if ($p) { exit 0 } else { exit 1 }" >nul 2>&1
if %ERRORLEVEL%==0 (
  echo [%date% %time%] upstream already running >> "%LOG%"
  exit /b 0
)

set "PATH=%NODEBIN%;%PATH%"
set "WEBUI_PORT=8001"
set "WEBUI_HOST=0.0.0.0"
set "WEBUI_AUTO_BUILD=true"

echo [%date% %time%] launching upstream dsa webui >> "%LOG%"
powershell -NoProfile -Command "Start-Process -FilePath '%PY%' -ArgumentList 'webui.py' -WorkingDirectory '%ROOT%' -RedirectStandardOutput '%LOG%.out' -RedirectStandardError '%LOG%.err' -WindowStyle Hidden"
echo [%date% %time%] launch issued >> "%LOG%"
endlocal
