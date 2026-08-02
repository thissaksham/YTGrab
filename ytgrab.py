"""YTGrab — single-exe yt-dlp UI for Windows.

WebView2 (ships with Windows 11) renders the UI and hosts the captive
login. YouTube downloads run ANONYMOUS on purpose: passing the account
session makes YouTube serve SABR-only streams that yield 0 downloadable
formats (verified), while anonymous gives the full format list. Login is
used for mark-watched (browser stat pings, no cookies to yt-dlp) and for
non-YouTube sites, whose session is handed to yt-dlp via a temp file that
is zeroed and deleted the moment the run ends.
All app data lives in %LOCALAPPDATA%\\YTGrab\\ (bin, profile, config, history).

  ytgrab.py            launch the UI
  ytgrab.py --setup    CLI: download/update yt-dlp + ffmpeg, then exit

Build: pyinstaller --onefile --windowed --name YTGrab --icon ytgrab.ico ytgrab.py
"""
import base64
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from urllib.parse import quote, urlparse
from ctypes import wintypes
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import webview

APP_NAME = "YTGrab"
APP_VERSION = "1.9.1"  # keep in sync with installer.iss AppVersion (drives the update-check)


# All app data (deps, browser profile, config, history) lives here for BOTH
# the portable exe and the installed build, so login/history/tools are shared.
APP_DIR = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
BIN_DIR = APP_DIR / "bin"
PROFILE_DIR = APP_DIR / "profile"
CONFIG_FILE = APP_DIR / "config.json"
HISTORY_FILE = APP_DIR / "history.json"
FAILED_FILE = APP_DIR / "failed.json"   # failed/unfinished downloads, retryable after restart
THUMB_DIR = APP_DIR / "thumbs"          # generated posters for imported local videos
LOG_FILE = APP_DIR / "ytgrab.log"
YTDLP = BIN_DIR / "yt-dlp.exe"
FFMPEG = BIN_DIR / "ffmpeg.exe"
FFPROBE = BIN_DIR / "ffprobe.exe"
FF_VER_FILE = BIN_DIR / "ffmpeg.ver"
NODE = BIN_DIR / "node.exe"  # JS runtime yt-dlp uses to solve YouTube's sig/n challenge

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
NODE_URL = "https://nodejs.org/dist/latest-v22.x/win-x64/node.exe"  # standalone, ~85 MB
FF_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FF_VER_URLS = ["https://www.gyan.dev/ffmpeg/builds/release-version",
               "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.ver"]

DEFAULT_FORMAT = ("bv*[vcodec~=vp9][height>=720][height<=1080]+ba[acodec~=opus]"
                  "/bv*[height>=720][height<=1080]+ba[acodec~=opus]"
                  "/bv*[vcodec~=vp9][height>1080]+ba[acodec~=opus]"
                  "/bv+ba/best")
BASE_OPTS = ["--no-warnings", "--embed-metadata", "--embed-thumbnail",
             "--convert-thumbnails", "jpg", "--write-info-json", "--retries", "3",
             "--progress", "--newline", "--merge-output-format", "mp4"]
VIDEO_EXTS = {".webm", ".mp4", ".mkv", ".avi", ".mov", ".flv", ".m4v", ".m4a", ".mp3", ".opus"}
NO_WINDOW = 0x08000000
UI_WIN = None

# yt-dlp output -> structured queue events
RE_YT_ID = re.compile(r"^\[youtube\] ([A-Za-z0-9_-]{11}): Downloading webpage")
RE_DEST = re.compile(r"^\[download\] Destination: (.+)$")
RE_PROG = re.compile(r"^\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*\S+"
                     r"(?:\s+at\s+(\S+))?(?:\s+ETA\s+(\S+))?")
RE_FILE_ID = re.compile(r"\[([A-Za-z0-9_-]{11})\]")


def log(msg):
    line = f"{datetime.now():%H:%M:%S} {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout:
        print(line)


# === config / history ===

