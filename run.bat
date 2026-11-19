@echo off
rem Run NetJoin from source (asks for admin). Add --fake for demo mode.
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
python -m netjoin %*
