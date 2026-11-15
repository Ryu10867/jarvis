@echo off
REM Starts Jarvis (HUD + voice). Right-click > "Run as administrator" to also enable failed-login alerts.
REM   run_jarvis.bat --check        self-test, does not start Jarvis
REM   run_jarvis.bat --mic-test     see what the microphone / wake word hears
REM   run_jarvis.bat --train-wake   teach Jarvis how you say "Jarvis"
REM   run_jarvis.bat --text         no microphone, type instead
cd /d "%~dp0"
title J.A.R.V.I.S.
".venv\Scripts\python.exe" -m jarvis %*
if errorlevel 1 pause
