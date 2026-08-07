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
import hashlib
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
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse
from ctypes import wintypes
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import webview

APP_NAME = "YTGrab"
APP_VERSION = "1.15.6"  # keep in sync with installer.iss AppVersion (drives the update-check)


# All app data (deps, browser profile, config, history) lives here for BOTH
# the portable exe and the installed build, so login/history/tools are shared.
APP_DIR = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
BIN_DIR = APP_DIR / "bin"
PROFILE_DIR = APP_DIR / "profile"
CONFIG_FILE = APP_DIR / "config.json"
HISTORY_FILE = APP_DIR / "history.json"
FAILED_FILE = APP_DIR / "failed.json"   # failed/unfinished downloads, retryable after restart
THUMB_DIR = APP_DIR / "thumbs"          # generated posters for imported local videos
SITES_FILE = APP_DIR / "supported_sites.json"   # cached copy of yt-dlp's list
SITES_URL = "https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/supportedsites.md"
SITES_MAX_AGE = 7 * 24 * 3600           # refetch weekly; yt-dlp adds sites often
LOG_FILE = APP_DIR / "ytgrab.log"
YTDLP = BIN_DIR / "yt-dlp.exe"
FFMPEG = BIN_DIR / "ffmpeg.exe"
FFPROBE = BIN_DIR / "ffprobe.exe"
FF_VER_FILE = BIN_DIR / "ffmpeg.ver"
NODE = BIN_DIR / "node.exe"  # JS runtime yt-dlp uses to solve YouTube's sig/n challenge

# OneDrive section (rclone-backed fast browser)
RCLONE = BIN_DIR / "rclone.exe"
RCLONE_CONF = APP_DIR / "rclone.conf"      # our own; the user's rclone setup stays untouched
OD_THUMB_DIR = APP_DIR / "odthumbs"        # OneDrive image previews (whole file, capped)
OD_STAGE_DIR = Path(tempfile.gettempdir()) / "ytgrab-od"
SYSTEM_RCLONE_CONF = Path(os.environ.get("APPDATA", "")) / "rclone" / "rclone.conf"
RCLONE_ZIP_URL = "https://downloads.rclone.org/rclone-current-windows-amd64.zip"
OD_REMOTE = "onedrive"
OD_THUMB_MAX = 50 * 1024 * 1024   # rclone has no thumbnail API: a preview IS the file
OD_DL_WORKERS = 2                # files in parallel; each is already multi-threaded
NEW_CONSOLE = 0x00000010         # CREATE_NEW_CONSOLE (visible, for the OAuth walkthrough)
RE_OD_STATS = re.compile(r",\s*(\d+)%,\s*([\d.]+\s*[KMGT]?i?B)/s,\s*ETA\s+(\S+)")
RE_STANZA = re.compile(r"^\[([^\]]+)\]\s*$", re.M)

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


DOWNLOADS_TAB = "downloads"   # built-in library tab; the rest are folder-backed
DOWNLOADS_NAME = "YT Downloads"
# 'fixed' tabs ship with the app and cannot be removed
DEFAULT_TABS = [{"id": "imported", "name": "Imported", "folder": "", "fixed": True}]


def short_path(p):
    """C:\\Users\\me\\AppData\\Local\\YTGrab\\bin -> %LOCALAPPDATA%\\YTGrab\\bin"""
    s = str(p)
    for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(var)
        if base and s.lower().startswith(base.lower()):
            return f"%{var}%" + s[len(base):]
    return s


RE_SUPPORTED = re.compile(r"^\s*-\s*\*\*(.+?)\*\*\s*(?::\s*(.*))?$")


def parse_supported(md):
    """yt-dlp's supportedsites.md -> [{'name','note','broken'}] (live, not baked in)."""
    out = []
    for line in (md or "").splitlines():
        m = RE_SUPPORTED.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        rest = (m.group(2) or "").strip()
        broken = "currently broken" in rest.lower()
        note = re.sub(r"\(\*\*.*?\*\*\)", "", rest)          # drop the broken marker
        note = re.sub(r"\[\*(.*?)\*\]\(##[^)]*\)", r"\1", note)   # netrc machine links
        note = re.sub(r"\s{2,}", " ", note).strip(" :")
        out.append({"name": name, "note": note, "broken": broken})
    return out


def load_sites_cache():
    d = _load_json(SITES_FILE, {})
    return d if isinstance(d, dict) else {}


def fetch_supported(force=False):
    """Cached weekly; falls back to whatever was cached if the network is down."""
    cache = load_sites_cache()
    fresh = cache.get("sites") and (time.time() - cache.get("ts", 0)) < SITES_MAX_AGE
    if fresh and not force:
        return cache
    try:
        with http_get(SITES_URL, timeout=25) as r:
            md = r.read().decode("utf-8", "replace")
        sites = parse_supported(md)
        if not sites:
            raise ValueError("nothing parsed")
        cache = {"ts": time.time(), "sites": sites}
        try:
            SITES_FILE.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
        return cache
    except Exception:
        return cache or {"ts": 0, "sites": []}


def site_name(host):
    """'www.sonyliv.com' -> 'Sonyliv'; used to name a site's own library."""
    h = re.sub(r"^(www|m|web)\.", "", (host or "").lower())
    label = h.split(".")[0] if h else "Other"
    return re.sub(r"[^A-Za-z0-9 ]", "", label).title() or "Other"


# everything from the first of these markers onward is release noise, not a title
RE_REL_CUT = re.compile(
    r"\b(?:\d{3,4}p|4k|uhd|hdr10\+?|hdr|sdr|10bit|8bit|web[- ]?dl|web[- ]?rip|webrip|"
    r"blu[- ]?ray|bluray|b[rd]rip|hdrip|dvdrip|hdtv|hdcam|camrip|"
    r"x26[45]|h\.?26[45]|hevc|avc1?|xvid|divx|"
    r"aac|ac3|eac3|ddp?\d|dts(?:[- ]?hd)?|atmos|truehd|"
    r"dual[- ]?audio|multi(?:sub)?|esubs?|msubs?|repack|proper|remastered|"
    r"amzn|dsnp|nf|hmax|sonyliv|zee5|jio|yts|yify|rarbg|psa|galaxyrg|moviesleech|"
    r"tgx|ettv|eztv|hq|hd)\b", re.I)
RE_BRACKETS = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")


def pretty_title(name):
    """'Heartstopper.Forever.2026.1080p.WEBRip.x264[YTS.GG]' -> 'Heartstopper Forever 2026'.
    Falls back to the tidied name when nothing looks like release noise."""
    s = re.sub(r"\.(mkv|mp4|avi|webm|mov|m4v|flv|ts)$", "", name or "", flags=re.I)
    s = re.sub(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]", r" \1 ", s)   # keep (2014)
    s = RE_BRACKETS.sub(" ", s)
    s = s.replace("_", " ").replace(".", " ")
    m = RE_REL_CUT.search(s)
    head = s[:m.start()] if m else s
    head = re.sub(r"[\s\-–—_]+$", "", head).strip()
    head = re.sub(r"\s{2,}", " ", head)
    if len(head) < 2:                       # nothing left worth showing
        head = re.sub(r"\s{2,}", " ", s).strip()
    return head or (name or "")


def _under(rel, folder):
    """Is rel inside folder (or folder itself)?"""
    return rel == folder or rel.startswith(folder + "/")


def rel_dir(path, root):
    """Sub-folder a file sits in, relative to its library root ('' at the top).
    Always '/'-separated so the UI can split it."""
    try:
        rel = Path(path).resolve().parent.relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return ""
    return "" if str(rel) == "." else rel.as_posix()


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


# === OneDrive section: rclone-backed fast browser ===
#
# Same idea as the legacy OneDriveFast.ps1: the sync client is built for many
# small, frequently-edited files (block-level hashing, no per-file parallelism,
# throttled per connection), while rclone does genuine multi-threaded chunked
# downloads, so one big file lands several times faster. Transfers are staged
# in %TEMP% and moved on completion, and land OUTSIDE the OneDrive folder on
# purpose: the sync client owns everything under there and dehydrates a file
# matching the cloud copy straight back to a placeholder.

OD_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
OD_KIND_EXTS = {
    "pdf": {".pdf"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "doc": {".doc", ".docx", ".txt", ".md", ".odt", ".rtf"},
    "sheet": {".xls", ".xlsx", ".csv", ".ods"},
}


def od_find_rclone():
    if RCLONE.exists():
        return str(RCLONE)
    return shutil.which("rclone")


def od_cmd(*args):
    # our own config file, always: the user's rclone setup is never read or
    # written (its onedrive stanza is migrated in once by od_ensure_conf)
    return [od_find_rclone(), "--config", str(RCLONE_CONF), *args]


def od_ensure_rclone(push):
    """Portable rclone into bin/ when the machine has none."""
    if od_find_rclone():
        return True
    push("[od] downloading rclone... (~20 MB, one-time)")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "rclone.zip"
            download_file(RCLONE_ZIP_URL, zpath, push, "rclone")
            with zipfile.ZipFile(zpath) as z:
                exe = next(n for n in z.namelist()
                           if n.rsplit("/", 1)[-1].lower() == "rclone.exe")
                with z.open(exe) as src, open(RCLONE, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 18)
        push("[od] rclone ready")
        return True
    except Exception as e:
        push(f"[od] rclone download failed: {e}")
        return False


def od_conf_remotes(text):
    """{name: stanza} of every [remote] with type = onedrive in a config text."""
    out = {}
    for m in RE_STANZA.finditer(text or ""):
        end = text.find("\n[", m.end())
        body = text[m.end(): end if end != -1 else len(text)]
        if re.search(r"(?im)^\s*type\s*=\s*onedrive\s*$", body):
            out[m.group(1).strip()] = f"[{OD_REMOTE}]" + body.rstrip() + "\n"
    return out


def od_ensure_conf():
    """True when our config has a onedrive remote. Migrates the user's existing
    rclone onedrive stanza (token included) so there is no second sign-in."""
    ours = RCLONE_CONF.read_text(encoding="utf-8", errors="replace") \
        if RCLONE_CONF.exists() else ""
    if od_conf_remotes(ours):
        return True
    if SYSTEM_RCLONE_CONF.exists():
        try:
            theirs = od_conf_remotes(SYSTEM_RCLONE_CONF.read_text(
                encoding="utf-8", errors="replace"))
        except OSError:
            theirs = {}
        if theirs:
            stanza = theirs.get(OD_REMOTE) or next(iter(theirs.values()))
            RCLONE_CONF.write_text(stanza, encoding="utf-8")
            log("[od] migrated the onedrive remote from the existing rclone config")
            return True
    return False


def od_test_remote():
    if not od_find_rclone() or not RCLONE_CONF.exists():
        return False
    try:
        r = subprocess.run(od_cmd("lsjson", f"{OD_REMOTE}:", "--max-depth", "1"),
                           capture_output=True, creationflags=NO_WINDOW, timeout=45)
        return r.returncode == 0
    except Exception:
        return False


def od_lsjson(remote_dir):
    """rclone lsjson -> parsed list, or raises with rclone's own message."""
    r = subprocess.run(od_cmd("lsjson", f"{OD_REMOTE}:{remote_dir}", "--no-modtime"),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=NO_WINDOW, timeout=60)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"rclone exited {r.returncode}")
    return json.loads(r.stdout or "[]")


def od_kind_of(name, mime, is_dir):
    if is_dir:
        return "folder"
    mime = (mime or "").lower()
    ext = Path(name).suffix.lower()
    if mime.startswith("image/") or ext in OD_IMG_EXTS:
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    for kind, exts in OD_KIND_EXTS.items():
        if ext in exts:
            return kind
    return "file"


def od_key_of(remote):
    return hashlib.sha1(remote.encode("utf-8")).hexdigest()[:16]


def od_on_disk(local):
    """Exists as real bytes; a cloud placeholder (offline/recall) doesn't count."""
    try:
        a = ctypes.windll.kernel32.GetFileAttributesW(str(local))
    except Exception:
        return False
    if a in (-1, 0xFFFFFFFF):
        return False
    return not (a & 0x1000 or a & 0x400000)  # OFFLINE | RECALL_ON_DATA_ACCESS


def od_sniff_image(path):
    try:
        head = Path(path).read_bytes()[:16]
    except OSError:
        return "application/octet-stream"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"GIF8":
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


# Image previews ride a tiny loopback server: the UI is one in-memory page, so
# it cannot read file:// images -- and base64-ing whole photos through
# evaluate_js is slow. Bound to 127.0.0.1, random port, token-gated path.
OD_THUMB_TOKEN = os.urandom(8).hex()
_OD_SRV = None


