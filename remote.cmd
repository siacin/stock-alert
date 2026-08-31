@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先运行 start.cmd 完成软件初始化。
  pause
  exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" remote.py %*
pause
