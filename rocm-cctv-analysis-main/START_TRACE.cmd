@echo off
title TRACE server
cd /d "%~dp0"
echo Stopping any TRACE server already running on port 8090...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
timeout /t 2 /nobreak >nul
set BRIDGE_PORT=8090
echo Starting TRACE. Open http://localhost:8090/ in your browser. Keep this window open.
start "" /min cmd /c "timeout /t 10 /nobreak >nul & start http://localhost:8090/"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_local.ps1"
pause
