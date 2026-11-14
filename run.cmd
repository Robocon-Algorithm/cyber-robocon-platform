@echo off
rem Double-clickable launcher: double-click this file to start the arena.
rem It bypasses the script execution policy for this run only, and keeps the
rem window open when something fails so the error stays readable.
setlocal
chcp 65001 >nul 2>&1
title BR Arena
set BR_PAUSE=1

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0my_run.ps1" %*
set CODE=%ERRORLEVEL%

if not "%CODE%"=="0" (
    echo.
    echo Exit code: %CODE%
    pause
)
endlocal
