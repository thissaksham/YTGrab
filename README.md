# YTGrab

A single-exe yt-dlp GUI for Windows with a Seal-style Material 3 interface.
The UI is rendered by WebView2 (already part of Windows 11) — no bundled
browser, no Qt, one ~14 MB portable executable.

![icon](ytgrab.ico)

## Features

- **Seal-style flow** — paste a link, a bottom sheet opens instantly with
  title/thumbnail/duration, pick quality, download. Videos appear as queue
  cards with live progress, speed and ETA parsed from yt-dlp output.
- **No cookie files** — log into YouTube (or any site) once inside the app's
  captive WebView2 profile. Cookies stay in the browser's encrypted storage;
  when yt-dlp needs a session (bot-check, member content) it gets a temp file
  that is zeroed and deleted the moment the run ends.
- **Mark as watched** — fires YouTube's own stats pings (playback + watchtime)
  as page JS inside the logged-in profile: real history entry, 100% progress
  bar, no playback, no credentials on disk. Optional per download.
- **Upload-date timestamps** — downloaded files get their created/modified
  time set to the video's upload date. Optional per download.
- **Self-managing dependencies** — yt-dlp and ffmpeg are downloaded and kept
  up to date automatically in `%LOCALAPPDATA%\YTGrab\bin`.
- Playlist/channel support with range selection, custom format selectors,
  `-F` format listing, per-site session handling for non-YouTube sites.

## Run

Download `YTGrab.exe`, double-click. First launch fetches yt-dlp + ffmpeg
(~90 MB, one time). Click the person icon to log into YouTube once.
Requires Windows 10/11 with the WebView2 runtime (preinstalled on 11).

## Build from source

```
pip install pywebview pyinstaller
python -m PyInstaller --onefile --windowed --name YTGrab --icon ytgrab.ico ytgrab.py
```

Everything lives in one file: [ytgrab.py](ytgrab.py). App data (deps, browser
profile, config, log) lives in `%LOCALAPPDATA%\YTGrab\`.

`legacy/` holds the batch-script workflow YTGrab replaced.

## Note

For personal use. Respect the terms of service of the sites you download
from and only download content you have the right to save.
