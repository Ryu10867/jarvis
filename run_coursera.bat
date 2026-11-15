@echo off
REM ============================================================================
REM  Coursera Mode for Jarvis
REM  Opens Chrome with the "Jarvis Coursera Link" helper already loaded, using
REM  your normal Chrome profile (so you stay logged into Coursera). Your open
REM  tabs are restored. After this, Jarvis can play lessons and the next video.
REM
REM  This closes Chrome first, because Chrome only loads the helper at startup.
REM  Nothing is lost - your tabs reopen.
REM ============================================================================
setlocal
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
set "EXT=%~dp0extension"

echo Closing Chrome so the Jarvis Coursera helper can load...
taskkill /IM chrome.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo Starting Chrome with the Coursera helper...
start "" "%CHROME%" --profile-directory=Default --restore-last-session --load-extension="%EXT%"
echo.
echo Done. Open your Coursera course, then tell Jarvis "resume my coursera course".
timeout /t 4 /nobreak >nul
