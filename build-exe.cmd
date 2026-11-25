@echo off
rem Builds ONE self-contained FutureAgri.exe and puts it right here, next to this file.
rem Needs Python 3.12 (64-bit) on PATH, once, on the build machine only. Output: .\FutureAgri.exe
cd /d "%~dp0"
python -m pip install --upgrade pip || goto :fail
python -m pip install -r services\console\requirements.txt pyinstaller==6.22.3 pyinstaller-hooks-contrib || goto :fail
python -m PyInstaller packaging\windows\futureagri.spec --noconfirm --clean || goto :fail
copy /y dist\FutureAgri.exe FutureAgri.exe || goto :fail
echo.
echo Done: %~dp0FutureAgri.exe  -- double-click it to start and open the console.
exit /b 0
:fail
echo Build failed.
exit /b 1
