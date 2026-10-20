@echo off
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10+ nahi mila. Install karo: https://www.python.org/downloads/  ^(Add to PATH tick karo^)
  pause
  exit /b 1
)
%PY% frametrim.py %*
if errorlevel 1 pause
