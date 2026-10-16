
@echo off
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set "PROFILE=%~dp0chrome-profile"

if not exist "%CHROME%" (
    set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
)

if not exist "%CHROME%" (
    echo Chrome was not found in the usual installation folders.
    pause
    exit /b 1
)

start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%PROFILE%" "https://labs.google/fx/tools/flow"
