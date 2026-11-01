@echo off
rem Check which data sources this computer can reach (writes diagnose_log.txt in the folder above scripts).
setlocal
set "ROOT=%~dp0..\.."
set "PYTHONPATH=%ROOT%\services\console"
"%ROOT%\.venv\Scripts\python.exe" -m fa_console.diagnose
