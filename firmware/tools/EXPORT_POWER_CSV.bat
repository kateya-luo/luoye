@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
python export_power_csv.py %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Retry with an explicit port, for example: EXPORT_POWER_CSV.bat COM22
pause
exit /b %RESULT%
