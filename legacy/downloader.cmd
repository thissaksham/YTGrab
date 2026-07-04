@echo off
title yt-dlp Downloader (with Updater and Hotstar Support)
setlocal EnableDelayedExpansion

:: === 1. Configuration ===

:: --- Main Downloader Settings ---
set "INFO=[+]"
set "ERROR=[!]"
set "PROGRESS=[*]"
set /a RETRY_DELAY=3
set /a MAX_RETRIES=3

:: You can adjust this variable if needed.
set "DEFAULT_FORMAT=bv*[vcodec~=vp9][height>=720][height<=1080]+ba[acodec~=opus]/bv*[height>=720][height<=1080]+ba[acodec~=opus]/bv*[vcodec~=vp9][height>1080]+ba[acodec~=opus]/bv+ba/best"
set YTDLP_OPTS=--no-warnings --embed-metadata --embed-thumbnail --convert-thumbnails jpg --write-info-json --retries 3 --progress --merge-output-format mp4
set /a VIDEO_SUCCESS=0
set /a VIDEO_FAILED=0

:: --- Environment Variable Paths ---
set "YTDLP_EXE=%YTDLP%"
set "FFMPEG_EXE=%FFMPEG%"
set "FFPROBE_EXE=%FFPROBE%"
set "COOKIES_FILE_YOUTUBE=%~dp0cookies_youtube.txt"

:: --- Updater Settings ---
set "YTDLP_URL=https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
set "FF_DOWNLOAD_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.7z"
set "FF_VER_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.7z.ver"

:: === 2. Initial Dependency Check ===
echo === CHECKING DEPENDENCIES ===
echo.

if not defined YTDLP (
    echo %ERROR% YTDLP environment variable is not set!
    echo Please set the YTDLP variable to point to yt-dlp.exe
    goto :END_SCRIPT_ERROR
)

if not defined FFMPEG (
    echo %ERROR% FFMPEG environment variable is not set!
    echo Please set the FFMPEG variable to point to ffmpeg.exe
    goto :END_SCRIPT_ERROR
)

if not defined FFPROBE (
    echo %ERROR% FFPROBE environment variable is not set!
    echo Please set the FFPROBE variable to point to ffprobe.exe
    goto :END_SCRIPT_ERROR
)

:: Extract parent directories from environment variables
for %%F in ("%YTDLP_EXE%") do set "YTDLP_DIR=%%~dpF"
for %%F in ("%FFMPEG_EXE%") do set "FFMPEG_DIR=%%~dpF"

:: Remove trailing backslash
if "%YTDLP_DIR:~-1%"=="\" set "YTDLP_DIR=%YTDLP_DIR:~0,-1%"
if "%FFMPEG_DIR:~-1%"=="\" set "FFMPEG_DIR=%FFMPEG_DIR:~0,-1%"

:: Set temp file paths
set "FF_DOWNLOAD_FILE=%FFMPEG_DIR%\ffmpeg-release-essentials.7z"
set "FF_VER_FILE=%FFMPEG_DIR%\ffmpeg-latest.ver"
set "FF_LOCAL_VER_TEMP=%FFMPEG_DIR%\local_ver.txt"

:: === 3. Common Tool Check ===
:: Check for curl
where curl >nul 2>&1
if %ERRORLEVEL% NEQ 0 goto :CURL_ERROR

:: Check for tar (for ffmpeg extraction)
where tar >nul 2>&1
if %ERRORLEVEL% NEQ 0 goto :FF_TAR_ERROR

:: Check for PowerShell (for timestamp updates and auth)
powershell -NoProfile -Command "exit 0" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( 
    echo %ERROR% PowerShell is not available.
    echo Timestamps and SonyLIV auth will not work.
    echo Press any key to continue...
    pause >nul
)

