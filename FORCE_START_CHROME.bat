@echo off
echo ===================================================
echo   FORCE LAUNCHING CHROME WITH REMOTE DEBUGGING
echo ===================================================
echo.
echo 1. Killing all hidden background Chrome processes...
taskkill /IM chrome.exe /F 2>nul
echo.
echo 2. Waiting for processes to completely die...
timeout /t 2 /nobreak >nul
echo.
echo 3. Launching Chrome with port 9222 open!
start chrome.exe --remote-debugging-port=9222
echo.
echo Done! Please navigate to your Incident Dashboard in the new window!
timeout /t 5 >nul
