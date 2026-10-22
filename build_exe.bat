@echo off
title Build NearBeam Executable
cd /d "%~dp0"
echo ========================================================
echo   Building NearBeam Standalone Executable (PyInstaller)
echo ========================================================
echo.
python -m PyInstaller --noconfirm NearBeam.spec
echo.
if %ERRORLEVEL% EQU 0 (
    echo ========================================================
    echo   BUILD SUCCESSFUL!
    echo   App folder is ready at: dist\NearBeam\ ^(run dist\NearBeam\NearBeam.exe^)
    echo ========================================================
) else (
    echo [ERROR] Build failed! Check the output above.
)
echo.
pause