:: === 4. YT-DLP Update Check ===
:YTDLP_CHECK
echo.
echo --- 1. CHECKING YT-DLP ---
if exist "%YTDLP_EXE%" (
    echo %INFO% Found yt-dlp at: %YTDLP_EXE%
    echo %INFO% Checking for updates...
    "%YTDLP_EXE%" -U
    set "YTDLP_STATUS=checked"
    goto :FFMPEG_CHECK
) else (
    echo %INFO% yt-dlp.exe not found at: %YTDLP_EXE%
    echo %INFO% Downloading to: %YTDLP_DIR%
    goto :YTDLP_DOWNLOAD
)

:YTDLP_DOWNLOAD
curl -L -o "%YTDLP_EXE%" "%YTDLP_URL%"
if %ERRORLEVEL% NEQ 0 goto :YTDLP_DOWNLOAD_ERROR
if not exist "%YTDLP_EXE%" goto :YTDLP_DOWNLOAD_ERROR
echo.
echo %INFO% Success! yt-dlp has been downloaded.
"%YTDLP_EXE%" --version
set "YTDLP_STATUS=downloaded"
goto :FFMPEG_CHECK

:YTDLP_DOWNLOAD_ERROR
echo.
echo %ERROR% Failed to download yt-dlp.exe!
echo Check your internet connection or try again later.
echo (Continuing to ffmpeg check anyway...)
set "YTDLP_STATUS=failed"
goto :FFMPEG_CHECK

:: === 5. FFMPEG Update Check ===
:FFMPEG_CHECK
echo.
echo --- 2. CHECKING FFMPEG ---
:: Check if files already exist
if exist "%FFMPEG_EXE%" (
    if exist "%FFPROBE_EXE%" (
        echo %INFO% Found existing ffmpeg and ffprobe at: %FFMPEG_DIR%
        echo %INFO% Checking version...
        goto :FF_VERSION_CHECK
    )
)
echo %INFO% FFMPEG dependencies not found or incomplete.
echo Proceeding to download...
goto :FF_PREP_DOWNLOAD

:FF_VERSION_CHECK
:: Get remote version string
curl -L -s -o "%FF_VER_FILE%" "%FF_VER_URL%"
if %ERRORLEVEL% NEQ 0 goto :FF_VER_CHECK_FAIL
set /p FF_REMOTE_VER= < "%FF_VER_FILE%"
del "%FF_VER_FILE%"

:: Get local version string
"%FFMPEG_EXE%" -version > "%FF_LOCAL_VER_TEMP%"
set /p FF_LOCAL_VER_LINE= < "%FF_LOCAL_VER_TEMP%"
del "%FF_LOCAL_VER_TEMP%"

:: Compare
echo.
echo Local version line: !FF_LOCAL_VER_LINE!
echo Remote version string: !FF_REMOTE_VER!
echo.
echo "!FF_LOCAL_VER_LINE!" | findstr /C:"!FF_REMOTE_VER!" >nul
if %ERRORLEVEL% EQU 0 (
    goto :FF_ALREADY_LATEST
) else (
    echo %INFO% Your version is outdated. Updating...
    goto :FF_PREP_DOWNLOAD
)

:FF_PREP_DOWNLOAD
:: Delete old files if they exist to prevent conflicts
if exist "%FFMPEG_EXE%" del "%FFMPEG_EXE%"
if exist "%FFPROBE_EXE%" del "%FFPROBE_EXE%"
goto :FF_DOWNLOAD

:FF_DOWNLOAD
echo %PROGRESS% Starting ffmpeg download...
if exist "%FF_DOWNLOAD_FILE%" (
    echo %INFO% FFmpeg .7z file already exists. Skipping download...
    goto :FF_EXTRACT
)

echo %PROGRESS% Downloading FFmpeg Essentials Build to %FF_DOWNLOAD_FILE%...
curl -L --connect-timeout 30 --max-time 300 -o "%FF_DOWNLOAD_FILE%" "%FF_DOWNLOAD_URL%"
if %ERRORLEVEL% NEQ 0 goto :FF_DOWNLOAD_ERROR
if not exist "%FF_DOWNLOAD_FILE%" goto :FF_DOWNLOAD_ERROR
echo %INFO% Download completed!

