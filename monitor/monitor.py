"""Account-deactivation monitor.

Every CHECK_INTERVAL_MINUTES it logs into each MindBody account (headless
Playwright, same flow as the main app's bot.py) and classifies it as:

  active       — login produced an idsrvauth cookie and the schedule loads
  deactivated  — credentials were rejected / a deactivation notice is shown
                 (and it was NOT a Cloudflare challenge)
  unknown      — Cloudflare challenge, timeout, or network error (transient)

It persists the last known status per account to /data and fires a Slack
alert only on the active -> deactivated edge (or first-seen-deactivated), so
transient failures never page you. A recovery notice is sent when an account
comes back.

Runs forever; a single failing check never kills the loop.
"""

import os
import sys
import json
import time
import traceback
from datetime import datetime, timezone

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

# Reuse the main app's login flow + constants so there's one source of truth
# for selectors, headers, and Cloudflare-challenge detection.
from bot import (
    BASE_URL,
    HEADERS,
    _CHALLENGE_MARKERS,
    apply_cookies,
    check_session,
)

DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DATA_DIR, "account_status.json")

STUDIO_ID = os.environ.get("MB_STUDIO_ID", "836167")


def _envq(key: str, default: str = "") -> str:
    """Read an env var, stripping one layer of surrounding quotes (Docker's
    --env-file keeps quotes literal)."""
    v = os.environ.get(key, default)
    if v and len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def _int_env(key: str, default: int) -> int:
    try:
        return int(_envq(key) or default)
    except (TypeError, ValueError):
        return default


CHECK_INTERVAL_MINUTES = _int_env("CHECK_INTERVAL_MINUTES", 5)
# Consecutive deactivated reads required before alerting (guards transients).
DEACTIVATION_CONFIRM_RUNS = _int_env("DEACTIVATION_CONFIRM_RUNS", 2)

SLACK_WEBHOOK_URL = _envq("SLACK_WEBHOOK_URL")

# Markers that mean the account is disabled/rejected (not just a challenge).
# Extend via DEACTIVATION_MARKERS (comma-separated).
_DEFAULT_DEACTIVATION_MARKERS = (
    "deactivat",            # "deactivated", "has been deactivated"
    "no longer active",
    "account is not active",
    "not active",
    "disabled",
    "suspended",
    "locked",
    "incorrect",            # creds rejected despite known-good password
    "invalid email or password",
    "does not exist",
)


def _deactivation_markers() -> tuple[str, ...]:
    extra = _envq("DEACTIVATION_MARKERS")
    markers = list(_DEFAULT_DEACTIVATION_MARKERS)
    if extra:
        markers += [m.strip().lower() for m in extra.split(",") if m.strip()]
    return tuple(markers)


def load_accounts() -> list[dict]:
    """Accounts come from MB1_*, MB2_, ... env vars (falls back to MB_* as one
    account). Mirrors the main app's loader so the same .env works here."""
    accts = []
    for i in range(1, 6):
        email = _envq(f"MB{i}_EMAIL")
        pw = _envq(f"MB{i}_PASSWORD")
        if email and pw:
            accts.append({
                "id": str(i),
                "name": _envq(f"MB{i}_NAME") or email.split("@")[0],
                "email": email,
                "password": pw,
            })
    if not accts:
        email, pw = _envq("MB_EMAIL"), _envq("MB_PASSWORD")
        if email and pw:
            accts.append({"id": "1", "name": email.split("@")[0], "email": email, "password": pw})
    return accts


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def notify_slack(text: str) -> bool:
    """POST a message to the configured Slack incoming webhook."""
    if not SLACK_WEBHOOK_URL:
        print("[slack] SLACK_WEBHOOK_URL not set — would have sent:\n" + text)
        return False
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
        if resp.status_code == 200:
            print("[slack] alert sent")
            return True
        print(f"[slack] webhook returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[slack] send failed: {e}")
    return False


def check_account_status(browser, acct: dict) -> tuple[str, str]:
    """Probe one account with a fresh browser context.

    Returns (status, detail) where status is 'active' | 'deactivated' | 'unknown'.
    """
    context = browser.new_context(
        user_agent=HEADERS["User-Agent"],
        viewport={"width": 1280, "height": 720},
        ignore_https_errors=True,
    )
    try:
        page = context.new_page()
        page.goto(f"{BASE_URL}/classic/ws?studioid={STUDIO_ID}",
                  wait_until="networkidle", timeout=60000)
        if "/classic/home" in page.url or "su1.asp" not in page.url:
            page.goto(f"{BASE_URL}/ASP/su1.asp?studioid={STUDIO_ID}",
                      wait_until="networkidle", timeout=30000)

        # If the login form isn't reachable, that's almost always a challenge
        # or transient page — treat as unknown, not deactivated.
        try:
            page.wait_for_selector("#su1UserName", timeout=30000)
        except Exception:
            body = (page.inner_text("body") or "").lower()
            if any(m in body for m in _CHALLENGE_MARKERS):
                return "unknown", "cloudflare challenge on login page"
            return "unknown", "login form did not load"

        page.fill("#su1UserName", acct["email"])
        page.fill("#su1Password", acct["password"])
        page.click("#btnSu1Login")

        # Login is async (reCAPTCHA -> POST -> redirect -> auth cookie). Poll for
        # the idsrvauth cookie the same way the main app does.
        got_auth = False
        for _ in range(30):
            page.wait_for_timeout(1000)
            if any(c["name"] == "idsrvauth" for c in context.cookies()):
                got_auth = True
                break
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        if got_auth:
            # Confirm by loading the schedule with a plain requests session,
            # exactly like the app's session check.
            cookies = {
                c["name"]: {"value": c["value"], "domain": c["domain"], "path": c.get("path", "/")}
                for c in context.cookies()
            }
            s = requests.Session()
            s.headers.update(HEADERS)
            proxy_url = os.environ.get("PROXY_URL", "")
            if proxy_url:
                s.proxies = {"http": proxy_url, "https": proxy_url}
                s.verify = False
            apply_cookies(s, cookies)
            if check_session(s):
                return "active", "login ok, schedule loaded"
            # Auth cookie but schedule won't load — ambiguous, don't cry wolf.
            return "unknown", "idsrvauth present but session check failed"

        # No auth cookie. Distinguish a real rejection from a challenge/timeout.
        body = (page.inner_text("body") or "").lower()
        if any(m in body for m in _CHALLENGE_MARKERS):
            return "unknown", "cloudflare challenge after submit"
        hit = next((m for m in _deactivation_markers() if m in body), None)
        if hit:
            return "deactivated", f"login rejected (matched '{hit}')"
        return "unknown", "no auth cookie, no known error text"
    finally:
        context.close()


