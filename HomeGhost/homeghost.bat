@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" goto novenv
if not exist ".env" goto noenv

call .venv\Scripts\activate.bat
python bot.py

echo.
echo [HomeGhost] Bot process exited.
pause
exit /b 0

:novenv
echo [HomeGhost] No virtual environment found at .venv
echo Run this once first, in PowerShell:
echo   python -m venv .venv
echo   .venv\Scripts\activate
echo   pip install -r requirements.txt
pause
exit /b 1

:noenv
echo [HomeGhost] No .env file found.
echo Copy .env.example to .env and fill in your settings first.
pause
exit /b 1
