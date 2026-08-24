@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d %~dp0
cl /nologo /utf-8 /std:c11 /W4 /WX /TC /D_CRT_SECURE_NO_WARNINGS ^
  /I..\components\agenda_todo\include test_agenda_protocol.c ^
  ..\components\agenda_todo\agenda_protocol.c /Fe:test_agenda_protocol.exe || exit /b 1
test_agenda_protocol.exe
set RC=%ERRORLEVEL%
del /q test_agenda_protocol.exe test_agenda_protocol.obj agenda_protocol.obj >nul 2>nul
exit /b %RC%
