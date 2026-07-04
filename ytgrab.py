"""YTGrab — single-exe yt-dlp UI for Windows.

WebView2 (ships with Windows 11) renders the UI and hosts the captive
YouTube login. No cookie file is kept: mark-watched fires YouTube's own
stats ping as page JS inside the logged-in profile, and login state is
read from the page itself. Downloads run anonymous; only if YouTube
bot-checks does the app hand the session to yt-dlp via a temp file that
is zeroed and deleted the moment that download ends.
All app data lives in %LOCALAPPDATA%\\YTGrab\\ (bin, profile, config).

  ytgrab.py            launch the UI
  ytgrab.py --setup    CLI: download/update yt-dlp + ffmpeg, then exit

Build: pyinstaller --onefile --windowed --name YTGrab ytgrab.py
"""
import ctypes
import json
import os
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
APP_DIR = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
BIN_DIR = APP_DIR / "bin"
PROFILE_DIR = APP_DIR / "profile"
CONFIG_FILE = APP_DIR / "config.json"
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
VIDEO_EXTS = {".webm", ".mp4", ".mkv", ".avi", ".mov", ".flv", ".m4v"}
NO_WINDOW = 0x08000000
UI_WIN = None

# yt-dlp output → structured queue events
RE_YT_ID = re.compile(r"^\[youtube\] ([A-Za-z0-9_-]{11}): Downloading webpage")
RE_DEST = re.compile(r"^\[download\] Destination: (.+)$")
RE_PROG = re.compile(r"^\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*\S+"
                     r"(?:\s+at\s+(\S+))?(?:\s+ETA\s+(\S+))?")
RE_FILE_ID = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
POST_PREFIXES = ("[Merger]", "[Metadata]", "[EmbedThumbnail]",
                 "[ThumbnailsConvertor]", "[ExtractAudio]", "[VideoConvertor]",
                 "[Fixup")


def log(msg):
    line = f"{datetime.now():%H:%M:%S} {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout:
        print(line)


# === config ===

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# === captive-profile helpers (no cookie ever leaves the browser) ===

JS_LOGGED_IN = ("(function(){try{if(!(window.ytcfg&&ytcfg.get))return -1;"
                "return ytcfg.get('LOGGED_IN')?1:0}catch(e){return -1}})()")
