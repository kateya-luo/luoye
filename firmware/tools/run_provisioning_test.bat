@echo off
rem Pure host tests for SoftAP form decoding and WiFi credential limits.
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d %~dp0
cl /nologo /utf-8 /std:c11 /W4 /D_CRT_SECURE_NO_WARNINGS ^
  /I..\components\net_uploader\include ^
  test_provisioning_form.c ..\components\net_uploader\provisioning_form.c ^
  /Fe:test_provisioning_form.exe
if errorlevel 1 exit /b 1
.\test_provisioning_form.exe
