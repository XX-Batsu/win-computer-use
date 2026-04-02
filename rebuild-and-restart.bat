@echo off
setlocal

rem Derive ROOT from this script's own location (strip trailing backslash)
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

echo [1/3] Building MCP server...
cd /d "%ROOT%\mcp-server"
call npm run build
if errorlevel 1 (
    echo ERROR: MCP server build failed.
    pause
    exit /b 1
)

echo [2/3] Stopping engine (port 8765)...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING"') do (
    echo Killing PID %%p
    taskkill /PID %%p /F >nul 2>&1
)

echo [3/3] Starting engine...
start "" wscript "%ROOT%\engine\start-hidden.vbs"

echo Done. Engine starting in background.
