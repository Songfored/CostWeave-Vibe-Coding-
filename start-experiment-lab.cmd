@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B -m costweave.lab_launcher
) else (
  py -3 -B -m costweave.lab_launcher
)
if errorlevel 1 (
  echo 实验台启动失败。请保留本窗口中的错误信息。
  pause
)
