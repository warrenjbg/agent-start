@echo off
rem Build HermesController.exe (PyInstaller one-folder) and the per-user installer (Inno Setup 6).
setlocal
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe

echo [1/4] icon
"%PY%" tools\make_icon.py || goto :fail
echo [2/4] default config
if not exist build mkdir build
copy /y config.json build\config.default.json >nul || goto :fail
echo [3/4] PyInstaller
"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN HermesController.spec || goto :fail
echo [4/4] installer
if not exist "%ISCC%" (echo Inno Setup not found at "%ISCC%" & goto :fail)
"%ISCC%" /Q installer\HermesController.iss || goto :fail
echo.
echo Done:
echo   dist\HermesController\HermesController.exe
echo   dist\HermesController-Setup-1.0.0.exe
exit /b 0
:fail
echo BUILD FAILED
exit /b 1
