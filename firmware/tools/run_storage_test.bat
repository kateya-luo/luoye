@echo off
rem Pure host tests for WAV power-loss repair and JSONL torn-tail handling.
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d %~dp0
cl /nologo /utf-8 /std:c11 /W4 /D_CRT_SECURE_NO_WARNINGS ^
  /I..\components\storage_sd\include ^
  test_storage_format.c ..\components\storage_sd\storage_format.c ^
  /Fe:test_storage_format.exe
if errorlevel 1 exit /b 1
.\test_storage_format.exe