# Same endpoints yt-dlp's --mark-watched hits, fired as page JS inside the
# logged-in profile so credentials never leave the browser. The playback ping
# creates the history entry; the watchtime ping (st/et = full length) records
# 100% watch progress — verified via startPercent:100 in the history feed.
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
    or timeout; return the accepted result (or None). grace: seconds to keep
    the page alive after success (lets in-flight requests leave)."""
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


def check_login():
    """True if the captive profile is signed into YouTube."""
    r = _hidden_poll("https://www.youtube.com", JS_LOGGED_IN,
                     lambda r: r in (0, 1), 30)
    return r == 1


def browser_mark_watched(url, push):
    """Fire the videostats ping from the logged-in profile (verified to
    create a real history entry)."""
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


def profile_session_jar(origin="https://www.youtube.com", require="youtube"):
    """Dump the profile's cookies for a site to an in-memory Netscape string.
    Used transiently: the caller writes it to a temp file for one yt-dlp run
    and destroys it right after. Never persisted."""
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


def ensure_deps(push):
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
        remote = ""
        for url in FF_VER_URLS:
            try:
                remote = http_get(url).read().decode().strip()
                if remote:
                    break
            except Exception:
                continue
        local = FF_VER_FILE.read_text().strip() if FF_VER_FILE.exists() else ""
        have = FFMPEG.exists() and FFPROBE.exists()
        if have and remote and remote == local:
            push(f"[deps] ffmpeg {local} is up to date")
        elif have and not remote:
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


def postprocess(dl_dir, started, api, mark=True, stamp=True):
    """For each fresh .info.json: optionally stamp the video file with its
    upload date and mark it watched, then delete the json."""
    push = api._push
    for jf in Path(dl_dir).glob("*.info.json"):
        try:
            if jf.stat().st_mtime < started - 5:
                continue
            info = json.loads(jf.read_text(encoding="utf-8"))
            vid = info.get("id", "")
            if stamp:
                cands = [p for p in Path(dl_dir).iterdir()
                         if p.suffix.lower() in VIDEO_EXTS and vid and vid in p.name]
                if cands:
                    target = max(cands, key=lambda p: p.stat().st_mtime)
                    epoch = epoch_from_info(info)
                    if epoch:
                        set_file_times(target, epoch)
                        push(f"[post] timestamp set: {target.name}")
            url = info.get("webpage_url", "")
            if mark and "youtube" in url and api.logged_in:
                browser_mark_watched(url, push)
            jf.unlink(missing_ok=True)
        except Exception as e:
            push(f"[post] cleanup error: {e}")


def is_playlist(url):
    return ("list=" in url) or ("/@" in url)


class Api:
    def __init__(self):
        self.cfg = load_config()
        self.proc = None
        self.busy = False
        self.logged_in = False
        self._jar_cache = {}  # require-key -> (jar_text, expiry); memory only

    def _push(self, line):
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.log({json.dumps(str(line))})")
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

    def _session_args(self, origin="https://www.youtube.com", require="youtube"):
        """(['--cookies', tmp], tmp) from the profile session, or ([], None).
        Caller must _shred(tmp) after the yt-dlp run. Jar text is cached in
        memory for 10 min so repeated retries don't re-open hidden windows."""
        cached = self._jar_cache.get(require)
        if cached and cached[1] > time.time():
            jar = cached[0]
        else:
            jar = profile_session_jar(origin, require)
            # negative results cached briefly so cookie-less sites don't cost
            # a 25s probe on every action
            self._jar_cache[require] = (jar, time.time() + (600 if jar else 120))
        if not jar:
            return [], None
        tmp = APP_DIR / f"session-{os.urandom(4).hex()}.tmp"
        tmp.write_text(jar, encoding="utf-8", newline="\n")
        return ["--cookies", str(tmp)], tmp

    @staticmethod
    def _shred(tmp):
        if tmp:
            try:
                tmp.write_bytes(b"\0" * 8192)
                tmp.unlink()
            except OSError:
                pass

    def _site_session(self, url):
        """Transient session args for a NON-YouTube url ([], None otherwise)."""
        low = url.lower()
        if "youtube.com" in low or "youtu.be" in low:
            return [], None
        host, key = site_key(url)
        if not host:
            return [], None
        return self._session_args(f"https://{host}/", key)

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

    def list_formats(self, url):
        url = (url or "").strip()
        if not url:
            return "Paste a URL first."
        cmd = [YTDLP, "--no-warnings", "-F", "--socket-timeout", "10",
               "--retries", "2"]
        if is_playlist(url):
            cmd += ["--playlist-items", "1"]
        site_args, site_tmp = self._site_session(url)
        cmd += site_args
        cmd.append(url)
        try:
            _, out = run_quiet(cmd, timeout=180)
            if ("Sign in to confirm" in out or "not a bot" in out) and self.logged_in:
                self._push("[*] formats: bot-check — retrying with your session...")
                args, tmp = self._session_args()
                if args:
                    try:
                        _, out = run_quiet(cmd[:-1] + args + [url], timeout=180)
                    finally:
                        self._shred(tmp)
            return out or "No output."
        except Exception as e:
            return f"Failed: {e}"
        finally:
            self._shred(site_tmp)

    def _oembed(self, url):
        """Bot-check-proof fallback: YouTube oEmbed (title/channel/thumb only)."""
        try:
            u = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
            with http_get(u, timeout=8) as resp:
                d = json.loads(resp.read().decode())
            return {"ok": True, "kind": "video",
                    "title": d.get("title") or "Unknown title",
                    "uploader": d.get("author_name") or "", "duration": "",
                    "thumb": d.get("thumbnail_url") or ""}
        except Exception:
            return None

    def fetch_info(self, url):
        """Seal-style pre-download info fetch for the config sheet.
        Must always return a dict — a raised exception would reject the JS
        promise and strand the sheet in its loading state."""
        url = (url or "").strip().strip('"')
        if not url:
            return {"ok": False, "error": "no-url"}
        base = [YTDLP, "--no-warnings", "-J", "--socket-timeout", "10",
                "--retries", "2", "--extractor-retries", "1"]
        base += ["--flat-playlist"] if is_playlist(url) else ["--no-playlist"]

        def attempt(extra):
            r = subprocess.run([str(c) for c in base + extra + [url]],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=NO_WINDOW, timeout=45)
            d = json.loads(r.stdout) if (r.stdout or "").strip() else None
            if not isinstance(d, dict):
                tail = (r.stderr or "").strip().splitlines()
                raise ValueError(tail[-1] if tail else "no data from yt-dlp")
            return d

        site_args, site_tmp = self._site_session(url)
        try:
            return self.fetch_info_from(attempt(site_args))
        except Exception as e:
            err = str(e)
            self._push(f"[!] info fetch: {err[:160]}")
            if "Sign in to confirm" in err or "not a bot" in err:
                if not is_playlist(url):
                    info = self._oembed(url)
                    if info:
                        return info
                if self.logged_in:
                    self._push("[*] info fetch: bot-check — retrying with your session...")
                    args, tmp = self._session_args()
                    if args:
                        try:
                            data = attempt(args)
                        except Exception as e2:
                            return {"ok": False, "error": str(e2)[:200]}
                        finally:
                            self._shred(tmp)
                        return self.fetch_info_from(data)
                err = ("YouTube bot-check — the download itself will still "
                       "auto-retry with your session")
            return {"ok": False, "error": err[:200]}
        finally:
            self._shred(site_tmp)

    def fetch_info_from(self, data):
        """Build the sheet dict from a yt-dlp -J payload."""
        if data.get("_type") == "playlist" or "entries" in data:
            entries = data.get("entries") or []
            thumb = ""
            for e in entries:
                if len(e.get("id") or "") == 11:
                    thumb = f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"
                    break
            return {"ok": True, "kind": "playlist",
                    "title": data.get("title") or "Playlist",
                    "uploader": data.get("uploader") or data.get("channel") or "",
                    "count": len(entries), "thumb": thumb}
        dur = data.get("duration")
        if dur:
            h, rem = divmod(int(dur), 3600)
            m, s = divmod(rem, 60)
            dur = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        return {"ok": True, "kind": "video",
                "title": data.get("title") or "Unknown title",
                "uploader": data.get("uploader") or data.get("channel") or "",
                "duration": dur or "", "thumb": data.get("thumbnail") or ""}

    def login(self, url=""):
        """Open a login window for the pasted URL's site (YouTube if empty)."""
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
            w.events.closed += lambda *a: self._push(
                f"[login] {host} session saved in profile")
        return "opened"

    def _recheck_login(self):
        self.logged_in = check_login()
        self._push("[login] signed in" if self.logged_in
                   else "[login] not signed in — click Login and complete sign-in")
        self._set_state()

    def update_deps(self):
        threading.Thread(target=lambda: (ensure_deps(self._push), self._set_state()),
                         daemon=True).start()
        return "updating"

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           creationflags=NO_WINDOW, capture_output=True)
            self._push("[!] download cancelled")
        return "ok"

    def start_download(self, url, fmt_mode, custom_fmt, pl_start, pl_end,
                       mark_watched=True, set_timestamp=True):
        url = (url or "").strip().strip('"')
        if not url:
            return "no-url"
        if self.busy:
            return "busy"
        if not (YTDLP.exists() and FFMPEG.exists()):
            return "no-deps"
        fmt = custom_fmt.strip() if (fmt_mode == "custom" and custom_fmt.strip()) else DEFAULT_FORMAT
        items = None
        if is_playlist(url) and (pl_start or pl_end):
            items = f"{pl_start or ''}:{pl_end or ''}"
        self.cfg["mark_watched"] = bool(mark_watched)
        self.cfg["set_timestamp"] = bool(set_timestamp)
        save_config(self.cfg)
        threading.Thread(target=self._download_worker,
                         args=(url, fmt, items, bool(mark_watched),
                               bool(set_timestamp)),
                         daemon=True).start()
        return "started"

    def _item(self, **kw):
        """Upsert a card in the UI queue."""
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.item({json.dumps(kw)})")
            except Exception:
                pass

    def _make_parser(self):
        """Turn raw yt-dlp lines into per-video queue card updates."""
        state = {"cur": None, "items": {}}

        def parse(line):
            m = RE_YT_ID.match(line)
            if m:
                vid = m.group(1)
                state["cur"] = vid
                if vid not in state["items"]:
                    state["items"][vid] = {"status": "fetching", "last_pct": -1}
                    self._item(key=vid, status="fetching",
                               thumb=f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
                return
            m = RE_DEST.match(line)
            if m:
                name = os.path.basename(m.group(1))
                idm = RE_FILE_ID.search(name)
                key = idm.group(1) if idm else name
                state["cur"] = key
                it = state["items"].setdefault(key, {"last_pct": -1})
                it["status"] = "downloading"
                title = re.sub(r"\.f\d+\.\w+$|\.\w+$", "", name)
                title = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", title).strip()
                kw = {"key": key, "status": "downloading", "title": title}
                if idm:
                    kw["thumb"] = f"https://i.ytimg.com/vi/{idm.group(1)}/mqdefault.jpg"
                self._item(**kw)
                return
            cur = state["cur"]
            it = state["items"].get(cur)
            if it is None:
                return
            m = RE_PROG.match(line)
            if m:
                pct = float(m.group(1))
                if int(pct) != it["last_pct"]:  # throttle to whole percents
                    it["last_pct"] = int(pct)
                    it["status"] = "downloading"
                    self._item(key=cur, status="downloading", pct=pct,
                               speed=m.group(2) or "", eta=m.group(3) or "")
                return
            if line.startswith(POST_PREFIXES):
                if it["status"] != "processing":
                    it["status"] = "processing"
                    self._item(key=cur, status="processing", pct=100)
                return
            if line.startswith("ERROR"):
                it["status"] = "failed"
                self._item(key=cur, status="failed")

        def finalize(ok):
            for key, it in state["items"].items():
                if it["status"] == "failed":
                    continue
                it["status"] = "done" if ok else "failed"
                self._item(key=key, status=it["status"],
                           pct=100 if ok else None)

        return parse, finalize

    def _run_ytdlp(self, cmd, dl_dir, on_line=None):
        """Stream one yt-dlp run into the log; returns (exit_code, bot_checked)."""
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

    def _download_worker(self, url, fmt, items, mark=True, stamp=True):
        self.busy = True
        self._set_state()
        started = time.time()
        dl_dir = self.download_dir()
        cmd = [YTDLP, "-f", fmt, *BASE_OPTS, "--ffmpeg-location", str(BIN_DIR)]
        if items:
            cmd += ["--playlist-items", items]
        if is_playlist(url):
            cmd += ["--ignore-errors"]  # ponytail: yt-dlp presses on per-video; no outer retry loop
        auth = domain_auth(url)
        site_tmp = None
        if not auth:
            args, site_tmp = self._site_session(url)
            if args:
                auth = args
                self._push("[*] using saved site session (transient)")
        cmd += auth
        cmd.append(url)
        self._push(f"[*] downloading: {url}")
        parse, finalize = self._make_parser()
        code, botcheck = self._run_ytdlp(cmd, dl_dir, parse)
        if code != 0 and botcheck:
            if self.logged_in:
                self._push("[!] YouTube bot-check — retrying with your signed-in "
                           "session (used for this download only, then destroyed)")
                args, tmp = self._session_args()
                if args:
                    try:
                        code, _ = self._run_ytdlp(cmd[:-1] + args + [cmd[-1]],
                                                  dl_dir, parse)
                    finally:
                        self._shred(tmp)
                else:
                    self._push("[!] could not read session from profile")
            else:
                self._push("[!] YouTube bot-check — click Login so retries can "
                           "use your session")
        self._shred(site_tmp)
        postprocess(dl_dir, started, self, mark, stamp)
        finalize(code == 0)
        self._push("[+] done" if code == 0 else f"[!] finished with errors (exit {code})")
        self.busy = False
        self._set_state()
        if UI_WIN:
            try:
                UI_WIN.evaluate_js(f"ui.done({json.dumps(code == 0)})")
            except Exception:
                pass


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
:root {
  color-scheme: dark;
  --bg:#141218; --surf1:#1D1B20; --surf2:#211F26; --surf3:#2B2930;
  --on:#E6E0E9; --onvar:#CAC4D0; --outline:#938F99; --outvar:#49454F;
  --primary:#D0BCFF; --onprimary:#381E72; --seccont:#4A4458; --onseccont:#E8DEF8;
  --green:#81C995; --red:#F2B8B5; --amber:#FFD8A8;
}
* { box-sizing:border-box; }
body { margin:0; height:100vh; display:flex; flex-direction:column; gap:12px;
  padding:14px 18px 18px; background:var(--bg); color:var(--on);
  font:14px/1.5 Roboto,"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased; user-select:none; }
svg { flex:none; }
header { display:flex; align-items:center; gap:12px; padding:4px 2px; }
header h1 { margin:0; font-size:22px; font-weight:400; letter-spacing:.1px; }
.chip { height:28px; padding:0 12px; border-radius:8px; display:inline-flex;
  align-items:center; gap:7px; font-size:12px; font-weight:500;
  color:var(--onvar); border:1px solid var(--outvar); }
.chip .dot { width:7px; height:7px; border-radius:50%; background:var(--outline); }
.chip.ok { border-color:transparent; background:rgba(129,201,149,.14); color:var(--green); }
.chip.ok .dot { background:var(--green); }
.chip.warn { border-color:transparent; background:rgba(255,216,168,.14); color:var(--amber); }
.chip.warn .dot { background:var(--amber); }
.spacer { flex:1; }
.btn { height:40px; padding:0 22px; border:none; border-radius:20px; cursor:pointer;
  display:inline-flex; align-items:center; gap:8px; font:500 14px/1 inherit;
  transition:filter .15s, background .15s, opacity .15s; }
.btn:disabled { opacity:.38; cursor:default; }
.btn:focus-visible, .iconbtn:focus-visible, input:focus-visible, select:focus-visible {
  outline:2px solid var(--primary); outline-offset:2px; }
.filled { background:var(--primary); color:var(--onprimary); }
.filled:hover:not(:disabled) { filter:brightness(1.07); }
.tonal { background:var(--seccont); color:var(--onseccont); }
.tonal:hover:not(:disabled) { filter:brightness(1.12); }
.textbtn { background:transparent; color:var(--primary); height:40px; padding:0 14px;
  border:none; border-radius:20px; cursor:pointer; font:500 14px/1 inherit; }
.textbtn:hover { background:rgba(208,188,255,.08); }
.iconbtn { width:40px; height:40px; border:none; border-radius:50%; cursor:pointer;
  background:transparent; color:var(--onvar); display:inline-flex;
  align-items:center; justify-content:center; transition:background .15s; }
.iconbtn:hover { background:rgba(230,224,233,.08); }
input[type=text], input[type=number] { padding:10px 14px; font:13.5px/1.4 inherit;
  color:var(--on); background:var(--surf3); border:1px solid transparent;
  border-radius:10px; transition:border-color .15s; }
input:focus { border-color:var(--primary); outline:none; }
input::placeholder { color:#8F8A96; }
select { appearance:none; -webkit-appearance:none; padding:10px 36px 10px 14px;
  font:500 13px/1.2 inherit; color:var(--on); cursor:pointer; border:none;
  border-radius:10px;
  background:var(--surf3) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%23CAC4D0' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>")
    no-repeat right 12px center; }
.card { background:var(--surf2); border-radius:16px; padding:14px 16px;
  display:flex; flex-direction:column; gap:10px; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
#url { flex:1; min-width:260px; }
.dim { color:var(--onvar); font-size:12.5px; }
.path { font-size:12.5px; color:var(--primary); overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; max-width:430px; }
#queue { flex:1; min-height:0; overflow-y:auto; display:flex; flex-direction:column;
  gap:8px; padding:2px; }
.empty { flex:1; display:flex; flex-direction:column; align-items:center;
  justify-content:center; gap:12px; color:#8F8A96;
  border:1.5px dashed var(--outvar); border-radius:16px; }
.empty svg { opacity:.45; }
.empty span { font-size:13px; }
.qcard { display:flex; gap:12px; align-items:center; padding:10px 12px; flex:none;
  background:var(--surf2); border-radius:16px; }
.qthumb { width:100px; height:56px; border-radius:10px; object-fit:cover;
  background:var(--surf3); flex:none; }
.qbody { flex:1; min-width:0; display:flex; flex-direction:column; gap:6px; }
.qtitle { font-size:13.5px; font-weight:500; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; }
.qmeta { font-size:11.5px; color:var(--onvar); font-variant-numeric:tabular-nums; }
.qbar { height:4px; border-radius:2px; background:var(--outvar); overflow:hidden; }
.qbar i { display:block; height:100%; width:0; border-radius:2px;
  background:var(--primary); transition:width .3s ease-out; }
.qcard.done .qbar i { background:var(--green); width:100%; }
.qcard.failed .qbar i { background:var(--red); }
.qcard.processing .qbar i { background:#CCC2DC; }
.qstate { flex:none; width:28px; height:28px; display:flex; align-items:center;
  justify-content:center; }
.spin { width:15px; height:15px; border-radius:50%;
  border:2px solid var(--outvar); border-top-color:var(--primary);
  animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.okmark { color:var(--green); } .badmark { color:var(--red); }
.console { flex:none; background:var(--surf1); border-radius:16px; overflow:hidden; }
.console-head { display:flex; align-items:center; gap:9px; padding:11px 16px;
  cursor:pointer; color:var(--onvar); font-size:12.5px; font-weight:500; }
.console-head:hover { color:var(--on); }
.chev { transition:transform .2s; }
.console.open .chev { transform:rotate(90deg); }
#log { display:none; height:165px; overflow-y:auto; padding:4px 16px 12px;
  border-top:1px solid var(--outvar); white-space:pre-wrap; user-select:text;
  font:12px/1.65 "Cascadia Mono",Consolas,monospace; color:#A8A2B0; }
.console.open #log { display:block; }
#log .c-green { color:var(--green); } #log .c-red { color:var(--red); }
#log .c-blue { color:#A8C7FA; } #log .c-pink { color:var(--primary); }
#log .c-dim { color:#79747E; }
#scrim { position:fixed; inset:0; background:rgba(0,0,0,.5); opacity:0;
  pointer-events:none; transition:opacity .2s; z-index:9; }
#scrim.open { opacity:1; pointer-events:auto; }
#sheet { position:fixed; left:50%; bottom:0; transform:translate(-50%,105%);
  width:min(580px,94vw); max-height:86vh; overflow-y:auto;
  background:var(--surf2); border-radius:28px 28px 0 0; padding:6px 24px 20px;
  transition:transform .25s cubic-bezier(.2,0,0,1); z-index:10;
  display:flex; flex-direction:column; gap:14px; }
#sheet.open { transform:translate(-50%,0); }
.handle { width:32px; height:4px; border-radius:2px; background:var(--outvar);
  margin:8px auto 2px; flex:none; }
.media { display:flex; gap:14px; align-items:center; }
#s-thumb { width:128px; height:72px; border-radius:12px; object-fit:cover;
  background:var(--surf3); flex:none; }
#s-title { font-size:15px; font-weight:500; line-height:1.35; display:-webkit-box;
  -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.msub { font-size:12.5px; color:var(--onvar); margin-top:4px; }
.srow { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.slabel { font-size:13px; font-weight:500; color:var(--onvar); min-width:52px; }
#customfmt { width:220px; display:none; }
#customfmt.active { display:inline-block; }
#plrow { display:none; }
#plrow.show { display:flex; }
#plrow input { width:86px; }
.ck { display:inline-flex; align-items:center; gap:9px; font-size:13px;
  color:var(--onvar); cursor:pointer; user-select:none; }
.ck input { width:18px; height:18px; accent-color:var(--primary); cursor:pointer; }
#fmtout { display:none; max-height:170px; overflow:auto; margin:0;
  padding:10px 12px; border-radius:12px; background:var(--surf1);
  white-space:pre; user-select:text;
  font:11.5px/1.55 "Cascadia Mono",Consolas,monospace; color:#B7B0C0; }
.sactions { display:flex; gap:8px; align-items:center; }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:rgba(255,255,255,.12); border-radius:5px;
  border:2px solid transparent; background-clip:content-box; }
::-webkit-scrollbar-track { background:transparent; }
@media (prefers-reduced-motion: reduce) {
  * { transition:none !important; animation:none !important; }
}
</style></head><body>

<header>
  <h1>YTGrab</h1>
  <span id="deps" class="chip"><span class="dot"></span>checking deps</span>
  <span id="auth" class="chip"><span class="dot"></span>login</span>
  <span class="spacer"></span>
  <button class="iconbtn" aria-label="Update dependencies" title="Update yt-dlp and ffmpeg"
          onclick="pywebview.api.update_deps()">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg></button>
  <button class="iconbtn" id="loginbtn" aria-label="Login"
          title="Log into the pasted URL's site (YouTube if empty)"
          onclick="pywebview.api.login(document.getElementById('url').value)">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="8" r="4"/><path d="M4 21c0-3.5 3.6-6 8-6s8 2.5 8 6"/></svg></button>
</header>

<section class="card">
  <div class="row">
    <input type="text" id="url" placeholder="Paste a video, playlist or channel link"
           spellcheck="false">
    <button class="btn filled" id="dl" onclick="startDl()">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/></svg>
      <span id="dlLabel">Download</span></button>
    <button class="btn tonal" id="cancel" onclick="pywebview.api.cancel()" disabled>
      Cancel</button>
  </div>
  <div class="row">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
         style="color:#8F8A96">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
    <span class="dim">Save to</span>
    <span id="dir" class="path"></span>
    <button class="textbtn" onclick="pickDir()">Change</button>
  </div>
</section>

<div id="queue">
  <div class="empty" id="empty">
    <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/></svg>
    <span>Paste a link above — downloads appear here as cards</span>
  </div>
</div>

<section class="card console" id="console" style="padding:0">
  <div class="console-head" onclick="toggleConsole()">
    <svg class="chev" width="13" height="13" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2.4" stroke-linecap="round"
         stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
    Console
    <span class="spacer"></span>
    <span id="counts" class="chip"><span class="dot"></span>ok 0 · failed 0</span>
  </div>
  <div id="log"></div>
</section>

<div id="scrim" onclick="closeSheet()"></div>
<div id="sheet" role="dialog" aria-modal="true" aria-label="Download options">
  <div class="handle"></div>
  <div class="media">
    <img id="s-thumb" alt="">
    <div style="min-width:0">
      <div id="s-title">…</div>
      <div id="s-sub" class="msub"></div>
    </div>
  </div>
  <div class="srow">
    <span class="slabel">Quality</span>
    <select id="quality" aria-label="Quality" onchange="qualityChanged()">
      <option value="default" selected>Default · 720–1080 VP9</option>
      <option value="bv*+ba/b">Best available</option>
      <option value="bv*[height<=2160]+ba/b[height<=2160]">4K · 2160p</option>
      <option value="bv*[height<=1440]+ba/b[height<=1440]">1440p</option>
      <option value="bv*[height<=1080]+ba/b[height<=1080]">1080p</option>
      <option value="bv*[height<=720]+ba/b[height<=720]">720p</option>
      <option value="bv*[height<=480]+ba/b[height<=480]">480p</option>
      <option value="ba[ext=m4a]/ba">Audio only</option>
      <option value="custom">Custom selector…</option>
    </select>
    <input type="text" id="customfmt" placeholder="e.g. 137+140 or bv+ba" spellcheck="false">
  </div>
  <div class="srow" id="plrow">
    <span class="slabel">Range</span>
    <input type="number" id="plstart" min="1" placeholder="start">
    <input type="number" id="plend" min="1" placeholder="end">
    <span class="msub">blank = all</span>
  </div>
  <div class="srow">
    <label class="ck"><input type="checkbox" id="ck-watched" checked>
      Mark as watched</label>
    <label class="ck"><input type="checkbox" id="ck-stamp" checked>
      Set file date to upload date</label>
  </div>
  <pre id="fmtout"></pre>
  <div class="sactions">
    <button class="textbtn" onclick="sheetFormats()">Formats</button>
    <span class="spacer"></span>
    <button class="textbtn" onclick="closeSheet()">Cancel</button>
    <button class="btn filled" id="s-go" onclick="confirmDl()">Download</button>
  </div>
</div>

<script>
var okCount = 0, failCount = 0, lastProg = null;
var logEl, pendingUrl = null;
var STATUS_LABEL = { fetching:"Fetching info", downloading:"Downloading",
                     processing:"Processing", done:"Completed", failed:"Failed" };
var ICON_OK = '<svg class="okmark" width="18" height="18" viewBox="0 0 24 24" fill="none"' +
  ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M20 6 9 17l-5-5"/></svg>';
var ICON_BAD = '<svg class="badmark" width="18" height="18" viewBox="0 0 24 24" fill="none"' +
  ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
function toggleConsole() {
  document.getElementById("console").classList.toggle("open");
}
function lineClass(t) {
  if (t.indexOf("[+]") === 0) return "c-green";
  if (t.indexOf("[!]") === 0 || t.indexOf("ERROR") === 0) return "c-red";
  if (t.indexOf("[post]") === 0) return "c-blue";
  if (t.indexOf("[*]") === 0) return "c-pink";
  if (t.indexOf("[deps]") === 0 || t.indexOf("[login]") === 0) return "c-dim";
  return "";
}
function setChip(el, label, cls) {
  el.className = "chip " + cls;
  el.innerHTML = '<span class="dot"></span>' + label;
}
var ui = {
  item: function (o) {
    var e = document.getElementById("empty");
    if (e) e.remove();
    var el = document.getElementById("q-" + o.key);
    if (!el) {
      el = document.createElement("div");
      el.className = "qcard";
      el.id = "q-" + o.key;
      el.innerHTML = '<img class="qthumb" style="display:none" alt="">' +
        '<div class="qbody"><div class="qtitle">…</div>' +
        '<div class="qmeta">Starting</div><div class="qbar"><i></i></div></div>' +
        '<div class="qstate"><span class="spin"></span></div>';
      document.getElementById("queue").prepend(el);
    }
    if (o.thumb) {
      var im = el.querySelector(".qthumb");
      if (!im.getAttribute("src")) {
        im.onerror = function () { im.style.display = "none"; };
        im.src = o.thumb;
        im.style.display = "";
      }
    }
    if (o.title) el.querySelector(".qtitle").textContent = o.title;
    if (o.pct != null) el.querySelector(".qbar i").style.width = o.pct + "%";
    if (o.status) {
      el.className = "qcard " + o.status;
      var meta = STATUS_LABEL[o.status] || o.status;
      if (o.status === "downloading" && o.pct != null) {
        meta += " · " + Math.round(o.pct) + "%";
        if (o.speed) meta += " · " + o.speed;
        if (o.eta) meta += " · ETA " + o.eta;
      }
      el.querySelector(".qmeta").textContent = meta;
      var st = el.querySelector(".qstate");
      if (o.status === "done") st.innerHTML = ICON_OK;
      else if (o.status === "failed") st.innerHTML = ICON_BAD;
      else if (!st.querySelector(".spin")) st.innerHTML = '<span class="spin"></span>';
    }
  },
  log: function (t) {
    var isProg = t.indexOf("[download]") === 0 && t.indexOf("%") !== -1;
    if (isProg && lastProg) { lastProg.textContent = t; }
    else {
      var d = document.createElement("div");
      d.textContent = t;
      var c = lineClass(t);
      if (c) d.className = c;
      logEl.appendChild(d);
      lastProg = isProg ? d : null;
      if (logEl.childElementCount > 2000) logEl.removeChild(logEl.firstChild);
    }
    logEl.scrollTop = logEl.scrollHeight;
  },
  setState: function (s) {
    document.getElementById("dir").textContent = s.dir;
    setChip(document.getElementById("auth"),
            s.logged_in ? "signed in" : "login needed",
            s.logged_in ? "ok" : "warn");
    setChip(document.getElementById("deps"),
            s.deps_ok ? "deps ready" : "deps missing",
            s.deps_ok ? "ok" : "warn");
    document.getElementById("dl").disabled = s.busy || !s.deps_ok;
    document.getElementById("s-go").disabled = s.busy || !s.deps_ok;
    document.getElementById("cancel").disabled = !s.busy;
  },
  done: function (ok) {
    if (ok) { okCount++; } else { failCount++; }
    setChip(document.getElementById("counts"),
            "ok " + okCount + " · failed " + failCount,
            failCount ? "warn" : (okCount ? "ok" : ""));
  }
};
function qualityChanged() {
  var q = document.getElementById("quality").value;
  document.getElementById("customfmt").className = q === "custom" ? "active" : "";
}
function startDl() {
  var u = document.getElementById("url").value.trim();
  if (!u) { ui.log("[!] paste a URL first"); return; }
  pendingUrl = u;
  openSheet(u);
  pywebview.api.fetch_info(u).then(function (info) {
    if (pendingUrl === u) fillSheet(info, u);
  })["catch"](function () {
    if (pendingUrl === u) fillSheet({ ok: false, error: "" }, u);
  });
}
function openSheet(u) {
  var t = document.getElementById("s-thumb");
  t.style.display = "none";
  t.removeAttribute("src");
  document.getElementById("s-title").textContent = "Fetching info…";
  document.getElementById("s-sub").textContent = u;
  var isPl = u.indexOf("list=") !== -1 || u.indexOf("/@") !== -1;
  document.getElementById("plrow").className = isPl ? "srow show" : "srow";
  document.getElementById("fmtout").style.display = "none";
  document.getElementById("scrim").className = "open";
  document.getElementById("sheet").className = "open";
}
function fillSheet(info, u) {
  var t = document.getElementById("s-thumb");
  if (info && info.ok) {
    document.getElementById("s-title").textContent = info.title;
    var sub = info.uploader || "";
    if (info.kind === "playlist") {
      sub = (sub ? sub + " · " : "") + info.count + " videos";
      document.getElementById("plrow").className = "srow show";
    } else if (info.duration) {
      sub = (sub ? sub + " · " : "") + info.duration;
    }
    document.getElementById("s-sub").textContent = sub;
    if (info.thumb) {
      t.onerror = function () { t.style.display = "none"; };
      t.src = info.thumb;
      t.style.display = "";
    }
  } else {
    document.getElementById("s-title").textContent =
      "Couldn't fetch info — you can still download";
    document.getElementById("s-sub").textContent = (info && info.error) || u;
  }
}
function closeSheet() {
  document.getElementById("scrim").className = "";
  document.getElementById("sheet").className = "";
}
function confirmDl() {
  if (!pendingUrl) return;
  var q = document.getElementById("quality").value;
  var mode = q === "default" ? "default" : "custom";
  var custom = q === "custom" ? document.getElementById("customfmt").value : q;
  if (q === "default") custom = "";
  closeSheet();
  pywebview.api.start_download(
    pendingUrl, mode, custom,
    document.getElementById("plstart").value,
    document.getElementById("plend").value,
    document.getElementById("ck-watched").checked,
    document.getElementById("ck-stamp").checked
  ).then(function (r) {
    if (r === "busy") ui.log("[!] a download is already running");
    if (r === "no-deps") ui.log("[!] dependencies missing — tap the refresh icon");
  });
}
function sheetFormats() {
  var out = document.getElementById("fmtout");
  out.style.display = "block";
  out.textContent = "Fetching formats…";
  pywebview.api.list_formats(pendingUrl || document.getElementById("url").value)
    .then(function (t) { out.textContent = t; });
}
function pickDir() {
  pywebview.api.pick_folder().then(function (d) {
    document.getElementById("dir").textContent = d;
  });
}
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") closeSheet();
});
window.addEventListener("pywebviewready", function () {
  logEl = document.getElementById("log");
  pywebview.api.get_state().then(function (s) {
    ui.setState(s);
    document.getElementById("ck-watched").checked = s.mark_watched !== false;
    document.getElementById("ck-stamp").checked = s.set_timestamp !== false;
  });
});
</script></body></html>"""


def bootstrap(api):
    for _ in range(60):  # wait for the page before pushing log lines
        try:
            UI_WIN.evaluate_js("1")
            break
        except Exception:
            time.sleep(0.25)
    api._push(f"[*] {APP_NAME} started — data dir: {APP_DIR}")
    ensure_deps(api._push)
    api._set_state()
    api._push("[login] checking sign-in state...")
    api._recheck_login()


def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if "--setup" in sys.argv:
        ok = ensure_deps(log)
        sys.exit(0 if ok else 1)
    # purge cookies extracted by earlier versions — nothing leaves the profile now
    (APP_DIR / "cookies_youtube.txt").unlink(missing_ok=True)
    global UI_WIN
    api = Api()
    UI_WIN = webview.create_window(APP_NAME, html=HTML, js_api=api,
                                   width=980, height=760, min_size=(760, 560))
    webview.start(lambda: bootstrap(api), private_mode=False,
                  storage_path=str(PROFILE_DIR))


if __name__ == "__main__":
    main()
