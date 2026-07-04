"""Seal-style YouTube login for yt-dlp on Windows.

Opens a WebView2 window (Edge runtime, ships with Windows 11) with its own
persistent profile stored next to this script. Log in once; after that
`--refresh` silently dumps fresh cookies to cookies_youtube.txt.

Usage:
  python yt_login.py            visible window: log into YouTube, then close it
  python yt_login.py --refresh  hidden window: rewrite cookies_youtube.txt
                                exit 0 = ok, 2 = not logged in, 1 = error
"""
import sys
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import webview

SCRIPT_DIR = Path(__file__).resolve().parent
COOKIES_FILE = SCRIPT_DIR / "cookies_youtube.txt"
PROFILE_DIR = SCRIPT_DIR / ".yt_webview_profile"
URL = "https://www.youtube.com"


def netscape(jars):
    lines = ["# Netscape HTTP Cookie File"]
    for jar in jars:
        for name, m in jar.items():
            domain = m["domain"] or ".youtube.com"
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
            lines.append("\t".join([
                prefix + domain,
                "TRUE" if domain.startswith(".") else "FALSE",
                m["path"] or "/",
                "TRUE" if m["secure"] else "FALSE",
                str(epoch),
                name,
                m.value,
            ]))
    return "\n".join(lines) + "\n"


def logged_in(jars):
    return any(name == "LOGIN_INFO" for jar in jars for name in jar)


def grab(window):
    """Snapshot cookies, but only while actually on youtube.com (mid-login
    the window is on accounts.google.com and we'd dump the wrong domain)."""
    try:
        jars = window.get_cookies()
    except Exception:
        return None
    if jars and any("youtube" in (m["domain"] or "") for j in jars for m in j.values()):
        return jars
    return None


def main():
    refresh = "--refresh" in sys.argv
    window = webview.create_window(
        "Refreshing cookies..." if refresh else "Log into YouTube, then close this window",
        URL,
        hidden=refresh,
        width=1100,
        height=800,
    )
    result = {"code": 1}
    done = threading.Event()

    def worker():
        closed = threading.Event()
        window.events.closed += lambda *a: closed.set()
        last = None
        deadline = time.time() + (45 if refresh else 24 * 3600)
        while not closed.is_set() and time.time() < deadline:
            time.sleep(2)
            jars = grab(window)
            if jars:
                last = jars
                if refresh and logged_in(jars):
                    break
        if last:
            COOKIES_FILE.write_text(netscape(last), encoding="utf-8", newline="\n")
            if logged_in(last):
                print(f"[+] Wrote {sum(len(j) for j in last)} cookies -> {COOKIES_FILE}")
                result["code"] = 0
            else:
                print("[!] Cookies written, but you are NOT logged in.")
                print("    Run without --refresh and log into YouTube in the window.")
                result["code"] = 2
        else:
            print("[!] Could not read cookies from the browser window.")
        done.set()
        if not closed.is_set():
            try:
                window.destroy()
            except Exception:
                pass

    webview.start(worker, private_mode=False, storage_path=str(PROFILE_DIR))
    done.wait(timeout=15)
    sys.exit(result["code"])


if __name__ == "__main__":
    main()
