@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "%~dp0"
cl /nologo /utf-8 /std:c11 /W4 /WX /I..\components\power_mgr\include test_power_soc.c ..\components\power_mgr\power_soc.c /Fe:test_power_soc.exe
if errorlevel 1 exit /b 1
.\test_power_soc.exe