:FF_EXTRACT
echo.
echo %PROGRESS% Starting extraction...

echo %PROGRESS% Cleaning up old ffmpeg build directories...
:: This FOR /D loop only targets directories
FOR /D %%D IN ("%FFMPEG_DIR%\*") DO (
    echo Removing old directory: %%D
    rmdir /S /Q "%%D"
)

echo %PROGRESS% Extracting archive...
tar -xf "%FF_DOWNLOAD_FILE%" -C "%FFMPEG_DIR%"
if %ERRORLEVEL% NEQ 0 goto :FF_EXTRACT_ERROR

:FF_MOVE
echo %PROGRESS% Searching for extracted folder...
set "FF_EXTRACTED_FOLDER="
:: This FOR /D loop finds the name of the folder that was just extracted
FOR /D %%i IN ("%FFMPEG_DIR%\*") DO (
    set "FF_EXTRACTED_FOLDER=%%i"
)

if not defined FF_EXTRACTED_FOLDER (
    echo %ERROR% Could not find the extracted main folder inside "%FFMPEG_DIR%".
    goto :END_SCRIPT_ERROR
)
echo %INFO% Found folder: %FF_EXTRACTED_FOLDER%

echo %PROGRESS% Moving ffmpeg.exe and ffprobe.exe...
move "%FF_EXTRACTED_FOLDER%\bin\ffmpeg.exe" "%FFMPEG_DIR%\"
move "%FF_EXTRACTED_FOLDER%\bin\ffprobe.exe" "%FFMPEG_DIR%\"

if exist "%FFMPEG_EXE%" goto :FF_SUCCESS
goto :FF_MOVE_ERROR

:FF_SUCCESS
echo.
echo %INFO% Success! ffmpeg.exe and ffprobe.exe are in %FFMPEG_DIR%
echo %PROGRESS% Deleting temporary archive: %FF_DOWNLOAD_FILE%
del "%FF_DOWNLOAD_FILE%"
echo %PROGRESS% Deleting leftover extracted folder...
rmdir /S /Q "%FF_EXTRACTED_FOLDER%"
echo.
echo %INFO% All ffmpeg operations completed!
set "FF_STATUS=up-to-date"
goto :START_DOWNLOADER

:FF_ALREADY_LATEST
echo.
echo %INFO% Your ffmpeg is already up to date.
goto :START_DOWNLOADER

:FF_VER_CHECK_FAIL
echo.
echo %ERROR% Warning: Could not download the remote ffmpeg version file.
echo %INFO% Your existing ffmpeg installation will be used.
echo %INFO% If you want to force an update, delete ffmpeg.exe and ffprobe.exe.
echo.
goto :START_DOWNLOADER

:: === 6. Main Downloader ===
:START_DOWNLOADER
echo.
:: Refresh YouTube cookies from the saved embedded-browser login (yt_login.py)
if exist "%~dp0yt_login.py" (
    where python >nul 2>&1
    if not errorlevel 1 (
        echo %PROGRESS% Refreshing YouTube cookies from saved login...
        python "%~dp0yt_login.py" --refresh
        if errorlevel 2 echo %INFO% No YouTube login saved. Run once: python "%~dp0yt_login.py"
    )
)
echo === All checks complete ===
echo.
echo Press Enter to continue to downloader...
pause >nul
cls
echo === Video Downloader ===
echo.
:START
set "URL="
set /p "URL=Paste URL (or press Enter to exit): "
if "!URL!"=="" goto CLEANUP

