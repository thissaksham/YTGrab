# YTGrab

A modern, single-file **yt-dlp GUI for Windows**. Paste a link, choose quality, and
your downloads appear in a searchable, thumbnail-driven library. No command line,
no manual cookie files, and no bundled browser — the UI is rendered by the
WebView2 runtime that ships with Windows 11 (and is available for Windows 10).

## What it does

YTGrab wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp) behind a clean desktop
interface. It downloads the tools it needs on first run, keeps yt-dlp up to date
automatically, and stores your signed-in browser profile locally so YouTube and
other sites see a real browser session instead of an anonymous downloader.

## Features

### Downloads

- **One link, any source** — YouTube videos, Shorts, playlists, channels, and
  any other site yt-dlp supports (hundreds of sites, list fetched live).
- **Auto or Custom quality** — Auto picks the best codec (VP9 → AV1 → HEVC, or
  Legacy H.264) up to your chosen resolution. Custom lists the real formats
  available for that video and pairs your pick with the best audio track.
- **Playlist / channel ranges** — download the whole list or a specific slice.
- **Queue** — downloads run one at a time; click the button again to queue more.
- **Live progress cards** — thumbnail, title, channel, phase, speed, ETA, and
  percentage are updated in real time.
- **Retry after failure** — failed downloads keep their metadata and can be
  retried from the library, even after restarting the app.
- **Cancel** — stop the active download at any time.

### Library

- **Persistent gallery** — every download is a card with thumbnail, channel,
  size, format, and duration. History is saved across restarts.
- **Multiple libraries** — YouTube downloads go to *YT Downloads* by default,
  non-YouTube sites get their own auto-created site library, and you can add
  custom folder-backed libraries for your own collections.
- **Sub-folders & breadcrumbs** — navigate inside a library, hide folders from
  view, and restore hidden folders later.
- **Import local videos** — drag-and-drop files onto the window, or use the
  import button, to move videos into the active library and catalog them.
- **Sort, filter, search, group** — filter by All / Active / Done / Failed,
  sort by release date, date added, title, or size, search by title, and group
  YouTube downloads by channel.
- **Missing-file cleanup** — purge library entries whose files no longer exist.

### Actions

- **Play / Reveal / Delete** — double-click a finished video to play it with the
  default player, reveal it in File Explorer, or send it to the Recycle Bin.
- **Set upload-date timestamp** — downloaded files can keep their original
  upload date as the Windows creation/modification time.
- **Mark as watched** — after a YouTube download, the app fires YouTube's own
  history ping from inside the signed-in browser so the video appears in your
  watched history.
- **Skip download** — mark a video watched without downloading anything.

### Accounts & cookies

- **Built-in browser login** — sign into YouTube once inside the app's own
  WebView2 window. Cookies stay in the local encrypted profile; no cookie file
  is ever exported to disk for YouTube.
- **Other sites** — add and sign into sites like Hotstar or SonyLIV; their
  session cookies are read from the profile at download time and passed to
  yt-dlp.
- **Avoids bot checks** — signed-in session cookies plus a bundled Node.js
  runtime help yt-dlp solve YouTube's signature and n-parameter challenges.

### OneDrive fast browser

- **Browse your OneDrive** — optional rclone-powered browser with thumbnails,
  folders, and file kinds.
- **Multi-threaded downloads** — download files from OneDrive with rclone's
  chunked parallelism, staging in `%TEMP%` and moving finished files to the
  chosen destination.
- **Picks up existing rclone sign-in** — if you already use rclone with
  OneDrive, YTGrab reuses that token instead of asking you to sign in again.

### Tools & updates

- **Self-managing dependencies** — yt-dlp, ffmpeg, ffprobe, and Node are
  downloaded on first launch. yt-dlp updates itself every launch; ffmpeg is
  updated only on demand from the Profile panel.
- **In-app updater** — when a new release is available, click the update chip
  in the header to download and install it automatically (installer or portable,
  depending on which build you are running).
- **Release automation** — pushing a version bump to `installer.iss` triggers a
  GitHub Actions workflow that builds the installer and publishes a release.

## Requirements

