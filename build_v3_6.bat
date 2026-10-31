@echo off
setlocal
cd /d "%~dp0"
if not exist launcher\launcher.exe (
  echo Missing launcher\launcher.exe. Restore the existing launcher template first.
  exit /b 1
)
if not exist launcher\_internal (
  echo Missing launcher\_internal. Restore the complete working launcher folder first.
  exit /b 1
)
python -m PyInstaller --noconsole --onefile --clean --noconfirm --collect-all customtkinter --name SteamLaunchHelper_v3_6 --icon steamlaunchhelper.ico --add-data "steamlaunchhelper.ico;." --add-data "steamlaunchhelper.png;." SteamLaunchHelper_v3_6.py
if errorlevel 1 exit /b 1
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $p='dist\SteamLaunchHelper_v3_6'; New-Item -ItemType Directory -Force -Path $p | Out-Null; Copy-Item 'dist\SteamLaunchHelper_v3_6.exe' $p -Force; Copy-Item 'launcher' $p -Recurse -Force; Compress-Archive -Path ($p+'\*') -DestinationPath 'dist\SteamLaunchHelper_v3_6.zip' -Force"
if errorlevel 1 exit /b 1
echo Build ready: dist\SteamLaunchHelper_v3_6.zip
