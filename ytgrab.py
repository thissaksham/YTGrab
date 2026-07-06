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
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from urllib.parse import quote, urlparse
from ctypes import wintypes
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import webview

APP_NAME = "YTGrab"


# All app data (deps, browser profile, config, history) lives here for BOTH
# the portable exe and the installed build, so login/history/tools are shared.
APP_DIR = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
BIN_DIR = APP_DIR / "bin"
PROFILE_DIR = APP_DIR / "profile"
CONFIG_FILE = APP_DIR / "config.json"
HISTORY_FILE = APP_DIR / "history.json"
LOG_FILE = APP_DIR / "ytgrab.log"
YTDLP = BIN_DIR / "yt-dlp.exe"
FFMPEG = BIN_DIR / "ffmpeg.exe"
FFPROBE = BIN_DIR / "ffprobe.exe"
FF_VER_FILE = BIN_DIR / "ffmpeg.ver"

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
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


def yt_args(url):
    """More bot-resistant player clients for YouTube; keeps the full format
    list (session cookies would drop it to 0 via SABR, so we stay anonymous)."""
    if is_youtube(url):
        return ["--extractor-args", "youtube:player_client=default,web_safari"]
    return []


class Api:
    def __init__(self):
        self.cfg = load_config()
        self.history = load_history()
        self.proc = None
        self.busy = False
        self.logged_in = False
        self._jar_cache = {}          # require-key -> (jar_text, expiry); memory only
        self._q = queue.Queue()
        self._worker_up = False

    # --- UI bridge ---

    def _push(self, line):
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.log({json.dumps(str(line))})")
            except Exception:
                pass

    def _item(self, **kw):
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.item({json.dumps(kw)})")
            except Exception:
                pass

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
                "set_timestamp": self.cfg.get("set_timestamp", True)}

    def pick_folder(self):
        res = UI_WIN.create_file_dialog(webview.FOLDER_DIALOG, directory=self.download_dir())
        if res:
            self.cfg["download_dir"] = res[0]
            save_config(self.cfg)
        return self.download_dir()

    # --- history ---

    def get_history(self):
        return [{**e, "exists": Path(e.get("path", "")).exists()} for e in self.history]

    def _add_history(self, e):
        self.history = [x for x in self.history if x.get("id") != e["id"]]
        self.history.insert(0, e)
        self.history = self.history[:1000]
        save_history(self.history)
        self._item(key=e["id"], status="done", title=e["title"], channel=e["channel"],
                   duration=e["duration"], size=e["size_h"], format=e["format"],
                   thumb=e["thumb"], path=e["path"])

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
        """Remove an item from the list/history. Does NOT delete the file."""
        self.history = [e for e in self.history if e.get("id") != key]
        save_history(self.history)
        return "ok"

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

    # --- queue ---

    def start_download(self, url, fmt_mode, custom_fmt, pl_start, pl_end,
                       mark_watched=True, set_timestamp=True, meta=None):
        url = (url or "").strip().strip('"')
        if not url:
            return "no-url"
        if not (YTDLP.exists() and FFMPEG.exists()):
            return "no-deps"
        fmt = custom_fmt.strip() if (fmt_mode == "custom" and custom_fmt.strip()) else DEFAULT_FORMAT
        items = None
        if is_playlist(url) and (pl_start or pl_end):
            items = f"{pl_start or ''}:{pl_end or ''}"
        self.cfg["mark_watched"] = bool(mark_watched)
        self.cfg["set_timestamp"] = bool(set_timestamp)
        save_config(self.cfg)

        meta = meta or {}
        vid = meta.get("id")
        if vid:
            key, placeholder = vid, False
            self._item(key=key, status="queued", title=meta.get("title"),
                       channel=meta.get("uploader"), duration=meta.get("duration"),
                       thumb=meta.get("thumb"))
        else:
            key, placeholder = f"job-{os.urandom(3).hex()}", True
            self._item(key=key, status="queued",
                       title=meta.get("title") or url, thumb=meta.get("thumb"))

        self._q.put((url, fmt, items, bool(mark_watched), bool(set_timestamp),
                     key, placeholder))
        if not self._worker_up:
            self._worker_up = True
            threading.Thread(target=self._queue_loop, daemon=True).start()
        return "queued"

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

        def parse(line):
            m = RE_YT_ID.match(line)
            if m:
                vid = m.group(1)
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

    def _download_worker(self, url, fmt, items, mark, stamp, job_key, placeholder):
        if placeholder:
            self._drop(job_key)
        self.busy = True
        self._set_state()
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
            self._push("[!] YouTube bot-check: this IP is temporarily flagged "
                       "(usually from many rapid downloads). Wait a bit and retry; "
                       "it clears on its own. (Cookies don't help - they yield 0 "
                       "downloadable formats via SABR.)")

        entries = postprocess(dl_dir, started, self, mark, stamp)
        done_ids = set()
        for e in entries:
            self._add_history(e)
            done_ids.add(e["id"])
        for key, it in state["items"].items():
            if key not in done_ids and it.get("status") != "done":
                self._item(key=key, status="failed")
        if not placeholder and job_key not in done_ids and not entries:
            self._item(key=job_key, status="failed")

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
  --bg:#0E0E11; --s1:#141418; --s2:#17171B; --s3:#1C1C22;
  --line:#232329; --line2:#2A2A31;
  --tx:#EAEAF0; --mut:#8A8A94; --dim:#5E5E68;
  --ac:#7D6FE8; --ac2:#8B79EE; --acbg:#221C3A; --actx:#C4B9F7;
  --ok:#6BD6A8; --warn:#E7B968; --danger:#E8837D;
}
*{box-sizing:border-box;}
body{margin:0;height:100vh;display:flex;flex-direction:column;gap:15px;
  padding:16px 20px 16px;background:var(--bg);color:var(--tx);
  font:14px/1.5 Inter,"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;user-select:none;}
svg{flex:none;}
header{display:flex;align-items:center;gap:13px;}
header h1{margin:0;font-size:18px;font-weight:600;letter-spacing:-.3px;}
.stat{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);}
.stat .dot{width:6px;height:6px;border-radius:50%;background:var(--dim);}
.stat.ok .dot{background:var(--ok);}
.stat.warn .dot{background:var(--warn);}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--dim);}
.chip .dot{width:6px;height:6px;border-radius:50%;background:var(--dim);}
.chip.ok .dot{background:var(--ok);} .chip.warn .dot{background:var(--warn);}
.sp{flex:1;}
.ib{width:34px;height:34px;border-radius:9px;border:0.5px solid var(--line2);background:transparent;
  color:var(--mut);display:inline-flex;align-items:center;justify-content:center;
  cursor:pointer;transition:background .14s,color .14s;}