- Windows 10 or Windows 11
- WebView2 runtime (preinstalled on Windows 11; download for Windows 10 from
  [Microsoft](https://developer.microsoft.com/microsoft-edge/webview2/))
- Internet connection for first-launch dependency downloads

> The app is unsigned, so Windows SmartScreen may warn on first run. Click
> **More info**, then **Run anyway**.

## Install

Get the latest release from the
[**Releases**](https://github.com/thissaksham/YTGrab/releases/latest) page:

- **`YTGrab-Setup-x.y.z.exe`** — installer (recommended). Creates shortcuts and
  a clean uninstall entry. Dependencies are fetched on first launch.
- **`YTGrab.exe`** — portable single-file build. Downloads its tools on first
  launch and can be run from any folder.

During setup you can optionally create desktop and Start Menu shortcuts, and
opt in to `YTDLP`, `FFMPEG`, and `FFPROBE` environment variables pointing at the
app-managed binaries.

## First launch

The first time you run YTGrab it will:

1. Create `%LOCALAPPDATA%\YTGrab` for tools, browser profile, config, and
   history.
2. Download yt-dlp.exe, ffmpeg.exe, ffprobe.exe, and node.exe (roughly
   150–200 MB total).
3. Open the main window once the tools are ready.

Open the **Profile** panel (person icon in the top-right) and click **Sign in**
to log into YouTube. You only need to do this once; the session persists across
restarts.

## Usage

1. Paste a link into the top bar and press **Download** (or Enter).
2. Choose **Auto** or **Custom** quality, set a playlist range if applicable,
   and toggle **Mark as watched** / **Set file date** as desired.
3. Click **Download** in the dialog to queue the item.
4. Watch progress on the card; finished videos appear in the library.
5. Double-click a finished card to play it, or use the action buttons to reveal
   or delete it.

Drag video files from File Explorer onto the window to import them into the
active library. Switch libraries from the sidebar, and add new folder-backed
libraries with the **Add library** button.

## Data & storage

All app data lives in:

```
%LOCALAPPDATA%\YTGrab\
```

Subfolders:

- `bin\` — yt-dlp, ffmpeg, ffprobe, node, rclone
- `profile\` — WebView2 browser profile and signed-in sessions
- `thumbs\` — generated posters for imported local videos
- `config.json` — settings and library tabs
- `history.json` — download / import history
- `failed.json` — failed jobs that can be retried
- `ytgrab.log` — activity log

Downloads land in `~/Downloads/YTGrab` by default and are organized into
sub-folders such as `youtube/`, `imported/`, and site-specific folders unless
you disable automatic sub-folders in Settings.

## Build from source

You need Python 3.12+ and a Windows environment.

```bash
# 1. Install Python dependencies
pip install pyinstaller pywebview

# 2. Build the one-dir PyInstaller app
pyinstaller --onedir --windowed --name YTGrab --icon ytgrab.ico ytgrab.py

# 3. Build the installer (requires Inno Setup)
#    https://jrsoftware.org/isinfo.php
iscc installer.iss
```

The installer expects the PyInstaller output at `dist\YTGrab\` (onedir build).
`installer.iss` controls the version; the GitHub Actions release workflow reads
that version, syncs it into `ytgrab.py`, creates a tag, builds the installer,
and publishes a release.

### Manual dependency setup

If you want to pre-download the tools without opening the UI:

```bash
python ytgrab.py --setup
```

## Project structure

```text
YTGrab/
├── ytgrab.py          # Entire app: UI HTML/CSS/JS + Python backend
├── ytgrab.ico         # Application icon
├── installer.iss      # Inno Setup installer script
├── legacy/            # Old batch-script workflow this app replaced
│   ├── downloader.cmd
│   └── yt_login.py
└── .github/workflows/
    └── release.yml    # Automated build + release pipeline
```

## Updating

YTGrab checks for new releases on launch. If one is available, an **Update**
chip appears in the header. Clicking it downloads and installs the update, then
restarts the app.

You can also update manually by downloading the latest installer or portable
executable from the Releases page.

## Notes

- YTGrab is for personal use. Respect the terms of service of the sites you
  download from, and only download content you have the right to save.
- YouTube occasionally rate-limits bursts of downloads; if you hit a bot-check,
  wait a minute and retry.
- DRM-protected content cannot be downloaded.
