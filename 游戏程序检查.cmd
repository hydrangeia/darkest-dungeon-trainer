@echo off
chcp 65001 >nul
"%~dp0runtime\python.exe" -B -X utf8 -I "%~dp0app\probe_game.py"
pause
