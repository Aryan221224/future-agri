@echo off
rem Time the satellite step on this computer, printing each result as it arrives (writes diagnose_speed.txt).
setlocal
set "ROOT=%~dp0..\.."
set "PYTHONPATH=%ROOT%\services\console"
"%ROOT%\.venv\Scripts\python.exe" -m fa_console.diagnose --speed
pause
