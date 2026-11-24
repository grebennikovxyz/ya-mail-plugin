@echo off
setlocal
where ya-mail >nul 2>&1
if errorlevel 1 goto use_uv
call ya-mail connect
if errorlevel 1 pause
exit /b
:use_uv
where uv >nul 2>&1
if errorlevel 1 (
  echo Install uv: https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv run --no-project --python 3.13 --with "%~dp0." ya-mail connect
if errorlevel 1 pause
