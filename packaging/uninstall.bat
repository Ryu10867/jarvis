@echo off
REM Removes J.A.R.V.I.S. (the program, the jarvis command and the Desktop shortcut).
REM Your settings/models/logs in %LOCALAPPDATA%\Jarvis are kept unless you answer yes below.
setlocal
set "APP=%LOCALAPPDATA%\Programs\Jarvis"
if exist "%APP%\venv\Scripts\python.exe" "%APP%\venv\Scripts\python.exe" -P -m jarvis uninstall-data
rmdir /s /q "%APP%"
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do del "%%D\Jarvis.lnk" 2>nul
powershell -NoProfile -Command "$b=Join-Path $env:LOCALAPPDATA 'Programs\Jarvis\bin'; $p=[Environment]::GetEnvironmentVariable('Path','User'); [Environment]::SetEnvironmentVariable('Path', (($p -split ';') | Where-Object { $_ -and $_ -ne $b }) -join ';', 'User')"
echo Jarvis has been removed.
pause
