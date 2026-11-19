@echo off
rem Build a portable NetJoin folder in dist\NetJoin and a zip to share.
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
for /f %%v in ('python -c "import netjoin;print(netjoin.__version__)"') do set "VER=%%v"
pyinstaller --noconfirm --clean --name NetJoin --windowed --uac-admin --icon netjoin\static\netjoin.ico ^
  --add-data "netjoin\static;netjoin\static" ^
  --collect-submodules uvicorn --collect-submodules scapy --collect-data scapy --hidden-import pystray._win32 launcher.py || goto :err
powershell -NoProfile -Command "Compress-Archive -Force -Path dist\NetJoin -DestinationPath dist\NetJoin-%VER%.zip" || goto :err
echo.
echo Built: dist\NetJoin\NetJoin.exe
echo Zip:   dist\NetJoin-%VER%.zip  (unzip anywhere and run NetJoin.exe)
exit /b 0
:err
echo Build failed.
exit /b 1