def od_start_thumb_server():
    global _OD_SRV
    if _OD_SRV:
        return f"http://127.0.0.1:{_OD_SRV.server_address[1]}/t/{OD_THUMB_TOKEN}/"
    OD_THUMB_DIR.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            parts = self.path.strip("/").split("/")
            if (len(parts) != 3 or parts[0] != "t" or parts[1] != OD_THUMB_TOKEN
                    or not re.fullmatch(r"[0-9a-f]{16}", parts[2])):
                self.send_error(404)
                return
            try:
                data = (OD_THUMB_DIR / parts[2]).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", od_sniff_image(OD_THUMB_DIR / parts[2]))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)

    _OD_SRV = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    _OD_SRV.daemon_threads = True
    threading.Thread(target=_OD_SRV.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{_OD_SRV.server_address[1]}/t/{OD_THUMB_TOKEN}/"


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


def build_entry(info, target, vid, tab=DOWNLOADS_TAB):
    thumb = (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
             if len(vid) == 11 else (info.get("thumbnail") or ""))
    size = target.stat().st_size
    url = info.get("webpage_url", "")
    src = "yt" if is_youtube(url) or len(vid) == 11 else "web"
    return {
        "id": vid or target.stem,
        "title": info.get("title") or target.stem,
        "channel": info.get("uploader") or info.get("channel") or "",
        "duration": fmt_dur(info["duration"]) if info.get("duration") else "",
        "format": fmt_label(info, target),
        "size": size, "size_h": human_size(size),
        "thumb": thumb, "path": str(target), "ts": time.time(),
        "released": epoch_from_info(info) or 0, "tab": tab, "source": src,
    }


def postprocess(dl_dir, started, api, mark=True, stamp=True, tab=DOWNLOADS_TAB):
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
                entries.append(build_entry(info, target, vid, tab))
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
        self.active_path = ""                      # sub-folder open in that tab
        self._trash = None                         # last removed library, for undo
        migrated = False
        RE_YTID = re.compile(r"\[([a-zA-Z0-9_-]{11})\]")
        for e in self.history:
            # Only assign a tab if the entry has none at all
            if not e.get("tab"):
                e["tab"] = DOWNLOADS_TAB if e.get("source") in ("yt", "web") else "imported"
                migrated = True

            # If it's a YouTube download missing thumbnail, link official thumbnail
            m = RE_YTID.search(str(e.get("path") or e.get("file") or ""))
            if m:
                yid = m.group(1)
                if not e.get("thumb") or e.get("thumb", "").startswith("data:"):
                    e["id"] = yid
                    e["source"] = "yt"
                    e["thumb"] = f"https://i.ytimg.com/vi/{yid}/mqdefault.jpg"
                    migrated = True

            # local titles used to be the raw filename; tidy them once
            if e.get("source") == "local" and not e.get("file"):
                e["file"] = Path(e.get("path") or e.get("title") or "").name
                e["title"] = pretty_title(Path(e["file"]).stem or e.get("title") or "")
                migrated = True

        # ── one-time v1.15.5 migration: fix wrongly-assigned tabs ──
        if self.cfg.get("_mig") != "1.15.5":
            # Build lookup: normalised folder → tab id (longest first)
            tab_dirs = []
            for t in self.tabs:
                tid = t["id"]
                tf = os.path.normpath(self.tab_folder(tid)).lower()
                tab_dirs.append((tf, tid))
            base = os.path.normpath(self.download_dir()).lower()
            tab_dirs.append((os.path.join(base, "youtube"), DOWNLOADS_TAB))
            tab_dirs.append((os.path.join(base, "imported"), "imported"))
            # Also match download_dir root itself → downloads
            tab_dirs.append((base, DOWNLOADS_TAB))
            tab_dirs.sort(key=lambda x: len(x[0]), reverse=True)

            for e in self.history:
                p = e.get("path", "")
                if not p:
                    continue
                p_lower = os.path.normpath(p).lower()
                matched = None
                for tf, tid in tab_dirs:
                    if tf and (p_lower == tf or p_lower.startswith(tf + os.sep)):
                        matched = tid
                        break
                if matched and e.get("tab") != matched:
                    e["tab"] = matched
                    migrated = True
                elif not matched and e.get("tab") == "imported":
                    # Path doesn't match ANY registered folder — shouldn't be in imported
                    imp_folder = os.path.normpath(self.tab_folder("imported")).lower()
                    if not (p_lower == imp_folder or p_lower.startswith(imp_folder + os.sep)):
                        e["tab"] = DOWNLOADS_TAB
                        migrated = True

            self.cfg["_mig"] = "1.15.5"
            save_config(self.cfg)

        if migrated:
            save_history(self.history)

        # ── background: backfill missing channel info for YT entries ──
        missing_ch = [(i, e) for i, e in enumerate(self.history)
                      if e.get("source") == "yt" and not e.get("channel")]
        if missing_ch:
            def _backfill_channels(entries, hist_ref):
                ytdlp = str(BIN / "yt-dlp.exe")
                RE = re.compile(r"\[([a-zA-Z0-9_-]{11})\]")
                changed = False
                for idx, e in entries:
                    m = RE.search(e.get("path") or e.get("file") or "")
                    if not m:
                        continue
                    vid = m.group(1)
                    try:
                        r = subprocess.run(
                            [ytdlp, "--print", "%(uploader)s", "--no-download",
                             "--no-warnings", "-q", f"https://youtu.be/{vid}"],
                            capture_output=True, text=True, timeout=15)
                        ch = (r.stdout or "").strip()
                        if ch:
                            e["channel"] = ch
                            changed = True
                    except Exception:
                        pass
                if changed:
                    save_history(hist_ref)
            threading.Thread(target=_backfill_channels,
                             args=(missing_ch, self.history),
                             daemon=True).start()
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
        self._od = {"opened": False, "connected": False, "tbase": "",
                    "jobs": {}, "q": queue.Queue(), "workers": False,
                    "thumbs": ThreadPoolExecutor(max_workers=3),
                    "thumb_done": set(), "lock": threading.Lock()}
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
        if d and Path(d).exists():
            return d
        default_base = Path.home() / "Downloads" / "YTGrab"
        default_base.mkdir(parents=True, exist_ok=True)
        return str(default_base)

    def subfolder_path(self, category):
        base = Path(self.download_dir())
        if self.cfg.get("use_subfolders", True):
            sub = base / category
            sub.mkdir(parents=True, exist_ok=True)
            return str(sub)
        return str(base)

    def get_settings(self):
        return {
            "download_dir": self.download_dir(),
            "use_subfolders": self.cfg.get("use_subfolders", True),
            "od_streams": self.cfg.get("od_streams", 8),
            "mark_watched": self.cfg.get("mark_watched", True),
            "set_timestamp": self.cfg.get("set_timestamp", True)
        }

    def set_settings(self, download_dir=None, use_subfolders=None, od_streams=None, mark_watched=None, set_timestamp=None):
        if download_dir is not None and Path(download_dir).exists():
            self.cfg["download_dir"] = download_dir
        if use_subfolders is not None:
            self.cfg["use_subfolders"] = bool(use_subfolders)
        if od_streams is not None:
            self.cfg["od_streams"] = int(od_streams)
        if mark_watched is not None:
            self.cfg["mark_watched"] = bool(mark_watched)
        if set_timestamp is not None:
            self.cfg["set_timestamp"] = bool(set_timestamp)
        save_config(self.cfg)
        return self.get_settings()

    def get_state(self):
        return {"dir": self.download_dir(), "logged_in": self.logged_in,
                "deps_ok": YTDLP.exists() and FFMPEG.exists(),
                "default_format": DEFAULT_FORMAT, "busy": self.busy,
                "mark_watched": self.cfg.get("mark_watched", True),
                "set_timestamp": self.cfg.get("set_timestamp", True),
                "use_subfolders": self.cfg.get("use_subfolders", True),
                "views": self.views}

    def pick_folder(self):
        res = UI_WIN.create_file_dialog(webview.FOLDER_DIALOG, directory=self.download_dir())
        if res:
            self.cfg["download_dir"] = res[0]
            save_config(self.cfg)
        return self.download_dir()

    def pick_base_dir(self):
        res = UI_WIN.create_file_dialog(webview.FOLDER_DIALOG, directory=self.download_dir())
        if res:
            self.cfg["download_dir"] = res[0]
            save_config(self.cfg)
            return res[0]
        return self.download_dir()

    def pick_files(self):
        """Keyboard/menu route to the same thing drag & drop does."""
        fd = getattr(webview, "FileDialog", None)   # OPEN_DIALOG is deprecated in 6.x
        res = UI_WIN.create_file_dialog(
            fd.OPEN if fd else webview.OPEN_DIALOG,
            allow_multiple=True, directory=self.subfolder_path("imported"),
            file_types=("Video files (*.mp4;*.mkv;*.webm;*.mov;*.avi;*.m4v;*.flv)",
                        "All files (*.*)"))
        if res:
            self.import_paths(list(res))
        return "ok"

    # --- library tabs ---

    def get_tabs(self):
        return [dict(t, folder=self.tab_folder(t["id"])) for t in self.tabs]

    def site_tab(self, url):
        """(tab_id, folder) a download from this url belongs in."""
        if is_youtube(url):
            return DOWNLOADS_TAB, Path(self.subfolder_path("youtube"))
        host, _ = site_key(url)
        if not host:
            return DOWNLOADS_TAB, Path(self.subfolder_path("youtube"))
        tid = "site-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
        t = next((x for x in self.tabs if x["id"] == tid), None)
        sname = site_name(host)
        if not t:
            folder = Path(self.subfolder_path(sname.lower()))
            t = {"id": tid, "name": sname, "folder": str(folder), "site": host}
            self.tabs.append(t)
            self.cfg["tabs"] = self.tabs
            save_config(self.cfg)
            self._push(f"[tab] new library '{sname}' for {host}")
            if UI_WIN:
                try:
                    UI_WIN.evaluate_js("refreshTabs()")
                except Exception:
                    pass
        folder = Path(t.get("folder") or self.subfolder_path(sname.lower()))
        folder.mkdir(parents=True, exist_ok=True)
        return t["id"], folder

    def tab_folder(self, tid):
        """Folder a tab points at. Blank/built-in falls back to subfolder."""
        if tid == "imported":
            return self.subfolder_path("imported")
        if tid and tid != DOWNLOADS_TAB:
            t = next((x for x in self.tabs if x["id"] == tid), None)
            if t and t.get("folder"):
                return t["folder"]
        return self.subfolder_path("youtube")

    def set_tab(self, tid):
        """JS tells us which tab is showing, so a drop lands in the right folder."""
        self.active_tab = tid or DOWNLOADS_TAB
        self.active_path = ""
        return "ok"

    def set_path(self, rel):
        """Sub-folder currently open, so drops land where the user is looking."""
        self.active_path = (rel or "").strip("/")
        return "ok"

    def drop_dir(self, tid):
        d = Path(self.tab_folder(tid))
        if tid == self.active_tab and self.active_path:
            d = d / self.active_path
        return d

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

    def set_view(self, tid, sort, direction, filt, group=False):
        """Remember how each tab is sorted/filtered -- it's per library, not global."""
        self.views[tid or DOWNLOADS_TAB] = {"sort": sort, "dir": direction,
                                            "filter": filt, "group": bool(group)}
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
        """Drop the whole library and its catalogue entries. Files stay on disk,
        and the removal is kept in memory so it can be undone."""
        t = next((x for x in self.tabs if x["id"] == tid), None)
        if not t or t.get("fixed"):
            self._push("[tab] that library is built in and can't be removed")
            return self.get_tabs()
        removed = [e for e in self.history if e.get("tab") == tid]
        self._trash = {"tab": dict(t), "entries": [dict(e) for e in removed],
                       "at": self.tabs.index(t)}
        self.tabs = [x for x in self.tabs if x["id"] != tid]
        self.cfg["tabs"] = self.tabs
        save_config(self.cfg)
        self.history = [e for e in self.history if e.get("tab") != tid]
        save_history(self.history)
        for e in removed:
            self._drop(e["id"])
        self.active_tab = DOWNLOADS_TAB
        self._push(f"[tab] removed library '{t.get('name')}' "
                   f"({len(removed)} videos de-listed; files kept)")
        return self.get_tabs()

    def undo_remove_tab(self):
        """Put back the last removed library."""
        tr = getattr(self, "_trash", None)
        if not tr:
            return self.get_tabs()
        self._trash = None
        self.tabs.insert(min(tr.get("at", len(self.tabs)), len(self.tabs)), tr["tab"])
        self.cfg["tabs"] = self.tabs
        save_config(self.cfg)
        for e in tr["entries"]:
            self.history.insert(0, e)
        save_history(self.history)
        for e in tr["entries"]:
            self._add_item_from_entry(e)
        self._push(f"[tab] restored '{tr['tab'].get('name')}'")
        return self.get_tabs()

    def _add_item_from_entry(self, e):
        self._item(key=e["id"], status="done", title=e.get("title"),
                   channel=e.get("channel"), duration=e.get("duration"),
                   size=e.get("size_h"), format=e.get("format"),
                   thumb=entry_thumb(e), path=e.get("path"), ts=e.get("ts"),
                   released=e.get("released"), source=e.get("source"),
                   tab=e.get("tab") or DOWNLOADS_TAB,
                   rel=rel_dir(e.get("path", ""),
                               self.tab_folder(e.get("tab") or DOWNLOADS_TAB)))

    def hide_folder(self, tid, rel):
        """Take one sub-folder out of a library's view. Files are untouched and
        re-scans skip it until it's un-hidden."""
        rel = (rel or "").strip("/")
        t = next((x for x in self.tabs if x["id"] == tid), None)
        if not t or not rel:
            return {"tabs": self.get_tabs(), "n": 0}
        ex = [x for x in (t.get("excluded") or []) if x != rel]
        ex.append(rel)
        t["excluded"] = ex
        self.cfg["tabs"] = self.tabs
        save_config(self.cfg)
        root = self.tab_folder(tid)
        gone = [e for e in self.history
                if e.get("tab") == tid and _under(rel_dir(e.get("path", ""), root), rel)]
        self.history = [e for e in self.history if e not in gone]
        save_history(self.history)
        for e in gone:
            self._drop(e["id"])
        self._push(f"[tab] hid '{rel}' ({len(gone)} videos de-listed; files kept)")
        return {"tabs": self.get_tabs(), "n": len(gone)}

    def unhide_all(self, tid):
        t = next((x for x in self.tabs if x["id"] == tid), None)
        if t:
            t["excluded"] = []
            self.cfg["tabs"] = self.tabs
            save_config(self.cfg)
            self._push("[tab] hidden folders restored - re-scanning")
            threading.Thread(target=self._scan_worker, args=(tid,), daemon=True).start()
        return self.get_tabs()

    def scan_tab(self, tid):
        """Index videos sitting in the tab's folder that aren't catalogued yet."""
        threading.Thread(target=self._scan_worker, args=(tid,), daemon=True).start()
        return "started"

    def _scan_worker(self, tid):
        """Walk the whole tree: sub-folders are part of the library too, and
        each video keeps the folder it lives in (see rel_dir)."""
        folder = Path(self.tab_folder(tid))
        known = {(e.get("path") or "").lower() for e in self.history}
        t = next((x for x in self.tabs if x["id"] == tid), None)
        hidden = (t or {}).get("excluded") or []
        try:
            files = sorted(p for p in folder.rglob("*")
                           if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                           and str(p).lower() not in known
                           and not any(s.startswith(".") for s in p.relative_to(folder).parts)
                           and not any(_under(rel_dir(p, folder), h) for h in hidden))
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
        dest = self.drop_dir(tid)
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
            "id": key, "title": pretty_title(target.stem), "file": target.name,
            "channel": "", "source": "local", "tab": tid,
            "duration": fmt_dur(meta["duration"]) if meta.get("duration") else "",
            "format": (f"{h}p " if h else "") + target.suffix.lstrip(".").lower(),
            "size": st.st_size, "size_h": human_size(st.st_size),
            "thumb": "", "thumb_file": str(tf) if tf else "",
            "path": str(target), "ts": time.time(), "released": st.st_mtime,
        })
        return True

    # --- history ---

    def resolve_entry_path(self, e):
        old_path = Path(e.get("path", ""))
        if old_path.exists():
            return str(old_path), True
        
        fname = e.get("file") or old_path.name
        if not fname:
            return str(old_path), False

        tab_dir = Path(self.tab_folder(e.get("tab") or DOWNLOADS_TAB))
        base_dir = Path(self.download_dir())
        old_dir = old_path.parent

        candidates = [
            tab_dir / fname,
            tab_dir / "youtube" / fname,
            tab_dir / "imported" / fname,
            base_dir / fname,
            base_dir / "youtube" / fname,
            base_dir / "imported" / fname,
            old_dir / "youtube" / fname,
            old_dir / "imported" / fname,
        ]
        for cand in candidates:
            if cand.exists():
                e["path"] = str(cand)
                e["file"] = cand.name
                return str(cand), True

        for search_root in (tab_dir, old_dir, base_dir):
            try:
                if search_root.exists():
                    found = next((p for p in search_root.rglob(fname) if p.is_file()), None)
                    if found:
                        e["path"] = str(found)
                        e["file"] = found.name
                        return str(found), True
            except Exception:
                pass

        return str(old_path), False

    def get_history(self):
        migrated = False
        res = []
        for e in self.history:
            path, ok = self.resolve_entry_path(e)
            if ok and path != e.get("_prev_path"):
                migrated = True
            res.append({**e, "thumb": entry_thumb(e),
                        "rel": rel_dir(e.get("path", ""), self.tab_folder(e.get("tab") or DOWNLOADS_TAB)),
                        "exists": ok})
        if migrated:
            save_history(self.history)
        return res

    def purge_missing(self):
        """Remove history entries whose files no longer exist anywhere on disk."""
        before = len(self.history)
        valid = []
        for e in self.history:
            _, ok = self.resolve_entry_path(e)
            if ok:
                valid.append(e)
        self.history = valid
        save_history(self.history)
        self._push(f"[history] removed {before - len(self.history)} missing entry(ies)")
        if UI_WIN:
            try:
                UI_WIN.evaluate_js("refreshView()")
            except Exception:
                pass
        return len(self.history)

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
                   tab=e.get("tab") or DOWNLOADS_TAB,
                   rel=rel_dir(e.get("path", ""),
                               self.tab_folder(e.get("tab") or DOWNLOADS_TAB)))

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

    # --- profile ---

    def get_profile(self):
        """Signed-in sites plus dependency health, for the profile panel."""
        sites = [{"host": "www.youtube.com", "name": "YouTube", "builtin": True,
                  "status": "in" if self.logged_in else "out"}]
        for s in self.cfg.get("sites") or []:
            if "youtube" in s.get("host", ""):
                continue
            sites.append({"host": s["host"], "name": s.get("name") or site_name(s["host"]),
                          "builtin": False, "status": "saved", "when": s.get("when", 0)})
        def dep(p, label):
            return {"name": label, "ok": p.exists(),
                    "where": str(p) if p.exists() else "not installed"}
        deps = [dep(YTDLP, "yt-dlp"), dep(FFMPEG, "ffmpeg"),
                dep(FFPROBE, "ffprobe"), dep(NODE, "node (YouTube JS challenge)"),
                dep(RCLONE, "rclone (OneDrive section)")]
        return {"sites": sites, "deps": deps, "bin": short_path(BIN_DIR)}

    def get_supported(self, refresh=False):
        """yt-dlp's own supported-site list, fetched live and cached."""
        c = fetch_supported(bool(refresh))
        return {"sites": c.get("sites", []), "ts": c.get("ts", 0),
                "count": len(c.get("sites", []))}

    def add_site(self, url):
        """Remember a site and open its login window."""
        u = (url or "").strip()
        if not u:
            return self.get_profile()
        if not re.match(r"^https?://", u, re.I):
            u = "https://" + u
        host, _ = site_key(u)
        if not host:
            self._push("[login] that doesn't look like a website address")
            return self.get_profile()
        sites = [s for s in (self.cfg.get("sites") or []) if s.get("host") != host]
        sites.append({"host": host, "name": site_name(host), "when": time.time()})
        self.cfg["sites"] = sites
        save_config(self.cfg)
        self.login(u)
        return self.get_profile()

    def remove_site(self, host):
        self.cfg["sites"] = [s for s in (self.cfg.get("sites") or [])
                             if s.get("host") != host]
        save_config(self.cfg)
        self._push(f"[login] removed {host} from the signed-in list")
        return self.get_profile()

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
                sites = [s for s in (self.cfg.get("sites") or []) if s.get("host") != host]
                sites.append({"host": host, "name": site_name(host), "when": time.time()})
                self.cfg["sites"] = sites
                save_config(self.cfg)
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

    # --- OneDrive (fast rclone browser) ---

    def od_dest(self):
        d = self.cfg.get("od_dest")
        if d and Path(d).exists():
            return d
        return self.subfolder_path("onedrive")

    def od_local(self, remote):
        return Path(self.od_dest()) / Path(*remote.split("/"))

    def od_pick_dest(self):
        res = UI_WIN.create_file_dialog(webview.FOLDER_DIALOG, directory=self.od_dest())
        if res:
            self.cfg["od_dest"] = res[0]
            save_config(self.cfg)
        return self.od_dest()

    def od_open_dest(self):
        d = Path(self.od_dest())
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d)
        return "ok"

    def od_open(self):
        """First open of the section: rclone + sign-in + the preview server.
        Runs inside the JS promise's thread, so a slow first call (rclone
        download, token refresh) never freezes the window."""
        od = self._od
        if not od["opened"]:
            od["opened"] = True
            od_ensure_rclone(self._push)
            if od_ensure_conf():
                self._push("[od] checking the OneDrive connection...")
                od["connected"] = od_test_remote()
                self._push("[od] connected" if od["connected"] else
                           "[od] sign-in expired - use Connect in the OneDrive section")
            else:
                self._push("[od] no OneDrive sign-in found - use Connect in the OneDrive section")
            od["tbase"] = od_start_thumb_server()
            od["thumb_done"] = {p.name for p in OD_THUMB_DIR.glob("*")
                                if re.fullmatch(r"[0-9a-f]{16}", p.name)}
        return {"connected": od["connected"], "dest": self.od_dest(),
                "tbase": od["tbase"], "rclone": bool(od_find_rclone())}

    def od_reconnect(self):
        threading.Thread(target=self._od_reconnect_worker, daemon=True).start()
        return "started"

    def _od_reconnect_worker(self):
        if not od_find_rclone():
            self._push("[od] rclone isn't installed yet")
            return
        have = od_ensure_conf()
        cmd = [od_find_rclone(), "--config", str(RCLONE_CONF), "config"]
        cmd += (["reconnect", f"{OD_REMOTE}:", "--auto-confirm"] if have
                else ["create", OD_REMOTE, "onedrive", "region=global"])
        self._push("[od] a console window opened - sign in there, it closes itself")
        try:
            subprocess.Popen(cmd, creationflags=NEW_CONSOLE).wait(timeout=600)
        except Exception as e:
            self._push(f"[od] sign-in failed: {e}")
            return
        self._od["connected"] = od_test_remote()
        self._push("[od] signed in" if self._od["connected"]
                   else "[od] still not connected")
        if UI_WIN:
            try:
                UI_WIN.evaluate_js("ui.odstate({connected:%s})"
                                   % ("true" if self._od["connected"] else "false"))
            except Exception:
                pass

    def od_list(self, path):
        path = (path or "").strip("/")
        try:
            raw = od_lsjson(path)
        except Exception as e:
            self._push(f"[od] listing failed: {str(e)[:160]}")
            return {"ok": False, "path": path, "error": str(e)[:200]}
        od = self._od
        entries = []
        for it in raw:
            name = it.get("Name") or ""
            if not name:
                continue
            remote = f"{path}/{name}" if path else name
            is_dir = bool(it.get("IsDir"))
            size = max(int(it.get("Size") or 0), 0)
            kind = od_kind_of(name, it.get("MimeType"), is_dir)
            key = od_key_of(remote)
            job = od["jobs"].get(key)
            e = {"key": key, "name": name, "remote": remote, "isdir": is_dir,
                 "size": size, "size_h": human_size(size), "kind": kind,
                 "ondisk": False if is_dir else od_on_disk(self.od_local(remote)),
                 "job": job["status"] if job and job["status"] in ("queued", "running") else ""}
            if kind in ("image", "video") and 0 < size:
                if kind == "image" and size > OD_THUMB_MAX:
                    pass
                elif key in od["thumb_done"]:
                    e["thumb"] = key
                else:
                    od["thumbs"].submit(self._od_fetch_thumb, remote, key, kind)
            entries.append(e)
        entries.sort(key=lambda e: (not e["isdir"], e["name"].lower()))
        return {"ok": True, "path": path, "entries": entries}

    def _od_fetch_thumb(self, remote, key, kind="image"):
        dest = OD_THUMB_DIR / key
        tmp = dest.with_suffix(".part")
        try:
            if kind == "video":
                sample = dest.with_suffix(".sample")
                cmd = od_cmd("cat", f"{OD_REMOTE}:{remote}", "--head", str(5 * 1024 * 1024))
                with open(sample, "wb") as f:
                    subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.DEVNULL,
                                   creationflags=NO_WINDOW, timeout=60)
                if sample.exists() and sample.stat().st_size > 0:
                    make_thumb(sample, tmp, 0)
                sample.unlink(missing_ok=True)
            else:
                r = subprocess.run(od_cmd("copyto", f"{OD_REMOTE}:{remote}", str(tmp),
                                          "--ignore-times"),
                                   capture_output=True, creationflags=NO_WINDOW, timeout=120)
            if tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(dest)
                self._od["thumb_done"].add(key)
                if UI_WIN:
                    try:
                        UI_WIN.evaluate_js(f"ui.odthumb({json.dumps(key)})")
                    except Exception:
                        pass
            else:
                tmp.unlink(missing_ok=True)
        except Exception:
            tmp.unlink(missing_ok=True)

    def od_download(self, remote, name):
        if not od_find_rclone():
            return "no-rclone"
        od = self._od
        key = od_key_of(remote)
        with od["lock"]:
            j = od["jobs"].get(key)
            if j and j["status"] in ("queued", "running"):
                return "dup"
            od["jobs"][key] = {"status": "queued", "pct": 0, "speed": "", "eta": "",
                               "name": name or Path(remote).name, "remote": remote,
                               "proc": None}
            if not od["workers"]:
                od["workers"] = True
                for _ in range(OD_DL_WORKERS):
                    threading.Thread(target=self._od_dl_loop, daemon=True).start()
            od["q"].put(key)
        self._od_job_push(key, status="queued", name=od["jobs"][key]["name"])
        self._od_strip_push()
        return "queued"

    def _od_job_push(self, key, **kw):
        kw["key"] = key
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.odjob({json.dumps(kw)})")
            except Exception:
                pass

    def _od_dl_loop(self):
        while True:
            key = self._od["q"].get()
            try:
                self._od_dl_one(key)
            except Exception as e:
                self._push(f"[od] download error: {e}")
                j = self._od["jobs"].get(key)
                if j and j["status"] == "running":
                    j["status"] = "error"
                    self._od_job_push(key, status="error", error=str(e)[:160])
            finally:
                self._od["q"].task_done()
                self._od_strip_push()

    def _od_dl_one(self, key):
        j = self._od["jobs"][key]
        if j["status"] == "cancelled":
            return
        remote = j["remote"]
        final = self.od_local(remote)
        stage = OD_STAGE_DIR / (os.urandom(6).hex() + "_" + j["name"])
        OD_STAGE_DIR.mkdir(parents=True, exist_ok=True)
        j["status"] = "running"
        self._od_job_push(key, status="running", pct=0)
        self._od_strip_push()
        cmd = od_cmd("copyto", f"{OD_REMOTE}:{remote}", str(stage),
                     "--multi-thread-streams", str(int(self.cfg.get("od_streams") or 16)),
                     "--stats", "1s", "--stats-one-line", "--stats-log-level", "NOTICE")
        proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", creationflags=NO_WINDOW)
        j["proc"] = proc
        last = 0.0
        err_lines = []
        for line in proc.stderr:
            err_lines.append(line.strip())
            m = RE_OD_STATS.search(line or "")
            if not m:
                continue
            now = time.time()
            if now - last < 0.35:      # paint at ~3 fps, not per stats line
                continue
            last = now
            j["pct"], j["speed"], j["eta"] = int(m.group(1)), m.group(2) + "/s", m.group(3)
            self._od_job_push(key, status="running", pct=j["pct"],
                              speed=j["speed"], eta=j["eta"])
            self._od_strip_push()
        proc.wait()
        j["proc"] = None

        if j["status"] == "cancelled":
            stage.unlink(missing_ok=True)
            return
        if proc.returncode != 0:
            stage.unlink(missing_ok=True)
            j["status"] = "error"
            err_msg = next((l for l in reversed(err_lines) if l and "NOTICE" not in l), f"rclone exited {proc.returncode}")
            self._push(f"[od] {j['name']} failed: {err_msg}")
            self._od_job_push(key, status="error", error=err_msg[:160])
            return

        # staging keeps a half-finished transfer out of the destination
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(final))
        j["pct"], j["status"] = 100, "done"
        self._push(f"[od] saved: {final}")
        self._od_job_push(key, status="done", pct=100, ondisk=True)

    def od_cancel(self, remote):
        key = od_key_of(remote)
        j = self._od["jobs"].get(key)
        if not j or j["status"] not in ("queued", "running"):
            return "none"
        j["status"] = "cancelled"
        p = j.get("proc")
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
        self._od_job_push(key, status="cancelled")
        self._push(f"[od] cancelled: {j['name']}")
        self._od_strip_push()
        return "ok"

    def od_cancel_all(self):
        n = 0
        for remote in [j["remote"] for j in self._od["jobs"].values()
                       if j["status"] in ("queued", "running")]:
            if self.od_cancel(remote) == "ok":
                n += 1
        return n

    def _od_strip_push(self):
        agg = {"active": 0, "queued": 0, "speed": 0.0, "name": "", "pct": 0}
        for j in self._od["jobs"].values():
            if j["status"] == "queued":
                agg["queued"] += 1
            elif j["status"] == "running":
                agg["active"] += 1
                agg["name"] = j["name"]
                agg["pct"] = j["pct"]
                sm = re.match(r"([\d.]+)\s*([KMGT]?)i?B/s", j.get("speed") or "")
                if sm:
                    mul = {"": 1 / 1024, "K": 1 / 1024, "M": 1, "G": 1024,
                           "T": 1024 * 1024}[sm.group(2) or "M"]
                    agg["speed"] += float(sm.group(1)) * mul
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.odstrip({json.dumps(agg)})")
            except Exception:
                pass

    def od_open_local(self, remote):
        p = self.od_local(remote)
        if od_on_disk(p):
            os.startfile(p)
            return "ok"
        return "missing"

    def od_reveal_local(self, remote):
        p = self.od_local(remote)
        if p.exists():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            subprocess.Popen(["explorer", str(p.parent if p.parent.exists()
                                                else self.od_dest())])
        return "ok"

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
        dest_tab, dest_dir = self.site_tab(url)   # non-YouTube sites get their own library
        dl_dir = str(dest_dir)
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
        entries = postprocess(dl_dir, started, self, mark, stamp, dest_tab)
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
#conbtn{margin-left:auto;}
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
.hactx{display:inline-flex;align-items:center;height:24px;padding:0 10px;border:none;
  border-radius:99px;background:var(--s1);color:var(--mut);cursor:pointer;
  font:600 11px/1 inherit;white-space:nowrap;
  transition:background .18s var(--ease),color .18s var(--ease);}
