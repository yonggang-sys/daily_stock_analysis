# keepalive for upstream daily_stock_analysis webui (port 8001) - runs hidden
$ErrorActionPreference = 'SilentlyContinue'
$ROOT = 'C:\Users\z09432\WorkBuddy\2026-07-09-09-57-01\daily_stock_analysis'
$PY = "$ROOT\.venv\Scripts\python.exe"
$NODEBIN = 'C:\Users\z09432\.workbuddy\binaries\node\versions\22.22.2'
$LOG = 'C:\tmp\upstream_dsa.log'

# skip if webui.py already running
$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*webui.py*' }
if ($p) { exit 0 }

$env:PATH = "$NODEBIN;$env:PATH"
$env:WEBUI_PORT = '8001'
$env:WEBUI_HOST = '0.0.0.0'
$env:WEBUI_AUTO_BUILD = 'true'

if (Test-Path $PY) {
    Start-Process -FilePath $PY -ArgumentList 'webui.py' `
        -WorkingDirectory $ROOT -WindowStyle Hidden `
        -RedirectStandardOutput ($LOG + '.out') -RedirectStandardError ($LOG + '.err')
}
exit 0
