@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0trainer_ui.py" %*
) else (
    py -3 "%~dp0trainer_ui.py" %*
)
if errorlevel 1 pause
endlocal
