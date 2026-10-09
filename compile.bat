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

REM ---- Colors (ANSI escape codes - Windows 10 and up) ----
for /F "tokens=1,2 delims=#" %%a in ('"prompt #$H#$E# & echo on & for %%b in (1) do rem"') do set "ESC=%%b"
set "RESET=%ESC%[0m"
set "BOLD=%ESC%[1m"
set "DIMC=%ESC%[2m"
set "RED=%ESC%[91m"
set "GREEN=%ESC%[92m"
set "YELLOW=%ESC%[93m"
set "BLUE=%ESC%[94m"
set "MAGENTA=%ESC%[95m"
set "CYAN=%ESC%[96m"
set "OKTAG=%GREEN%%BOLD%[OK]%RESET%"
set "ERRTAG=%RED%%BOLD%[ERROR]%RESET%"
set "STEPC=%MAGENTA%%BOLD%"

echo.
echo %CYAN%%BOLD%+============================================================+%RESET%
echo %CYAN%%BOLD%^|%RESET%  %MAGENTA%%BOLD%MetaScrub builder%RESET%  %DIMC%- Python in, shiny portable exe out%RESET%      %CYAN%%BOLD%^|%RESET%
echo %CYAN%%BOLD%+============================================================+%RESET%
echo.

echo.
echo %STEPC%[1/4] Python%RESET%
REM ---- Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo %YELLOW%Python 3 not found. Trying to install it with winget...%RESET%
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    REM Pick up the fresh install in this session
    set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"
    python --version >nul 2>&1
    if errorlevel 1 (
        echo %ERRTAG% Automatic Python install did not work.
        echo         Install it from https://www.python.org/downloads/
        echo         and tick "Add python.exe to PATH" during setup,
        echo         then run this script again.
        pause
        exit /b 1
    )
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo %OKTAG% Python %PYVER%

echo.
echo %STEPC%[2/4] PyInstaller%RESET%
REM ---- PyInstaller ----
echo.
echo %CYAN%Installing PyInstaller (this is quick)...%RESET%
python -m pip install --upgrade --quiet pyinstaller
if errorlevel 1 (
    echo %ERRTAG% pip failed to install PyInstaller. Check your internet connection.
    pause
    exit /b 1
)
echo %OKTAG% PyInstaller ready

echo.
echo %STEPC%[3/4] ExifTool%RESET%
REM ---- ExifTool ----
REM NOTE: exiftool.org now points downloads at SourceForge, so we fetch
REM from there. The Windows zip contains exiftool.exe PLUS a required
REM exiftool_files support folder - both get bundled into the build.
REM The zip names the exe exiftool(-k).exe - normalize it in case the
REM user extracted the zip by hand without renaming.
if not exist exiftool.exe if exist "exiftool(-k).exe" (
    ren "exiftool(-k).exe" exiftool.exe
    echo %OKTAG% Renamed the stock exe to exiftool.exe
)
echo.
if exist exiftool.exe if exist exiftool_files (
    echo %OKTAG% exiftool.exe + exiftool_files already present, skipping download.
) else (
    echo %CYAN%Finding latest ExifTool release...%RESET%
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$html = (Invoke-WebRequest -Uri 'https://exiftool.org/' -UseBasicParsing).Content; " ^
        "$m = [regex]::Match($html, 'exiftool-(\d+\.\d+)_64\.zip'); " ^
        "if ($m.Success) { $m.Groups[1].Value } else { '' }" > etver.txt
    set /p ETVER=<etver.txt
    del etver.txt
    if "!ETVER!"=="" (
        echo %ERRTAG% Could not determine the latest ExifTool version.
        echo         Get the Windows 64-bit zip by hand from
        echo         https://sourceforge.net/projects/exiftool/files/
        echo         extract it next to this script, rename the exe
        echo         to exiftool.exe, and run again.
        pause
        exit /b 1
    )
    echo %CYAN%Downloading ExifTool !ETVER! for Windows from SourceForge...%RESET%
    echo %YELLOW%^(This is about 11 MB, give it a moment.^)%RESET%
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Invoke-WebRequest -Uri 'https://sourceforge.net/projects/exiftool/files/exiftool-!ETVER!_64.zip/download' -OutFile 'exiftool_dl.zip'"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$zip = Get-Item 'exiftool_dl.zip' -ErrorAction SilentlyContinue; " ^
        "$ok = $zip -and $zip.Length -gt 1000000; " ^
        "if (-not $ok) { exit 1 }"
    if errorlevel 1 (
        echo %ERRTAG% Download failed or the file looks wrong.
        echo         Check your connection and run again, or grab the
        echo         Windows 64-bit zip by hand from
        echo         https://sourceforge.net/projects/exiftool/files/
        del exiftool_dl.zip 2>nul
        pause
        exit /b 1
    )
    echo %CYAN%Extracting...%RESET%
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -Path 'exiftool_dl.zip' -DestinationPath 'exiftool_tmp' -Force; " ^
        "$exe = Get-ChildItem 'exiftool_tmp' -Recurse -Filter '*.exe' | Select-Object -First 1; " ^
        "Copy-Item $exe.FullName 'exiftool.exe' -Force; " ^
        "$dat = Get-ChildItem 'exiftool_tmp' -Recurse -Directory -Filter 'exiftool_files' | Select-Object -First 1; " ^
        "Copy-Item $dat.FullName 'exiftool_files' -Recurse -Force"
    rmdir /s /q exiftool_tmp 2>nul
    del exiftool_dl.zip 2>nul
    if not exist exiftool.exe (
        echo %ERRTAG% Extraction failed - exiftool.exe not found in the zip.
        pause
        exit /b 1
    )
    if not exist exiftool_files (
        echo %ERRTAG% Extraction failed - exiftool_files folder not found in the zip.
        echo         The exe needs that folder next to it to run.
        pause
        exit /b 1
    )
    echo %OKTAG% exiftool.exe + exiftool_files ready
)

