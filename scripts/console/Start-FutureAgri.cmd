@echo off
rem Start the Future Agri Mission Console (http://127.0.0.1:8099). Close this window to stop it.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-FutureAgri.ps1" %*
if errorlevel 1 pause