.hactx:hover{background:var(--s3);color:var(--tx);}
.hactx.danger:hover{background:rgba(255,107,107,.14);color:var(--danger);}
#toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,14px);opacity:0;
  pointer-events:none;z-index:40;display:flex;align-items:center;gap:14px;
  padding:12px 16px;border-radius:99px;background:rgba(22,22,30,.97);
  border:1px solid var(--line2);color:var(--tx);font:13px/1 inherit;
  box-shadow:0 16px 40px rgba(0,0,0,.6);backdrop-filter:blur(10px);
  transition:opacity .2s var(--ease),transform .2s var(--ease);}
#toast.on{opacity:1;pointer-events:auto;transform:translate(-50%,0);}
#toast button{border:none;background:transparent;color:#FF9CC4;cursor:pointer;
  font:700 12.5px/1 inherit;padding:2px 4px;}
#toast button:hover{text-decoration:underline;}
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
.gtoggle{display:none;align-items:center;gap:7px;height:33px;padding:0 12px;border-radius:99px;
  border:1px solid var(--line2);background:var(--s1);color:var(--mut);cursor:pointer;
  font:600 11.5px/1 inherit;white-space:nowrap;transition:color .18s var(--ease),border-color .18s var(--ease);}
.gtoggle.on{display:inline-flex;}
.gtoggle:hover{color:var(--tx);}
.gtoggle input{width:15px;height:15px;accent-color:var(--ac);cursor:pointer;flex:none;}
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

