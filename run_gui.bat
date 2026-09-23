@echo off
rem SimplyConvert GUI launcher - Windows
cd /d "%~dp0"
python simplyconvert_gui.py
if errorlevel 1 pause