echo.
echo %STEPC%[4/4] Pre-flight checks%RESET%
REM ---- Pre-build sanity check ----
REM A packed exe like exiftool.exe can get quarantined by Defender between
REM the download and the build - catch that here with a useful message.
if not exist exiftool.exe (
    echo %ERRTAG% exiftool.exe is missing from %CD%.
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
    echo %ERRTAG% exiftool_files folder is missing from %CD%.
    echo         Re-run this script to download it again.
    pause
    exit /b 1
)
if not exist audio_scrub.py (
    echo %ERRTAG% audio_scrub.py is missing - grab the latest files and rebuild.
    pause
    exit /b 1
)
echo %OKTAG% Build inputs verified.

REM ---- Clear previous build ----
REM Windows cannot overwrite a running exe, so drop the old one first.
REM If it is still open this fails and we say so plainly.
if exist dist\MetaScrub.exe (
    del dist\MetaScrub.exe >nul 2>&1
    if exist dist\MetaScrub.exe (
        echo %ERRTAG% Cannot replace dist\MetaScrub.exe because it is still running.
        echo         Close MetaScrub fully - check Task Manager for MetaScrub.exe -
        echo         then run this script again.
        pause
        exit /b 1
    )
    echo %OKTAG% Removed previous build.
)

REM ---- Build ----
echo.
echo %YELLOW%%BOLD%Building MetaScrub.exe with PyInstaller (this takes a minute)...%RESET%
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name MetaScrub ^
    --add-data "exiftool.exe;." ^
    --add-data "exiftool_files;exiftool_files" ^
    MetaScrub.py
if errorlevel 1 (
    echo %ERRTAG% PyInstaller build failed. See output above.
    pause
    exit /b 1
)

echo.
echo %GREEN%%BOLD%+============================================================+%RESET%
echo %GREEN%%BOLD%^|%RESET%   Build complete:  %BOLD%dist\MetaScrub.exe%RESET%                           %GREEN%%BOLD%^|%RESET%
echo %GREEN%%BOLD%^|%RESET%                                                            %GREEN%%BOLD%^|%RESET%
echo %GREEN%%BOLD%^|%RESET%   Fully portable - exiftool and its support folder         %GREEN%%BOLD%^|%RESET%
echo %GREEN%%BOLD%^|%RESET%   are bundled inside. Run it, pick a folder, scrub.        %GREEN%%BOLD%^|%RESET%
echo %GREEN%%BOLD%+============================================================+%RESET%
echo.
pause