/* ---------- folder cards: same footprint as a video card ---------- */
.fc{position:relative;border-radius:var(--r-lg);overflow:hidden;background:var(--s1);
  border:1px solid var(--line);cursor:pointer;text-align:left;padding:0;color:inherit;
  font:inherit;display:block;width:100%;
  transition:transform .22s var(--ease),border-color .22s var(--ease),box-shadow .22s var(--ease);}
.fc:hover{border-color:var(--line2);transform:translateY(-3px);box-shadow:0 14px 34px rgba(0,0,0,.55);}
.fth{position:relative;aspect-ratio:16/9;background:var(--s3);overflow:hidden;
  display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:2px;}
.fth.one{grid-template-columns:1fr;grid-template-rows:1fr;}
.fth img{width:100%;height:100%;object-fit:cover;transition:transform .35s var(--ease);}
.fc:hover .fth img{transform:scale(1.05);}
.fth .fempty{grid-column:1/-1;grid-row:1/-1;display:flex;align-items:center;justify-content:center;
  color:#33333F;}
.fth::after{content:"";position:absolute;inset:auto 0 0 0;height:46%;pointer-events:none;
  background:linear-gradient(to top,rgba(4,4,8,.8),transparent);}
.fbadge{position:absolute;z-index:2;bottom:8px;right:8px;display:inline-flex;align-items:center;
  gap:5px;font-size:10.5px;font-weight:650;background:rgba(4,4,8,.72);color:#EAEAF2;
  padding:3px 8px;border-radius:6px;backdrop-filter:blur(6px);font-variant-numeric:tabular-nums;}
.fkind{position:absolute;z-index:2;top:8px;left:8px;width:26px;height:26px;border-radius:8px;
  display:flex;align-items:center;justify-content:center;background:rgba(4,4,8,.66);
  color:var(--ac);backdrop-filter:blur(6px);}
.crumb{display:inline-flex;align-items:center;gap:4px;}
.crumb button{border:none;background:transparent;color:var(--mut);cursor:pointer;
  font:12px/1 inherit;padding:3px 5px;border-radius:6px;}
.crumb button:hover{background:var(--s2);color:var(--tx);}
.crumb button.here{color:var(--tx);font-weight:640;cursor:default;}
.crumb .sep{color:var(--dim);opacity:.6;font-size:11px;}

/* ================= grid ================= */
#grid,#odgrid{flex:1;min-height:0;overflow-y:auto;display:grid;
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
/* ---------- profile panel ---------- */
#profscrim{position:fixed;inset:0;background:rgba(4,4,9,.74);opacity:0;pointer-events:none;
  transition:opacity .2s var(--ease);z-index:21;backdrop-filter:blur(3px);}
#profscrim.open{opacity:1;pointer-events:auto;}
#prof{position:fixed;left:50%;top:50%;transform:translate(-50%,-48%) scale(.98);opacity:0;
  pointer-events:none;width:min(540px,94vw);max-height:86vh;overflow:hidden;background:var(--s2);
  border:1px solid var(--line2);border-radius:var(--r-xl);padding:22px;z-index:22;
  display:flex;flex-direction:column;gap:18px;
  transition:opacity .2s var(--ease),transform .2s var(--ease);box-shadow:0 30px 76px rgba(0,0,0,.66);}
#prof.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.ppane{display:flex;flex-direction:column;gap:18px;overflow-y:auto;min-height:0;}
#sitespane{display:none;}
#prof.sites #mainpane{display:none;}
#prof.sites #sitespane{display:flex;}
.sitehead{display:flex;align-items:center;gap:9px;}
.backb{display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 11px;border:none;
  border-radius:99px;background:var(--s1);color:var(--mut);cursor:pointer;font:600 12px/1 inherit;
  transition:background .18s var(--ease),color .18s var(--ease);}
.backb:hover{background:var(--s3);color:var(--tx);}
#ssearch{flex:1;min-width:0;height:34px;border:1px solid var(--line2);border-radius:99px;
  background:var(--s1);color:var(--tx);padding:0 14px;font:12.5px/1 inherit;}
#ssearch:focus{outline:none;border-color:var(--ac);}
#ssearch::placeholder{color:var(--dim);}
#sslist{display:flex;flex-direction:column;gap:3px;overflow-y:auto;min-height:0;flex:1;}
.ssrow{display:flex;align-items:center;gap:9px;padding:8px 11px;border-radius:var(--r-sm);
  background:var(--s1);border:1px solid transparent;}
.ssrow:hover{border-color:var(--line);}
.ssnm{font:600 12.5px/1.3 inherit;color:var(--tx);white-space:nowrap;}
.ssnote{flex:1;min-width:0;font-size:11px;color:var(--dim);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;}
.ssbroken{font-size:10px;font-weight:700;color:var(--warn);background:rgba(247,185,85,.13);
  padding:2px 7px;border-radius:99px;white-space:nowrap;}
.ssfoot{display:flex;align-items:center;gap:8px;}
.phead{display:flex;align-items:center;gap:8px;}
.phead h3{margin:0;font-size:17px;font-weight:680;letter-spacing:-.3px;}
.sitelist{display:flex;flex-direction:column;gap:6px;}
.srow{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:var(--r-md);
  border:1px solid var(--line);background:var(--s1);}
