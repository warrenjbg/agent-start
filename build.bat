@echo off
rem Build AgentStart.exe (PyInstaller one-folder) and the per-user installer (Inno Setup 6).
setlocal
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
rem machine-wide Inno Setup install (winget run elevated) lives under Program Files (x86)
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"

if not exist "%PY%" (echo Project venv not found: %PY% & goto :fail)

echo [1/5] version (version.py is the single source)
set VER=
for /f "usebackq delims=" %%v in (`"%PY%" tools\sync_version.py --print`) do set VER=%%v
if not defined VER goto :fail
echo       %VER%
echo [2/5] icon
"%PY%" tools\make_icon.py || goto :fail
echo [3/5] default config
if not exist build mkdir build
copy /y config.default.json build\config.default.json >nul || goto :fail
echo [4/5] PyInstaller
"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN AgentStart.spec || goto :fail
echo [5/5] installer
if not exist "%ISCC%" (echo Inno Setup not found at "%ISCC%" & goto :fail)
"%ISCC%" /Q installer\AgentStart.iss || goto :fail
echo.
echo Done:
echo   dist\AgentStart\AgentStart.exe
echo   dist\AgentStart-Setup-%VER%.exe
exit /b 0
:fail
echo BUILD FAILED
exit /b 1