def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_config():
    d = _load_json(CONFIG_FILE, {})
    return d if isinstance(d, dict) else {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_history():
    d = _load_json(HISTORY_FILE, [])
    return d if isinstance(d, list) else []


def save_history(h):
    try:
        HISTORY_FILE.write_text(json.dumps(h, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_failed():
    d = _load_json(FAILED_FILE, {})
    return d if isinstance(d, dict) else {}


def save_failed(f):
    try:
        FAILED_FILE.write_text(json.dumps(f, indent=2), encoding="utf-8")
    except OSError:
        pass


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_dur(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# === local file import (drag & drop) ===

def ffprobe_meta(path):
    """Duration/resolution/codecs of a local file. {} if ffprobe is missing."""
    if not FFPROBE.exists():
        return {}
    try:
        r = subprocess.run([str(FFPROBE), "-v", "quiet", "-print_format", "json",
                            "-show_format", "-show_streams", str(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW, timeout=60)
        d = json.loads(r.stdout or "{}")
    except Exception:
        return {}
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    dur = (d.get("format") or {}).get("duration")
    try:
        dur = float(dur) if dur else 0.0
    except (TypeError, ValueError):
        dur = 0.0
    return {"duration": dur, "height": v.get("height"), "vcodec": v.get("codec_name")}


def make_thumb(src, dest, at):
    """Grab one frame as a small jpg poster. False if it couldn't be made."""
    if not FFMPEG.exists():
        return False
    try:
        subprocess.run([str(FFMPEG), "-v", "quiet", "-y", "-ss", str(at), "-i", str(src),
                        "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "6", str(dest)],
                       capture_output=True, creationflags=NO_WINDOW, timeout=90)
    except Exception:
        return False
    return dest.exists() and dest.stat().st_size > 0


def entry_thumb(e):
    """History thumb for the UI. Local posters are inlined as a data URI --
    the UI is loaded from a string, so it cannot fetch file:// images."""
    if e.get("thumb"):
        return e["thumb"]
    tf = e.get("thumb_file")
    if not tf:
        return ""
    try:
        return "data:image/jpeg;base64," + base64.b64encode(Path(tf).read_bytes()).decode()
    except OSError:
        return ""


def unique_path(p):
    """A free path next to p: 'clip.mp4' -> 'clip (2).mp4'."""
    if not p.exists():
        return p
    i = 2
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


# === captive-profile helpers (no cookie ever persists to disk) ===

JS_LOGGED_IN = ("(function(){try{if(!(window.ytcfg&&ytcfg.get))return -1;"
                "return ytcfg.get('LOGGED_IN')?1:0}catch(e){return -1}})()")
# Same endpoints yt-dlp's --mark-watched hits, fired as page JS inside the
# logged-in profile so credentials never leave the browser. The playback ping
# creates the history entry; the watchtime ping (st/et = full length) records
# 100% watch progress -- verified via startPercent:100 in the history feed.
JS_MARK_WATCHED = (
    "(function(){try{"
    "var pr=window.ytInitialPlayerResponse;"
    "if(!pr||!pr.playbackTracking)return 0;"
    "var pt=pr.playbackTracking;"
    "var pb=pt.videostatsPlaybackUrl&&pt.videostatsPlaybackUrl.baseUrl;"
    "var wt=pt.videostatsWatchtimeUrl&&pt.videostatsWatchtimeUrl.baseUrl;"
    "if(!pb)return 0;"
    "if(window.__ytgrab_done)return 1;"
    "var a='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';"
    "var c='';for(var i=0;i<16;i++){c+=a.charAt(Math.floor(Math.random()*64));}"
    "var len=parseFloat((pr.videoDetails&&pr.videoDetails.lengthSeconds)||'2')-1;"
    "if(!(len>0)){len=1}"
    "var ping=function(base,extra){var u=new URL(base);"
    "u.searchParams.set('ver','2');u.searchParams.set('cpn',c);"
    "u.searchParams.set('cmt',String(len));u.searchParams.set('el','detailpage');"
    "for(var k in extra){u.searchParams.set(k,extra[k])}"
    "fetch(u.toString(),{mode:'no-cors',credentials:'include',keepalive:true})};"
    "ping(pb,{});"
    "if(wt){ping(wt,{st:'0',et:String(len)})}"
    "window.__ytgrab_done=1;return 1}catch(e){return -1}})()")


def _hidden_poll(url, js, accept, timeout, grace=0):
    """Open url hidden in the captive profile, poll js until accept(result)
    or timeout; return the accepted result (or None)."""
    w = webview.create_window("ytgrab-worker", url, hidden=True)
    closed = threading.Event()
    w.events.closed += lambda *a: closed.set()
    hit = None
    deadline = time.time() + timeout
    while not closed.is_set() and time.time() < deadline:
        time.sleep(2.5)
        try:
            r = w.evaluate_js(js)
        except Exception:
            r = None
        if accept(r):
            hit = r
            break
    if hit is not None and grace:
        time.sleep(grace)
    if not closed.is_set():
        try:
            w.destroy()
        except Exception:
            pass
    return hit


def browser_mark_watched(url, push):
    """Fire the videostats ping from the logged-in profile."""
    r = _hidden_poll(url, JS_MARK_WATCHED, lambda r: r == 1, 45, grace=4)
    ok = r == 1
    push("[post] marked as watched" if ok else "[post] mark-watched failed")
    return ok


def site_key(url):
    """('www.hotstar.com', 'hotstar') from a URL; ('','') if unparseable."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "", ""
    parts = host.split(".")
    return host, (parts[-2] if len(parts) >= 2 else host)


def jars_to_netscape(jars, origin):
    if not jars:
        return None
    fallback = "." + (urlparse(origin).hostname or "")
    lines = ["# Netscape HTTP Cookie File"]
    for jar in jars:
        for name, m in jar.items():
            domain = m["domain"] or fallback
            epoch = 0
            if m["expires"]:
                try:
                    epoch = int(float(m["expires"]))
                except (TypeError, ValueError):
                    try:
                        epoch = int(parsedate_to_datetime(m["expires"]).timestamp())
                    except Exception:
                        epoch = 0
            prefix = "#HttpOnly_" if m["httponly"] else ""
            lines.append("\t".join([prefix + domain,
                                    "TRUE" if domain.startswith(".") else "FALSE",
                                    m["path"] or "/",
                                    "TRUE" if m["secure"] else "FALSE",
                                    str(epoch), name, m.value]))
    return "\n".join(lines) + "\n"


def probe_youtube(timeout=30):
    """One hidden window -> (logged_in: bool, jar_text: str|None). Warms both
    the login badge and the session cache in a single pass."""
    w = webview.create_window("ytgrab-worker", "https://www.youtube.com", hidden=True)
    closed = threading.Event()
    w.events.closed += lambda *a: closed.set()
    logged, jars = None, None
    deadline = time.time() + timeout
    while not closed.is_set() and time.time() < deadline:
        time.sleep(2)
        if logged is None:
            try:
                r = w.evaluate_js(JS_LOGGED_IN)
            except Exception:
                r = None
            if r in (0, 1):
                logged = (r == 1)
        if jars is None:
            try:
                got = w.get_cookies()
            except Exception:
                got = None
            if got and any("youtube" in (m["domain"] or "")
                           for j in got for m in j.values()):
                jars = got
        if logged is not None and (jars is not None or logged is False):
            break
    if not closed.is_set():
        try:
            w.destroy()
        except Exception:
            pass
    return bool(logged), jars_to_netscape(jars, "https://www.youtube.com")


def profile_session_jar(origin="https://www.youtube.com", require="youtube"):
    """Netscape cookie text for a site's session from the profile, or None."""
    w = webview.create_window("ytgrab-worker", origin, hidden=True)
    closed = threading.Event()
    w.events.closed += lambda *a: closed.set()
    jars = None
    deadline = time.time() + 25
    while not closed.is_set() and time.time() < deadline:
        time.sleep(2)
        try:
            got = w.get_cookies()
        except Exception:
            got = None
        if got and (not require or
                    any(require in (m["domain"] or "") for j in got for m in j.values())):
            jars = got
            break
    if not closed.is_set():
        try:
            w.destroy()
        except Exception:
            pass
    return jars_to_netscape(jars, origin)


# === file times (mtime + Windows creation time) ===

def set_file_times(path, epoch):
    os.utime(path, (epoch, epoch))
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    t = int(epoch * 10_000_000) + 116444736000000000
    ft = wintypes.FILETIME(t & 0xFFFFFFFF, (t >> 32) & 0xFFFFFFFF)
    h = k32.CreateFileW(str(path), 0x100, 0, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES
    if h and h != wintypes.HANDLE(-1).value:
        k32.SetFileTime(h, ctypes.byref(ft), None, None)
        k32.CloseHandle(h)


class _SHFILEOP(ctypes.Structure):
    _fields_ = (("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR))


def recycle(path):
    """Send a file to the Recycle Bin (a recoverable delete). True on success."""
    try:
        buf = ctypes.create_unicode_buffer(str(path) + "\x00")  # double-null list
        op = _SHFILEOP()
        op.wFunc = 3  # FO_DELETE
        op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
        op.fFlags = 0x40 | 0x10 | 0x400 | 0x04  # ALLOWUNDO|NOCONFIRM|NOERRORUI|SILENT
        shell32 = ctypes.windll.shell32
        shell32.SHFileOperationW.argtypes = [ctypes.c_void_p]
        shell32.SHFileOperationW.restype = ctypes.c_int
        rc = shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


# === dependency manager ===

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "YTGrab/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)


def download_file(url, dest, push, label):
    tmp = dest.with_suffix(dest.suffix + ".part")
    with http_get(url, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last_pct = 0, -10
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct >= last_pct + 10:
                    last_pct = pct
                    push(f"[deps] {label}: {pct}%")
    tmp.replace(dest)


RELEASES_API = "https://api.github.com/repos/thissaksham/YTGrab/releases/latest"
RELEASES_URL = "https://github.com/thissaksham/YTGrab/releases/latest"


def _ver_tuple(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def check_update():
    """Latest release tag if newer than this build, else None. Never raises."""
    try:
        with http_get(RELEASES_API, timeout=10) as r:
            tag = json.loads(r.read().decode()).get("tag_name") or ""
        if tag and _ver_tuple(tag) > _ver_tuple(APP_VERSION):
            return tag
    except Exception:
        pass
    return None


def latest_release():
    """(tag, {asset_name: download_url}) for the latest release, else (None, {})."""
    try:
        with http_get(RELEASES_API, timeout=10) as r:
            data = json.loads(r.read().decode())
        tag = data.get("tag_name") or ""
        assets = {a.get("name"): a.get("browser_download_url")
                  for a in data.get("assets", []) if a.get("name")}
        return tag, assets
    except Exception:
        return None, {}


def is_installed_build():
    """True for the installed onedir build (has _internal beside the exe); False
    for the portable onefile exe or a source run -- picks installer vs portable."""
    if not getattr(sys, "frozen", False):
        return False
    return (Path(sys.executable).resolve().parent / "_internal").is_dir()


def run_quiet(cmd, push=None, timeout=None):
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW)
    out = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            out.append(line)
            if push:
                push(line)
    proc.wait(timeout=timeout)
    return proc.returncode, "\n".join(out)


def ensure_deps(push, force_ffmpeg=False):
    """yt-dlp is kept current every launch (YouTube breaks it constantly).
    ffmpeg is a stable tool: fetched once when missing and never auto-updated
    -- pass force_ffmpeg=True (the Update button) to refresh it on demand."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if YTDLP.exists():
            push("[deps] checking yt-dlp for updates...")
            run_quiet([YTDLP, "-U"], lambda l: push("[deps] " + l))
        else:
            push("[deps] downloading yt-dlp.exe...")
            download_file(YTDLP_URL, YTDLP, push, "yt-dlp")
            push("[deps] yt-dlp downloaded")
    except Exception as e:
        push(f"[deps] yt-dlp setup failed: {e}")

    try:
        if not NODE.exists():
            push("[deps] downloading node.exe (solves YouTube's JS challenge)... (~85 MB)")
            download_file(NODE_URL, NODE, push, "node")
            push("[deps] node ready")
    except Exception as e:
        push(f"[deps] node setup failed (YouTube may hit bot-checks): {e}")

    have_ff = FFMPEG.exists() and FFPROBE.exists()
    if have_ff and not force_ffmpeg:
        push("[deps] ffmpeg present (auto-update off; use Update to refresh)")
        return YTDLP.exists() and have_ff
    try:
        remote = ""
        for url in FF_VER_URLS:
            try:
                remote = http_get(url).read().decode().strip()
                if remote:
                    break
            except Exception:
                continue
        local = FF_VER_FILE.read_text().strip() if FF_VER_FILE.exists() else ""
        if have_ff and remote and remote == local:
            push(f"[deps] ffmpeg {local} is up to date")
        elif have_ff and not remote:
            push("[deps] ffmpeg version check failed; keeping existing build")
        else:
            push(f"[deps] downloading ffmpeg {remote or ''}...".rstrip() + " (~90 MB)")
            with tempfile.TemporaryDirectory() as td:
                zpath = Path(td) / "ff.zip"
                download_file(FF_ZIP_URL, zpath, push, "ffmpeg")
                with zipfile.ZipFile(zpath) as z:
                    for member in z.namelist():
                        base = member.rsplit("/", 1)[-1].lower()
                        if base in ("ffmpeg.exe", "ffprobe.exe"):
                            with z.open(member) as src, open(BIN_DIR / base, "wb") as dst:
                                while True:
                                    chunk = src.read(1 << 18)
                                    if not chunk:
                                        break
                                    dst.write(chunk)
            FF_VER_FILE.write_text(remote or "unknown")
            push("[deps] ffmpeg ready")
    except Exception as e:
        push(f"[deps] ffmpeg setup failed: {e}")
    return YTDLP.exists() and FFMPEG.exists() and FFPROBE.exists()


# === download pipeline ===

def domain_auth(url):
    u = url.lower()
    if "sonyliv.com" in u:
        f = APP_DIR / "cookies_sonyliv.txt"
        if f.exists():
            tok = f.read_text(encoding="utf-8").strip()
            if tok:
                return ["--username", "token", "--password", tok]
    if "hotstar.com" in u:
        f = APP_DIR / "cookies_hotstar.txt"
        if f.exists():
            return ["--cookies", str(f)]
    return []


def epoch_from_info(info):
    ts = info.get("timestamp")
    if ts:
        return float(ts)
    ud = info.get("upload_date")
    if ud:
        try:
            return datetime.strptime(ud, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def fmt_label(info, path):
    ext = (path.suffix.lstrip(".").lower() if path else info.get("ext") or "")
    h = info.get("height")
    if h:
        return f"{h}p {ext}".strip()
    if info.get("acodec") and info.get("acodec") != "none" and not info.get("height"):
        abr = info.get("abr")
        return (f"{int(abr)}kbps " if abr else "audio ") + ext
    return info.get("format_note") or ext or "?"


def _short_codec(c):
    if not c or c == "none":
        return ""
    c = c.lower()
    for pre, name in (("vp9", "VP9"), ("vp09", "VP9"), ("av01", "AV1"), ("av1", "AV1"),
                      ("avc", "H.264"), ("h264", "H.264"), ("hev", "HEVC"), ("h265", "HEVC"),
                      ("opus", "Opus"), ("mp4a", "AAC"), ("aac", "AAC"), ("mp3", "MP3")):
        if c.startswith(pre):
            return name
    return c.split(".")[0].upper()


def _vrank(f):
    """Sort key for choosing a video stream: prefer VP9, then AV1, then others,
    highest bitrate within a codec. Matches the user's VP9 preference."""
    vc = (f.get("vcodec") or "").lower()
    pref = 0 if vc.startswith(("vp9", "vp09")) else 1 if vc.startswith(("av01", "av1")) else 2
    return (pref, -(f.get("tbr") or 0))


def build_vformats(data):
    """Video formats for the custom picker; each pairs with best audio.
    Video-only (DASH) streams preferred so audio is always the best track."""
    try:
        formats = data.get("formats") or []
        auds = [f for f in formats if f.get("acodec") not in (None, "none")
                and f.get("vcodec") in (None, "none")]
        opus = [f for f in auds if "opus" in (f.get("acodec") or "").lower()]
        best_aud = (max(opus, key=lambda f: (f.get("abr") or 0)) if opus
                    else max(auds, key=lambda f: (f.get("abr") or 0), default=None))
        aud_size = (best_aud.get("filesize") or best_aud.get("filesize_approx") or 0) if best_aud else 0
        vids = [f for f in formats if f.get("height") and f.get("vcodec") not in (None, "none")
                and f.get("acodec") in (None, "none")]
        if not vids:
            vids = [f for f in formats if f.get("height") and f.get("vcodec") not in (None, "none")]
        if not vids:
            return []
        vids.sort(key=lambda f: (-(f.get("height") or 0), _vrank(f)))
        out = []
        for f in vids:
            h = f.get("height")
            vc = _short_codec(f.get("vcodec"))
            fps = f.get("fps")
            vid_only = f.get("acodec") in (None, "none")
            vsize = f.get("filesize") or f.get("filesize_approx") or 0
            size = vsize + (aud_size if vid_only else 0)
            parts = []
            if fps and fps >= 50:
                parts.append(f"{int(round(fps))}fps")
            if vc:
                parts.append(vc)
            if vid_only:
                parts.append("Opus" if opus else _short_codec(best_aud.get("acodec")) if best_aud else "")
            fid = f.get("format_id")
            fmt = f"{fid}+ba/{fid}" if vid_only else fid
            out.append({"label": f"{h}p", "sub": " · ".join([p for p in parts if p]),
                        "size": ("~" + human_size(size)) if size else "", "fmt": fmt})
        return out
    except Exception:
        return []


def build_entry(info, target, vid):
    thumb = (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
             if len(vid) == 11 else (info.get("thumbnail") or ""))
    size = target.stat().st_size
    return {
        "id": vid or target.stem,
        "title": info.get("title") or target.stem,
        "channel": info.get("uploader") or info.get("channel") or "",
        "duration": fmt_dur(info["duration"]) if info.get("duration") else "",
        "format": fmt_label(info, target),
        "size": size, "size_h": human_size(size),
        "thumb": thumb, "path": str(target), "ts": time.time(),
        "released": epoch_from_info(info) or 0, "tab": "downloads",
    }


def postprocess(dl_dir, started, api, mark=True, stamp=True):
    """For each fresh .info.json: optionally stamp the file date and mark it
    watched (with live phase updates), record a history entry, delete the json.
    Returns the list of history entries built."""
    push = api._push
    entries = []
    for jf in sorted(Path(dl_dir).glob("*.info.json"), key=lambda p: p.stat().st_mtime):
        try:
            if jf.stat().st_mtime < started - 5:
                continue
            info = json.loads(jf.read_text(encoding="utf-8"))
            vid = info.get("id", "")
            cands = [p for p in Path(dl_dir).iterdir()
                     if p.suffix.lower() in VIDEO_EXTS and vid and vid in p.name]
            target = max(cands, key=lambda p: p.stat().st_mtime) if cands else None
            if target and stamp:
                api._item(key=vid, status="processing", phase="Setting date")
                epoch = epoch_from_info(info)
                if epoch:
                    set_file_times(target, epoch)
                    push(f"[post] timestamp set: {target.name}")
            url = info.get("webpage_url", "")
            if mark and "youtube" in url and api.logged_in:
                api._item(key=vid, status="processing", phase="Marking watched")
                browser_mark_watched(url, push)
            if target:
                entries.append(build_entry(info, target, vid))
            jf.unlink(missing_ok=True)
        except Exception as e:
            push(f"[post] cleanup error: {e}")
    return entries


def is_playlist(url):
    return ("list=" in url) or ("/@" in url)


def is_youtube(url):
    low = url.lower()
    return "youtube.com" in low or "youtu.be" in low


RE_YTID_URL = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/|/v/|/live/)([A-Za-z0-9_-]{11})")


def youtube_id(url):
    m = RE_YTID_URL.search(url or "")
    return m.group(1) if m else ""


def yt_args(url):
    """Player clients that keep the full format list (session cookies would drop
    it to 0 via SABR, so we stay anonymous), plus a bundled JS runtime so yt-dlp
    can solve YouTube's signature/n challenge. Without the runtime those fail,
    downloads throttle, and YouTube bot-checks the session after a few videos.
    Deno (yt-dlp's default) proved flaky here; node solved it reliably."""
    if is_youtube(url):
        a = ["--extractor-args", "youtube:player_client=default,web_safari"]
        if NODE.exists():
            a += ["--no-js-runtimes", "--js-runtimes", f"node:{NODE}"]
        return a
    return []


DOWNLOADS_TAB = "downloads"   # built-in library tab; the rest are folder-backed
# 'fixed' tabs ship with the app and cannot be removed
DEFAULT_TABS = [{"id": "imported", "name": "Imported", "folder": "", "fixed": True}]


class Api:
    def __init__(self):
        self.cfg = load_config()
        self.history = load_history()
        self.tabs = self.cfg.get("tabs") or [dict(t) for t in DEFAULT_TABS]
        for d in DEFAULT_TABS:   # keep shipped tabs present and non-removable
            t = next((x for x in self.tabs if x["id"] == d["id"]), None)
            if t:
                t["fixed"] = True
            else:
                self.tabs.insert(0, dict(d))
        self.views = self.cfg.get("views") or {}   # per-tab sort/direction/filter
        self.active_tab = DOWNLOADS_TAB
        migrated = False
        for e in self.history:     # entries predating tabs: sort them into one
            if not e.get("tab"):
                e["tab"] = "imported" if e.get("source") == "local" else DOWNLOADS_TAB
                migrated = True
        if migrated:
            save_history(self.history)
        self.proc = None
        self.busy = False
        self.logged_in = False
        self._jar_cache = {}          # require-key -> (jar_text, expiry); memory only
        self._jobs = {}               # card key -> job args, for retry
        self._cardmeta = {}           # card key -> last title/thumb/etc, for persisting failures
        self.failed = load_failed()   # card key -> failed-job record, survives restart
        self._q = queue.Queue()
        self._worker_up = False
        self._updating = False
        for k, r in self.failed.items():   # make failed downloads retryable after a restart
            self._jobs[k] = (r.get("url"), r.get("fmt"), r.get("items"),
                             r.get("mark", True), r.get("stamp", True), r.get("skip", False))
            self._cardmeta[k] = {f: r[f] for f in ("title", "channel", "thumb", "duration")
                                 if r.get(f) is not None}

    # --- UI bridge ---

    def _push(self, line):
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.log({json.dumps(str(line))})")
            except Exception:
                pass

    def _item(self, **kw):
        key = kw.get("key")
        if key:   # remember display fields so a failed card can be rebuilt after restart
            m = self._cardmeta.setdefault(key, {})
            for f in ("title", "channel", "thumb", "duration"):
                if kw.get(f) is not None:
                    m[f] = kw[f]
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.item({json.dumps(kw)})")
            except Exception:
                pass

    def _save_failed(self, key):
        """Persist a failed download so it survives a restart and stays retryable."""
        job = self._jobs.get(key)
        if not job:
            return
        url, fmt, items, mark, stamp, skip = job
        rec = {"url": url, "fmt": fmt, "items": items, "mark": mark,
               "stamp": stamp, "skip": skip}
        rec.update(self._cardmeta.get(key, {}))
        self.failed[key] = rec
        save_failed(self.failed)

    def _clear_failed(self, key):
        if key in self.failed:
            del self.failed[key]
            save_failed(self.failed)

    def _drop(self, key):
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.drop({json.dumps(key)})")
            except Exception:
                pass

    def _set_state(self, **extra):
        state = {"dir": self.download_dir(), "logged_in": self.logged_in,
                 "deps_ok": YTDLP.exists() and FFMPEG.exists(), "busy": self.busy}
        state.update(extra)
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.setState({json.dumps(state)})")
            except Exception:
                pass

    # --- session / auth ---

    def _cache_jar(self, key, jar):
        self._jar_cache[key] = (jar, time.time() + 600)

    def _session_args(self, origin, require):
        """(['--cookies', tmp], tmp) from the profile session, or ([], None).
        Caller must _shred(tmp). Jar text cached in memory for 10 min."""
        cached = self._jar_cache.get(require)
        if cached and cached[1] > time.time():
            jar = cached[0]
        else:
            jar = profile_session_jar(origin, require)
            self._jar_cache[require] = (jar, time.time() + (600 if jar else 120))
        if not jar:
            return [], None
        tmp = APP_DIR / f"session-{os.urandom(4).hex()}.tmp"
        tmp.write_text(jar, encoding="utf-8", newline="\n")
        return ["--cookies", str(tmp)], tmp

    def _auth_for(self, url):
        """Auth for a url -> (args, tmp). tmp may be None.
        YouTube stays ANONYMOUS: the account session makes YT serve SABR-only
        streams (0 downloadable formats). Non-YouTube sites use their session;
        sonyliv/hotstar use their saved credential files."""
        da = domain_auth(url)
        if da:
            return da, None
        if is_youtube(url):
            return [], None
        host, key = site_key(url)
        if host:
            return self._session_args(f"https://{host}/", key)
        return [], None

    @staticmethod
    def _shred(tmp):
        if tmp:
            try:
                tmp.write_bytes(b"\0" * 8192)
                tmp.unlink()
            except OSError:
                pass

    # --- state / folders ---

    def download_dir(self):
        d = self.cfg.get("download_dir")
        if d and Path(d).is_dir():
            return d
        return str(Path.home() / "Downloads")

    def get_state(self):
        return {"dir": self.download_dir(), "logged_in": self.logged_in,
                "deps_ok": YTDLP.exists() and FFMPEG.exists(),
                "default_format": DEFAULT_FORMAT, "busy": self.busy,
                "mark_watched": self.cfg.get("mark_watched", True),
                "set_timestamp": self.cfg.get("set_timestamp", True),
                "views": self.views}

    def pick_folder(self):
        res = UI_WIN.create_file_dialog(webview.FOLDER_DIALOG, directory=self.download_dir())
        if res:
            self.cfg["download_dir"] = res[0]
            save_config(self.cfg)
        return self.download_dir()

    def pick_files(self):
        """Keyboard/menu route to the same thing drag & drop does."""
        fd = getattr(webview, "FileDialog", None)   # OPEN_DIALOG is deprecated in 6.x
        res = UI_WIN.create_file_dialog(
            fd.OPEN if fd else webview.OPEN_DIALOG,
            allow_multiple=True, directory=self.download_dir(),
            file_types=("Video files (*.mp4;*.mkv;*.webm;*.mov;*.avi;*.m4v;*.flv)",
                        "All files (*.*)"))
        if res:
            self.import_paths(list(res))
        return "ok"

    # --- library tabs ---

    def get_tabs(self):
        return [dict(t, folder=self.tab_folder(t["id"])) for t in self.tabs]

    def tab_folder(self, tid):
        """Folder a tab points at. Blank/built-in falls back to the download dir."""
        if tid and tid != DOWNLOADS_TAB:
            t = next((x for x in self.tabs if x["id"] == tid), None)
            if t and t.get("folder"):
                return t["folder"]
        return self.download_dir()

    def set_tab(self, tid):
        """JS tells us which tab is showing, so a drop lands in the right folder."""
        self.active_tab = tid or DOWNLOADS_TAB
        return "ok"

    def add_tab(self):
        """New tab pointing at a folder the user picks; indexes what's already there."""
        fd = getattr(webview, "FileDialog", None)
        res = UI_WIN.create_file_dialog(fd.FOLDER if fd else webview.FOLDER_DIALOG)
        if not res:
            return self.get_tabs()
        folder = res[0]
        if any(Path(t.get("folder") or "") == Path(folder) for t in self.tabs):
            self._push("[tab] a tab for that folder already exists")
            return self.get_tabs()
        tid = "t-" + os.urandom(4).hex()
        self.tabs.append({"id": tid, "name": Path(folder).name or folder, "folder": folder})
        self.cfg["tabs"] = self.tabs
        save_config(self.cfg)
        self._push(f"[tab] added '{Path(folder).name}' -> {folder}")
        threading.Thread(target=self._scan_worker, args=(tid,), daemon=True).start()
        return self.get_tabs()

    def rename_tab(self, tid, name):
        t = next((x for x in self.tabs if x["id"] == tid), None)
        if not t or not (name or "").strip():
            return self.get_tabs()
        t["name"] = name.strip()[:40]
        self.cfg["tabs"] = self.tabs
        save_config(self.cfg)
        return self.get_tabs()

    def set_view(self, tid, sort, direction, filt):
        """Remember how each tab is sorted/filtered -- it's per library, not global."""
        self.views[tid or DOWNLOADS_TAB] = {"sort": sort, "dir": direction, "filter": filt}
        self.cfg["views"] = self.views
        save_config(self.cfg)
        return "ok"

    def check_files(self, tid=None):
        """Which catalogued files are still on disk. Lets the UI flag anything
        deleted outside the app without needing a restart."""
        out = {}
        for e in self.history:
            if tid and (e.get("tab") or DOWNLOADS_TAB) != tid:
                continue
            p = e.get("path") or ""
            out[e["id"]] = bool(p) and Path(p).exists()
        return out

    def remove_tab(self, tid):
        """Drop the tab and its catalogue entries. Files on disk are untouched."""
        t = next((x for x in self.tabs if x["id"] == tid), None)
        if not t or t.get("fixed"):
            self._push("[tab] that tab is built in and can't be removed")
            return self.get_tabs()
        self.tabs = [t for t in self.tabs if t["id"] != tid]
        self.cfg["tabs"] = self.tabs
        save_config(self.cfg)
        gone = [e["id"] for e in self.history if e.get("tab") == tid]
        self.history = [e for e in self.history if e.get("tab") != tid]
        save_history(self.history)
        for k in gone:
            self._drop(k)
        self.active_tab = DOWNLOADS_TAB
        self._push(f"[tab] removed tab ({len(gone)} entries de-listed; files kept)")
        return self.get_tabs()

    def scan_tab(self, tid):
        """Index videos sitting in the tab's folder that aren't catalogued yet."""
        threading.Thread(target=self._scan_worker, args=(tid,), daemon=True).start()
        return "started"

    def _scan_worker(self, tid):
        folder = Path(self.tab_folder(tid))
        known = {(e.get("path") or "").lower() for e in self.history}
        try:
            files = [p for p in sorted(folder.iterdir())
                     if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                     and str(p).lower() not in known]
        except OSError as e:
            self._push(f"[!] can't read {folder}: {e}")
            return
        if not files:
            self._push(f"[tab] nothing new in {folder}")
            return
        self._push(f"[tab] indexing {len(files)} video(s) in {folder}")
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        for f in files:
            key = "local-" + os.urandom(5).hex()
            try:
                self._item(key=key, status="processing", phase="Indexing",
                           title=f.stem, tab=tid)
                self._index_file(f, key, tid)
            except Exception as e:
                self._push(f"[!] index failed for {f.name}: {e}")
                self._item(key=key, status="failed", title=f.stem, tab=tid)
        self._resort()

    # --- local import ---

    def import_paths(self, paths, tid=None):
        """Add local videos: move each into the active tab's folder (unless it
        already lives there) and catalogue it under that tab."""
        tid = tid or self.active_tab
        if tid == DOWNLOADS_TAB:      # dropping on Downloads files it under Imported
            tid = self.tabs[0]["id"] if self.tabs else "imported"
        vids, skipped = [], 0
        for p in paths or []:
            q = Path(p)
            if q.is_file() and q.suffix.lower() in VIDEO_EXTS:
                vids.append(q)
            else:
                skipped += 1
        if skipped:
            self._push(f"[import] ignored {skipped} non-video item(s)")
        if not vids:
            return "none"
        threading.Thread(target=self._import_worker, args=(vids, tid), daemon=True).start()
        return "started"

    def _import_worker(self, vids, tid):
        dest = Path(self.tab_folder(tid))
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        ok = 0
        for src in vids:
            key = "local-" + os.urandom(5).hex()
            try:
                self._item(key=key, status="processing", phase="Importing",
                           title=src.stem, tab=tid)
                ok += 1 if self._import_one(src, dest, key, tid) else 0
            except Exception as e:
                self._push(f"[!] import failed for {src.name}: {e}")
                self._item(key=key, status="failed", title=src.stem, tab=tid)
        self._push(f"[+] imported {ok} of {len(vids)} file(s)")
        self._resort()

    def _resort(self):
        if UI_WIN:
            try:
                UI_WIN.evaluate_js("sortGrid(curSort);refreshView();")
            except Exception:
                pass

    def _import_one(self, src, dest, key, tid):
        already = src.parent.resolve() == dest.resolve()
        target = src if already else unique_path(dest / src.name)
        if not already:
            self._item(key=key, status="processing", phase="Moving")
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            self._push(f"[import] moved {src.name} -> {dest}")
        return self._index_file(target, key, tid)

    def _index_file(self, target, key, tid):
        self._item(key=key, status="processing", phase="Reading video")
        meta = ffprobe_meta(target)
        tf = THUMB_DIR / f"{key}.jpg"
        seek = min(3, int(meta.get("duration") or 0) // 10) if meta.get("duration") else 0
        if not (make_thumb(target, tf, seek) or make_thumb(target, tf, 0)):
            tf = None
        st = target.stat()
        h = meta.get("height")
        self._add_history({
            "id": key, "title": target.stem, "channel": "", "source": "local",
            "tab": tid,
            "duration": fmt_dur(meta["duration"]) if meta.get("duration") else "",
            "format": (f"{h}p " if h else "") + target.suffix.lstrip(".").lower(),
            "size": st.st_size, "size_h": human_size(st.st_size),
            "thumb": "", "thumb_file": str(tf) if tf else "",
            "path": str(target), "ts": time.time(), "released": st.st_mtime,
        })
        return True

    # --- history ---

    def get_history(self):
        return [{**e, "thumb": entry_thumb(e), "exists": Path(e.get("path", "")).exists()}
                for e in self.history]

    def get_pending(self):
        """Failed/unfinished downloads from a previous run, so they can be retried."""
        return [{"key": k, "title": r.get("title") or r.get("url"),
                 "channel": r.get("channel"), "thumb": r.get("thumb"),
                 "duration": r.get("duration")} for k, r in self.failed.items()]

    def _add_history(self, e):
        self.history = [x for x in self.history if x.get("id") != e["id"]]
        self.history.insert(0, e)
        self.history = self.history[:1000]
        save_history(self.history)
        self._item(key=e["id"], status="done", title=e["title"], channel=e["channel"],
                   duration=e["duration"], size=e["size_h"], format=e["format"],
                   thumb=entry_thumb(e), path=e["path"], ts=e.get("ts"),
                   released=e.get("released"), source=e.get("source"),
                   tab=e.get("tab") or DOWNLOADS_TAB)

    def play(self, key):
        for e in self.history:
            if e.get("id") == key:
                p = e.get("path", "")
                if p and Path(p).exists():
                    try:
                        os.startfile(p)  # opens in default player
                        return "ok"
                    except OSError as ex:
                        self._push(f"[!] could not open file: {ex}")
                        return "error"
                self._push("[!] file no longer exists at its saved location")
                return "missing"
        return "unknown"

    def reveal(self, key):
        for e in self.history:
            if e.get("id") == key:
                p = Path(e.get("path", ""))
                if p.exists():
                    subprocess.Popen(["explorer", "/select,", str(p)])
                    return "ok"
                if p.parent.exists():
                    subprocess.Popen(["explorer", str(p.parent)])
                    return "ok"
        return "missing"

    def remove(self, key):
        """Delete the download: send its file to the Recycle Bin, then drop it
        from history. The Recycle Bin makes an accidental click recoverable."""
        entry = next((e for e in self.history if e.get("id") == key), None)
        result = "nofile"
        if entry:
            p = entry.get("path", "")
            if p and Path(p).exists():
                if recycle(p):
                    self._push(f"[+] deleted (Recycle Bin): {Path(p).name}")
                    result = "deleted"
                else:
                    self._push(f"[!] could not delete file: {Path(p).name}")
                    result = "error"
        if entry and entry.get("thumb_file"):
            Path(entry["thumb_file"]).unlink(missing_ok=True)
        self.history = [e for e in self.history if e.get("id") != key]
        save_history(self.history)
        self._clear_failed(key)   # also dismiss it from the retry-after-restart list
        return result

    # --- info / formats ---

    def _oembed(self, url):
        try:
            u = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
            with http_get(u, timeout=8) as resp:
                d = json.loads(resp.read().decode())
            return {"ok": True, "kind": "video", "id": None,
                    "title": d.get("title") or "Unknown title",
                    "uploader": d.get("author_name") or "", "duration": "",
                    "thumb": d.get("thumbnail_url") or ""}
        except Exception:
            return None

    def fetch_info(self, url):
        """Pre-download info fetch for the config sheet. Always returns a dict."""
        url = (url or "").strip().strip('"')
        if not url:
            return {"ok": False, "error": "no-url"}
        base = [YTDLP, "--no-warnings", "-J", "--socket-timeout", "10",
                "--retries", "2", "--extractor-retries", "1", *yt_args(url)]
        base += ["--flat-playlist"] if is_playlist(url) else ["--no-playlist"]
        args, tmp = self._auth_for(url)

        def attempt():
            r = subprocess.run([str(c) for c in base + args + [url]],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=NO_WINDOW, timeout=45)
            d = json.loads(r.stdout) if (r.stdout or "").strip() else None
            if not isinstance(d, dict):
                tail = (r.stderr or "").strip().splitlines()
                raise ValueError(tail[-1] if tail else "no data from yt-dlp")
            return d

        try:
            return self.fetch_info_from(attempt())
        except Exception as e:
            err = str(e)
            self._push(f"[!] info fetch: {err[:160]}")
            if ("Sign in to confirm" in err or "not a bot" in err) and not is_playlist(url):
                info = self._oembed(url)
                if info:
                    return info
            return {"ok": False, "error": err[:200]}
        finally:
            self._shred(tmp)

    def fetch_info_from(self, data):
        if data.get("_type") == "playlist" or "entries" in data:
            entries = data.get("entries") or []
            thumb = ""
            for e in entries:
                if len(e.get("id") or "") == 11:
                    thumb = f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"
                    break
            return {"ok": True, "kind": "playlist", "id": None,
                    "title": data.get("title") or "Playlist",
                    "uploader": data.get("uploader") or data.get("channel") or "",
                    "count": len(entries), "thumb": thumb}
        dur = data.get("duration")
        return {"ok": True, "kind": "video", "id": data.get("id"),
                "title": data.get("title") or "Unknown title",
                "uploader": data.get("uploader") or data.get("channel") or "",
                "duration": fmt_dur(dur) if dur else "",
                "thumb": data.get("thumbnail") or "",
                "vformats": build_vformats(data)}

    def list_formats(self, url):
        url = (url or "").strip()
        if not url:
            return "Paste a URL first."
        cmd = [YTDLP, "--no-warnings", "-F", "--socket-timeout", "10",
               "--retries", "2", *yt_args(url)]
        if is_playlist(url):
            cmd += ["--playlist-items", "1"]
        args, tmp = self._auth_for(url)
        cmd += args
        cmd.append(url)
        try:
            _, out = run_quiet(cmd, timeout=180)
            return out or "No output."
        except Exception as e:
            return f"Failed: {e}"
        finally:
            self._shred(tmp)

    # --- login ---

    def login(self, url=""):
        host, _key = site_key((url or "").strip())
        is_yt = not host or "youtube" in host or "youtu.be" in host
        target = "https://www.youtube.com" if is_yt else f"https://{host}/"
        name = "YouTube" if is_yt else host
        w = webview.create_window(f"Log into {name}, then close this window",
                                  target, width=1100, height=800)
        if is_yt:
            w.events.closed += lambda *a: threading.Thread(
                target=self._recheck_login, daemon=True).start()
        else:
            def done(*a):
                self._jar_cache.pop(_key, None)  # force re-read next use
                self._push(f"[login] {host} session saved in profile")
            w.events.closed += done
        return "opened"

    def _recheck_login(self):
        logged, jar = probe_youtube()
        self.logged_in = logged
        if jar:
            self._cache_jar("youtube", jar)
        self._push("[login] signed in" if logged
                   else "[login] not signed in - click Login and complete sign-in")
        self._set_state()

    def update_deps(self):
        # manual button: force an ffmpeg refresh too
        threading.Thread(
            target=lambda: (ensure_deps(self._push, force_ffmpeg=True), self._set_state()),
            daemon=True).start()
        return "updating"

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           creationflags=NO_WINDOW, capture_output=True)
            self._push("[!] download cancelled")
        return "ok"

    def retry(self, key):
        job = self._jobs.get(key)
        if not job:
            return "unknown"
        self._item(key=key, status="queued")
        self._q.put((*job, key, False))
        return "queued"

    def open_releases(self):
        webbrowser.open(RELEASES_URL)
        return "ok"

    def run_update(self):
        """One-click update: download the right asset (installer or portable) and
        apply it in place, then relaunch. Falls back to opening the releases page."""
        if self._updating:
            return "busy"
        self._updating = True
        threading.Thread(target=self._do_update, daemon=True).start()
        return "started"

    def _upd_ui(self, text):
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.updating({json.dumps(text)})")
            except Exception:
                pass

    def _upd_push(self, line):
        self._push(line)
        m = re.search(r"(\d+)%", line)
        if m:
            self._upd_ui(f"Downloading {m.group(1)}%")

    def _do_update(self):
        try:
            tag, assets = latest_release()
            if not tag or not assets:
                self._push("[update] couldn't reach GitHub - opening the releases page")
                webbrowser.open(RELEASES_URL)
                return
            installed = is_installed_build()
            want = None
            for name in assets:
                low = name.lower()
                if installed and low.endswith(".exe") and "setup" in low:
                    want = name
                    break
                if not installed and low == "ytgrab.exe":
                    want = name
                    break
            if not want:
                self._push("[update] no matching asset - opening the releases page")
                webbrowser.open(RELEASES_URL)
                return
            dest = Path(tempfile.gettempdir()) / want
            self._push(f"[update] downloading {want} ({tag})...")
            self._upd_ui("Downloading update...")
            download_file(assets[want], dest, self._upd_push, "update")
            self._upd_ui("Installing...")
            if installed:
                self._push("[update] installing - the app closes and reopens in a moment")
                subprocess.Popen([str(dest), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                                  "/FORCECLOSEAPPLICATIONS", "/NORESTART", "/NOCANCEL"],
                                 creationflags=NO_WINDOW)
            else:
                self._push("[update] replacing the portable exe and restarting...")
                self._swap_portable(dest)
            time.sleep(1.5)
            self._quit()
        except Exception as e:
            self._push(f"[update] failed: {e} - opening the releases page")
            try:
                webbrowser.open(RELEASES_URL)
            except Exception:
                pass
        finally:
            self._updating = False

    def _swap_portable(self, newexe):
        """Can't overwrite a running exe -> a helper waits for exit, swaps, relaunches."""
        cur = Path(sys.executable)
        bat = Path(tempfile.gettempdir()) / "ytgrab_update.bat"
        bat.write_text(
            "@echo off\r\n"
            "ping -n 3 127.0.0.1 >nul\r\n"
            f'move /y "{newexe}" "{cur}" >nul\r\n'
            f'start "" "{cur}"\r\n'
            'del "%~f0"\r\n',
            encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=NO_WINDOW)

    def _quit(self):
        try:
            if UI_WIN:
                UI_WIN.destroy()
        except Exception:
            pass
        os._exit(0)

    # --- queue ---

    def start_download(self, url, fmt_mode, custom_fmt, pl_start, pl_end,
                       mark_watched=True, set_timestamp=True,
                       skip_download=False, meta=None):
        url = (url or "").strip().strip('"')
        if not url:
            return "no-url"
        if not (YTDLP.exists() and FFMPEG.exists()):
            return "no-deps"
        fmt = custom_fmt.strip() if (fmt_mode == "custom" and custom_fmt.strip()) else DEFAULT_FORMAT
        self.cfg["mark_watched"] = bool(mark_watched)
        self.cfg["set_timestamp"] = bool(set_timestamp)
        save_config(self.cfg)
        if not self._worker_up:
            self._worker_up = True
            threading.Thread(target=self._queue_loop, daemon=True).start()
        if is_playlist(url):
            # expand into per-video jobs so each video gets its own
            # postprocess (timestamp, mark-watched, json cleanup, history)
            # the moment it finishes -- like the original downloader.cmd
            threading.Thread(target=self._enqueue_playlist,
                             args=(url, fmt, pl_start, pl_end,
                                   bool(mark_watched), bool(set_timestamp),
                                   bool(skip_download)),
                             daemon=True).start()
            return "queued"

        meta = meta or {}
        vid = meta.get("id") or youtube_id(url)
        if vid:
            key, placeholder = vid, False
            self._item(key=key, status="queued", title=meta.get("title"),
                       channel=meta.get("uploader"), duration=meta.get("duration"),
                       thumb=meta.get("thumb"))
        else:
            key, placeholder = f"job-{os.urandom(3).hex()}", True
            self._item(key=key, status="queued",
                       title=meta.get("title") or url, thumb=meta.get("thumb"))

        if not meta.get("title"):
            threading.Thread(target=self._prefetch, args=([(key, url)],), daemon=True).start()
        self._jobs[key] = (url, fmt, None, bool(mark_watched), bool(set_timestamp),
                           bool(skip_download))
        self._q.put((*self._jobs[key], key, placeholder))
        return "queued"

    def _enqueue_playlist(self, url, fmt, pl_start, pl_end, mark, stamp, skip=False):
        """Flat-extract a playlist/channel and queue one job per video.
        A given range is passed to yt-dlp itself (-I) so huge channels only
        extract the requested slice instead of paging through everything."""
        def num(v, d):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d
        start = max(num(pl_start, 1), 1)
        end = num(pl_end, 0)
        rng = f"{start}:{end if end else ''}" if (pl_start or pl_end) else ""
        self._push("[*] fetching playlist entries..." +
                   (f" (items {rng})" if rng else " (whole list - may take a while)"))
        base = [YTDLP, "--no-warnings", "-J", "--flat-playlist",
                "--socket-timeout", "10", "--retries", "2", *yt_args(url)]
        if rng:
            base += ["-I", rng]
        args, tmp = self._auth_for(url)
        data = None
        try:
            r = subprocess.run([str(c) for c in base + args + [url]],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=NO_WINDOW,
                               timeout=120 if rng else 900)
            data = json.loads(r.stdout) if (r.stdout or "").strip() else None
        except Exception as e:
            self._push(f"[!] playlist fetch failed: {e}")
        finally:
            self._shred(tmp)
        entries = (data or {}).get("entries") or []
        if not entries:
            self._push("[!] no videos found in this playlist/channel")
            return
        self._push(f"[*] queueing {len(entries)} videos")
        pf = []
        for e in entries:
            vid = e.get("id") or ""
            vurl = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
            if not vurl:
                continue
            key = vid if vid else f"job-{os.urandom(3).hex()}"
            kw = {"key": key, "status": "queued", "title": e.get("title") or vurl}
            if len(vid) == 11:
                kw["thumb"] = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
            if e.get("duration"):
                kw["duration"] = fmt_dur(e["duration"])
            self._item(**kw)
            if not e.get("title"):
                pf.append((key, vurl))
            self._jobs[key] = (vurl, fmt, None, mark, stamp, skip)
            self._q.put((*self._jobs[key], key, False))
        if pf:
            threading.Thread(target=self._prefetch, args=(pf,), daemon=True).start()

    def _prefetch(self, pairs):
        """Fill title + thumbnail for queued cards (fast oEmbed) so you can tell
        which queued item is which before it starts downloading."""
        for key, url in pairs:
            try:
                info = self._oembed(url)
            except Exception:
                info = None
            if info and info.get("title"):
                kw = {"key": key, "title": info["title"]}
                if info.get("thumb"):
                    kw["thumb"] = info["thumb"]
                self._item(**kw)
            time.sleep(0.15)

    def _queue_loop(self):
        while True:
            job = self._q.get()
            try:
                self._download_worker(*job)
            except Exception as e:
                self._push(f"[!] worker error: {e}")
            finally:
                self._q.task_done()

    def _make_parser(self):
        """Turn raw yt-dlp lines into per-video queue card updates with phases."""
        state = {"cur": None, "items": {}}

        def finish_prev(new_key):
            # a new video has started: the previous one is finished (playlists)
            prev = state["cur"]
            if prev and prev != new_key:
                pit = state["items"].get(prev)
                if pit and pit.get("status") not in ("failed", "done"):
                    pit["status"] = "done"
                    self._item(key=prev, status="done", pct=100)

        def parse(line):
            m = RE_YT_ID.match(line)
            if m:
                vid = m.group(1)
                finish_prev(vid)
                state["cur"] = vid
                it = state["items"].setdefault(vid, {"pct": -1, "dests": 0})
                it["status"] = "fetching"
                self._item(key=vid, status="fetching", phase="Fetching info",
                           thumb=f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
                return
            m = RE_DEST.match(line)
            if m:
                name = os.path.basename(m.group(1))
                idm = RE_FILE_ID.search(name)
                key = idm.group(1) if idm else re.sub(r"\.f\d+\.\w+$|\.\w+$", "", name)
                finish_prev(key)
                state["cur"] = key
                it = state["items"].setdefault(key, {"pct": -1, "dests": 0})
                it["dests"] += 1
                it["phase"] = "Downloading video" if it["dests"] == 1 else "Downloading audio"
                it["status"] = "downloading"
                title = re.sub(r"\.f\d+\.\w+$|\.\w+$", "", name)
                title = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", title).strip()
                kw = {"key": key, "status": "downloading", "phase": it["phase"], "title": title}
                if idm:
                    kw["thumb"] = f"https://i.ytimg.com/vi/{idm.group(1)}/mqdefault.jpg"
                self._item(**kw)
                return
            it = state["items"].get(state["cur"])
            if it is None:
                return
            key = state["cur"]
            m = RE_PROG.match(line)
            if m:
                pct = float(m.group(1))
                if int(pct) != it["pct"]:
                    it["pct"] = int(pct)
                    self._item(key=key, status="downloading", phase=it.get("phase"),
                               pct=pct, speed=m.group(2) or "", eta=m.group(3) or "")
                return
            phase = None
            if line.startswith("[Merger]"):
                phase = "Merging"
            elif line.startswith("[Metadata]"):
                phase = "Embedding metadata"
            elif line.startswith(("[EmbedThumbnail]", "[ThumbnailsConvertor]")):
                phase = "Embedding thumbnail"
            elif line.startswith("[ExtractAudio]"):
                phase = "Extracting audio"
            elif line.startswith("[Fixup"):
                phase = "Finalizing"
            if phase:
                it["status"] = "processing"
                it["phase"] = phase
                self._item(key=key, status="processing", phase=phase, pct=100)
                return
            if line.startswith("ERROR"):
                it["status"] = "failed"
                self._item(key=key, status="failed")

        return parse, state

    def _run_ytdlp(self, cmd, dl_dir, on_line=None):
        code, botcheck = 1, False
        try:
            self.proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace",
                                         creationflags=NO_WINDOW, cwd=dl_dir)
            for line in self.proc.stdout:
                line = line.rstrip()
                if line:
                    if "Sign in to confirm" in line or "not a bot" in line:
                        botcheck = True
                    self._push(line)
                    if on_line:
                        try:
                            on_line(line)
                        except Exception:
                            pass
            self.proc.wait()
            code = self.proc.returncode
        except Exception as e:
            self._push(f"[!] error: {e}")
        finally:
            self.proc = None
        return code, botcheck

    def _download_worker(self, url, fmt, items, mark, stamp, skip, job_key, placeholder):
        if placeholder:
            self._drop(job_key)
        self.busy = True
        self._set_state()
        if skip:
            # skip-download: only mark the video watched, nothing touches disk
            self._item(key=job_key, status="processing", phase="Marking watched")
            ok = False
            if not mark:
                self._push("[!] skip-download with mark-watched off: nothing to do")
            elif is_youtube(url) and self.logged_in:
                ok = browser_mark_watched(url, self._push)
            else:
                self._push("[!] mark-watched needs a YouTube link and a signed-in profile")
            self._item(key=job_key, status="done" if ok else "failed")
            self._clear_failed(job_key) if ok else self._save_failed(job_key)
            self.busy = False
            self._set_state()
            if UI_WIN:
                try:
                    UI_WIN.evaluate_js(f"ui.done({json.dumps(ok)})")
                except Exception:
                    pass
            return
        started = time.time()
        dl_dir = self.download_dir()
        cmd = [YTDLP, "-f", fmt, *BASE_OPTS, "--ffmpeg-location", str(BIN_DIR),
               *yt_args(url)]
        if items:
            cmd += ["--playlist-items", items]
        if is_playlist(url):
            cmd += ["--ignore-errors"]
        auth, tmp = self._auth_for(url)  # anonymous for YouTube; session for other sites
        cmd += auth
        cmd.append(url)
        self._push(f"[*] downloading: {url}")
        parse, state = self._make_parser()
        code, botcheck = self._run_ytdlp(cmd, dl_dir, parse)
        self._shred(tmp)

        if code != 0 and botcheck:
            if not NODE.exists():
                self._push("[!] YouTube bot-check: the JS-challenge runtime (node) isn't "
                           "installed yet. Let the deps download finish (see log above), "
                           "then hit Retry.")
            else:
                self._push("[!] YouTube bot-check. The JS challenge failed this time - "
                           "hit Retry; if it persists, wait a minute (YouTube rate-limits "
                           "bursts) and retry.")

        for k in state["items"]:
            self._jobs.setdefault(k, (url, fmt, items, mark, stamp, False))
        entries = postprocess(dl_dir, started, self, mark, stamp)
        done_ids = set()
        for e in entries:
            self._add_history(e)
            self._clear_failed(e["id"])
            done_ids.add(e["id"])
        for key, it in state["items"].items():
            if key not in done_ids and it.get("status") != "done":
                self._item(key=key, status="failed")
                self._save_failed(key)
        if not placeholder and job_key not in done_ids and not entries:
            self._item(key=job_key, status="failed")
            self._save_failed(job_key)

        self._push("[+] done" if code == 0 else f"[!] finished with errors (exit {code})")
        self.busy = False
        self._set_state()
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.done({json.dumps(code == 0)})")
            except Exception:
                pass


HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
:root{
  color-scheme:dark;
  --bg:#08080C; --s1:#101017; --s2:#15151E; --s3:#1B1B26; --s4:#24242F;
  --line:rgba(255,255,255,.07); --line2:rgba(255,255,255,.12);
  --tx:#F5F5F8; --mut:#9E9EAE; --dim:#6C6C7D;
  --ac:#E0397F; --ac2:#8B5CF6; --ac-tx:#FFFFFF; --ac-soft:rgba(224,57,127,.14);
  --info:#4DA6FF; --ok:#3DDC97; --warn:#F7B955; --danger:#FF6B6B;
  --r-sm:9px; --r-md:12px; --r-lg:16px; --r-xl:22px;
  --ease:cubic-bezier(0.16,1,0.3,1);
  --side:212px;
}
*{box-sizing:border-box;}
html{height:100%;}
body{margin:0;height:100%;overflow:hidden;background:var(--bg);color:var(--tx);
  font:14px/1.5 "Segoe UI Variable Text",Inter,"Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;user-select:none;}
/* cinematic ambient wash */
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(48% 38% at 76% -6%,rgba(224,57,127,.13),transparent 68%),
             radial-gradient(42% 34% at 8% 4%,rgba(139,92,246,.11),transparent 66%);}
svg{flex:none;}
.sp{flex:1;}
:focus-visible{outline:2px solid var(--ac2);outline-offset:2px;border-radius:5px;}
.app{position:relative;z-index:1;height:100%;display:flex;}

/* ================= sidebar ================= */
.side{width:var(--side);flex:none;display:flex;flex-direction:column;gap:3px;
  padding:15px 11px 11px;border-right:1px solid var(--line);background:rgba(10,10,15,.55);}
.brandrow{display:flex;align-items:center;gap:9px;padding:2px 6px 15px;}
.mark{width:29px;height:29px;border-radius:9px;flex:none;display:flex;align-items:center;
  justify-content:center;color:#fff;background:linear-gradient(140deg,var(--ac),var(--ac2));
  box-shadow:0 5px 16px rgba(224,57,127,.35);}
.brandrow h1{margin:0;font-size:15.5px;font-weight:640;letter-spacing:-.2px;}
.brandrow .ver{font-size:9.5px;font-weight:650;color:var(--dim);letter-spacing:.4px;}
.navlbl{font-size:9.5px;font-weight:700;letter-spacing:1.1px;color:var(--dim);
  padding:0 8px 7px;}
.nav{display:flex;flex-direction:column;gap:2px;overflow-y:auto;min-height:0;}
.lt{position:relative;display:flex;align-items:center;gap:9px;height:35px;padding:0 10px;
  border:none;border-radius:var(--r-sm);background:transparent;color:var(--mut);
  cursor:pointer;font:550 13px/1 inherit;text-align:left;width:100%;
  transition:background .18s var(--ease),color .18s var(--ease);}
.lt:hover{background:var(--s2);color:var(--tx);}
.lt.on{background:var(--s3);color:var(--tx);}
.lt.on::before{content:"";position:absolute;left:-11px;top:8px;bottom:8px;width:3px;
  border-radius:0 3px 3px 0;background:linear-gradient(var(--ac),var(--ac2));}
.lt svg{color:var(--dim);}
.lt.on svg{color:var(--ac);}
.ltname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.lcnt{font-size:10px;font-weight:650;color:var(--dim);background:var(--s4);
  padding:2px 6px;border-radius:99px;font-variant-numeric:tabular-nums;}
.lt.on .lcnt{background:var(--ac-soft);color:#FF9CC4;}
.addlib{display:flex;align-items:center;gap:9px;height:33px;padding:0 10px;margin-top:4px;
  border:1px dashed var(--line2);border-radius:var(--r-sm);background:transparent;
  color:var(--dim);cursor:pointer;font:550 12.5px/1 inherit;width:100%;
  transition:color .18s var(--ease),border-color .18s var(--ease);}
.addlib:hover{color:var(--ac);border-color:var(--ac);}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--dim);}
.chip .dot{width:6px;height:6px;border-radius:50%;background:var(--dim);flex:none;}
.chip.ok .dot{background:var(--ok);} .chip.warn .dot{background:var(--warn);}

/* ================= main ================= */
.main{flex:1;min-width:0;display:flex;flex-direction:column;}
.topbar{display:flex;align-items:center;gap:9px;padding:14px 22px 12px;flex:none;}
.urlwrap{flex:1;min-width:0;position:relative;display:flex;align-items:center;}
.urlwrap .uic{position:absolute;left:15px;color:var(--dim);pointer-events:none;
  transition:color .18s var(--ease);}
.urlwrap:focus-within .uic{color:var(--ac);}
#url{width:100%;height:46px;border:1px solid var(--line2);border-radius:99px;background:var(--s1);
  color:var(--tx);padding:0 18px 0 43px;font:14.5px/1 inherit;
  transition:border-color .18s var(--ease),background .18s var(--ease);}
#url:focus{outline:none;border-color:var(--ac);background:var(--s2);}
#url::placeholder{color:var(--dim);}
.btn{height:46px;border:none;border-radius:99px;cursor:pointer;display:inline-flex;
  align-items:center;justify-content:center;gap:8px;font:650 13.5px/1 inherit;
  transition:transform .18s var(--ease),filter .18s var(--ease),opacity .18s var(--ease);}
.btn:disabled{opacity:.4;cursor:default;}
.btn.dl{padding:0 22px;color:var(--ac-tx);background:linear-gradient(135deg,var(--ac),var(--ac2));
  box-shadow:0 6px 20px rgba(224,57,127,.3);}
.btn.dl:hover:not(:disabled){filter:brightness(1.1);}
.btn.dl:active:not(:disabled){transform:scale(.97);}
.ib{position:relative;width:38px;height:38px;border-radius:50%;border:1px solid var(--line2);
  background:transparent;color:var(--mut);display:inline-flex;align-items:center;
  justify-content:center;cursor:pointer;transition:background .18s var(--ease),color .18s var(--ease);}
.ib:hover{background:var(--s2);color:var(--tx);}
/* status rides on the control it belongs to, not in a far-off corner */
.idot{position:absolute;right:1px;bottom:1px;width:9px;height:9px;border-radius:50%;
  background:var(--warn);border:2px solid var(--bg);}
.idot.ok{background:var(--ok);}
.ibadge{position:absolute;right:0;top:0;min-width:16px;height:16px;padding:0 4px;display:none;
  align-items:center;justify-content:center;border-radius:99px;background:var(--danger);
  color:#2A0505;border:2px solid var(--bg);font:700 9.5px/1 inherit;font-variant-numeric:tabular-nums;}
.ibadge.on{display:flex;}
.warnpill{display:none;align-items:center;gap:7px;height:32px;padding:0 13px;border-radius:99px;
  background:rgba(247,185,85,.13);border:1px solid rgba(247,185,85,.4);color:var(--warn);
  font:600 11.5px/1 inherit;white-space:nowrap;}
.warnpill.on{display:inline-flex;}
.warnpill .spin{width:11px;height:11px;border:2px solid rgba(247,185,85,.3);
  border-top-color:var(--warn);border-radius:50%;animation:spin .8s linear infinite;flex:none;}
.upd{display:none;align-items:center;gap:6px;height:32px;padding:0 13px;border:none;
  border-radius:99px;background:var(--ac-soft);color:#FF9CC4;cursor:pointer;
  font:650 11.5px/1 inherit;border:1px solid rgba(224,57,127,.4);
  transition:background .18s var(--ease);}
.upd:hover{background:rgba(224,57,127,.24);}

/* library header */
.libhead{display:flex;align-items:flex-end;gap:12px;padding:4px 22px 14px;flex:none;flex-wrap:wrap;}
.libttl{display:flex;align-items:center;gap:9px;}
.libttl h2{margin:0;font-size:23px;font-weight:680;letter-spacing:-.6px;}
.hicon{width:30px;height:30px;border-radius:9px;background:var(--s3);color:var(--ac);
  display:flex;align-items:center;justify-content:center;flex:none;}
#libsub{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--dim);margin-top:4px;}
.fpath{display:inline-flex;align-items:center;gap:6px;max-width:340px;min-width:0;
  padding:3px 8px;border-radius:7px;background:var(--s1);border:1px solid var(--line);
  color:var(--mut);font:11.5px/1 inherit;cursor:default;}
.fpath span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.fpath svg{color:var(--dim);}
.subcount{color:var(--dim);}
.subdot{color:var(--dim);opacity:.5;}
.hact{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;
  border:none;border-radius:7px;background:transparent;color:var(--dim);cursor:pointer;
  transition:background .18s var(--ease),color .18s var(--ease);}
.hact:hover{background:var(--s3);color:var(--tx);}
.hact.danger:hover{color:var(--danger);}
.tools{display:flex;align-items:center;gap:7px;}
.chips{display:flex;gap:3px;background:var(--s1);border:1px solid var(--line);
  border-radius:var(--r-sm);padding:3px;}
.chipb{display:inline-flex;align-items:center;gap:6px;height:27px;padding:0 11px;border:none;
  border-radius:7px;background:transparent;color:var(--mut);cursor:pointer;font:600 11.5px/1 inherit;
  transition:background .18s var(--ease),color .18s var(--ease);}
.chipb:hover{color:var(--tx);}
.chipb.on{background:var(--s4);color:var(--tx);}
.cnt{font-size:10px;font-weight:700;color:var(--dim);font-variant-numeric:tabular-nums;}
.chipb.on .cnt{color:#FF9CC4;}
.searchwrap{position:relative;display:flex;align-items:center;}
.searchwrap svg{position:absolute;left:11px;color:var(--dim);pointer-events:none;}
#q{width:168px;height:33px;border:1px solid var(--line2);border-radius:99px;background:var(--s1);
  color:var(--tx);padding:0 12px 0 32px;font:12.5px/1 inherit;transition:border-color .18s var(--ease);}
#q:focus{outline:none;border-color:var(--ac);}
#q::placeholder{color:var(--dim);}
#sortsel{height:33px;border:1px solid var(--line2);border-radius:99px;background:var(--s1);
  color:var(--mut);font:600 11.5px/1 inherit;padding:0 29px 0 13px;cursor:pointer;
  appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%239E9EAE' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>");
  background-repeat:no-repeat;background-position:right 10px center;}
#sortsel:hover{color:var(--tx);}
#sortdir{width:33px;height:33px;border:1px solid var(--line2);border-radius:50%;background:var(--s1);
  color:var(--mut);cursor:pointer;display:inline-flex;align-items:center;justify-content:center;
  transition:color .18s var(--ease),background .18s var(--ease);}
#sortdir:hover{color:var(--tx);background:var(--s2);}

/* ================= grid ================= */
#grid{flex:1;min-height:0;overflow-y:auto;display:grid;
  grid-template-columns:repeat(auto-fill,minmax(214px,1fr));grid-auto-rows:max-content;
  gap:17px;align-content:start;padding:2px 22px 20px;}
.empty{grid-column:1/-1;min-height:280px;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:7px;color:var(--dim);text-align:center;padding:30px;}
.emptyic{width:62px;height:62px;border-radius:19px;background:var(--s1);border:1px solid var(--line);
  display:flex;align-items:center;justify-content:center;color:var(--dim);margin-bottom:7px;}
.empty b{color:var(--tx);font-weight:620;font-size:15px;}
.empty span{font-size:12.5px;max-width:330px;line-height:1.55;}
.gc{position:relative;border-radius:var(--r-lg);overflow:hidden;background:var(--s1);
  border:1px solid var(--line);
  transition:transform .22s var(--ease),border-color .22s var(--ease),box-shadow .22s var(--ease);}
.gc.hide{display:none;}
.gc:hover{border-color:var(--line2);transform:translateY(-3px);
  box-shadow:0 14px 34px rgba(0,0,0,.55);}
.gc.playable{cursor:pointer;}
.gc.failed{border-color:rgba(255,107,107,.34);}
.gc.missing .gth{filter:grayscale(1) brightness(.5);}
.gth{position:relative;aspect-ratio:16/9;background:var(--s3);display:flex;align-items:center;
  justify-content:center;overflow:hidden;}
.gimg{width:100%;height:100%;object-fit:cover;transition:transform .35s var(--ease);}
.gc:hover .gimg{transform:scale(1.05);}
.gph{position:absolute;color:#33333F;}
.gth::after{content:"";position:absolute;inset:auto 0 0 0;height:52%;pointer-events:none;
  background:linear-gradient(to top,rgba(4,4,8,.82),transparent);}
.gbadge{position:absolute;bottom:8px;right:8px;z-index:2;font-size:10.5px;font-weight:650;
  background:rgba(4,4,8,.72);color:#EAEAF2;padding:3px 7px;border-radius:6px;
  font-variant-numeric:tabular-nums;backdrop-filter:blur(6px);}
.gbadge:empty{display:none;}
.gbadge.live{background:linear-gradient(135deg,var(--ac),var(--ac2));color:#fff;}
.gprog{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(0,0,0,.5);
  display:none;z-index:3;}
.gprog i{display:block;height:100%;width:0;border-radius:0 3px 3px 0;
  background:linear-gradient(90deg,var(--ac),var(--ac2));transition:width .3s var(--ease);}
.gc.downloading .gprog,.gc.processing .gprog{display:block;}
.gc.processing .gprog i{background:var(--info);}
.gplay{position:absolute;z-index:2;width:46px;height:46px;border-radius:50%;
  background:rgba(6,6,12,.55);border:1px solid rgba(255,255,255,.22);color:#fff;
  display:none;align-items:center;justify-content:center;cursor:pointer;
  backdrop-filter:blur(6px);transition:transform .2s var(--ease),background .2s var(--ease);}
.gc.done.playable:hover .gplay,.gc.done.playable:focus-within .gplay{display:flex;}
.gplay:hover{background:rgba(6,6,12,.8);transform:scale(1.08);}
.gacts{position:absolute;top:8px;right:8px;z-index:3;display:flex;gap:5px;opacity:0;
  transform:translateY(-4px);transition:opacity .2s var(--ease),transform .2s var(--ease);}
.gc:hover .gacts,.gc:focus-within .gacts{opacity:1;transform:none;}
.ga{width:29px;height:29px;border-radius:8px;border:1px solid rgba(255,255,255,.14);
  background:rgba(6,6,12,.7);color:#EAEAF2;display:flex;align-items:center;justify-content:center;
  cursor:pointer;backdrop-filter:blur(6px);
  transition:background .18s var(--ease),color .18s var(--ease);}
.ga:hover{background:rgba(6,6,12,.94);color:#fff;}
.ga.gdel:hover{color:var(--danger);}
.ga.gretry:hover{color:var(--ok);}
.ga.gcancel:hover{color:var(--danger);}
.gm{padding:11px 12px 13px;}
.gt{font-size:12.5px;font-weight:600;line-height:1.4;margin-bottom:6px;letter-spacing:-.1px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:35px;}
.gs{font-size:11px;color:var(--mut);display:flex;align-items:center;gap:5px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.gs .ok{color:var(--ok);} .gs .bad{color:var(--danger);}
.gs .spin{width:11px;height:11px;border:2px solid var(--line2);border-top-color:var(--ac);
  border-radius:50%;animation:spin .8s linear infinite;flex:none;}
@keyframes spin{to{transform:rotate(360deg);}}

/* ================= console drawer ================= */
.console{position:fixed;left:0;right:0;bottom:0;z-index:14;display:none;flex-direction:column;
  background:rgba(12,12,18,.97);border-top:1px solid var(--line2);
  box-shadow:0 -18px 44px rgba(0,0,0,.55);backdrop-filter:blur(12px);}
.console.open{display:flex;}
.chead{display:flex;align-items:center;gap:9px;padding:9px 18px;color:var(--mut);
  font:600 12px/1 inherit;border-bottom:1px solid var(--line);}
.cx{width:25px;height:25px;border-radius:7px;border:none;background:transparent;color:var(--dim);
  display:flex;align-items:center;justify-content:center;cursor:pointer;}
.cx:hover{background:var(--s3);color:var(--tx);}
#log{height:186px;overflow-y:auto;padding:9px 18px;white-space:pre-wrap;user-select:text;
  font:11.5px/1.65 "Cascadia Mono",Consolas,monospace;color:#A6A6BA;}
#log .g{color:var(--ok);} #log .r{color:var(--danger);} #log .b{color:var(--info);}
#log .p{color:#FF9CC4;} #log .d{color:#5E5E70;}

/* ================= drop + modal ================= */
#drop{position:fixed;inset:0;z-index:30;display:none;align-items:center;justify-content:center;
  background:rgba(4,4,9,.86);backdrop-filter:blur(5px);}
#drop.on{display:flex;}
.dropcard{display:flex;flex-direction:column;align-items:center;gap:13px;padding:44px 58px;
  border:2px dashed var(--ac);border-radius:var(--r-xl);background:var(--ac-soft);
  color:#FF9CC4;text-align:center;}
.dropcard b{font-size:17px;font-weight:680;color:var(--tx);}
.dropcard span{font-size:12.5px;color:var(--mut);max-width:330px;}
#scrim{position:fixed;inset:0;background:rgba(4,4,9,.74);opacity:0;pointer-events:none;
  transition:opacity .2s var(--ease);z-index:19;backdrop-filter:blur(3px);}
#scrim.open{opacity:1;pointer-events:auto;}
#dlg{position:fixed;left:50%;top:50%;transform:translate(-50%,-48%) scale(.98);opacity:0;
  pointer-events:none;width:min(520px,94vw);max-height:88vh;overflow-y:auto;background:var(--s2);
  border:1px solid var(--line2);border-radius:var(--r-xl);padding:24px;z-index:20;
  display:flex;flex-direction:column;gap:16px;
  transition:opacity .2s var(--ease),transform .2s var(--ease);box-shadow:0 30px 76px rgba(0,0,0,.66);}
#dlg.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.media{display:flex;gap:13px;align-items:center;}
#s-thumb{width:128px;height:72px;border-radius:var(--r-md);object-fit:cover;background:var(--s3);flex:none;}
#s-title{font-size:15px;font-weight:640;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden;}
.msub{font-size:12px;color:var(--mut);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.field{display:flex;flex-direction:column;gap:8px;}
.slabel{font-size:10px;letter-spacing:.9px;font-weight:700;color:var(--dim);}
.qlist{display:flex;flex-direction:column;gap:5px;max-height:250px;overflow-y:auto;}
.qload{padding:16px;text-align:center;color:var(--dim);font-size:12.5px;}
.qrow{display:flex;align-items:center;gap:10px;width:100%;padding:11px 13px;border-radius:var(--r-md);
  border:1px solid var(--line2);background:var(--s1);cursor:pointer;text-align:left;
  transition:background .16s var(--ease),border-color .16s var(--ease);}
.qrow:hover{background:var(--s3);}
.qrow.on{border-color:var(--ac);background:var(--ac-soft);}
.qmain{font:650 13.5px/1 inherit;color:var(--tx);min-width:78px;flex:none;}
.qrow.on .qmain{color:#FF9CC4;}
.qsub{flex:1;font-size:11.5px;color:var(--mut);}
.qsize{font-size:11.5px;color:var(--mut);font-variant-numeric:tabular-nums;flex:none;}
.qck{color:var(--ac);opacity:0;flex:none;display:flex;}
.qrow.on .qck{opacity:1;}
.modeseg{display:flex;gap:3px;background:var(--s1);border-radius:var(--r-md);padding:4px;}
.ms{flex:1;height:34px;border:none;border-radius:9px;background:transparent;color:var(--mut);
  font:640 13px/1 inherit;cursor:pointer;transition:background .18s var(--ease),color .18s var(--ease);}
.ms:hover{color:var(--tx);}
.ms.on{background:linear-gradient(135deg,var(--ac),var(--ac2));color:#fff;}
#autopane{display:flex;flex-direction:column;gap:15px;}
#custompane{display:none;flex-direction:column;gap:15px;}
.fseg{display:flex;gap:8px;}
.fs{flex:1;min-height:54px;padding:9px 13px;border-radius:var(--r-md);border:1px solid var(--line2);
  background:var(--s1);color:var(--mut);cursor:pointer;display:flex;flex-direction:column;
  align-items:flex-start;gap:4px;transition:background .18s var(--ease),color .18s var(--ease),border-color .18s var(--ease);}
.fs:hover{color:var(--tx);background:var(--s3);}
.fs .ft{font:640 13px/1 inherit;}
.fs .fd{font:400 11px/1.2 inherit;color:var(--dim);}
.fs.on{background:var(--ac-soft);color:#FF9CC4;border-color:var(--ac);}
.fs.on .fd{color:#D593B0;}
#vqual{height:42px;border:1px solid var(--line2);border-radius:var(--r-md);background:var(--s1);
  color:var(--tx);font:13px/1 inherit;padding:0 34px 0 14px;cursor:pointer;
  appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%239E9EAE' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>");
  background-repeat:no-repeat;background-position:right 12px center;}
#plrow{display:none;}
#plrow.on{display:flex;}
.plinputs{display:flex;gap:8px;align-items:center;}
#plstart,#plend{height:40px;width:92px;border:1px solid var(--line2);border-radius:var(--r-md);
  background:var(--s1);color:var(--tx);font:13px/1 inherit;padding:0 13px;}
.opts{display:flex;flex-direction:column;gap:2px;}
.ck{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--mut);cursor:pointer;
  padding:9px 10px;border-radius:var(--r-sm);transition:background .16s var(--ease);}
.ck:hover{background:var(--s1);color:var(--tx);}
.ck input{width:17px;height:17px;accent-color:var(--ac);cursor:pointer;flex:none;}
.sact{display:flex;gap:8px;align-items:center;}
.tbtn{background:none;border:none;color:var(--mut);font:640 13px/1 inherit;cursor:pointer;
  height:42px;padding:0 16px;border-radius:99px;transition:background .18s var(--ease),color .18s var(--ease);}
.tbtn:hover{background:var(--s1);color:var(--tx);}
::-webkit-scrollbar{width:10px;height:10px;}
::-webkit-scrollbar-thumb{background:#2A2A38;border-radius:6px;border:3px solid transparent;background-clip:content-box;}
::-webkit-scrollbar-thumb:hover{background:#3A3A4C;background-clip:content-box;}
::-webkit-scrollbar-track{background:transparent;}
@media (max-width:900px){
  :root{--side:60px;}
  .ltname,.lcnt,.brandrow h1,.brandrow .ver,.navlbl,.addlib span{display:none;}
  .fpath{max-width:190px;} #depswarn span:not(.spin){display:none;}
  .side{align-items:center;padding:15px 8px 11px;}
  .brandrow{padding:2px 0 15px;}
  .lt,.addlib{justify-content:center;padding:0;}
  .libttl h2{font-size:19px;}
  #q{width:120px;}
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important;}
  body::before{display:none;}
}
</style></head><body>

<div class="app">
<aside class="side">
  <div class="brandrow">
    <span class="mark" aria-hidden="true">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"
        stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M5 20h14"/></svg>
    </span>
    <h1>YTGrab</h1><span class="ver">v__APP_VERSION__</span>
  </div>
  <div class="navlbl">LIBRARIES</div>
  <nav class="nav" id="tabbar" role="tablist" aria-label="Libraries"></nav>
  <button class="addlib" onclick="addTab()" title="Add a library folder">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
    <span>Add library</span>
  </button>
  <span class="sp"></span>
</aside>

<main class="main">
  <header class="topbar">
    <div class="urlwrap">
      <svg class="uic" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.5-1.5"/></svg>
      <input type="text" id="url" placeholder="Paste a video, playlist or channel link" spellcheck="false"
             aria-label="Video, playlist or channel link">
    </div>
    <button class="btn dl" id="dl" onclick="startDl()">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
        stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/></svg>
      <span id="dlLabel">Download</span></button>
    <span id="depswarn" class="warnpill" role="status">
      <span class="spin"></span><span id="depstxt">Setting up…</span>
    </span>
    <button id="upd" class="upd" onclick="updateClick()"></button>
    <button class="ib" id="importbtn" title="Add local video files" aria-label="Import local videos"
            onclick="pywebview.api.pick_files()">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="M12 15V3"/><path d="m8 7 4-4 4 4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
    </button>
    <button class="ib" id="conbtn" onclick="toggleConsole()" title="Activity log" aria-label="Activity log">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="m4 17 6-5-6-5"/><path d="M12 19h8"/></svg>
      <span class="ibadge" id="conbadge"></span>
    </button>
    <button class="ib" id="loginbtn" title="Log into the pasted URL's site (YouTube if empty)"
            aria-label="Log in" onclick="pywebview.api.login(document.getElementById('url').value)">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-3.5 3.6-6 8-6s8 2.5 8 6"/></svg>
      <span class="idot" id="authdot"></span>
    </button>
  </header>

  <section class="libhead">
    <div>
      <div class="libttl">
        <span class="hicon" id="libicon"></span>
        <h2 id="libtitle">Downloads</h2>
      </div>
      <div id="libsub"></div>
    </div>
    <span class="sp"></span>
    <div class="tools">
      <div class="chips" id="filtertabs" role="tablist" aria-label="Filter by state">
        <button class="chipb on" role="tab" aria-selected="true" data-f="all" onclick="pickFilter('all')">All <span class="cnt" id="c-all">0</span></button>
        <button class="chipb" role="tab" aria-selected="false" data-f="active" onclick="pickFilter('active')">Active <span class="cnt" id="c-active">0</span></button>
        <button class="chipb" role="tab" aria-selected="false" data-f="done" onclick="pickFilter('done')">Done <span class="cnt" id="c-done">0</span></button>
        <button class="chipb" role="tab" aria-selected="false" data-f="failed" onclick="pickFilter('failed')">Failed <span class="cnt" id="c-failed">0</span></button>
      </div>
      <div class="searchwrap">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input type="text" id="q" placeholder="Search" spellcheck="false" aria-label="Search this library"
               oninput="pickQuery(this.value)">
      </div>
      <select id="sortsel" aria-label="Sort by" onchange="pickSort(this.value)">
        <option value="ts">Date added</option>
        <option value="released" selected>Release date</option>
        <option value="title">Title</option>
        <option value="size">Size</option>
      </select>
      <button id="sortdir" onclick="flipDir()" aria-label="Toggle sort direction"></button>
    </div>
  </section>

  <div id="grid">
    <div class="empty" id="empty">
      <span class="emptyic" id="empty-ic"></span>
      <b id="empty-t">No downloads yet</b>
      <span id="empty-s">Paste a link above and your videos appear here</span>
    </div>
  </div>
</main>
</div>

<div class="console" id="console">
  <div class="chead">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round"><path d="m4 17 6-5-6-5"/><path d="M12 19h8"/></svg>
    Activity<span class="sp"></span>
    <span id="counts" class="chip"><span class="dot"></span>ok 0 &middot; failed 0</span>
    <button class="cx" onclick="toggleConsole()" aria-label="Hide activity log">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
    </button>
  </div>
  <div id="log" role="log" aria-live="polite"></div>
</div>

<div id="drop">
  <div class="dropcard">
    <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
      stroke-linecap="round" stroke-linejoin="round"><path d="M12 15V3"/><path d="m8 7 4-4 4 4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
    <b>Drop videos to add them</b>
    <span id="dropto">They move to your Imported folder and appear under that library</span>
  </div>
</div>

<div id="scrim" onclick="closeDlg()"></div>
<div id="dlg" role="dialog" aria-modal="true" aria-label="Download options">
  <div class="media">
    <img id="s-thumb" alt="">
    <div style="min-width:0"><div id="s-title">&hellip;</div><div id="s-sub" class="msub"></div></div>
  </div>
  <div class="modeseg" id="modeseg">
    <button class="ms on" data-m="auto" onclick="switchMode('auto')">Auto</button>
    <button class="ms" data-m="custom" onclick="switchMode('custom')">Custom</button>
  </div>
  <div id="autopane">
    <div class="field">
      <span class="slabel">VIDEO FORMAT</span>
      <div class="fseg" id="vfmtseg">
        <button class="fs on" data-v="quality" onclick="pickVfmt(this)"><span class="ft">Quality</span><span class="fd">VP9 / AV1 / H.265</span></button>
        <button class="fs" data-v="legacy" onclick="pickVfmt(this)"><span class="ft">Legacy</span><span class="fd">H.264 &middot; most compatible</span></button>
      </div>
    </div>
    <div class="field">
      <span class="slabel">VIDEO QUALITY</span>
      <select id="vqual">
        <option value="best">Best quality</option>
        <option value="2160">2160p (4K)</option>
        <option value="1440">1440p</option>
        <option value="1080" selected>1080p</option>
        <option value="720">720p</option>
        <option value="480">480p</option>
        <option value="360">360p</option>
        <option value="lowest">Lowest quality</option>
      </select>
    </div>
    <span class="msub">Audio is always the best available track.</span>
  </div>
  <div id="custompane">
    <div class="field">
      <span class="slabel">CHOOSE A VIDEO &mdash; AUDIO IS ALWAYS BEST</span>
      <div class="qlist" id="vlist"><div class="qload">Loading formats&hellip;</div></div>
    </div>
  </div>
  <div class="field" id="plrow">
    <span class="slabel">PLAYLIST RANGE</span>
    <div class="plinputs">
      <input type="number" id="plstart" min="1" placeholder="start" aria-label="Playlist start">
      <input type="number" id="plend" min="1" placeholder="end" aria-label="Playlist end">
      <span class="msub">blank = all</span>
    </div>
  </div>
  <div class="opts">
    <label class="ck"><input type="checkbox" id="ck-watched" checked>Mark as watched</label>
    <label class="ck"><input type="checkbox" id="ck-stamp" checked>Set file date to upload date</label>
    <label class="ck"><input type="checkbox" id="ck-skip" onchange="skipChanged()">Skip download (only mark watched)</label>
  </div>
  <div class="sact">
    <span class="sp"></span>
    <button class="tbtn" onclick="closeDlg()">Cancel</button>
    <button class="btn dl" id="s-go" onclick="confirmDl()" style="height:42px;padding:0 22px;font-size:13.5px">Download</button>
  </div>
</div>

<script>
var okCount=0,failCount=0,lastProg=null,logEl,gridEl,pendingUrl=null,lastInfo=null,defaultFmt="";
var dlMode="auto",curVfmt="quality";
var cards={};
var isBusy=false;
var LABEL={fetching:"Fetching info",downloading:"Downloading",processing:"Processing",
           done:"Completed",failed:"Failed",queued:"Queued"};
var ACTIVE={queued:1,fetching:1,downloading:1,processing:1};
var P={
 folder:'<path d="M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2z"/>',
 trash:'<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
 check:'<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.5-4.5"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 alert:'<path d="M12 3 2 20h20z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
 retry:'<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
 x:'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
 plus:'<path d="M12 5v14"/><path d="M5 12h14"/>',
 up:'<path d="M12 20V5"/><path d="m6 11 6-6 6 6"/>',
 down:'<path d="M12 4v15"/><path d="m6 13 6 6 6-6"/>',
 film:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 12h18"/>',
 edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'
};
function ic(n,sz,cls){return '<svg class="'+(cls||'')+'" width="'+(sz||16)+'" height="'+(sz||16)+
  '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+P[n]+'</svg>';}
function play(sz){return '<svg width="'+(sz||20)+'" height="'+(sz||20)+
  '" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';}
function lineClass(t){
  if(t.indexOf("[+]")===0)return"g";
  if(t.indexOf("[!]")===0||t.indexOf("ERROR")===0)return"r";
  if(t.indexOf("[post]")===0)return"b";
  if(t.indexOf("[*]")===0)return"p";
  if(t.indexOf("[deps]")===0||t.indexOf("[login]")===0||t.indexOf("[tab]")===0)return"d";
  return"";
}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function toggleConsole(){document.getElementById("console").classList.toggle("open");}

/* ================= libraries ================= */
var activeTab="downloads",dlFolder="",allTabs=[{id:"downloads",name:"Downloads",builtin:true}];
function tabOf(c){return c.tab||"downloads";}
function tabById(id){
  for(var i=0;i<allTabs.length;i++)if(allTabs[i].id===id)return allTabs[i];
  return allTabs[0];
}
function renderTabs(list){
  allTabs=[{id:"downloads",name:"Downloads",builtin:true}].concat(list||[]);
  if(tabById(activeTab).id!==activeTab)activeTab="downloads";
  var bar=document.getElementById("tabbar");bar.innerHTML="";
  allTabs.forEach(function(t){
    var b=document.createElement("button");
    b.className="lt"+(t.id===activeTab?" on":"");
    b.setAttribute("role","tab");
    b.setAttribute("aria-selected",t.id===activeTab?"true":"false");
    b.title=t.folder||t.name;
    b.innerHTML=ic(t.builtin?"down":"film",15)+'<span class="ltname">'+esc(t.name)+
                '</span><span class="lcnt">0</span>';
    b.onclick=function(){switchTab(t.id);};
    bar.appendChild(b);
  });
}
function switchTab(id){
  activeTab=id;
  try{pywebview.api.set_tab(id);}catch(e){}
  var t=tabById(id);
  document.getElementById("libtitle").textContent=t.name;
  document.getElementById("libicon").innerHTML=ic(t.builtin?"down":"film",16);
  document.getElementById("filtertabs").style.display=t.builtin?"flex":"none";
  renderSub();
  renderTabs(allTabs.slice(1));
  loadView(id);
  refreshView();
  checkFiles();
}
/* every library shows its own folder next to its heading */
function renderSub(){
  var t=tabById(activeTab),sub=document.getElementById("libsub");
  if(!sub)return;
  sub.innerHTML="";
  var path=t.builtin?dlFolder:(t.folder||"");
  var p=document.createElement("span");
  p.className="fpath";p.title=path;
  p.innerHTML=ic("folder",12)+"<span>"+esc(path)+"</span>";
  sub.appendChild(p);
  if(t.builtin){
    sub.appendChild(hbtn("edit","Change download folder",pickDir));
  }else{
    sub.appendChild(hbtn("retry","Re-scan this folder for new videos",function(){
      pywebview.api.scan_tab(t.id);}));
    if(!t.fixed)sub.appendChild(hbtn("trash","Remove this library (files are kept)",
      removeTab,"danger"));
  }
  var d=document.createElement("span");d.className="subdot";d.textContent="·";
  var c=document.createElement("span");c.className="subcount";c.id="subcount";
  sub.appendChild(d);sub.appendChild(c);
}
function hbtn(icon,title,fn,cls){
  var b=document.createElement("button");
  b.className="hact "+(cls||"");b.title=title;b.setAttribute("aria-label",title);
  b.innerHTML=ic(icon,13);b.onclick=fn;return b;
}
function addTab(){
  pywebview.api.add_tab().then(function(list){
    renderTabs(list);
    if(list&&list.length)switchTab(list[list.length-1].id);
  });
}
function removeTab(){
  var t=tabById(activeTab);
  if(t.builtin||t.fixed)return;
  if(!confirm('Remove the "'+t.name+'" library?\n\nIt is only removed from YTGrab — your files stay in '+t.folder))return;
  pywebview.api.remove_tab(t.id).then(function(list){
    activeTab="downloads";renderTabs(list);switchTab("downloads");
  });
}

/* ================= per-library view state ================= */
var views={},curSort="released",curDir=-1,curFilter="all",curQuery="";
var NAT={ts:-1,released:-1,title:1,size:-1};
function loadView(tid){
  var v=views[tid]||{};
  curSort=v.sort||"released";
  curDir=(v.dir===1||v.dir===-1)?v.dir:(NAT[curSort]||-1);
  curFilter=v.filter||"all";
  curQuery="";
  document.getElementById("q").value="";
  document.getElementById("sortsel").value=curSort;
  updDirIcon();syncChips();
  sortGrid(curSort);
}
function saveView(){
  views[activeTab]={sort:curSort,dir:curDir,filter:curFilter};
  try{pywebview.api.set_view(activeTab,curSort,curDir,curFilter);}catch(e){}
}
function syncChips(){
  var cs=document.querySelectorAll("#filtertabs .chipb");
  for(var i=0;i<cs.length;i++){
    var on=cs[i].getAttribute("data-f")===curFilter;
    cs[i].classList.toggle("on",on);cs[i].setAttribute("aria-selected",on?"true":"false");
  }
}
function updDirIcon(){
  var b=document.getElementById("sortdir");
  if(b){b.innerHTML=ic(curDir>0?"up":"down",14);
    b.title=curDir>0?"Ascending":"Descending";}
}
function pickSort(mode){curSort=mode;curDir=NAT[mode]||-1;updDirIcon();sortGrid(mode);saveView();}
function flipDir(){curDir=-curDir;updDirIcon();sortGrid(curSort);saveView();}
function pickFilter(f){curFilter=f;syncChips();refreshView();saveView();}
function pickQuery(v){curQuery=(v||"").trim().toLowerCase();refreshView();}

function bucket(c){
  if(c.status==="failed")return "failed";
  if(ACTIVE[c.status])return "active";
  return "done";
}
function refreshView(){
  var n={all:0,active:0,done:0,failed:0},shown=0,per={};
  var nodes=gridEl.querySelectorAll(".gc");
  for(var i=0;i<nodes.length;i++){
    var el=nodes[i],c=cards[el.id.slice(2)]||{},b=bucket(c),t=tabOf(c);
    per[t]=(per[t]||0)+1;
    var mine=t===activeTab;
    if(mine){n.all++;n[b]++;}
    var hit=mine&&(curFilter==="all"||curFilter===b)&&
            (!curQuery||(c.title||"").toLowerCase().indexOf(curQuery)!==-1);
    el.classList.toggle("hide",!hit);
    if(hit)shown++;
  }
  ["all","active","done","failed"].forEach(function(k){
    var e=document.getElementById("c-"+k);if(e)e.textContent=n[k];
  });
  var lc=document.querySelectorAll("#tabbar .lt .lcnt");
  for(var j=0;j<lc.length&&j<allTabs.length;j++)lc[j].textContent=per[allTabs[j].id]||0;
  var t2=tabById(activeTab);
  var sc=document.getElementById("subcount");
  if(sc)sc.textContent=shown+(shown===1?" video":" videos");
  var e=document.getElementById("empty");
  if(e){
    e.style.display=shown===0?"flex":"none";
    var t=document.getElementById("empty-t"),ss=document.getElementById("empty-s");
    document.getElementById("empty-ic").innerHTML=ic(t2.builtin?"down":"film",26);
    if(curQuery){t.textContent="No matches";ss.textContent='Nothing here matches "'+curQuery+'"';}
    else if(!t2.builtin){t.textContent="Nothing in "+t2.name+" yet";
      ss.textContent="Drop video files here, or use the re-scan button next to the folder above";}
    else if(curFilter==="active"){t.textContent="Nothing downloading";ss.textContent="Queued and in-progress downloads show up here";}
    else if(curFilter==="failed"){t.textContent="No failed downloads";ss.textContent="Failures stay here until you retry or remove them";}
    else{t.textContent="No downloads yet";ss.textContent="Paste a link above and your videos appear here";}
  }
}

/* ---- files removed outside the app show as missing without a restart ---- */
function checkFiles(){
  if(!window.pywebview||!pywebview.api||!pywebview.api.check_files)return;
  pywebview.api.check_files(activeTab).then(function(map){
    for(var k in map){
      var c=cards[k];
      if(c&&c.status==="done"&&c.exists!==map[k])ui.item({key:k,exists:map[k]});
    }
  })["catch"](function(){});
}

/* ================= drag & drop ================= */
var dragDepth=0;
function hasFiles(e){
  var t=e.dataTransfer&&e.dataTransfer.types;
  if(!t)return false;
  for(var i=0;i<t.length;i++)if(t[i]==="Files")return true;
  return false;
}
function showDrop(on){
  dragDepth=on?dragDepth:0;
  var d=document.getElementById("drop");if(!d)return;
  if(on){
    var tb=tabById(activeTab),name=tb.builtin?(allTabs[1]?allTabs[1].name:"Imported"):tb.name;
    document.getElementById("dropto").textContent=
      "They move to your "+name+" folder and appear under that library";
  }
  d.classList.toggle("on",!!on);
}
window.addEventListener("dragenter",function(e){
  if(!hasFiles(e))return;
  e.preventDefault();dragDepth++;showDrop(true);
});
window.addEventListener("dragover",function(e){if(hasFiles(e))e.preventDefault();});
window.addEventListener("dragleave",function(e){
  if(!hasFiles(e))return;
  dragDepth--;if(dragDepth<=0)showDrop(false);
});
window.addEventListener("drop",function(e){
  if(!hasFiles(e))return;
  e.preventDefault();showDrop(false);
});

/* ================= cards ================= */
function subHtml(o){
  if(o.status==="done"){
    if(o.exists===false)return ic('alert',13,'bad')+"<span>File missing</span>";
    var parts=[o.size,o.format].filter(Boolean).join(" · ")||o.channel||"Completed";
    return ic('check',13,'ok')+"<span>"+esc(parts)+"</span>";
  }
  if(o.status==="failed")return ic('alert',13,'bad')+"<span>Failed</span>";
  if(o.status==="downloading"){
    var p=[o.phase||"Downloading"];if(o.speed)p.push(o.speed);
    return '<span class="spin"></span><span>'+esc(p.join(" · "))+"</span>";
  }
  if(o.status==="queued")return ic('clock',12)+"<span>Queued</span>";
  return '<span class="spin"></span><span>'+esc(o.phase||LABEL[o.status]||"")+"</span>";
}
function makeCard(key){
  var el=document.createElement("div");el.className="gc";el.id="g-"+key;
  el.innerHTML=
    '<div class="gth"><img class="gimg" style="display:none" alt=""><span class="gph">'+play(28)+'</span>'+
    '<div class="gbadge"></div>'+
    '<div class="gacts"><button class="ga gretry" aria-label="Retry download" style="display:none">'+ic('retry',15)+'</button>'+
    '<button class="ga gcancel" aria-label="Cancel download" style="display:none">'+ic('x',15)+'</button>'+
    '<button class="ga gfolder" aria-label="Show in folder" style="display:none">'+ic('folder',15)+'</button>'+
    '<button class="ga gdel" aria-label="Delete video (to Recycle Bin)" style="display:none">'+ic('trash',15)+'</button></div>'+
    '<button class="gplay" aria-label="Play video">'+play(20)+'</button>'+
    '<div class="gprog"><i></i></div></div>'+
    '<div class="gm"><div class="gt">…</div><div class="gs"></div></div>';
  el.addEventListener("dblclick",function(){if(cards[key]&&cards[key].path)pywebview.api.play(key);});
  el.querySelector(".gplay").addEventListener("click",function(e){e.stopPropagation();pywebview.api.play(key);});
  el.querySelector(".gfolder").addEventListener("click",function(e){e.stopPropagation();pywebview.api.reveal(key);});
  el.querySelector(".gretry").addEventListener("click",function(e){e.stopPropagation();pywebview.api.retry(key);});
  el.querySelector(".gcancel").addEventListener("click",function(e){e.stopPropagation();pywebview.api.cancel();});
  el.querySelector(".gdel").addEventListener("click",function(e){e.stopPropagation();pywebview.api.remove(key);ui.drop(key);});
  gridEl.prepend(el);
  return el;
}
var ui={
  item:function(o){
    var key=o.key,el=document.getElementById("g-"+key);
    if(!el){
      if(!o.status&&!cards[key])return;
      el=makeCard(key);
    }
    var c=cards[key]||{};for(var k in o)if(o[k]!=null)c[k]=o[k];cards[key]=c;
    if(c.thumb){var im=el.querySelector(".gimg");if(!im.getAttribute("src")){
      im.onerror=function(){im.style.display="none";};
      im.onload=function(){el.querySelector(".gph").style.display="none";};
      im.src=c.thumb;im.style.display="";}}
    el.className="gc "+(c.status||"")+(c.exists===false?" missing":"")+
                 (c.path&&c.exists!==false?" playable":"");
    el.querySelector(".gt").textContent=c.title||"…";
    var b=el.querySelector(".gbadge");
    b.className="gbadge";
    if(c.status==="downloading"&&c.pct!=null){b.textContent=Math.round(c.pct)+"%";b.className="gbadge live";}
    else if(c.status==="queued"){b.textContent="Queued";}
    else if(c.status==="processing"){b.textContent="Processing";b.className="gbadge live";}
    else if(c.duration){b.textContent=c.duration;}else{b.textContent="";}
    el.querySelector(".gs").innerHTML=subHtml(c);
    if(c.pct!=null)el.querySelector(".gprog i").style.width=c.pct+"%";
    var act=(c.status==="downloading"||c.status==="processing"||c.status==="fetching");
    el.querySelector(".gcancel").style.display=act?"flex":"none";
    el.querySelector(".gretry").style.display=c.status==="failed"?"flex":"none";
    el.querySelector(".gfolder").style.display=(c.status==="done"&&c.path)?"flex":"none";
    el.querySelector(".gdel").style.display=(c.status==="done"||c.status==="failed")?"flex":"none";
    refreshView();
  },
  drop:function(key){var n=document.getElementById("g-"+key);if(n)n.remove();delete cards[key];refreshView();},
  log:function(t){
    var isP=t.indexOf("[download]")===0&&t.indexOf("%")!==-1;
    if(isP&&lastProg){lastProg.textContent=t;}
    else{var d=document.createElement("div");d.textContent=t;var c=lineClass(t);if(c)d.className=c;
      logEl.appendChild(d);lastProg=isP?d:null;
      if(logEl.childElementCount>2000)logEl.removeChild(logEl.firstChild);}
    logEl.scrollTop=logEl.scrollHeight;
  },
  setState:function(s){
    if(s.dir){dlFolder=s.dir;renderSub();}
    var ad=document.getElementById("authdot");
    ad.className="idot"+(s.logged_in?" ok":"");
    document.getElementById("loginbtn").title=s.logged_in
      ? "Signed in — click to sign into another site"
      : "Not signed in — click to log in (YouTube if the box is empty)";
    // deps only take up space while they actually need attention
    var dw=document.getElementById("depswarn");
    dw.classList.toggle("on",!s.deps_ok);
    document.getElementById("dl").disabled=!s.deps_ok;
    document.getElementById("s-go").disabled=!s.deps_ok;
    isBusy=!!s.busy;
    document.getElementById("dlLabel").textContent=isBusy?"Queue":"Download";
  },
  done:function(ok){
    if(ok){okCount++;}else{failCount++;}
    var el=document.getElementById("counts");
    el.className="chip "+(failCount?"warn":(okCount?"ok":""));
    el.innerHTML='<span class="dot"></span>ok '+okCount+' · failed '+failCount;
    var bd=document.getElementById("conbadge");   // failures surface on the log button
    bd.textContent=failCount||"";
    bd.classList.toggle("on",failCount>0);
    sortGrid(curSort);
  },
  updateAvail:function(v){
    var u=document.getElementById("upd");
    u.dataset.ver=v;
    u.innerHTML=ic('down',13)+'<span>Update '+esc(v)+'</span>';
    u.style.display="inline-flex";
  },
  updating:function(t){
    var u=document.getElementById("upd");
    if(u)u.innerHTML='<span>'+esc(t)+'</span>';
  }
};
function updateClick(){
  var u=document.getElementById("upd");
  if(u.dataset.busy)return;
  if(!confirm("Download and install "+(u.dataset.ver||"the update")+" now?\n\nThe app will close and reopen automatically."))return;
  u.dataset.busy="1";
  u.innerHTML='<span>Starting update…</span>';
  pywebview.api.run_update();
}
function sortGrid(mode){
  curSort=mode;
  var nodes=Array.prototype.slice.call(gridEl.querySelectorAll(".gc"));
  nodes.sort(function(a,b){
    var ca=cards[a.id.slice(2)]||{},cb=cards[b.id.slice(2)]||{};
    var aa=ca.status!=="done",ba=cb.status!=="done";
    if(aa!==ba)return aa?-1:1;               // active/failed pinned on top
    if(aa)return 0;
    var base;
    if(mode==="title")base=(ca.title||"").localeCompare(cb.title||"");
    else if(mode==="released")base=(ca.released||0)-(cb.released||0);
    else if(mode==="size")base=(ca.bytes||0)-(cb.bytes||0);
    else base=(ca.ts||0)-(cb.ts||0);
    return base*curDir;
  });
  nodes.forEach(function(n){gridEl.appendChild(n);});
}
function skipChanged(){
  var skip=document.getElementById("ck-skip").checked;
  ["modeseg","autopane","custompane"].forEach(function(id){
    var el=document.getElementById(id);
    if(el){el.style.opacity=skip?"0.35":"";el.style.pointerEvents=skip?"none":"";}
  });
  var w=document.getElementById("ck-watched"),st=document.getElementById("ck-stamp");
  if(skip)w.checked=true;
  w.disabled=skip; st.disabled=skip;
  w.parentElement.style.opacity=skip?"0.55":"";
  st.parentElement.style.opacity=skip?"0.35":"";
  document.getElementById("s-go").textContent=skip?"Mark watched":(isBusy?"Queue":"Download");
}
function switchMode(m){
  dlMode=m;
  var ms=document.querySelectorAll("#modeseg .ms");
  for(var i=0;i<ms.length;i++)ms[i].classList.toggle("on",ms[i].getAttribute("data-m")===m);
  document.getElementById("autopane").style.display=m==="auto"?"flex":"none";
  document.getElementById("custompane").style.display=m==="custom"?"flex":"none";
}
function pickVfmt(el){
  curVfmt=el.getAttribute("data-v");
  var bs=document.querySelectorAll("#vfmtseg .fs");
  for(var i=0;i<bs.length;i++)bs[i].classList.remove("on");
  el.classList.add("on");
}
function autoFmt(){
  var q=document.getElementById("vqual").value;
  if(q==="lowest")return "wv*+ba/w";
  var hc=q==="best"?"":"[height<="+q+"]";
  var fb=q==="best"?"bv*+ba/b":"bv*[height<="+q+"]+ba/b[height<="+q+"]";
  if(curVfmt==="legacy")return "bv*"+hc+"[vcodec^=avc]+ba/"+fb;
  var cs=["[vcodec~='^vp0?9']","[vcodec~='^av01']","[vcodec~='^(hev1|hvc1)']"];
  return cs.map(function(c){return "bv*"+hc+c+"+ba";}).join("/")+"/"+fb;
}
function genericV(){return [
  {label:"1080p",sub:"",size:"",fmt:"bv*[height<=1080]+ba/b[height<=1080]"},
  {label:"720p",sub:"",size:"",fmt:"bv*[height<=720]+ba/b[height<=720]"},
  {label:"480p",sub:"",size:"",fmt:"bv*[height<=480]+ba/b[height<=480]"},
  {label:"360p",sub:"",size:"",fmt:"bv*[height<=360]+ba/b[height<=360]"}
];}
function renderVList(vformats){
  var list=(vformats&&vformats.length)?vformats.slice():genericV();
  list.unshift({label:"Best available",sub:"Best video + best audio",size:"",fmt:"bv*+ba/b"});
  var ql=document.getElementById("vlist");ql.innerHTML="";
  list.forEach(function(o,i){
    var b=document.createElement("button");
    b.className="qrow"+(i===0?" on":"");
    b.setAttribute("data-fmt",o.fmt);
    b.innerHTML='<span class="qmain">'+esc(o.label)+'</span><span class="qsub">'+esc(o.sub||"")+'</span><span class="qsize">'+esc(o.size||"")+'</span><span class="qck">'+ic("check",15)+'</span>';
    b.onclick=function(){var rs=ql.querySelectorAll(".qrow");for(var j=0;j<rs.length;j++)rs[j].classList.remove("on");b.classList.add("on");};
    ql.appendChild(b);
  });
}
function startDl(){
  var u=document.getElementById("url").value.trim();
  if(!u){ui.log("[!] paste a URL first");return;}
  pendingUrl=u;lastInfo=null;openDlg(u);
  pywebview.api.fetch_info(u).then(function(info){if(pendingUrl===u){lastInfo=info;fillDlg(info,u);}})
    ["catch"](function(){if(pendingUrl===u)fillDlg({ok:false,error:""},u);});
}
function openDlg(u){
  var t=document.getElementById("s-thumb");t.style.display="none";t.removeAttribute("src");
  document.getElementById("s-title").textContent="Fetching info…";
  document.getElementById("s-sub").textContent=u;
  switchMode("auto");
  curVfmt="quality";
  document.querySelector('#vfmtseg .fs[data-v="quality"]').classList.add("on");
  document.querySelector('#vfmtseg .fs[data-v="legacy"]').classList.remove("on");
  document.getElementById("vqual").value="1080";
  document.getElementById("vlist").innerHTML='<div class="qload">Loading formats…</div>';
  document.getElementById("ck-skip").checked=false;skipChanged();
  var isPl=u.indexOf("list=")!==-1||u.indexOf("/@")!==-1;
  document.getElementById("plrow").classList.toggle("on",isPl);
  document.getElementById("scrim").classList.add("open");
  document.getElementById("dlg").classList.add("open");
  setTimeout(function(){var g=document.getElementById("s-go");if(g)g.focus();},60);
}
function fillDlg(info,u){
  var t=document.getElementById("s-thumb");
  if(info&&info.ok){
    document.getElementById("s-title").textContent=info.title;
    var sub=info.uploader||"";
    if(info.kind==="playlist"){sub=(sub?sub+" · ":"")+info.count+" videos";
      document.getElementById("plrow").classList.add("on");}
    else if(info.duration){sub=(sub?sub+" · ":"")+info.duration;}
    document.getElementById("s-sub").textContent=sub;
    if(info.thumb){t.onerror=function(){t.style.display="none";};t.src=info.thumb;t.style.display="";}
  }else{
    document.getElementById("s-title").textContent="Couldn't fetch info — you can still download";
    document.getElementById("s-sub").textContent=(info&&info.error)||u;
  }
  renderVList(info&&info.ok?info.vformats:null);
}
function closeDlg(){
  document.getElementById("scrim").classList.remove("open");
  document.getElementById("dlg").classList.remove("open");
  document.getElementById("url").focus();
}
function confirmDl(){
  if(!pendingUrl)return;
  var fmt;
  if(dlMode==="auto")fmt=autoFmt();
  else{var sel=document.querySelector("#vlist .qrow.on");fmt=sel?sel.getAttribute("data-fmt"):"bv*+ba/b";}
  closeDlg();
  document.getElementById("url").value="";
  pywebview.api.start_download(pendingUrl,"custom",fmt,
    document.getElementById("plstart").value,document.getElementById("plend").value,
    document.getElementById("ck-watched").checked,document.getElementById("ck-stamp").checked,
    document.getElementById("ck-skip").checked,
    (lastInfo&&lastInfo.ok&&lastInfo.kind==="video")?
      {id:lastInfo.id,title:lastInfo.title,uploader:lastInfo.uploader,duration:lastInfo.duration,thumb:lastInfo.thumb}:
      (lastInfo&&lastInfo.ok?{title:lastInfo.title,thumb:lastInfo.thumb}:{}))
    .then(function(r){if(r==="no-deps")ui.log("[!] dependencies missing");});
}
function pickDir(){pywebview.api.pick_folder().then(function(d){dlFolder=d;renderSub();});}
document.addEventListener("keydown",function(e){
  var open=document.getElementById("dlg").classList.contains("open");
  if(e.key==="Escape"){if(open){closeDlg();}else{document.getElementById("console").classList.remove("open");}return;}
  if((e.ctrlKey||e.metaKey)&&e.key==="f"){e.preventDefault();document.getElementById("q").focus();return;}
  if(e.key==="Enter"){if(open){confirmDl();}
    else if(document.activeElement===document.getElementById("url")){startDl();}}
});
window.addEventListener("focus",checkFiles);
setInterval(checkFiles,15000);
window.addEventListener("pywebviewready",function(){
  logEl=document.getElementById("log");gridEl=document.getElementById("grid");
  updDirIcon();
  document.getElementById("url").focus();
  pywebview.api.get_state().then(function(s){
    ui.setState(s);
    defaultFmt=s.default_format||"";
    views=s.views||{};
    document.getElementById("ck-watched").checked=s.mark_watched!==false;
    document.getElementById("ck-stamp").checked=s.set_timestamp!==false;
    return pywebview.api.get_tabs();
  }).then(function(list){
    renderTabs(list);switchTab("downloads");
    return pywebview.api.get_history();
  }).then(function(list){
    list.slice().reverse().forEach(function(e){
      ui.item({key:e.id,status:"done",title:e.title,channel:e.channel,duration:e.duration,
               size:e.size_h,bytes:e.size,format:e.format,thumb:e.thumb,path:e.path,
               exists:e.exists,ts:e.ts,released:e.released,source:e.source,tab:e.tab});
    });
    return pywebview.api.get_pending();
  }).then(function(pend){
    pend.forEach(function(p){
      ui.item({key:p.key,status:"failed",title:p.title,channel:p.channel,
               thumb:p.thumb,duration:p.duration,tab:"downloads"});
    });
    sortGrid(curSort);refreshView();
  });
});
</script></body></html>"""


def register_drop(api):
    """Whole-window drop target. Only a pywebview-registered 'drop' listener
    gets real paths (WebView2 hands them over as pywebviewFullPath); the plain
    JS listener alongside it only drives the overlay and preventDefault."""
    def on_drop(e):
        files = ((e or {}).get("dataTransfer") or {}).get("files") or []
        api.import_paths([f.get("pywebviewFullPath") for f in files
                          if f.get("pywebviewFullPath")])
    try:
        from webview.dom import DOMEventHandler
        UI_WIN.dom.get_element("body").events.drop += DOMEventHandler(
            on_drop, prevent_default=True)
    except Exception as e:
        api._push(f"[!] drag-and-drop unavailable: {e}")


def bootstrap(api):
    for _ in range(60):
        try:
            UI_WIN.evaluate_js("1")
            break
        except Exception:
            time.sleep(0.25)
    register_drop(api)
    api._push(f"[*] {APP_NAME} started - data dir: {APP_DIR}")
    ensure_deps(api._push)
    api._set_state()
    api._push("[login] checking sign-in state...")
    api._recheck_login()
    latest = check_update()
    if latest:
        api._push(f"[*] update available: {latest} - click the chip in the header")
        try:
            UI_WIN.evaluate_js(f"ui.updateAvail({json.dumps(latest)})")
        except Exception:
            pass


def main():
    if "--ping" in sys.argv:   # boot-cost probe: exits before any window
        sys.exit(0)
    if "--where" in sys.argv:  # diagnostic: report resolved paths
        (Path(tempfile.gettempdir()) / "ytgrab_where.txt").write_text(
            f"frozen={getattr(sys, 'frozen', False)}\n"
            f"executable={sys.executable}\n"
            f"exe_parent={Path(sys.executable).resolve().parent}\n"
            f"has_internal={(Path(sys.executable).resolve().parent / '_internal').is_dir()}\n"
            f"APP_DIR={APP_DIR}\n", encoding="utf-8")
        sys.exit(0)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if "--setup" in sys.argv:
        ok = ensure_deps(log)
        sys.exit(0 if ok else 1)
    (APP_DIR / "cookies_youtube.txt").unlink(missing_ok=True)
    global UI_WIN
    api = Api()
    UI_WIN = webview.create_window(APP_NAME, html=HTML.replace("__APP_VERSION__", APP_VERSION), js_api=api,
                                   width=980, height=780, min_size=(760, 560))
    webview.start(lambda: bootstrap(api), private_mode=False,
                  storage_path=str(PROFILE_DIR))


if __name__ == "__main__":
    main()
