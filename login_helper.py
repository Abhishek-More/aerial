#!/usr/bin/env python3
"""
Login helper / auto-reauth for the aerial booking app.

Uses a PERSISTENT real-Chrome profile (warmed by a one-time manual login) so that
MindBody's reCAPTCHA passes invisibly on subsequent automated logins. Captures the
session — including the httpOnly `idsrvauth` cookie a headless login can't get —
and uploads it to the running container via /api/upload-cookies.

Modes:
  (no args)   Interactive: opens a visible window, waits up to 5 min for you to
              log in / solve any challenge. Use this for the one-time warm-up
              and any time the profile's session has fully lapsed.
  --auto      Automatic: short timeout, no human. Reuses the warmed profile; if a
              reCAPTCHA challenge appears it gives up (exit 1) so the scheduler can
              alert you to re-run interactively. This is what launchd runs.

Env overrides:
  AERIAL_HEADLESS=1   run headless (mechanics testing only; reCAPTCHA will block)
  UPLOAD_URL          default http://localhost:5050/api/upload-cookies
  AERIAL_PROFILE_DIR  default ~/.aerial-chrome-profile
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

STUDIO_URL = "https://clients.mindbodyonline.com/classic/ws?studioid=836167"
MEMBER_URL = "https://clients.mindbodyonline.com/ASP/main_info.asp?studioid=836167"
UPLOAD_URL = os.environ.get("UPLOAD_URL", "http://localhost:5050/api/upload-cookies")
PROFILE_DIR = os.environ.get("AERIAL_PROFILE_DIR", os.path.expanduser("~/.aerial-chrome-profile"))
HEADLESS = os.environ.get("AERIAL_HEADLESS") == "1"
AUTO = "--auto" in sys.argv

# Generous human window when interactive; short unattended window when --auto.
LOGIN_TIMEOUT = 45 if AUTO else 300

# Pull credentials from api/.env to prefill / auto-submit the form.
ENV = {}
env_path = Path(__file__).parent / "api" / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            ENV[k.strip()] = v.strip().strip('"').strip("'")
EMAIL = ENV.get("MB_EMAIL", "")
PASSWORD = ENV.get("MB_PASSWORD", "")

from playwright.sync_api import sync_playwright


def _has_auth(ctx):
    return any(c["name"] == "idsrvauth" for c in ctx.cookies())


def main():
    mode = "AUTO" if AUTO else "INTERACTIVE"
    print(f"[helper] Mode: {mode} | profile: {PROFILE_DIR} | headless: {HEADLESS}")
    os.makedirs(PROFILE_DIR, exist_ok=True)

    with sync_playwright() as p:
        launch = dict(user_data_dir=PROFILE_DIR, headless=HEADLESS, viewport={"width": 1280, "height": 800})
        try:
            ctx = p.chromium.launch_persistent_context(channel="chrome", **launch)
        except Exception as e:
            print(f"[helper] Real Chrome unavailable ({e}); using bundled Chromium.")
            ctx = p.chromium.launch_persistent_context(**launch)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 1) Maybe the warmed profile is still logged in — check the member page first.
        print("[helper] Checking existing session...")
        page.goto(MEMBER_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        if not _has_auth(ctx):
            # 2) Need to log in. Go to the login form.
            page.goto(STUDIO_URL, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("#su1UserName", timeout=15000)
                if EMAIL:
                    page.fill("#su1UserName", EMAIL)
                if PASSWORD:
                    page.fill("#su1Password", PASSWORD)
                if AUTO:
                    # Unattended: submit immediately and rely on the warmed profile.
                    page.click("#btnSu1Login")
                    print("[helper] Submitted login.")
                else:
                    # Interactive: prefilled only — YOU click Log In when ready
                    # (e.g. after signing into Google in another tab first).
                    print("[helper] Credentials prefilled — not auto-submitting; click Log In yourself.")
            except Exception:
                if AUTO:
                    print("[helper] Login form not found and not authed — giving up (auto).")
                    ctx.close()
                    sys.exit(1)
                print("[helper] Log in manually in the window.")

            if not AUTO:
                print(f"\n  >>> In the Chrome window: click 'Log In' yourself (take your time, "
                      f"sign into Google first if you like), solve any reCAPTCHA. <<<\n")

            print(f"[helper] Waiting up to {LOGIN_TIMEOUT}s for idsrvauth cookie...")
            deadline = time.time() + LOGIN_TIMEOUT
            while time.time() < deadline and not _has_auth(ctx):
                page.wait_for_timeout(1000)

        if not _has_auth(ctx):
            print("[helper] No idsrvauth cookie. "
                  + ("reCAPTCHA likely needs a human — re-run interactively." if AUTO
                     else "Login not completed."))
            ctx.close()
            sys.exit(1)

        page.wait_for_timeout(1500)  # let idsrvauth1 / related cookies settle
        cookies = ctx.cookies()
        jar = {
            c["name"]: {"value": c["value"], "domain": c["domain"], "path": c.get("path", "/")}
            for c in cookies
        }
        ctx.close()

    print(f"[helper] Captured {len(jar)} cookies (idsrvauth present: {'idsrvauth' in jar}).")
    print(f"[helper] Uploading to {UPLOAD_URL} ...")
    req = urllib.request.Request(
        UPLOAD_URL,
        data=json.dumps(jar).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("[helper] Upload response:", resp.read().decode())
        print("[helper] Done. The app is now authenticated.")
    except Exception as e:
        print(f"[helper] Upload failed: {e}")
        print("[helper] Is the container running and mapped to localhost:5050?")
        sys.exit(1)


if __name__ == "__main__":
    main()
