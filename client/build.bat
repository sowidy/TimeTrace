@echo off
chcp 65001 >nul
setlocal

rem ---------------------------------------------------------------------------
rem KEEP THIS FILE PURE ASCII (English comments only).
rem cmd.exe reads .bat files using the system OEM codepage (e.g. 936/GBK on
rem Chinese Windows), NOT UTF-8. Non-ASCII characters in comments get decoded
rem into garbage that cmd then tries to execute, which breaks the whole script.
rem `chcp 65001` does not help here -- it only changes console output encoding,
rem not how the batch file itself is decoded.
rem ---------------------------------------------------------------------------
rem
rem This script lives in client/ (it builds the client only -- server/ is
rem deployed separately and is not packaged into the exe).

rem cd to this script's folder (= client/) so that app.spec, app.ico and the
rem relative output dirs (build\, dist\) all land under client/.
rem %~dp0 is the folder containing this .bat file, with a trailing backslash.
cd /d "%~dp0"

echo ========================================
echo   TimeTrace - Build Script
echo ========================================
echo.

rem Prefer the project's own virtualenv, which lives at the REPO ROOT
rem (one level up from client/), not inside client/. Dependencies
rem (PySide6, pywin32, ...) are installed there, while the system python is a
rem different interpreter that would fail to find them.
set "PY=python"
if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
echo Using Python: %PY%
echo.

rem Install PyInstaller if not present
%PY% -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    %PY% -m pip install pyinstaller
    echo.
)

rem Generate app.ico into client/ (the script writes there by absolute path,
rem so the current directory does not matter).
echo Generating app icon...
%PY% tools\generate_icon.py
echo.

rem If a previous build is still running, its exe holds dist\TimeTrace\ and
rem NOTHING can delete that folder -- rmdir fails silently, and PyInstaller
rem then refuses to write into a non-empty output dir. --noconfirm (below)
rem only handles the "directory not empty" case, not a locked file, so warn
rem about it up front instead of letting the build fail with a confusing error.
tasklist /fi "imagename eq TimeTrace.exe" 2>nul | find /i "TimeTrace.exe" >nul
if not errorlevel 1 (
    echo WARNING: TimeTrace.exe is running.
    echo           Close it first, or the build will fail.
    echo.
)

rem Clean previous build
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

rem Build.
rem --noconfirm: let PyInstaller clear dist\TimeTrace\ itself. Without it,
rem a leftover (non-empty) output dir makes PyInstaller abort with
rem "The output directory ... is not empty".
echo Building with PyInstaller...
%PY% -m PyInstaller app.spec --clean --noconfirm
echo.

rem Check BOTH the exit code and the artifact. Checking only "does the exe
rem exist" is not enough: a stale exe from an earlier build makes the script
rem report SUCCESS even when this build never produced anything.
set "BUILD_OK=0"
if not errorlevel 1 if exist "dist\TimeTrace\TimeTrace.exe" set "BUILD_OK=1"

if "%BUILD_OK%"=="1" (
    echo ========================================
    echo   Build SUCCESS!
    echo   Output: client\dist\TimeTrace\
    echo ========================================
    echo.
    echo To distribute: zip the client\dist\TimeTrace folder
) else (
    echo ========================================
    echo   Build FAILED!  ^(see the PyInstaller output above^)
    echo ========================================
    echo.
    echo Common causes:
    echo   - TimeTrace.exe is still running ^(see the warning above^)
    echo   - a missing module: add it to hiddenimports in app.spec
)

pause
