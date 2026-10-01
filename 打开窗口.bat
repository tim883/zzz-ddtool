@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0src;%PYTHONPATH%
set "DDTOOL_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%DDTOOL_PYTHON%" goto env_error
"%DDTOOL_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info[:3] == (3, 13, 5) else 1)"
if errorlevel 1 goto env_error
"%DDTOOL_PYTHON%" -m zzz_ddtool
if errorlevel 1 (
  echo.
  echo 启动失败，详情见 log\text\ddtool.log。
  pause
)
exit /b

:env_error
echo 需要项目内 .venv，且 Python 版本必须为 3.13.5。
echo 建环境：uv venv --python 3.13.5 .venv
echo 装依赖：uv pip install --python .venv\Scripts\python.exe -r requirements-lock.txt "pytest==9.1.1" -e .
pause
exit /b 1
