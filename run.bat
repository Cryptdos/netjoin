@echo off
rem Run NetJoin from source (asks for admin). Add --fake for demo mode.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Could not create .venv - is Python 3.12+ installed and on PATH? & exit /b 1)
)
call ".venv\Scripts\activate.bat"
python -m pip install -q -r requirements.txt
python -m netjoin %*
