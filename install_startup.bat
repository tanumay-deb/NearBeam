@echo off
title Enable NearBeam on Windows Startup
cd /d "%~dp0"
echo Registering NearBeam to start with Windows...
python -c "from core.autostart import set_autostart; success = set_autostart(True); print('Success!' if success else 'Failed!')"
echo.
echo NearBeam will now start silently in the background when you log into Windows.
echo You can disable it anytime from the NearBeam app or by running uninstall_startup.bat.
echo.
pause