def _fmt_account(acct: dict) -> str:
    return f"{acct['name']} <{acct['email']}> (account {acct['id']})"


def _handle_result(acct: dict, status: str, detail: str, state: dict) -> None:
    """Update state for one account and alert on active->deactivated edges."""
    aid = acct["id"]
    prev = state.get(aid, {})
    prev_status = prev.get("status")
    streak = prev.get("deactivated_streak", 0)
    ever_active = prev.get("ever_active", False)
    now = datetime.now(timezone.utc).isoformat()

    if status == "unknown":
        # Transient — keep the last known status, log, and move on.
        print(f"[check] {acct['name']}: unknown ({detail}); keeping prev status "
              f"'{prev_status or 'none'}'")
        state[aid] = {**prev, "id": aid, "name": acct["name"], "email": acct["email"],
                      "ever_active": ever_active,
                      "last_checked": now, "last_detail": detail}
        return

    if status == "active":
        if prev_status == "deactivated" and prev.get("alerted"):
            notify_slack(
                f":white_check_mark: *MindBody account reactivated*\n"
                f"{_fmt_account(acct)} is active again.\n"
                f"_{detail} — {now}_"
            )
        state[aid] = {"id": aid, "name": acct["name"], "email": acct["email"],
                      "status": "active", "deactivated_streak": 0, "alerted": False,
                      "ever_active": True,
                      "since": now if prev_status != "active" else prev.get("since", now),
                      "last_checked": now, "last_detail": detail}
        print(f"[check] {acct['name']}: active")
        return

    # status == "deactivated"
    streak += 1
    already_alerted = prev.get("alerted", False)
    entry = {"id": aid, "name": acct["name"], "email": acct["email"],
             "status": "deactivated", "deactivated_streak": streak,
             "alerted": already_alerted, "ever_active": ever_active,
             "since": prev.get("since", now) if prev_status == "deactivated" else now,
             "last_checked": now, "last_detail": detail}

    if not already_alerted and streak >= DEACTIVATION_CONFIRM_RUNS:
        header = (":rotating_light: *MindBody account deactivated*"
                  if ever_active else
                  ":rotating_light: *MindBody account is deactivated* (first check)")
        notify_slack(
            f"{header}\n"
            f"{_fmt_account(acct)} can no longer sign in.\n"
            f"Reason: {detail}\n"
            f"Confirmed over {streak} consecutive check(s). _{now}_"
        )
        entry["alerted"] = True
    else:
        print(f"[check] {acct['name']}: deactivated ({detail}); streak {streak}/"
              f"{DEACTIVATION_CONFIRM_RUNS}, alerted={entry['alerted']}")

    state[aid] = entry


def run_check() -> None:
    """One full pass over every account. Never raises — the scheduler must keep
    running no matter what a single check does."""
    accounts = load_accounts()
    if not accounts:
        print("[check] No accounts configured (set MB1_EMAIL/MB1_PASSWORD, ...).")
        return

    print(f"[check] === {datetime.now(timezone.utc).isoformat()} — {len(accounts)} account(s) ===")
    state = load_state()

    from playwright.sync_api import sync_playwright

    proxy_url = os.environ.get("PROXY_URL", "")
    try:
        with sync_playwright() as p:
            launch_args = {"headless": True}
            if proxy_url:
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                cfg = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}"}
                if parsed.username:
                    cfg["username"] = parsed.username
                if parsed.password:
                    cfg["password"] = parsed.password
                launch_args["proxy"] = cfg
            browser = p.chromium.launch(**launch_args)
            try:
                for acct in accounts:
                    try:
                        status, detail = check_account_status(browser, acct)
                    except Exception as e:
                        status, detail = "unknown", f"probe error: {type(e).__name__}: {e}"
                        print(f"[check] {acct['name']}: probe raised: {e}")
                    _handle_result(acct, status, detail, state)
            finally:
                browser.close()
    except Exception:
        print("[check] pass failed:\n" + traceback.format_exc())
    finally:
        try:
            save_state(state)
        except Exception as e:
            print(f"[check] could not persist state: {e}")


def main() -> None:
    print(f"[monitor] starting — interval {CHECK_INTERVAL_MINUTES} min, "
          f"confirm runs {DEACTIVATION_CONFIRM_RUNS}, state at {STATE_FILE}")
    if not SLACK_WEBHOOK_URL:
        print("[monitor] WARNING: SLACK_WEBHOOK_URL not set — alerts will only be logged.")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_check,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="account_status_check",
        next_run_time=datetime.now(timezone.utc),  # run immediately on boot
        max_instances=1,
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("[monitor] shutting down")


if __name__ == "__main__":
    sys.exit(main())
