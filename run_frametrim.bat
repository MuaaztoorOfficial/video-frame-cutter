@echo off
cd /d "%~dp0"
py -m pip show pillow >nul 2>nul || py -m pip install pillow
py frametrim.py
pause
