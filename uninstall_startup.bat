@echo off
title Disable NearBeam from Windows Startup
cd /d "%~dp0"
echo Removing NearBeam from Windows startup...
python -c "from core.autostart import set_autostart; success = set_autostart(False); print('Success!' if success else 'Failed!')"
echo.
echo NearBeam will no longer start automatically with Windows.
echo.
pause