.ib:hover{background:var(--s2);color:var(--tx);}
.inrow{display:flex;gap:10px;}
#url{flex:1;min-width:0;height:48px;border:0.5px solid var(--line2);border-radius:13px;
  background:var(--s2);color:var(--tx);padding:0 17px;font:14.5px/1 inherit;transition:border-color .14s;}
#url:focus{outline:none;border-color:var(--ac);}
#url::placeholder{color:var(--dim);}
.btn{height:48px;border:none;border-radius:13px;cursor:pointer;display:inline-flex;align-items:center;
  gap:8px;font:600 14px/1 inherit;transition:filter .14s,opacity .14s,background .14s;}
.btn:disabled{opacity:.4;cursor:default;}
.btn.dl{padding:0 24px;background:var(--ac);color:#fff;}
.btn.dl:hover:not(:disabled){filter:brightness(1.08);}
.btn.cancel{display:none;padding:0 18px;background:var(--s3);color:var(--mut);}
.btn.cancel:hover:not(:disabled){background:#24242B;color:var(--tx);}
:focus-visible{outline:2px solid var(--ac);outline-offset:2px;}
.save{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--mut);
  align-self:flex-start;padding:6px 12px;border-radius:9px;background:var(--s1);border:0.5px solid var(--line);}
.save .fi{color:var(--dim);}
.save b{color:var(--tx);font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:340px;}
.save button{background:none;border:none;color:var(--ac2);font:500 12.5px/1 inherit;cursor:pointer;
  padding-left:5px;border-left:0.5px solid var(--line2);margin-left:4px;}
.save button:hover{color:var(--actx);}
.lbl{font-size:11px;letter-spacing:.8px;color:var(--dim);}
.tabs{display:flex;gap:4px;}
.tab{background:none;border:none;color:var(--mut);font:500 13px/1 inherit;cursor:pointer;
  padding:8px 14px;border-radius:9px;transition:background .14s,color .14s;}
.tab:hover{color:var(--tx);}
.tab.on{color:var(--tx);background:var(--s2);}
#grid.show-active .gc.done{display:none;}
#grid.show-hist .gc:not(.done){display:none;}
#grid{flex:1;min-height:0;overflow-y:auto;display:grid;
  grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px;align-content:start;padding:1px;}
