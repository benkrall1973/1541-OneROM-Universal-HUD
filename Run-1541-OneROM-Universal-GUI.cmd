@echo off
setlocal
cd /d "%~dp0"
py src\1541_onerom_universal_gui.py
if errorlevel 1 (
    echo.
    echo The Universal GUI stopped with an error.
    pause
)