:: Remove surrounding quotes
if "!URL:~0,1!"=="""" set "URL=!URL:~1,-1!"

:: Optional format override (Enter keeps the default selector)
set "FORMAT=!DEFAULT_FORMAT!"
set "FMT_CHOICE="
set /p "FMT_CHOICE=Format (Enter=default, L=list formats, or paste selector): "
if /i "!FMT_CHOICE!"=="L" (
    "%YTDLP_EXE%" --no-warnings -F "!URL!"
    set "FMT_CHOICE="
    set /p "FMT_CHOICE=Format selector (Enter=default): "
)
if not "!FMT_CHOICE!"=="" set "FORMAT=!FMT_CHOICE!"
:: Detect YouTube links
echo "!URL!" | findstr /i "youtube.com youtu.be" >nul 2>&1
if !errorlevel! equ 0 goto CHECK_YT_TYPE

:: Non-YouTube URL
echo %INFO% Non-YouTube URL detected.
goto DOWNLOAD_GENERIC

:CHECK_YT_TYPE
:: Check for playlist or channel
echo "!URL!" | find /i "list=" >nul
if !errorlevel! equ 0 (
    echo %INFO% YouTube playlist detected.
    goto DOWNLOAD_YT_PLAYLIST
)

echo "!URL!" | find /i "/@" >nul
if !errorlevel! equ 0 (
    echo %INFO% YouTube channel detected.
    goto DOWNLOAD_YT_PLAYLIST
)

:: Single video
echo %INFO% YouTube single video detected.
goto DOWNLOAD_YT_SINGLE

:DOWNLOAD_YT_SINGLE
echo.
echo %PROGRESS% Downloading video: !URL!
"%YTDLP_EXE%" -f "!FORMAT!" %YTDLP_OPTS% "!URL!"
set "EXIT_CODE=!errorlevel!"

if !EXIT_CODE! neq 0 (
    echo.
    echo %ERROR% Download failed.
    set /a VIDEO_FAILED+=1
    echo Press any key to continue...
    pause >nul
    goto LOOPEND
)

set /a VIDEO_SUCCESS+=1
call :UPDATE_TIMESTAMP "!URL!"
echo %INFO% Download successful

:: Mark as watched if cookies file exists
if exist "%COOKIES_FILE_YOUTUBE%" (
    echo %PROGRESS% Marking video as watched...
    "%YTDLP_EXE%" --cookies "%COOKIES_FILE_YOUTUBE%" --mark-watched --simulate --skip-download --no-warnings "!URL!" >nul
    echo %INFO% Marked video as watched...
) else (
    echo %INFO% cookies_youtube.txt not found, skipping mark as watched
)

echo.
goto LOOPEND

:DOWNLOAD_YT_PLAYLIST
echo.
echo %PROGRESS% Extracting playlist/channel video URLs...
set "VIDEO_IDS="
for /f "tokens=*" %%A in ('"%YTDLP_EXE%" --no-warnings --flat-playlist --get-id !URL!') do (
    set "VIDEO_IDS=!VIDEO_IDS! %%A"
)

if "!VIDEO_IDS!"=="" (
    echo %ERROR% Failed to extract video list.
    echo Press any key to continue...
    pause >nul
    goto LOOPEND
)

:: Prompt for start and end video numbers (optional)
set /p "START_NUM=Enter starting video number (default 1): "
if "!START_NUM!"=="" set START_NUM=1
if !START_NUM! lss 1 set START_NUM=1

:: Count total videos for validation
set /a TOTAL_AVAILABLE_VIDEOS=0
for %%A in (!VIDEO_IDS!) do set /a TOTAL_AVAILABLE_VIDEOS+=1

set /p "END_NUM=Enter ending video number (default !TOTAL_AVAILABLE_VIDEOS!): "
if "!END_NUM!"=="" set END_NUM=!TOTAL_AVAILABLE_VIDEOS!
if !END_NUM! lss !START_NUM! set END_NUM=!START_NUM!
if !END_NUM! gtr !TOTAL_AVAILABLE_VIDEOS! set END_NUM=!TOTAL_AVAILABLE_VIDEOS!

:: Convert VIDEO_IDS to array and slice from START_NUM to END_NUM
set "TEMP_IDS="
set /a IDX=0
for %%A in (!VIDEO_IDS!) do (
    set /a IDX+=1
    if !IDX! geq !START_NUM! (
        if !IDX! leq !END_NUM! (
            set "TEMP_IDS=!TEMP_IDS! %%A"
        )
    )
)
set "VIDEO_IDS=!TEMP_IDS!"
if "!VIDEO_IDS!"=="" (
    echo %ERROR% No videos found in range !START_NUM! to !END_NUM!.
    echo Press any key to continue...
    pause >nul
    goto LOOPEND
)

set /a VIDEO_COUNT=0
set /a TOTAL_VIDEOS=0
for %%A in (!VIDEO_IDS!) do set /a TOTAL_VIDEOS+=1

if !TOTAL_VIDEOS! lss 1 (
    echo %ERROR% No videos found in selected range.
    echo Press any key to continue...
    pause >nul
    goto LOOPEND
)

echo %INFO% Starting individual downloads...
echo %INFO% Total Videos: !TOTAL_VIDEOS! (from !START_NUM! to !END_NUM!)
echo.
for %%A in (!VIDEO_IDS!) do (
    set /a VIDEO_COUNT+=1
    set "VID_URL=https://www.youtube.com/watch?v=%%A"
    set /a RETRY_COUNT=0
    set "DOWNLOAD_SUCCESS="

    :RETRY_LOOP_PLAYLIST
    echo %PROGRESS% Downloading video !VIDEO_COUNT! of !TOTAL_VIDEOS!: !VID_URL!
    if !RETRY_COUNT! gtr 0 (
        echo %INFO% Retry attempt !RETRY_COUNT! of !MAX_RETRIES!...
    )

    "%YTDLP_EXE%" -f "!FORMAT!" %YTDLP_OPTS% "!VID_URL!"
    set "EXIT_CODE=!errorlevel!"

    if !EXIT_CODE! equ 0 (
        set "DOWNLOAD_SUCCESS=1"
        set /a VIDEO_SUCCESS+=1
        call :UPDATE_TIMESTAMP "!VID_URL!"
        echo %INFO% Download successful
	
        :: Mark as watched if cookies file exists
        if exist "%COOKIES_FILE_YOUTUBE%" (
            echo %PROGRESS% Marking video as watched...
            "%YTDLP_EXE%" --cookies "%COOKIES_FILE_YOUTUBE%" --mark-watched --simulate --skip-download --no-warnings "!VID_URL!" >nul
            echo %INFO% Marked video as watched...
        ) else (
            echo %INFO% cookies_youtube.txt not found, skipping mark as watched
        )
    ) else (
        echo.
        echo %ERROR% Download failed.
        set /a RETRY_COUNT+=1
        if !RETRY_COUNT! lss !MAX_RETRIES! (
            echo %INFO% Retrying in !RETRY_DELAY! seconds...
            timeout /t !RETRY_DELAY! >nul
            goto RETRY_LOOP_PLAYLIST
        ) else (
            echo %ERROR% Max retries reached.
            set /a VIDEO_FAILED+=1
            echo Press any key to continue with next video, or Ctrl+C to abort...
            pause >nul
        )
    )
    echo.
)

goto LOOPEND

:: === FIXED GENERIC DOWNLOADER BLOCK ===
:DOWNLOAD_GENERIC
echo.
set /a RETRY_COUNT=0
set "AUTH_ADDED="
set "AUTH_ARGS="

:: --- Pre-emptive SonyLIV Check ---
echo "!URL!" | findstr /i "sonyliv.com" >nul 2>&1
if !errorlevel! equ 0 (
    set "SONY_FILE=%~dp0cookies_sonyliv.txt"
    if exist "!SONY_FILE!" (
        echo %INFO% SonyLIV detected. Found auth file, applying credentials...
        goto :LOAD_AUTH_AND_RETRY
    )
)

:: --- Pre-emptive Hotstar Check ---
echo "!URL!" | findstr /i "hotstar.com" >nul 2>&1
if !errorlevel! equ 0 (
    set "HOTSTAR_COOKIES=%~dp0cookies_hotstar.txt"
    if exist "!HOTSTAR_COOKIES!" (
        echo %INFO% Hotstar detected. Found cookies file, applying...
        set "AUTH_ARGS=--cookies "!HOTSTAR_COOKIES!""
    ) else (
        echo %ERROR% Hotstar detected but 'cookies_hotstar.txt' is missing!
        echo %INFO% Script will attempt download, but likely fail without auth.
    )
)

:RETRY_LOOP_GENERIC
echo %PROGRESS% Downloading (generic mode): "!URL!"
if !RETRY_COUNT! gtr 0 echo %INFO% Retry attempt !RETRY_COUNT! of !MAX_RETRIES!...

:: Execute yt-dlp (Append AUTH_ARGS if set)
"%YTDLP_EXE%" -f "!FORMAT!" %YTDLP_OPTS% !AUTH_ARGS! "!URL!"
set "EXIT_CODE=!errorlevel!"

:: Check Success
if !EXIT_CODE! equ 0 goto :GENERIC_SUCCESS

:: Check Failure - Logic Flow
echo "!URL!" | findstr /i "sonyliv.com" >nul 2>&1
if !errorlevel! neq 0 goto :GENERIC_ERROR_HANDLER

:: If we already added auth and it still failed, do not prompt again (avoid infinite loop)
if "!AUTH_ADDED!"=="1" goto :GENERIC_ERROR_HANDLER

:: SonyLIV Logic (Only reached if download failed AND no auth was used yet)
echo.
echo %ERROR% Download failed.
echo This SonyLIV video likely requires authentication.
set "SONY_FILE=%~dp0cookies_sonyliv.txt"

:: If file appeared (or was missed), use it. Otherwise, prompt user.
if exist "!SONY_FILE!" goto :LOAD_AUTH_AND_RETRY

:: If file missing, prompt user
echo %INFO% Please create "cookies_sonyliv.txt" in this folder: %~dp0
echo %INFO% Paste ONLY your accessToken (the long eyJhbG... string) inside it.
echo.
pause

if not exist "!SONY_FILE!" goto :GENERIC_ERROR_HANDLER

:LOAD_AUTH_AND_RETRY
:: We use FOR /F to read the file. This supports long lines (up to 8191 chars)
:: unlike set /p which fails at 1024.
set "SONY_TOKEN="
for /f "usebackq delims=" %%i in ("!SONY_FILE!") do set "SONY_TOKEN=%%i"

if "!SONY_TOKEN!"=="" (
    echo %ERROR% Failed to read token from file.
    goto :GENERIC_ERROR_HANDLER
)

echo %INFO% Token loaded. Retrying...
:: We construct the auth arguments safely
set "AUTH_ARGS=--username token --password "!SONY_TOKEN!""
set "AUTH_ADDED=1"
goto :RETRY_LOOP_GENERIC

:GENERIC_SUCCESS
set "DOWNLOAD_SUCCESS=1"
set /a VIDEO_SUCCESS+=1
echo %INFO% Download successful
:: Call cleanup to delete info.json and set timestamp
call :UPDATE_TIMESTAMP "!URL!"
goto :LOOPEND

:GENERIC_ERROR_HANDLER
echo.
echo %ERROR% Download failed.
set /a RETRY_COUNT+=1
if !RETRY_COUNT! geq !MAX_RETRIES! goto :GENERIC_FINAL_FAILURE

echo %INFO% Retrying in !RETRY_DELAY! seconds...
timeout /t !RETRY_DELAY! >nul
goto :RETRY_LOOP_GENERIC

:GENERIC_FINAL_FAILURE
echo %ERROR% Max retries reached.
set /a VIDEO_FAILED+=1
echo %INFO% Note: DRM protected videos (like Freedom at Midnight) cannot be downloaded.
echo Press any key to continue...
pause >nul
goto :LOOPEND

:UPDATE_TIMESTAMP
setlocal EnableDelayedExpansion
set "VIDEO_URL=%~1"
echo.
echo %PROGRESS% Updating file timestamp using PowerShell...

:: Escape single quotes for PowerShell
set "ESCAPED_URL=!VIDEO_URL:'=''!"

:: Final Robust PowerShell Logic
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$url = '!ESCAPED_URL!';" ^
    "$videoId = $null;" ^
    "$m = [regex]::Matches($url, '(?<=[/=v-])([a-zA-Z0-9_-]{11}|[0-9]{10})(?=[/?]|$)');" ^
    "if ($m.Count -gt 0) { $videoId = $m[$m.Count-1].Value } else { Write-Host 'Could not extract ID from URL'; exit 1 };" ^
    "Write-Host \"Extracted ID: $videoId\";" ^
    "$file = Get-ChildItem -Path '.' -File -Filter \"*$videoId*.*\" | Where-Object { $_.Extension -match '\.(webm|mp4|mkv|avi|mov|flv|m4v)' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1;" ^
    "if (-not $file) { Write-Host 'Video file not found in directory'; exit 1 };" ^
    "Write-Host \"Target File: $($file.Name)\";" ^
    "$jsonFile = Get-ChildItem -Path '.' -File -Filter \"*$videoId*.info.json\" | Sort-Object LastWriteTime -Descending | Select-Object -First 1;" ^
    "$ts = $null; if ($jsonFile) { try { $json = [System.IO.File]::ReadAllText($jsonFile.FullName) | ConvertFrom-Json; $ts = $json.timestamp } catch { } };" ^
    "if (-not $ts) { $ts = & '!YTDLP_EXE!' --no-warnings --ignore-no-formats-error --print timestamp $url 2>$null };" ^
    "if ($ts) { try { $utc = ([datetime]'1970-01-01').AddSeconds([double]$ts); $loc = [TimeZoneInfo]::ConvertTimeFromUtc($utc, [TimeZoneInfo]::Local); $file.LastWriteTime = $loc; $file.CreationTime = $loc; Write-Host ('Updated timestamp -> ' + $loc); if ($jsonFile) { Start-Sleep -Milliseconds 200; Remove-Item -LiteralPath $jsonFile.FullName -Force -ErrorAction SilentlyContinue; Write-Host 'Deleted metadata file.' }; exit 0 } catch { exit 1 } } else { Write-Host 'No timestamp found'; exit 1 }"

set "EXIT_CODE=!errorlevel!"
if !EXIT_CODE! neq 0 (
    echo %ERROR% Warning: Failed to update file timestamp or clean JSON.
)
endlocal
goto :EOF

:LOOPEND
goto START

:CLEANUP
echo.
echo %INFO% Cleaning up...
echo.
echo Goodbye!
timeout /t 1 /nobreak >nul
endlocal
exit /b

:: === 7. Error Handlers (Fatal) ===
:CURL_ERROR
echo %ERROR% curl not found!
echo Please ensure curl is installed and in your system's PATH.
goto :END_SCRIPT_ERROR

:FF_TAR_ERROR
echo %ERROR% tar.exe not found!
echo This script requires native Windows tar (included in Windows 10/11) to extract .7z files.
goto :END_SCRIPT_ERROR

:FF_DOWNLOAD_ERROR
echo %ERROR% Failed to download FFmpeg!
echo Check internet connection or try again later.
goto :END_SCRIPT_ERROR

:FF_EXTRACT_ERROR
echo.
echo %ERROR% Failed to extract ffmpeg files with tar!
echo (Errorlevel: %ERRORLEVEL%)
goto :END_SCRIPT_ERROR

:FF_MOVE_ERROR
echo.
echo %ERROR% FFMPEG files not found after move!
goto :END_SCRIPT_ERROR

:END_SCRIPT_ERROR
echo.
echo %ERROR% A fatal dependency error occurred.
echo The script cannot continue.
echo === All checks complete ===
pause
endlocal
exit /b 1