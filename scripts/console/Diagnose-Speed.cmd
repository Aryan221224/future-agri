@echo off
rem Measure download speed to the Sentinel-2 image bucket (writes diagnose_speed.txt).
setlocal
set "ROOT=%~dp0..\.."
set "PYTHONPATH=%ROOT%\services\console"
"%ROOT%\.venv\Scripts\python.exe" -m fa_console.diagnose --speed