.srow .sdot{width:7px;height:7px;border-radius:50%;background:var(--dim);flex:none;}
.srow.in .sdot{background:var(--ok);box-shadow:0 0 7px rgba(61,220,151,.6);}
.srow.out .sdot{background:var(--warn);}
.srow .snm{flex:1;min-width:0;font:600 13px/1.3 inherit;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.srow .sst{font-size:11px;color:var(--mut);white-space:nowrap;}
.srow .sact{display:flex;gap:4px;}
.openb{display:inline-flex;align-items:center;gap:5px;height:26px;padding:0 10px;border:none;
  border-radius:99px;background:var(--s3);color:var(--mut);cursor:pointer;font:600 11px/1 inherit;
  transition:background .18s var(--ease),color .18s var(--ease);}
.openb:hover{background:var(--ac-soft);color:#FF9CC4;}
.sbtn2{width:26px;height:26px;border:none;border-radius:7px;background:transparent;color:var(--dim);
  display:flex;align-items:center;justify-content:center;cursor:pointer;
  transition:background .18s var(--ease),color .18s var(--ease);}
.sbtn2:hover{background:var(--s3);color:var(--tx);}
.sbtn2.danger:hover{color:var(--danger);}
.addsite{display:flex;gap:7px;margin-top:3px;}
#newsite{flex:1;min-width:0;height:38px;border:1px solid var(--line2);border-radius:var(--r-md);
  background:var(--s1);color:var(--tx);padding:0 13px;font:13px/1 inherit;}
#newsite:focus{outline:none;border-color:var(--ac);}
#newsite::placeholder{color:var(--dim);}
.tbtn.solid{background:linear-gradient(135deg,var(--ac),var(--ac2));color:#fff;height:38px;
  padding:0 16px;border-radius:var(--r-md);}
.tbtn.solid:hover{filter:brightness(1.1);background:linear-gradient(135deg,var(--ac),var(--ac2));}
.depfoot{display:flex;align-items:center;gap:8px;margin-top:3px;}
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
/* ================= OneDrive section ================= */
#odtab{margin-bottom:12px;}
#odview{display:none;flex:1;min-height:0;flex-direction:column;}
body.cloud #odview{display:flex;}
body.cloud #grid,body.cloud .libhead{display:none;}
body.cloud .urlwrap,body.cloud #dl,body.cloud #importbtn{display:none;}
#odgrid{padding-bottom:96px;}
.odnav{display:flex;align-items:center;gap:12px;padding:12px 22px 8px;flex:none;min-width:0;}
#odcrumb{flex:1;min-width:0;display:flex;align-items:center;gap:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.subcount{font-size:12px;color:var(--dim);white-space:nowrap;flex:none;margin-left:auto;}
#q,#odq{width:180px;height:34px;border:1px solid var(--line2);border-radius:99px;background:var(--s1);
  color:var(--tx);padding:0 12px 0 32px;font:12.5px/1 inherit;transition:border-color .18s var(--ease);}
#q:focus,#odq:focus{outline:none;border-color:var(--ac);}
#q::placeholder,#odq::placeholder{color:var(--dim);}
.odsub{display:flex;align-items:center;gap:10px;padding:0 22px 14px;flex:none;flex-wrap:wrap;}
.destpill{display:inline-flex;align-items:center;gap:7px;height:32px;padding:0 14px;border-radius:99px;
  border:1px solid var(--line2);background:var(--s1);color:var(--mut);font:600 11.5px/1 inherit;
  cursor:pointer;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  transition:border-color .18s var(--ease),color .18s var(--ease),background .18s var(--ease);}
.destpill:hover{border-color:var(--ac);color:var(--tx);background:var(--s2);}
.hactx{height:32px;padding:0 14px;border-radius:99px;border:1px solid var(--line2);
  background:var(--s1);color:var(--mut);font:600 11.5px/1 inherit;cursor:pointer;
  display:inline-flex;align-items:center;gap:7px;transition:background .18s var(--ease),color .18s var(--ease),border-color .18s var(--ease);}
.hactx:hover{background:var(--s3);color:var(--tx);border-color:var(--line2);}
.oddot{width:8px;height:8px;border-radius:50%;background:var(--warn);flex:none;display:inline-block;margin-right:2px;}
.oddot.ok{background:var(--ok);box-shadow:0 0 8px rgba(61,220,151,.6);}
.subdot{color:var(--dim);opacity:.5;font-size:12px;}
/* ---------- settings modal ---------- */
#settingsscrim{position:fixed;inset:0;background:rgba(4,4,9,.74);opacity:0;pointer-events:none;
  transition:opacity .2s var(--ease);z-index:21;backdrop-filter:blur(3px);}
#settingsscrim.open{opacity:1;pointer-events:auto;}
#settingsdialog{position:fixed;left:50%;top:50%;transform:translate(-50%,-48%) scale(.98);opacity:0;
  pointer-events:none;width:min(520px,94vw);max-height:86vh;overflow-y:auto;background:var(--s2);
  border:1px solid var(--line2);border-radius:var(--r-xl);padding:22px;z-index:22;
  display:flex;flex-direction:column;gap:18px;
  transition:opacity .2s var(--ease),transform .2s var(--ease);box-shadow:0 30px 76px rgba(0,0,0,.66);}
#settingsdialog.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.dlbtn{margin-left:auto;display:inline-flex;align-items:center;gap:5px;height:24px;padding:0 10px;
  border:none;border-radius:99px;background:linear-gradient(135deg,var(--ac),var(--ac2));color:#fff;
  cursor:pointer;font:650 11px/1 inherit;flex:none;transition:filter .15s var(--ease);}