.empty{grid-column:1/-1;min-height:220px;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:12px;color:var(--dim);border:1px dashed var(--line2);border-radius:14px;}
.empty svg{opacity:.5;}
.empty span{font-size:13px;}
.gc{background:var(--s1);border:0.5px solid var(--line);border-radius:13px;overflow:hidden;}
.gc.playable{cursor:pointer;}
.gc.playable:hover{border-color:var(--line2);}
.gc.missing{opacity:.5;}
.gth{position:relative;aspect-ratio:16/9;background:var(--s3);display:flex;align-items:center;
  justify-content:center;overflow:hidden;}
.gimg{width:100%;height:100%;object-fit:cover;}
.gph{position:absolute;color:#3A3A42;}
.gbadge{position:absolute;top:7px;right:7px;font-size:10.5px;font-weight:500;
  background:rgba(8,8,10,.72);color:#D6D6DE;padding:2px 7px;border-radius:6px;}
.gprog{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(0,0,0,.4);display:none;}
.gprog i{display:block;height:100%;width:0;background:var(--ac);transition:width .3s ease-out;}
.gc.downloading .gprog,.gc.processing .gprog{display:block;}
.gc.processing .gprog i{background:#B9A8F5;}
.gplay{position:absolute;width:42px;height:42px;border-radius:50%;background:rgba(8,8,10,.55);
  color:#fff;display:none;align-items:center;justify-content:center;}
.gc.done.playable:hover .gplay{display:flex;}
.gacts{position:absolute;top:6px;left:6px;display:none;gap:5px;}
.gc.done:hover .gacts{display:flex;}
.ga{width:28px;height:28px;border-radius:8px;border:none;background:rgba(8,8,10,.72);color:#D6D6DE;
  display:flex;align-items:center;justify-content:center;cursor:pointer;}
.ga:hover{background:rgba(8,8,10,.92);color:#fff;}
.ga.gdel:hover{color:var(--danger);}
.gm{padding:9px 11px 11px;}
.gt{font-size:12.5px;font-weight:500;line-height:1.35;margin-bottom:5px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:34px;}
.gs{font-size:11px;color:var(--mut);display:flex;align-items:center;gap:5px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.gs .ok{color:var(--ok);} .gs .bad{color:var(--danger);}
.gs .spin{width:11px;height:11px;border:2px solid var(--line2);border-top-color:var(--ac);
  border-radius:50%;animation:spin .8s linear infinite;flex:none;}
@keyframes spin{to{transform:rotate(360deg);}}
.console{background:var(--s1);border:0.5px solid var(--line);border-radius:12px;overflow:hidden;flex:none;}
.chead{display:flex;align-items:center;gap:9px;padding:10px 14px;cursor:pointer;
  color:var(--mut);font-size:12px;font-weight:500;}
.chead:hover{color:var(--tx);}
.chev{transition:transform .2s;}
.console.open .chev{transform:rotate(90deg);}
#log{display:none;height:140px;overflow-y:auto;padding:2px 14px 12px;white-space:pre-wrap;user-select:text;
  border-top:0.5px solid var(--line);font:11.5px/1.6 "Cascadia Mono",Consolas,monospace;color:#9A9AA4;}
.console.open #log{display:block;}
#log .g{color:var(--ok);} #log .r{color:var(--danger);} #log .b{color:#A8C7FA;}
#log .p{color:var(--ac2);} #log .d{color:#57575F;}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.6);opacity:0;pointer-events:none;
  transition:opacity .18s;z-index:9;}
#scrim.open{opacity:1;pointer-events:auto;}
#dlg{position:fixed;left:50%;top:50%;transform:translate(-50%,-46%);opacity:0;pointer-events:none;
  width:min(500px,92vw);max-height:88vh;overflow-y:auto;background:var(--s2);border:0.5px solid var(--line2);
  border-radius:18px;padding:22px;z-index:10;display:flex;flex-direction:column;gap:17px;
  transition:opacity .18s,transform .18s;box-shadow:0 24px 60px rgba(0,0,0,.5);}
#dlg.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%);}
.media{display:flex;gap:14px;align-items:center;}
#s-thumb{width:126px;height:71px;border-radius:11px;object-fit:cover;background:var(--s3);flex:none;}
#s-title{font-size:15px;font-weight:500;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden;}
.msub{font-size:12px;color:var(--mut);margin-top:4px;}
.field{display:flex;flex-direction:column;gap:9px;}
.slabel{font-size:11px;letter-spacing:.6px;color:var(--dim);}
.qlist{display:flex;flex-direction:column;gap:5px;max-height:264px;overflow-y:auto;}
.qload{padding:16px;text-align:center;color:var(--dim);font-size:12.5px;}
.qrow{display:flex;align-items:center;gap:10px;width:100%;padding:11px 13px;border-radius:11px;
  border:0.5px solid var(--line2);background:var(--s3);cursor:pointer;text-align:left;
  transition:background .12s,border-color .12s;}
.qrow:hover{background:#20202A;}
.qrow.on{border-color:var(--ac);background:var(--acbg);}
.qmain{font:500 13.5px/1 inherit;color:var(--tx);min-width:78px;flex:none;}
.qrow.on .qmain{color:var(--actx);}
.qsub{flex:1;font-size:11.5px;color:var(--mut);}
.qsize{font-size:11.5px;color:var(--mut);font-variant-numeric:tabular-nums;flex:none;}
.qck{color:var(--ac2);opacity:0;flex:none;display:flex;}
.qrow.on .qck{opacity:1;}
.modeseg{display:flex;gap:4px;background:var(--s3);border-radius:12px;padding:4px;}
.ms{flex:1;height:36px;border:none;border-radius:9px;background:transparent;color:var(--mut);
  font:500 13px/1 inherit;cursor:pointer;transition:background .14s,color .14s;}
.ms:hover{color:var(--tx);}
.ms.on{background:var(--ac);color:#fff;}
#autopane{display:flex;flex-direction:column;gap:15px;}
#custompane{display:none;flex-direction:column;gap:15px;}
.fseg{display:flex;gap:8px;}
.fs{flex:1;min-height:54px;padding:9px 13px;border-radius:11px;border:0.5px solid var(--line2);
  background:var(--s3);color:var(--mut);cursor:pointer;display:flex;flex-direction:column;
  align-items:flex-start;gap:4px;transition:background .14s,color .14s,border-color .14s;}
.fs:hover{color:var(--tx);}
.fs .ft{font:500 13px/1 inherit;}
.fs .fd{font:400 11px/1.2 inherit;color:var(--dim);}
.fs.on{background:var(--acbg);color:var(--actx);border-color:transparent;}
.fs.on .fd{color:#A79BDA;}
#vqual{height:42px;border:0.5px solid var(--line2);border-radius:10px;background:var(--s3);
  color:var(--tx);font:13px/1 inherit;padding:0 34px 0 14px;cursor:pointer;
  appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%238A8A94' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>");
  background-repeat:no-repeat;background-position:right 12px center;}
#plrow{display:none;flex-direction:column;gap:9px;}
#plrow.on{display:flex;}
.plinputs{display:flex;gap:9px;align-items:center;}
#plstart,#plend{height:40px;width:92px;border:0.5px solid var(--line2);border-radius:10px;
  background:var(--s3);color:var(--tx);font:13px/1 inherit;padding:0 13px;}
.opts{display:flex;flex-direction:column;gap:11px;}
.ck{display:inline-flex;align-items:center;gap:9px;font-size:13px;color:var(--mut);cursor:pointer;}
.ck input{width:17px;height:17px;accent-color:var(--ac);cursor:pointer;}
.sact{display:flex;gap:8px;align-items:center;margin-top:2px;}
.tbtn{background:none;border:none;color:var(--mut);font:500 13px/1 inherit;cursor:pointer;
  height:40px;padding:0 16px;border-radius:10px;}
.tbtn:hover{background:var(--s1);color:var(--tx);}
::-webkit-scrollbar{width:9px;height:9px;}
::-webkit-scrollbar-thumb{background:#2C2C33;border-radius:5px;border:2px solid transparent;background-clip:content-box;}
::-webkit-scrollbar-track{background:transparent;}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important;}}
</style></head><body>

<header>
  <h1>YTGrab</h1>
  <span id="deps" class="stat"><span class="dot"></span>checking deps</span>
  <span id="auth" class="stat"><span class="dot"></span>login</span>
  <span class="sp"></span>
  <button class="ib" id="loginbtn" title="Log into the pasted URL's site (YouTube if empty)"
          aria-label="Login" onclick="pywebview.api.login(document.getElementById('url').value)">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-3.5 3.6-6 8-6s8 2.5 8 6"/></svg>
  </button>
</header>

<div class="inrow">
  <input type="text" id="url" placeholder="Paste a video, playlist or channel link" spellcheck="false">
  <button class="btn dl" id="dl" onclick="startDl()">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
      stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/></svg>
    <span id="dlLabel">Download</span></button>
  <button class="btn cancel" id="cancel" onclick="pywebview.api.cancel()">Cancel</button>
</div>

<div class="save">
  <svg class="fi" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round"><path d="M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2z"/></svg>
  <b id="dir"></b><button onclick="pickDir()">Change</button></div>

<div class="tabs">
  <button id="tab-active" class="tab" onclick="switchTab('active')">Downloads</button>
  <button id="tab-hist" class="tab on" onclick="switchTab('hist')">History</button>
</div>

<div id="grid">
  <div class="empty" id="empty">
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
      stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/></svg>
    <span>Paste a link above &mdash; your videos appear here</span>
  </div>
</div>

<div class="console" id="console">
  <div class="chead" onclick="document.getElementById('console').classList.toggle('open')">
    <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
      stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>Console
    <span class="sp"></span>
    <span id="counts" class="chip"><span class="dot"></span>ok 0 &middot; failed 0</span>
  </div>
  <div id="log"></div>
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
        <button class="fs on" data-v="quality" onclick="pickVfmt(this)"><span class="ft">Quality</span><span class="fd">AV1 / VP9 / H.265</span></button>
        <button class="fs" data-v="legacy" onclick="pickVfmt(this)"><span class="ft">Legacy</span><span class="fd">H.264 · most compatible</span></button>
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
      <span class="slabel">CHOOSE A VIDEO — audio is always best</span>
      <div class="qlist" id="vlist"><div class="qload">Loading formats…</div></div>
    </div>
  </div>
  <div class="field" id="plrow">
    <span class="slabel">PLAYLIST RANGE</span>
    <div class="plinputs">
      <input type="number" id="plstart" min="1" placeholder="start">
      <input type="number" id="plend" min="1" placeholder="end">
      <span class="msub">blank = all</span>
    </div>
  </div>
  <div class="opts">
    <label class="ck"><input type="checkbox" id="ck-watched" checked>Mark as watched</label>
    <label class="ck"><input type="checkbox" id="ck-stamp" checked>Set file date to upload date</label>
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
var LABEL={fetching:"Fetching info",downloading:"Downloading",processing:"Processing",
           done:"Completed",failed:"Failed",queued:"Queued"};
var P={
 folder:'<path d="M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2z"/>',
 trash:'<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
 check:'<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.5-4.5"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 alert:'<path d="M12 3 2 20h20z"/><path d="M12 10v4"/><path d="M12 17h.01"/>'
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
  if(t.indexOf("[deps]")===0||t.indexOf("[login]")===0)return"d";
  return"";
}
function setStat(el,label,cls){el.className="stat "+cls;el.innerHTML='<span class="dot"></span>'+label;}
var curTab="hist";
function switchTab(name){
  curTab=name;
  gridEl.className=name==="active"?"show-active":"show-hist";
  document.getElementById("tab-active").classList.toggle("on",name==="active");
  document.getElementById("tab-hist").classList.toggle("on",name==="hist");
  refreshView();
}
function refreshView(){
  var done=gridEl.querySelectorAll(".gc.done").length;
  var total=gridEl.querySelectorAll(".gc").length;
  var count=curTab==="hist"?done:(total-done);
  var e=document.getElementById("empty");
  if(!e)return;
  if(count===0){e.style.display="flex";
    e.querySelector("span").textContent=curTab==="hist"?"Nothing downloaded yet":"Nothing downloading right now";}
  else{e.style.display="none";}
}
function subHtml(o){
  if(o.status==="done"){
    var mark=o.exists===false?ic('alert',13,'bad'):ic('check',13,'ok');
    var parts=[o.size,o.format].filter(Boolean).join(" · ")||o.channel||"Completed";
    return mark+"<span>"+parts+"</span>";
  }
  if(o.status==="failed")return ic('alert',13,'bad')+"<span>Failed</span>";
  if(o.status==="downloading"){
    var p=[o.phase||"Downloading"];if(o.speed)p.push(o.speed);
    return '<span class="spin"></span><span>'+p.join(" · ")+"</span>";
  }
  if(o.status==="queued")return ic('clock',12)+"<span>Queued</span>";
  return '<span class="spin"></span><span>'+(o.phase||LABEL[o.status]||"")+"</span>";
}
function makeCard(key){
  var el=document.createElement("div");el.className="gc";el.id="g-"+key;
  el.innerHTML=
    '<div class="gth"><img class="gimg" style="display:none" alt=""><span class="gph">'+play(26)+'</span>'+
    '<div class="gbadge"></div>'+
    '<div class="gacts"><button class="ga gfolder" aria-label="Open folder">'+ic('folder',15)+'</button>'+
    '<button class="ga gdel" aria-label="Remove from list">'+ic('trash',15)+'</button></div>'+
    '<div class="gplay">'+play(20)+'</div>'+
    '<div class="gprog"><i></i></div></div>'+
    '<div class="gm"><div class="gt">…</div><div class="gs"></div></div>';
  el.addEventListener("dblclick",function(){if(cards[key]&&cards[key].path)pywebview.api.play(key);});
  el.querySelector(".gfolder").addEventListener("click",function(e){e.stopPropagation();pywebview.api.reveal(key);});
  el.querySelector(".gdel").addEventListener("click",function(e){e.stopPropagation();pywebview.api.remove(key);ui.drop(key);});
  gridEl.prepend(el);
  return el;
}
var ui={
  item:function(o){
    var key=o.key,el=document.getElementById("g-"+key);
    if(!el)el=makeCard(key);
    var c=cards[key]||{};for(var k in o)if(o[k]!=null)c[k]=o[k];cards[key]=c;
    if(c.thumb){var im=el.querySelector(".gimg");if(!im.getAttribute("src")){
      im.onerror=function(){im.style.display="none";};
      im.onload=function(){el.querySelector(".gph").style.display="none";};
      im.src=c.thumb;im.style.display="";}}
    el.className="gc "+(c.status||"")+(c.exists===false?" missing":"")+(c.path?" playable":"");
    el.querySelector(".gt").textContent=c.title||"…";
    var b=el.querySelector(".gbadge");
    if(c.status==="downloading"&&c.pct!=null)b.textContent=Math.round(c.pct)+"%";
    else if(c.status==="queued")b.textContent="Queued";
    else if(c.status==="processing")b.textContent="Processing";
    else if(c.duration)b.textContent=c.duration;else b.textContent="";
    el.querySelector(".gs").innerHTML=subHtml(c);
    if(c.pct!=null)el.querySelector(".gprog i").style.width=c.pct+"%";
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
    document.getElementById("dir").textContent=s.dir;
    setStat(document.getElementById("auth"),s.logged_in?"signed in":"login needed",s.logged_in?"ok":"warn");
    setStat(document.getElementById("deps"),s.deps_ok?"deps ready":"deps missing",s.deps_ok?"ok":"warn");
    document.getElementById("dl").disabled=!s.deps_ok;
    document.getElementById("s-go").disabled=!s.deps_ok;
    document.getElementById("cancel").style.display=s.busy?"inline-flex":"none";
  },
  done:function(ok){
    if(ok){okCount++;}else{failCount++;}
    var el=document.getElementById("counts");
    el.className="chip "+(failCount?"warn":(okCount?"ok":""));
    el.innerHTML='<span class="dot"></span>ok '+okCount+' · failed '+failCount;
  }
};
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
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
  var cf=curVfmt==="legacy"?"[vcodec^=avc]":"[vcodec~='^(av01|vp0?9|hev1|hvc1)']";
  if(q==="best")return "bv*"+cf+"+ba/bv*+ba/b";
  if(q==="lowest")return "wv*+ba/w";
  return "bv*[height<="+q+"]"+cf+"+ba/bv*[height<="+q+"]+ba/b[height<="+q+"]";
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
  var isPl=u.indexOf("list=")!==-1||u.indexOf("/@")!==-1;
  document.getElementById("plrow").className=isPl?"field on":"field";
  document.getElementById("scrim").className="open";
  document.getElementById("dlg").className="open";
}
function fillDlg(info,u){
  var t=document.getElementById("s-thumb");
  if(info&&info.ok){
    document.getElementById("s-title").textContent=info.title;
    var sub=info.uploader||"";
    if(info.kind==="playlist"){sub=(sub?sub+" · ":"")+info.count+" videos";
      document.getElementById("plrow").className="field on";}
    else if(info.duration){sub=(sub?sub+" · ":"")+info.duration;}
    document.getElementById("s-sub").textContent=sub;
    if(info.thumb){t.onerror=function(){t.style.display="none";};t.src=info.thumb;t.style.display="";}
  }else{
    document.getElementById("s-title").textContent="Couldn't fetch info — you can still download";
    document.getElementById("s-sub").textContent=(info&&info.error)||u;
  }
  renderVList(info&&info.ok?info.vformats:null);
}
function closeDlg(){document.getElementById("scrim").className="";document.getElementById("dlg").className="";}
function confirmDl(){
  if(!pendingUrl)return;
  var fmt;
  if(dlMode==="auto")fmt=autoFmt();
  else{var sel=document.querySelector("#vlist .qrow.on");fmt=sel?sel.getAttribute("data-fmt"):"bv*+ba/b";}
  closeDlg();
  switchTab("active");
  pywebview.api.start_download(pendingUrl,"custom",fmt,
    document.getElementById("plstart").value,document.getElementById("plend").value,
    document.getElementById("ck-watched").checked,document.getElementById("ck-stamp").checked,
    (lastInfo&&lastInfo.ok&&lastInfo.kind==="video")?
      {id:lastInfo.id,title:lastInfo.title,uploader:lastInfo.uploader,duration:lastInfo.duration,thumb:lastInfo.thumb}:
      (lastInfo&&lastInfo.ok?{title:lastInfo.title,thumb:lastInfo.thumb}:{}))
    .then(function(r){if(r==="no-deps")ui.log("[!] dependencies missing");});
}
function pickDir(){pywebview.api.pick_folder().then(function(d){document.getElementById("dir").textContent=d;});}
document.addEventListener("keydown",function(e){
  var open=document.getElementById("dlg").className.indexOf("open")!==-1;
  if(e.key==="Escape"){closeDlg();return;}
  if(e.key==="Enter"){if(open){confirmDl();}
    else if(document.activeElement===document.getElementById("url")){startDl();}}
});
window.addEventListener("pywebviewready",function(){
  logEl=document.getElementById("log");gridEl=document.getElementById("grid");
  switchTab("hist");
  document.getElementById("url").focus();
  pywebview.api.get_state().then(function(s){
    ui.setState(s);
    defaultFmt=s.default_format||"";
    document.getElementById("ck-watched").checked=s.mark_watched!==false;
    document.getElementById("ck-stamp").checked=s.set_timestamp!==false;
  });
  pywebview.api.get_history().then(function(list){
    list.slice().reverse().forEach(function(e){
      ui.item({key:e.id,status:"done",title:e.title,channel:e.channel,duration:e.duration,
               size:e.size_h,format:e.format,thumb:e.thumb,path:e.path,exists:e.exists});
    });
  });
});
</script></body></html>"""


def bootstrap(api):
    for _ in range(60):
        try:
            UI_WIN.evaluate_js("1")
            break
        except Exception:
            time.sleep(0.25)
    api._push(f"[*] {APP_NAME} started - data dir: {APP_DIR}")
    ensure_deps(api._push)
    api._set_state()
    api._push("[login] checking sign-in state...")
    api._recheck_login()


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
    UI_WIN = webview.create_window(APP_NAME, html=HTML, js_api=api,
                                   width=980, height=780, min_size=(760, 560))
    webview.start(lambda: bootstrap(api), private_mode=False,
                  storage_path=str(PROFILE_DIR))


if __name__ == "__main__":
    main()
