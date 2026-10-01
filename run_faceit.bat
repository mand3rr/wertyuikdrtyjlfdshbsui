@echo off
chcp 65001 >nul
cd /d "%~dp0"
call venv\Scripts\python.exe -X utf8 Faceit\main.py
pause
