@echo off
setlocal
set "ROOT=C:\Users\z09432\WorkBuddy\2026-07-09-09-57-01\daily_stock_analysis"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "WEBUI_PORT=8001"
set "WEBUI_HOST=0.0.0.0"
set "WEBUI_AUTO_BUILD=true"

if not exist "%PY%" (
  echo [ERR] venv python missing. Run this first in the venv:
  echo        pip install -r requirements.txt
  pause
  exit /b 1
)

echo.
echo Starting upstream daily_stock_analysis WebUI on http://localhost:8001
echo (WEBUI_AUTO_BUILD=true: it will npm-build the React frontend if npm is on PATH)
echo (If npm is NOT available here, build manually first:)
echo        cd apps\dsa-web
echo        npm ci
echo        npm run build
echo        (then re-run this bat, or set WEBUI_AUTO_BUILD=false)
echo.

"%PY%" webui.py
