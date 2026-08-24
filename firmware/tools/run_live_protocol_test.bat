@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d %~dp0
cl /nologo /utf-8 /std:c11 /W4 /WX /TC /D_CRT_SECURE_NO_WARNINGS /I..\components\net_uploader\include ^
  test_live_protocol.c ..\components\net_uploader\live_protocol.c ^
  /Fe:test_live_protocol.exe || exit /b 1
test_live_protocol.exe
set RC=%ERRORLEVEL%
del /q test_live_protocol.exe test_live_protocol.obj live_protocol.obj >nul 2>nul
exit /b %RC%
