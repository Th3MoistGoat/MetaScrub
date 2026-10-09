@echo off
REM ============================================================
REM  MetaScrub build script - produces a single MetaScrub.exe
REM  Run this on your Windows PC by double-clicking compile.bat
REM
REM  It will:
REM    1. Check for Python 3 - installs it via winget if missing
REM    2. Install PyInstaller via pip
REM    3. Download the latest ExifTool Windows 64-bit portable
REM    4. Bundle everything into one dist\MetaScrub.exe
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Building MetaScrub...

echo.
echo  === MetaScrub build ===
echo.

REM ---- Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo Python 3 not found. Trying to install it with winget...
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    REM Pick up the fresh install in this session
    set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"
    python --version >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Automatic Python install did not work.
        echo         Install it from https://www.python.org/downloads/
        echo         and tick "Add python.exe to PATH" during setup,
        echo         then run this script again.
        pause
        exit /b 1
    )
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER%

REM ---- PyInstaller ----
echo.
echo Installing PyInstaller (this is quick)...
python -m pip install --upgrade --quiet pyinstaller
if errorlevel 1 (
    echo [ERROR] pip failed to install PyInstaller. Check your internet connection.
    pause
    exit /b 1
)
echo [OK] PyInstaller ready

REM ---- ExifTool ----
REM NOTE: exiftool.org now points downloads at SourceForge, so we fetch
REM from there. The Windows zip contains exiftool.exe PLUS a required
REM exiftool_files support folder - both get bundled into the build.
REM The zip names the exe exiftool(-k).exe - normalize it in case the
REM user extracted the zip by hand without renaming.
if not exist exiftool.exe if exist "exiftool(-k).exe" (
    ren "exiftool(-k).exe" exiftool.exe
    echo [OK] Renamed the stock exe to exiftool.exe
)
echo.
if exist exiftool.exe if exist exiftool_files (
    echo [OK] exiftool.exe + exiftool_files already present, skipping download.
) else (
    echo Finding latest ExifTool release...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$html = (Invoke-WebRequest -Uri 'https://exiftool.org/' -UseBasicParsing).Content; " ^
        "$m = [regex]::Match($html, 'exiftool-(\d+\.\d+)_64\.zip'); " ^
        "if ($m.Success) { $m.Groups[1].Value } else { '' }" > etver.txt
    set /p ETVER=<etver.txt
    del etver.txt
    if "!ETVER!"=="" (
        echo [ERROR] Could not determine the latest ExifTool version.
        echo         Get the Windows 64-bit zip by hand from
        echo         https://sourceforge.net/projects/exiftool/files/
        echo         extract it next to this script, rename the exe
        echo         to exiftool.exe, and run again.
        pause
        exit /b 1
    )
    echo Downloading ExifTool !ETVER! for Windows from SourceForge...
    echo ^(This is about 11 MB, give it a moment.^)
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Invoke-WebRequest -Uri 'https://sourceforge.net/projects/exiftool/files/exiftool-!ETVER!_64.zip/download' -OutFile 'exiftool_dl.zip'"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$zip = Get-Item 'exiftool_dl.zip' -ErrorAction SilentlyContinue; " ^
        "$ok = $zip -and $zip.Length -gt 1000000; " ^
        "if (-not $ok) { exit 1 }"
    if errorlevel 1 (
        echo [ERROR] Download failed or the file looks wrong.
        echo         Check your connection and run again, or grab the
        echo         Windows 64-bit zip by hand from
        echo         https://sourceforge.net/projects/exiftool/files/
        del exiftool_dl.zip 2>nul
        pause
        exit /b 1
    )
    echo Extracting...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -Path 'exiftool_dl.zip' -DestinationPath 'exiftool_tmp' -Force; " ^
        "$exe = Get-ChildItem 'exiftool_tmp' -Recurse -Filter '*.exe' | Select-Object -First 1; " ^
        "Copy-Item $exe.FullName 'exiftool.exe' -Force; " ^
        "$dat = Get-ChildItem 'exiftool_tmp' -Recurse -Directory -Filter 'exiftool_files' | Select-Object -First 1; " ^
        "Copy-Item $dat.FullName 'exiftool_files' -Recurse -Force"
    rmdir /s /q exiftool_tmp 2>nul
    del exiftool_dl.zip 2>nul
    if not exist exiftool.exe (
        echo [ERROR] Extraction failed - exiftool.exe not found in the zip.
        pause
        exit /b 1
    )
    if not exist exiftool_files (
        echo [ERROR] Extraction failed - exiftool_files folder not found in the zip.
        echo         The exe needs that folder next to it to run.
        pause
        exit /b 1
    )
    echo [OK] exiftool.exe + exiftool_files ready
)

REM ---- Pre-build sanity check ----
REM A packed exe like exiftool.exe can get quarantined by Defender between
REM the download and the build - catch that here with a useful message.
if not exist exiftool.exe (
    echo [ERROR] exiftool.exe is missing from %CD%.
    echo         It was verified right after the download step, so something
    echo         on this PC removed it in between.
    echo         Usual suspect: Windows Security quarantined it.
    echo         Check Windows Security - Protection history, restore the file,
    echo         add this folder as an exclusion, then run this script again.
    echo.
    echo         Or download the Windows 64-bit zip by hand from
    echo         https://sourceforge.net/projects/exiftool/files/
    echo         extract exiftool.exe and exiftool_files next to this script,
    echo         and it will skip the download entirely.
    pause
    exit /b 1
)
if not exist exiftool_files (
    echo [ERROR] exiftool_files folder is missing from %CD%.
    echo         Re-run this script to download it again.
    pause
    exit /b 1
)
if not exist audio_scrub.py (
    echo [ERROR] audio_scrub.py is missing - grab the latest files and rebuild.
    pause
    exit /b 1
)
echo [OK] Build inputs verified.

REM ---- Clear previous build ----
REM Windows cannot overwrite a running exe, so drop the old one first.
REM If it is still open this fails and we say so plainly.
if exist dist\MetaScrub.exe (
    del dist\MetaScrub.exe >nul 2>&1
    if exist dist\MetaScrub.exe (
        echo [ERROR] Cannot replace dist\MetaScrub.exe because it is still running.
        echo         Close MetaScrub fully - check Task Manager for MetaScrub.exe -
        echo         then run this script again.
        pause
        exit /b 1
    )
    echo [OK] Removed previous build.
)

REM ---- Build ----
echo.
echo Building MetaScrub.exe with PyInstaller (this takes a minute)...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name MetaScrub ^
    --add-data "exiftool.exe;." ^
    --add-data "exiftool_files;exiftool_files" ^
    MetaScrub.py
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete:  dist\MetaScrub.exe
echo.
echo  The exe is fully portable - exiftool and its support
echo  folder are bundled inside. Just run it, pick a folder, and scrub.
echo ============================================================
echo.
pause
