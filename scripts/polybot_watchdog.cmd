@echo off
rem Copy this file into the Startup folder to auto-start the stack at logon:
rem   copy "C:\workspace\space\futurePrediction\scripts\polybot_watchdog.cmd" "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\"
start "" /min powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\workspace\space\futurePrediction\scripts\run_watchdog.ps1"
