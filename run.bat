@echo off
rem Agent Start - launches the widget without a console window.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
