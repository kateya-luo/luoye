@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d %~dp0
cl /nologo /utf-8 /std:c11 /W4 /WX /TC /D_CRT_SECURE_NO_WARNINGS /I..\components\net_uploader\include ^
  test_upload_protocol.c ..\components\net_uploader\upload_protocol.c ^
  /Fe:test_upload_protocol.exe || exit /b 1
test_upload_protocol.exe
set RC=%ERRORLEVEL%
del /q test_upload_protocol.exe test_upload_protocol.obj upload_protocol.obj >nul 2>nul
exit /b %RC%
