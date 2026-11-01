@echo off
rem Connect the GPU server (the H200) to the Future Agri console on this computer.
rem Usage: Connect-GPU-Server.cmd [-Server user@host] [-Port 22] [-TunnelPort 18099] [-Gpu auto]
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Connect-GPU-Server.ps1" %*
if errorlevel 1 pause
