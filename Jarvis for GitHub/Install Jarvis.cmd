@echo off
rem Installs Jarvis on this PC. Double-click this file to start.
title Jarvis setup
if not exist "%~dp0installer\install.ps1" (
    echo.
    echo  The kit is still zipped. First right-click the zip file, choose "Extract All",
    echo  then open the extracted folder and double-click "Install Jarvis" there.
    echo.
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1"
echo.
pause
