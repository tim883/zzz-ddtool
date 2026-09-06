@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0src;%PYTHONPATH%
py -3.11 -m zzz_ddtool
if errorlevel 1 (
  echo.
  echo 启动失败。请确认已装 Python 3.11，以及 pyyaml / PySide6-Essentials。
  echo 安装：py -3.11 -m pip install -e ".[dev]"
  pause
)
