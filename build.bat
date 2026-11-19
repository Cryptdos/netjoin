@echo off
rem Build a portable NetJoin folder in dist\NetJoin (zip it to share).
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto :err
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-dev.txt || goto :err
python -m unittest discover tests || goto :err
pyinstaller --noconfirm --clean --name NetJoin --console --uac-admin ^
  --add-data "netjoin\static;netjoin\static" ^
  --collect-submodules uvicorn launcher.py || goto :err
echo.
echo Built: dist\NetJoin\NetJoin.exe
exit /b 0
:err
echo Build failed.
exit /b 1
