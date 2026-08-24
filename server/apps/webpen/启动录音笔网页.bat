@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   浏览器录音笔 - 本地启动
echo   浏览器会自动打开；用完关掉这个黑窗口即停止
echo ============================================
start "" http://localhost:5500/index.html
python -m http.server 5500