.dlbtn:hover{filter:brightness(1.12);}
.dlbtn.ghost{background:var(--s4);color:var(--mut);cursor:default;}
.dlbtn.ghost.ok{background:rgba(61,220,151,.16);color:#7CF0C4;}
.dlbtn.retry{background:rgba(255,107,107,.16);color:#FDA4A4;cursor:pointer;}
#odstrip{position:fixed;left:var(--side);right:0;bottom:0;z-index:14;display:none;align-items:center;
  gap:18px;padding:12px 22px;background:rgba(12,12,18,.97);border-top:1px solid var(--line2);
  box-shadow:0 -16px 40px rgba(0,0,0,.5);backdrop-filter:blur(12px);}
#odstrip.on{display:flex;}
.odsp{font:650 28px/1 "Cascadia Mono",Consolas,monospace;color:#FF9CC4;min-width:120px;}
.odsp small{display:block;font:600 10px/1 inherit;color:var(--dim);margin-top:3px;letter-spacing:.6px;}
.odmid{flex:1;min-width:0;}
.odname{font-size:12.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.odbar{height:5px;border-radius:99px;background:var(--s3);margin:7px 0 5px;overflow:hidden;}
.odbar i{display:block;height:100%;width:0;border-radius:99px;
  background:linear-gradient(90deg,var(--ac),var(--ac2));transition:width .3s var(--ease);}
.odmeta{font:11px/1 "Cascadia Mono",Consolas,monospace;color:var(--dim);}
.gc.dir{cursor:pointer;}
.gbadge.on{background:rgba(61,220,151,.22);color:#7CF0C4;}
.empty .spin{width:22px;height:22px;border:3px solid var(--line2);border-top-color:var(--ac);
  border-radius:50%;animation:spin .8s linear infinite;}
@media (max-width:900px){#odstrip{left:60px;}}

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
  <button class="lt" id="odtab" role="tab" aria-selected="false" onclick="switchCloud()"
          title="Browse OneDrive and download at full speed (rclone)">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 19a4.5 4.5 0 0 0 .42-8.98 6.5 6.5 0 0 0-12.7 1.48A4 4 0 0 0 6 19z"/><path d="M12 12v5"/><path d="m9.5 14.5 2.5 2.5 2.5-2.5"/></svg>
    <span class="ltname">OneDrive</span>
  </button>
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
    <button class="ib" id="settingsbtn" onclick="openSettings()" title="Settings — download folders & options" aria-label="Settings">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    </button>
    <button class="ib" id="loginbtn" title="Profile — signed-in sites and dependencies"
            aria-label="Profile" onclick="openProf()">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-3.5 3.6-6 8-6s8 2.5 8 6"/></svg>
      <span class="idot" id="authdot"></span>
    </button>
  </header>

  <section class="libhead">
    <div>
      <div class="libttl">
        <span class="hicon" id="libicon"></span>
        <h2 id="libtitle">YT Downloads</h2>
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
      <label class="gtoggle" id="grouptoggle" title="Group these videos by channel">
        <input type="checkbox" id="ck-group" onchange="toggleGroup(this.checked)">
        <span>Group by channel</span>
      </label>
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
      <button id="purgemissing" class="ib" title="Clean up missing files" aria-label="Clean up missing files" onclick="purgeMissingClick()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
      </button>
    </div>
  </section>

  <div id="grid">
    <div class="empty" id="empty">
      <span class="emptyic" id="empty-ic"></span>
      <b id="empty-t">No downloads yet</b>
      <span id="empty-s">Paste a link above and your videos appear here</span>
    </div>
  </div>

  <div id="odview">
    <section class="odnav">
      <button class="hact" id="odup" title="Up one folder (Backspace)" aria-label="Up one folder"
              onclick="odUp()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
          stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V5"/><path d="m6 11 6-6 6 6"/></svg>
      </button>
      <nav class="crumb" id="odcrumb" aria-label="OneDrive location"></nav>
      <span class="sp"></span>
      <span class="subcount" id="odcount"></span>
      <div class="searchwrap">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input type="text" id="odq" placeholder="Filter this folder" spellcheck="false"
               aria-label="Filter this folder" oninput="odRender()">
      </div>
      <button class="hact" title="Refresh (F5)" aria-label="Refresh OneDrive folder" onclick="odGo(odPath,true)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
          stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>
      </button>
    </section>
    <section class="odsub">
      <button class="fpath destpill" id="oddestpill" onclick="odPickDest()"
              title="OneDrive downloads land here - click to change">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round"><path d="M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2z"/></svg>
        <span id="oddest">&hellip;</span>
      </button>
      <button class="hactx" onclick="pywebview.api.od_open_dest()">Open folder</button>
      <span class="subdot">&middot;</span>
      <button class="hactx" id="odconn" onclick="odReconnect()">
        <span class="oddot" id="odconndot"></span><span id="odconntxt">Connect</span>
      </button>
    </section>
    <div id="odgrid"></div>
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

<div id="toast" role="status" aria-live="polite"></div>

<div id="odstrip" role="status">
  <div class="odsp"><span id="odssp">--</span><small>MB/s</small></div>
  <div class="odmid">
    <div class="odname" id="odsname">&hellip;</div>
    <div class="odbar"><i id="odbari"></i></div>
    <div class="odmeta" id="odsmeta"></div>
  </div>
  <button class="hactx" onclick="pywebview.api.od_cancel_all()">Cancel all</button>
</div>

<div id="drop">
  <div class="dropcard">
    <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
      stroke-linecap="round" stroke-linejoin="round"><path d="M12 15V3"/><path d="m8 7 4-4 4 4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
    <b>Drop videos to add them</b>
    <span id="dropto">They move to your Imported folder and appear under that library</span>
  </div>
</div>

<div id="profscrim" onclick="closeProf()"></div>
<div id="prof" role="dialog" aria-modal="true" aria-label="Profile">
  <div class="phead">
    <h3>Profile</h3><span class="sp"></span>
    <button class="cx" onclick="closeProf()" aria-label="Close">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
    </button>
  </div>
  <div class="ppane" id="mainpane">
    <div class="field">
      <span class="slabel">SIGNED-IN SITES</span>
      <div id="sitelist" class="sitelist"></div>
      <div class="addsite">
        <input type="text" id="newsite" placeholder="Add a site, e.g. hotstar.com" spellcheck="false"
               aria-label="Website to sign into">
        <button class="tbtn solid" onclick="addSite()">Sign in</button>
      </div>
      <div class="depfoot">
        <span class="msub">Not sure if a site works?</span><span class="sp"></span>
        <button class="tbtn" onclick="openSites()">Supported websites</button>
      </div>
    </div>
    <div class="field">
      <span class="slabel">DEPENDENCIES</span>
      <div id="deplist" class="sitelist"></div>
      <div class="depfoot">
        <span id="depbin" class="msub"></span><span class="sp"></span>
        <button class="tbtn" onclick="pywebview.api.update_deps();setTimeout(loadProfile,1200)">Check for updates</button>
      </div>
    </div>
  </div>

  <div class="ppane" id="sitespane">
    <div class="sitehead">
      <button class="backb" onclick="closeSites()">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
          stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>Back
      </button>
      <input type="text" id="ssearch" placeholder="Search supported websites" spellcheck="false"
             aria-label="Search supported websites" oninput="renderSites()">
    </div>
    <div id="sslist"></div>
    <div class="ssfoot">
      <span id="ssmeta" class="msub"></span><span class="sp"></span>
      <button class="tbtn" onclick="loadSites(true)">Refresh list</button>
    </div>
  </div>
</div>

<div id="settingsscrim" onclick="closeSettings()"></div>
<div id="settingsdialog" role="dialog" aria-modal="true" aria-labelledby="stitle">
  <div class="phead">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    <h3 id="stitle">Settings</h3><span class="sp"></span>
    <button class="cx" onclick="closeSettings()" aria-label="Close">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
    </button>
  </div>
  <div class="field">
    <span class="slabel">DEFAULT DOWNLOAD LOCATION</span>
    <div style="display:flex;gap:8px;align-items:center;">
      <input type="text" id="set-folder" readonly style="flex:1;height:38px;border:1px solid var(--line2);border-radius:var(--r-md);background:var(--s1);color:var(--tx);padding:0 13px;font:12.5px/1 inherit;">
      <button class="backb" onclick="pickSettingsFolder()">Browse…</button>
    </div>
  </div>
  <div class="field">
    <span class="slabel">AUTOMATIC SUBFOLDERS</span>
    <label class="ck" style="background:var(--s1);border:1px solid var(--line2);border-radius:var(--r-md);padding:12px;">
      <input type="checkbox" id="set-subfolders" checked>
      <div>
        <div style="font-weight:600;color:var(--tx);">Create dedicated category subfolders</div>
        <div style="font-size:11px;color:var(--dim);margin-top:2px;">Organizes downloads into youtube/, hotstar/, imported/, and onedrive/</div>
      </div>
    </label>
  </div>
  <div class="field">
    <span class="slabel">ONEDRIVE PARALLEL STREAMS</span>
    <select id="set-streams" style="height:38px;border:1px solid var(--line2);border-radius:var(--r-md);background:var(--s1);color:var(--tx);padding:0 13px;font:12.5px/1 inherit;">
      <option value="4">4 streams</option>
      <option value="8" selected>8 streams (Recommended for high speed)</option>
      <option value="16">16 streams (Maximum speed)</option>
    </select>
  </div>
  <div class="sact" style="margin-top:8px;justify-content:flex-end;">
    <button class="tbtn" onclick="closeSettings()">Cancel</button>
    <button class="tbtn solid" onclick="saveSettingsModal()">Save Settings</button>
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
 edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
 login:'<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="m10 17 5-5-5-5"/><path d="M15 12H3"/>',
 user:'<circle cx="12" cy="8" r="4"/><path d="M4 21c0-3.5 3.6-6 8-6s8 2.5 8 6"/>',
 external:'<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6"/>'
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
var activeTab="downloads",curPath="",curChannel="",folderSig="",dlFolder="";
var allTabs=[{id:"downloads",name:"YT Downloads",builtin:true}];
function tabOf(c){return c.tab||"downloads";}
function tabById(id){
  for(var i=0;i<allTabs.length;i++)if(allTabs[i].id===id)return allTabs[i];
  return allTabs[0];
}
function renderTabs(list){
  allTabs=[{id:"downloads",name:"YT Downloads",builtin:true}].concat(list||[]);
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
  odMode=false;document.body.classList.remove("cloud");
  var ot=document.getElementById("odtab");
  if(ot){ot.classList.remove("on");ot.setAttribute("aria-selected","false");}
  activeTab=id;curPath="";curChannel="";folderSig="";
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
  var root=t.builtin?dlFolder:(t.folder||"");
  var path=curPath?root+"/"+curPath:root;
  var p=document.createElement("span");
  p.className="fpath";p.title=path;
  p.innerHTML=ic("folder",12)+"<span>"+esc(path)+"</span>";
  sub.appendChild(p);
  if(curChannel){                   // breadcrumb back out of a channel
    var cc=document.createElement("span");cc.className="crumb";
    var cb=document.createElement("button");
    cb.textContent=t.name;cb.onclick=function(){goChannel("");};
    cc.appendChild(cb);
    var cs=document.createElement("span");cs.className="sep";cs.textContent="/";
    cc.appendChild(cs);
    var cn=document.createElement("button");
    cn.textContent=curChannel;cn.className="here";
    cc.appendChild(cn);
    sub.appendChild(cc);
  }
  if(curPath){                      // breadcrumb back to the library root
    var cr=document.createElement("span");cr.className="crumb";
    var segs=curPath.split("/"),html=[];
    var rootb=document.createElement("button");
    rootb.textContent=t.name;rootb.onclick=function(){goPath("");};
    cr.appendChild(rootb);
    segs.forEach(function(s,i){
      var sep=document.createElement("span");sep.className="sep";sep.textContent="/";
      cr.appendChild(sep);
      var b=document.createElement("button");
      b.textContent=s;
      if(i===segs.length-1){b.className="here";}
      else{var to=segs.slice(0,i+1).join("/");b.onclick=function(){goPath(to);};}
      cr.appendChild(b);
    });
    sub.appendChild(cr);
  }
  // actions belong to whatever you're actually looking at
  if(t.builtin&&!curPath){
    sub.appendChild(hbtn("edit","Change download folder",pickDir));
  }else if(curPath){
    sub.appendChild(txtbtn("Hide this folder",
      "Take "+curPath+" out of this library (the files stay on disk)",
      function(){hideFolder(curPath);},"danger"));
  }else if(!t.builtin){
    sub.appendChild(hbtn("retry","Re-scan this folder for new videos",function(){
      pywebview.api.scan_tab(t.id);}));
    if(!t.fixed)sub.appendChild(txtbtn("Remove library",
      "Remove the whole "+t.name+" library from YTGrab (the files stay on disk)",
      removeTab,"danger"));
    var hid=(t.excluded||[]).length;
    if(hid)sub.appendChild(txtbtn(hid+" hidden","Show hidden folders again",function(){
      pywebview.api.unhide_all(t.id).then(function(list){renderTabs(list);renderSub();});}));
  }
  var d=document.createElement("span");d.className="subdot";d.textContent="·";
  var c=document.createElement("span");c.className="subcount";c.id="subcount";
  sub.appendChild(d);sub.appendChild(c);
}
function txtbtn(label,title,fn,cls){
  var b=document.createElement("button");
  b.className="hactx "+(cls||"");b.title=title;b.textContent=label;b.onclick=fn;
  return b;
}
function hideFolder(rel){
  var t=tabById(activeTab);
  if(!confirm('Hide "'+rel+'" from the '+t.name+" library?\n\n"+
              "It disappears from YTGrab only — the folder and its files stay on disk. "+
              'You can bring it back with "hidden" next to the library name.'))return;
  pywebview.api.hide_folder(t.id,rel).then(function(r){
    renderTabs((r.tabs||[]).slice(1));
    goPath(rel.indexOf("/")!==-1?rel.slice(0,rel.lastIndexOf("/")):"");
    toast("Hid "+rel+" ("+r.n+" videos de-listed)");
  });
}
/* brief message with an optional action, for things that need an undo */
var toastT=null;
function toast(msg,actionLabel,action){
  var el=document.getElementById("toast");
  el.innerHTML="";
  var s=document.createElement("span");s.textContent=msg;el.appendChild(s);
  if(actionLabel){
    var b=document.createElement("button");
    b.textContent=actionLabel;
    b.onclick=function(){el.classList.remove("on");action();};
    el.appendChild(b);
  }
  el.classList.add("on");
  clearTimeout(toastT);
  toastT=setTimeout(function(){el.classList.remove("on");},actionLabel?9000:4000);
}
function hbtn(icon,title,fn,cls){
  var b=document.createElement("button");
  b.className="hact "+(cls||"");b.title=title;b.setAttribute("aria-label",title);
  b.innerHTML=ic(icon,13);b.onclick=fn;return b;
}
/* Python calls this when a download creates a site library on the fly */
function refreshTabs(){
  pywebview.api.get_tabs().then(function(list){renderTabs(list);refreshView();});
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
  if(!confirm('Remove the whole "'+t.name+'" library?\n\n'+
              "Every video in it is de-listed from YTGrab (the files stay in "+t.folder+").\n\n"+
              'To hide just one folder, open that folder and use "Hide this folder" instead.'))return;
  pywebview.api.remove_tab(t.id).then(function(list){
    activeTab="downloads";renderTabs(list);switchTab("downloads");
    toast('Removed the "'+t.name+'" library',"Undo",function(){
      pywebview.api.undo_remove_tab().then(function(l){
        renderTabs(l);switchTab(t.id);
      });
    });
  });
}

/* ================= per-library view state ================= */
var views={},curSort="released",curDir=-1,curFilter="all",curQuery="",groupOn=false;
var NAT={ts:-1,released:-1,title:1,size:-1};
function loadView(tid){
  var v=views[tid]||{};
  curSort=v.sort||"released";
  curDir=(v.dir===1||v.dir===-1)?v.dir:(NAT[curSort]||-1);
  curFilter=v.filter||"all";
  groupOn=!!v.group;
  document.getElementById("ck-group").checked=groupOn;
  document.getElementById("grouptoggle").classList.toggle("on",tabById(tid).builtin);
  curQuery="";
  document.getElementById("q").value="";
  document.getElementById("sortsel").value=curSort;
  updDirIcon();syncChips();
  sortGrid(curSort);
}
function saveView(){
  views[activeTab]={sort:curSort,dir:curDir,filter:curFilter,group:groupOn};
  try{pywebview.api.set_view(activeTab,curSort,curDir,curFilter,groupOn);}catch(e){}
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
function toggleGroup(on){
  groupOn=!!on;curChannel="";folderSig="";
  sortGrid(curSort);refreshView();saveView();
}
function pickSort(mode){curSort=mode;curDir=NAT[mode]||-1;updDirIcon();sortGrid(mode);saveView();}
function flipDir(){curDir=-curDir;updDirIcon();sortGrid(curSort);saveView();}
function purgeMissingClick(){
  pywebview.api.purge_missing().then(function(c){
    toast("Cleaned up missing entries");
    refreshTabs();
  });
}
function pickFilter(f){curFilter=f;syncChips();refreshView();saveView();}
function pickQuery(v){curQuery=(v||"").trim().toLowerCase();refreshView();}

function bucket(c){
  if(c.status==="failed")return "failed";
  if(ACTIVE[c.status])return "active";
  return "done";
}
function relOf(c){return c.rel||"";}
function chanOf(c){return (c.channel||"").trim()||"Unknown channel";}
function inThisFolder(c){return relOf(c)===curPath;}
function underCurrent(r){
  if(!curPath)return r;
  if(r===curPath)return "";
  return r.indexOf(curPath+"/")===0?r.slice(curPath.length+1):null;
}
function goPath(rel){
  curPath=rel||"";curChannel="";
  try{pywebview.api.set_path(curPath);}catch(e){}
  renderSub();refreshView();
}
function goChannel(name){curChannel=name||"";renderSub();refreshView();}
/* release junk out of folder names, mirroring pretty_title() on the Python side */
var RE_CUT=/\b(?:\d{3,4}p|4k|uhd|hdr10\+?|hdr|sdr|10bit|8bit|web[- ]?dl|web[- ]?rip|webrip|blu[- ]?ray|bluray|b[rd]rip|hdrip|dvdrip|hdtv|hdcam|camrip|x26[45]|h\.?26[45]|hevc|avc1?|xvid|divx|aac|ac3|eac3|ddp?\d|dts(?:[- ]?hd)?|atmos|truehd|dual[- ]?audio|multi(?:sub)?|esubs?|msubs?|repack|proper|remastered|amzn|dsnp|nf|hmax|sonyliv|zee5|jio|yts|yify|rarbg|psa|galaxyrg|moviesleech|tgx|ettv|eztv|hq|hd)\b/i;
function prettyName(v){
  var s=String(v||"").replace(/[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]/g," $1 ")
                     .replace(/[\[\(\{][^\]\)\}]*[\]\)\}]/g," ")
                     .replace(/[_.]/g," ");
  var m=RE_CUT.exec(s);
  var head=(m?s.slice(0,m.index):s).replace(/[\s\-–—_]+$/,"").trim().replace(/\s{2,}/g," ");
  return head.length>1?head:String(v||"").replace(/\s{2,}/g," ").trim();
}
/* one card type for both sub-folders and channel groups, sized like a video card */
function makeFolderCard(label, raw, count, thumbs, kind, onOpen){
  var el=document.createElement("button");
  el.className="fc";el.title=raw||label;
  var pics=(thumbs||[]).slice(0,4);
  var body=pics.length
    ? pics.map(function(t){return '<img src="'+esc(t)+'" alt="">';}).join("")
    : '<span class="fempty">'+ic(kind==="channel"?"user":"folder",30)+"</span>";
  el.innerHTML='<div class="fth'+(pics.length===1?" one":"")+'">'+body+
      '<span class="fkind">'+ic(kind==="channel"?"user":"folder",14)+"</span>"+
      '<span class="fbadge">'+count+(count===1?" video":" videos")+"</span></div>"+
      '<div class="gm"><div class="gt">'+esc(label)+"</div>"+
      '<div class="gs">'+ic(kind==="channel"?"user":"folder",13)+"<span>"+
      esc(kind==="channel"?"Channel":"Folder")+"</span></div></div>";
  el.onclick=onOpen;
  return el;
}
/* sub-folders normally; channels when the library is grouped by channel */
function renderFolders(){
  var groups=[],t=tabById(activeTab);
  var byChannel=groupOn&&t.builtin;
  if(!curQuery&&(byChannel?!curChannel:true)){
    var acc={};
    for(var k in cards){
      var c=cards[k];
      if(tabOf(c)!==activeTab)continue;
      var key;
      if(byChannel){
        if(c.status!=="done")continue;         // in-flight items stay as cards
        key=chanOf(c);
      }else{
        var rest=underCurrent(relOf(c));
        if(rest===null||rest==="")continue;
        key=rest.split("/")[0];
      }
      var g=acc[key]||(acc[key]={n:0,thumbs:[]});
      g.n++;
      if(c.thumb&&g.thumbs.length<4)g.thumbs.push(c.thumb);
    }
    groups=Object.keys(acc).sort(function(a,b){return a.localeCompare(b);})
      .map(function(name){return {name:name,n:acc[name].n,thumbs:acc[name].thumbs};});
  }
  var sig=(byChannel?"c|":"f|")+curPath+"|"+curChannel+"|"+
          groups.map(function(g){return g.name+":"+g.n;}).join(",");
  if(sig===folderSig)return;                    // nothing changed, keep the DOM
  folderSig=sig;
  var old=gridEl.querySelectorAll(".fc");
  for(var i=0;i<old.length;i++)old[i].remove();
  for(var j=groups.length-1;j>=0;j--){
    (function(g){
      var el=makeFolderCard(byChannel?g.name:prettyName(g.name),g.name,g.n,g.thumbs,
        byChannel?"channel":"folder",
        byChannel?function(){goChannel(g.name);}
                : function(){goPath(curPath?curPath+"/"+g.name:g.name);});
      gridEl.insertBefore(el,gridEl.firstChild);
    })(groups[j]);
  }
}
function refreshView(){
  var n={all:0,active:0,done:0,failed:0},shown=0,per={};
  var nodes=gridEl.querySelectorAll(".gc");
  for(var i=0;i<nodes.length;i++){
    var el=nodes[i],c=cards[el.id.slice(2)]||{},b=bucket(c),t=tabOf(c);
    per[t]=(per[t]||0)+1;
    var mine=t===activeTab;
    if(mine){n.all++;n[b]++;}
    // a search looks through the whole library; otherwise stay in this folder/channel
    var here;
    if(groupOn&&tabById(activeTab).builtin){
      if(curQuery)here=true;
      else if(curChannel)here=chanOf(c)===curChannel;
      else here=c.status!=="done";      // in-flight/failed stay visible above the folders
    }else{
      here=curQuery?underCurrent(relOf(c))!==null:inThisFolder(c);
    }
    var hit=mine&&here&&(curFilter==="all"||curFilter===b)&&
            (!curQuery||(c.title||"").toLowerCase().indexOf(curQuery)!==-1);
    el.classList.toggle("hide",!hit);
    if(hit)shown++;
  }
  ["all","active","done","failed"].forEach(function(k){
    var e=document.getElementById("c-"+k);if(e)e.textContent=n[k];
  });
  var lc=document.querySelectorAll("#tabbar .lt .lcnt");
  for(var j=0;j<lc.length&&j<allTabs.length;j++)lc[j].textContent=per[allTabs[j].id]||0;
  renderFolders();
  var t2=tabById(activeTab);
  var sc=document.getElementById("subcount");
  if(sc)sc.textContent=shown+(shown===1?" video":" videos");
  var e=document.getElementById("empty");
  if(e){
    var folders=gridEl.querySelectorAll(".fc").length;
    e.style.display=(shown===0&&folders===0)?"flex":"none";
    var t=document.getElementById("empty-t"),ss=document.getElementById("empty-s");
    document.getElementById("empty-ic").innerHTML=ic(t2.builtin?"down":"film",26);
    if(curQuery){t.textContent="No matches";ss.textContent='Nothing here matches "'+curQuery+'"';}
    else if(curPath){t.textContent="Nothing in this folder";
      ss.textContent="Drop videos here to add them to "+curPath;}
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
    document.getElementById("dropto").textContent=curPath
      ? "They move into "+curPath+" and appear in this folder"
      : "They move to your "+name+" folder and appear under that library";
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

/* ================= OneDrive section ================= */
var odMode=false,odReady=false,odConnected=false,odPath="",odEntries=[],odJobs={},odTBase="";
var OD_I={
 folder:P.folder,
 image:'<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-4.5-4.5L7 21"/>',
 video:P.film,
 audio:'<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
 pdf:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
 doc:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/>',
 sheet:'<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 10h16M4 15h16M10 4v16M15 4v16"/>',
 archive:'<rect x="4" y="3" width="16" height="6" rx="1"/><path d="M5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9"/><path d="M10 13h4"/>',
 file:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'
};
var OD_C={
 folder:["#7FB3FF","rgba(96,165,250,.13)"], image:["#C4B5FD","rgba(167,139,250,.13)"],
 video:["#F9A8D4","rgba(244,114,182,.13)"], audio:["#67E8F9","rgba(34,211,238,.12)"],
 pdf:["#FCA5A5","rgba(248,113,113,.13)"],  doc:["#7FB3FF","rgba(96,165,250,.13)"],
 sheet:["#6EE7B7","rgba(52,211,153,.13)"], archive:["#FCD34D","rgba(251,191,36,.13)"],
 file:["#9AA1B5","rgba(138,147,168,.13)"]
};
function odIc(n,sz){return '<svg width="'+(sz||16)+'" height="'+(sz||16)+'" viewBox="0 0 24 24" fill="none"'+
  ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+OD_I[n]+'</svg>';}

function switchCloud(){
  if(odMode)return;
  odMode=true;
  document.body.classList.add("cloud");
  var t=document.getElementById("odtab");
  t.classList.add("on");t.setAttribute("aria-selected","true");
  var ts=document.querySelectorAll("#tabbar .lt");
  for(var i=0;i<ts.length;i++){ts[i].classList.remove("on");ts[i].setAttribute("aria-selected","false");}
  odInit();
}
function odConnPaint(){
  var d=document.getElementById("odconndot");
  if(d)d.className="oddot"+(odConnected?" ok":"");
  var t=document.getElementById("odconntxt");
  if(t)t.textContent=odConnected?"Connected":"Connect";
}
function odInit(){
  if(odReady){odRender();return;}
  var g=document.getElementById("odgrid");
  g.innerHTML='<div class="empty"><span class="spin"></span><b>Preparing OneDrive…</b>'+
    '<span>First open sets up rclone and checks your sign-in</span></div>';
  pywebview.api.od_open().then(function(s){
    odReady=true;odConnected=!!s.connected;odTBase=s.tbase||"";
    document.getElementById("oddest").textContent=s.dest||"";
    document.getElementById("oddestpill").title="OneDrive downloads land here: "+(s.dest||"");
    odConnPaint();
    if(!s.rclone){
      g.innerHTML='<div class="empty"><b>rclone is missing</b><span>The OneDrive engine could not '+
        'be downloaded - check the activity log, then reopen this section.</span></div>';
      return;
    }
    if(!odConnected){
      g.innerHTML='<div class="empty"><b>Connect your OneDrive</b><span>One sign-in, done in a '+
        'console window. If you already use rclone, your sign-in is picked up automatically.</span>'+
        '<button class="dlbtn" style="margin:8px auto 0;height:30px;padding:0 16px" '+
        'onclick="odReconnect()">Connect</button></div>';
      return;
    }
    odGo("");
  })["catch"](function(){
    g.innerHTML='<div class="empty"><b>OneDrive setup failed</b><span>See the activity log.</span></div>';
  });
}
function odGo(path){
  odPath=path||"";
  odCrumb();
  var g=document.getElementById("odgrid");
  g.innerHTML='<div class="empty"><span class="spin"></span><b>Loading…</b></div>';
  pywebview.api.od_list(odPath).then(function(r){
    if(r.path!==odPath)return;            // already navigated elsewhere
    if(!r.ok){
      g.innerHTML='<div class="empty"><b>Could not read this folder</b><span>'+
        esc(r.error||"Unknown error")+'</span></div>';
      return;
    }
    odEntries=r.entries||[];
    odRender();
  })["catch"](function(){
    g.innerHTML='<div class="empty"><b>Could not read this folder</b></div>';
  });
}
function odUp(){
  if(!odPath)return;
  odGo(odPath.indexOf("/")===-1?"":odPath.slice(0,odPath.lastIndexOf("/")));
}
function odCrumb(){
  var c=document.getElementById("odcrumb");c.innerHTML="";
  var root=document.createElement("button");
  root.textContent="OneDrive";
  if(!odPath)root.className="here";else root.onclick=function(){odGo("");};
  c.appendChild(root);
  var segs=odPath?odPath.split("/"):[];
  segs.forEach(function(s,i){
    var sep=document.createElement("span");sep.className="sep";sep.textContent="/";
    c.appendChild(sep);
    var b=document.createElement("button");b.textContent=s;
    if(i===segs.length-1){b.className="here";}
    else{var to=segs.slice(0,i+1).join("/");b.onclick=function(){odGo(to);};}
    c.appendChild(b);
  });
  document.getElementById("odup").disabled=!odPath;
}
function odRender(){
  var q=(document.getElementById("odq").value||"").trim().toLowerCase();
  var list=q?odEntries.filter(function(e){return e.name.toLowerCase().indexOf(q)!==-1;}):odEntries;
  var nf=0;odEntries.forEach(function(e){if(e.isdir)nf++;});
  document.getElementById("odcount").textContent=odEntries.length?
    nf+" folder"+(nf===1?"":"s")+" · "+(odEntries.length-nf)+" files":"";
  var g=document.getElementById("odgrid");
  g.innerHTML="";
  if(!list.length){
    g.innerHTML='<div class="empty"><b>'+(q?"No matches":"This folder is empty")+'</b>'+
      (q?'<span>Nothing here matches "'+esc(q)+'"</span>':"")+'</div>';
    return;
  }
  var frag=document.createDocumentFragment();
  list.forEach(function(e){frag.appendChild(odCard(e));});
  g.appendChild(frag);
}
function odCard(e){
  var el=document.createElement("div");
  el.className="gc"+(e.isdir?" dir":"")+
    (((odJobs[e.key]||{}).status||e.job)==="running"?" running":"");
  el.id="odg-"+e.key;
  var kc=OD_C[e.kind]||OD_C.file;
  var th='<div class="gth" style="background:'+kc[1]+'">';
  if(e.thumb){
    th+='<img class="gimg" src="'+odTBase+e.key+'" alt="" loading="lazy" '+
        'onerror="this.style.display=\'none\'">';
  }else{
    th+='<span class="gph" style="color:'+kc[0]+'">'+odIc(e.kind,34)+'</span>';
  }
  th+='<span class="gbadge'+(e.ondisk?" on":"")+'">'+
      (e.isdir?"":(e.ondisk?"On disk":e.size_h))+'</span>';
  th+='<div class="gacts" id="oda-'+e.key+'"></div>';
  th+='<div class="gprog"><i></i></div></div>';
  el.innerHTML=th+'<div class="gm"><div class="gt" title="'+esc(e.name)+'">'+esc(e.name)+
    '</div><div class="gs" id="ods-'+e.key+'"></div></div>';
  if(e.isdir){
    el.addEventListener("click",function(){odGo(e.remote);});
  }else{
    el.addEventListener("dblclick",function(){
      if(e.ondisk)pywebview.api.od_open_local(e.remote);
    });
  }
  odPaintEl(el,e);   // paint before attach: getElementById can't see fragments
  return el;
}
function odStateHtml(e){
  var j=odJobs[e.key]||{};
  var st=j.status||e.job||"";
  if(e.isdir)return "<span>Folder</span>";
  if(st==="running"){
    var p=[Math.round(j.pct||0)+"%"];
    if(j.speed)p.push(j.speed);
    if(j.eta)p.push("ETA "+j.eta);
    return '<span class="spin"></span><span>'+esc(p.join(" · "))+"</span>";
  }
  if(st==="queued")return "<span>Queued…</span>";
  if(st==="error")return '<span class="bad">Failed</span>'+
    '<button class="dlbtn retry" data-act="dl">Retry</button>';
  if(st==="cancelled")return '<span>Stopped</span>'+
    '<button class="dlbtn retry" data-act="dl">Download</button>';
  if(e.ondisk)return '<span>'+esc(e.size_h)+'</span>'+
    '<button class="dlbtn ghost ok">On disk</button>';
  return '<span>'+esc(e.size_h)+'</span>'+
    '<button class="dlbtn" data-act="dl">Download</button>';
}
function odPaint(key,e){
  if(!e)for(var i=0;i<odEntries.length;i++)if(odEntries[i].key===key){e=odEntries[i];break;}
  if(!e)return;
  var card=document.getElementById("odg-"+key);
  if(card)odPaintEl(card,e);
}
function odPaintEl(card,e){
  var key=e.key;
  var s=card.querySelector(".gs");
  if(s){
    s.innerHTML=odStateHtml(e);
    var b=s.querySelector("[data-act=dl]");
    if(b)b.onclick=function(ev){
      ev.stopPropagation();
      pywebview.api.od_download(e.remote,e.name);
    };
  }
  var a=card.querySelector(".gacts");
  if(a&&!e.isdir){
    var j=odJobs[key]||{};var st=j.status||e.job||"";
    a.innerHTML="";
    if(st==="running"||st==="queued"){
      a.innerHTML='<button class="ga gcancel" title="Cancel download" aria-label="Cancel download">'+
        ic("x",13)+'</button>';
      a.firstChild.onclick=function(ev){ev.stopPropagation();pywebview.api.od_cancel(e.remote);};
    }else if(e.ondisk){
      a.innerHTML='<button class="ga" title="Open" aria-label="Open">'+play(14)+'</button>'+
                  '<button class="ga" title="Show in folder" aria-label="Show in folder">'+
                  ic("folder",14)+'</button>';
      a.children[0].onclick=function(ev){ev.stopPropagation();pywebview.api.od_open_local(e.remote);};
      a.children[1].onclick=function(ev){ev.stopPropagation();pywebview.api.od_reveal_local(e.remote);};
    }
  }
  var jj=odJobs[key]||{};
  card.classList.toggle("running",(jj.status||e.job)==="running");
  var bar=card.querySelector(".gprog i");
  if(bar&&jj.pct!=null)bar.style.width=jj.pct+"%";
  var badge=card.querySelector(".gbadge");
  if(badge&&!e.isdir){
    if(jj.status==="running"){badge.textContent=Math.round(jj.pct||0)+"%";badge.className="gbadge";}
    else if(e.ondisk||jj.status==="done"){badge.textContent="On disk";badge.className="gbadge on";}
    else{badge.textContent=e.size_h;badge.className="gbadge";}
  }
}
ui.odjob=function(o){
  odJobs[o.key]=o;
  for(var i=0;i<odEntries.length;i++){
    if(odEntries[i].key===o.key){
      if(o.ondisk)odEntries[i].ondisk=true;
      if(o.status)odEntries[i].job=(o.status==="queued"||o.status==="running")?o.status:"";
      break;
    }
  }
  odPaint(o.key);
  if(o.status==="done")toast("Saved: "+(o.name||"download"));
  if(o.status==="error")toast("Failed: "+(o.name||""));
};
ui.odthumb=function(key){
  var card=document.getElementById("odg-"+key);if(!card)return;
  var th=card.querySelector(".gth");
  var ph=th.querySelector(".gph");if(ph)ph.style.display="none";
  if(!th.querySelector(".gimg")){
    var img=document.createElement("img");
    img.className="gimg";img.alt="";img.loading="lazy";
    img.onerror=function(){img.style.display="none";};
    img.src=odTBase+key;
    th.insertBefore(img,th.firstChild);
  }
};
ui.odstrip=function(a){
  var el=document.getElementById("odstrip");
  var on=a.active>0||a.queued>0;
  el.classList.toggle("on",on);
  if(!on)return;
  document.getElementById("odssp").textContent=
    a.speed>0?(a.speed<10?a.speed.toFixed(1):Math.round(a.speed)):"--";
  document.getElementById("odsname").textContent=a.name||"Waiting…";
  document.getElementById("odbari").style.width=(a.pct||0)+"%";
  document.getElementById("odsmeta").textContent=
    (a.pct||0)+"%"+(a.queued?" · "+a.queued+" queued":"");
};
ui.odstate=function(s){
  var was=odConnected;
  odConnected=!!s.connected;
  odConnPaint();
  if(odMode&&odReady&&odConnected&&!was)odGo(odPath||"");
};
function odPickDest(){
  pywebview.api.od_pick_dest().then(function(d){
    document.getElementById("oddest").textContent=d;
    document.getElementById("oddestpill").title="OneDrive downloads land here: "+d;
    if(odReady&&odConnected)odGo(odPath);   // on-disk flags depend on the dest
  });
}
function odReconnect(){
  if(odConnected&&!confirm("Re-run the OneDrive sign-in?\n\n"+
     "A console window opens and walks you through it."))return;
  pywebview.api.od_reconnect();
  toast("OneDrive sign-in window opening…");
}
document.addEventListener("keydown",function(e){
  if(!odMode)return;
  var typing=document.activeElement===document.getElementById("odq");
  if(e.key==="Backspace"&&!typing){e.preventDefault();odUp();}
  else if(e.key==="F5"||((e.ctrlKey||e.metaKey)&&e.key==="r")){e.preventDefault();odGo(odPath);}
  else if(e.key==="Escape"){
    var q=document.getElementById("odq");
    if(q.value){q.value="";odRender();}
  }
});
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
    if(groupOn&&tabById(activeTab).builtin){ // keep each channel's videos together
      var ga=(ca.channel||"~").toLowerCase(),gb=(cb.channel||"~").toLowerCase();
      if(ga!==gb)return ga<gb?-1:1;
    }
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

/* ================= settings ================= */
function openSettings(){
  pywebview.api.get_settings().then(function(s){
    document.getElementById("set-folder").value = s.download_dir || "";
    document.getElementById("set-subfolders").checked = s.use_subfolders !== false;
    document.getElementById("set-streams").value = s.od_streams || 8;
    document.getElementById("settingsscrim").classList.add("open");
    document.getElementById("settingsdialog").classList.add("open");
  });
}
function closeSettings(){
  document.getElementById("settingsscrim").classList.remove("open");
  document.getElementById("settingsdialog").classList.remove("open");
}
function pickSettingsFolder(){
  pywebview.api.pick_base_dir().then(function(d){
    if(d) document.getElementById("set-folder").value = d;
  });
}
function saveSettingsModal(){
  var d = document.getElementById("set-folder").value;
  var sub = document.getElementById("set-subfolders").checked;
  var st = parseInt(document.getElementById("set-streams").value, 10);
  pywebview.api.set_settings(d, sub, st).then(function(s){
    closeSettings();
    toast("Settings saved");
    refreshTabs();
  });
}

/* ================= profile ================= */
function openProf(){
  document.getElementById("profscrim").classList.add("open");
  document.getElementById("prof").classList.add("open");
  loadProfile();
}
function closeProf(){
  document.getElementById("profscrim").classList.remove("open");
  document.getElementById("prof").classList.remove("open","sites");
}
function loadProfile(){
  pywebview.api.get_profile().then(function(p){
    var sl=document.getElementById("sitelist");sl.innerHTML="";
    (p.sites||[]).forEach(function(s){
      var r=document.createElement("div");
      r.className="srow "+(s.status==="in"?"in":(s.status==="out"?"out":""));
      var st=s.status==="in"?"Signed in":(s.status==="out"?"Not signed in":"Session saved");
      r.innerHTML='<span class="sdot"></span><span class="snm">'+esc(s.name)+
                  '</span><span class="sst">'+st+"</span>";
      var acts=document.createElement("span");acts.className="sact";
      var lg=document.createElement("button");
      lg.className="openb";
      lg.title="Opens "+s.name+" in a window so you can sign in";
      lg.setAttribute("aria-label","Open "+s.name+" to sign in");
      lg.innerHTML=ic("external",12)+"<span>Open</span>";
      lg.onclick=function(){pywebview.api.login("https://"+s.host+"/");closeProf();};
      acts.appendChild(lg);
      if(!s.builtin){
        var rm=document.createElement("button");
        rm.className="sbtn2 danger";rm.title="Remove from this list";
        rm.setAttribute("aria-label","Remove "+s.name);
        rm.innerHTML=ic("trash",14);
        rm.onclick=function(){pywebview.api.remove_site(s.host).then(showProfile);};
        acts.appendChild(rm);
      }
      r.appendChild(acts);sl.appendChild(r);
    });
    var dl=document.getElementById("deplist");dl.innerHTML="";
    (p.deps||[]).forEach(function(d){
      var r=document.createElement("div");
      r.className="srow "+(d.ok?"in":"out");
      r.innerHTML='<span class="sdot"></span><span class="snm">'+esc(d.name)+
                  '</span><span class="sst">'+(d.ok?"Installed":"Missing")+"</span>";
      r.title=d.where;
      dl.appendChild(r);
    });
    document.getElementById("depbin").textContent=p.bin||"";
  });
}
function showProfile(p){loadProfile();}

/* ---- supported websites: pulled live from yt-dlp, cached by the backend ---- */
var suppSites=null;
function openSites(){
  document.getElementById("prof").classList.add("sites");
  if(!suppSites)loadSites(false);
  else renderSites();
  setTimeout(function(){var s=document.getElementById("ssearch");if(s)s.focus();},60);
}
function closeSites(){document.getElementById("prof").classList.remove("sites");}
function loadSites(refresh){
  var box=document.getElementById("sslist");
  box.innerHTML='<div class="qload">Loading the list from yt-dlp…</div>';
  document.getElementById("ssmeta").textContent="";
  pywebview.api.get_supported(!!refresh).then(function(d){
    suppSites=d.sites||[];
    var when=d.ts?new Date(d.ts*1000).toLocaleDateString():"never";
    document.getElementById("ssmeta").textContent=
      suppSites.length?(d.count+" sites · updated "+when):"Couldn't reach the list — check your connection";
    renderSites();
  })["catch"](function(){
    box.innerHTML='<div class="qload">Couldn\'t load the list.</div>';
  });
}
function renderSites(){
  var box=document.getElementById("sslist");if(!box)return;
  if(!suppSites){box.innerHTML="";return;}
  var q=(document.getElementById("ssearch").value||"").trim().toLowerCase();
  var list=q?suppSites.filter(function(s){
      return s.name.toLowerCase().indexOf(q)!==-1||
             (s.note||"").toLowerCase().indexOf(q)!==-1;}):suppSites;
  var shown=list.slice(0,400);            // list is ~1700 long; keep the DOM sane
  box.innerHTML="";
  if(!shown.length){
    box.innerHTML='<div class="qload">No site matches "'+esc(q)+'"</div>';
    return;
  }
  shown.forEach(function(s){
    var r=document.createElement("div");r.className="ssrow";
    r.innerHTML='<span class="ssnm">'+esc(s.name)+'</span>'+
                '<span class="ssnote">'+esc(s.note||"")+'</span>'+
                (s.broken?'<span class="ssbroken">may be broken</span>':"");
    box.appendChild(r);
  });
  if(list.length>shown.length){
    var more=document.createElement("div");more.className="qload";
    more.textContent="+"+(list.length-shown.length)+" more — keep typing to narrow it down";
    box.appendChild(more);
  }
}
function addSite(){
  var i=document.getElementById("newsite"),v=i.value.trim();
  if(!v)return;
  i.value="";
  pywebview.api.add_site(v).then(function(){loadProfile();closeProf();});
}
document.addEventListener("keydown",function(e){
  var open=document.getElementById("dlg").classList.contains("open");
  var prof=document.getElementById("prof");
  if(e.key==="Escape"){
    if(prof.classList.contains("sites")){closeSites();return;}
    if(prof.classList.contains("open")){closeProf();return;}
    if(open){closeDlg();}else{document.getElementById("console").classList.remove("open");}
    return;
  }
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
               exists:e.exists,ts:e.ts,released:e.released,source:e.source,tab:e.tab,
               rel:e.rel});
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
    if OD_STAGE_DIR.is_dir():   # a live stage file is locked by its rclone, so
        for f in OD_STAGE_DIR.glob("*"):   # a concurrent instance just skips it
            try:
                f.unlink()
            except OSError:
                pass
    global UI_WIN
    api = Api()
    UI_WIN = webview.create_window(APP_NAME, html=HTML.replace("__APP_VERSION__", APP_VERSION), js_api=api,
                                   width=980, height=780, min_size=(760, 560))
    webview.start(lambda: bootstrap(api), private_mode=False,
                  storage_path=str(PROFILE_DIR))


if __name__ == "__main__":
    main()
