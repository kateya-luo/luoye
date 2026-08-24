@echo off
rem State machine regression test (MSVC). Run after editing app_state.c.
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d %~dp0
cl /nologo /utf-8 /std:c11 /W3 /D_CRT_SECURE_NO_WARNINGS /I..\main test_app_state.c ..\main\app_state.c /Fe:test_app_state.exe
if errorlevel 1 exit /b 1
.\test_app_state.exe
