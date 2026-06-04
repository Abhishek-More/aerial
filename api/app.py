import json
import os
import random
import re
import smtplib
import sys
import threading
import time as _time
from collections import deque
from datetime import datetime, timedelta
from email.message import EmailMessage

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from flask import Flask, Response, jsonify, render_template, request

from bot import get_classes, get_session, signup_for_class, login_with_playwright, get_credentials, apply_cookies, save_cookie_jar, check_session, get_user_name, HEADERS, COOKIE_JAR_FILE, BASE_URL

app = Flask(__name__)

# --- Boot log capture ---
_boot_log = deque(maxlen=200)
_boot_done = threading.Event()
_original_stdout = sys.stdout


class _BootLogCapture:
    """Tee stdout to both the terminal and the boot log buffer."""
    def __init__(self, original):
        self._original = original

    def write(self, msg):
        self._original.write(msg)
        if msg.strip():
            _boot_log.append(msg.strip())

    def flush(self):
        self._original.flush()


sys.stdout = _BootLogCapture(_original_stdout)

DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
LOG_FILE = os.path.join(DATA_DIR, "booking_log.json")

# Signup opens exactly 1 week + 15 minutes before class start
SIGNUP_OFFSET = timedelta(weeks=1, minutes=15)
# Re-auth 5 minutes before signup opens
REAUTH_BEFORE = timedelta(minutes=5)

# "Notify when a full class opens up" — re-check on a randomized interval
FULL_CHECK_MIN = 300  # 5 min
FULL_CHECK_MAX = 600  # 10 min
# Auto-book a freed-up watched class only if it starts at least this far out.
BOOK_LEAD_TIME = timedelta(hours=24)

# Lightweight activity counters, summarized hourly (see hourly_recap)
_stats_lock = threading.Lock()
STATS = {"checks": 0, "cache_refreshes": 0, "opens": 0, "booked": 0, "book_failed": 0,
         "emails_sent": 0, "emails_failed": 0, "reauths": 0, "cookie_uploads": 0,
         "auth_checks": 0, "auth_heals": 0}


def bump(key, n=1):
    with _stats_lock:
        STATS[key] = STATS.get(key, 0) + n

# Global session
_session = None
_session_lock = threading.Lock()

# In-memory class cache
_class_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def get_bot_session():
    global _session
    _boot_done.wait()  # block until startup init finishes
    with _session_lock:
        if _session is None:
            print("[app] Initializing bot session...")
            _session = get_session()
            print("[app] Bot session ready.")
        return _session


def _init_session_background():
    """Run session init eagerly on startup so boot logs stream in real time."""
    global _session
    try:
        with _session_lock:
            print("[app] Initializing bot session...")
            _session = get_session()
            print("[app] Bot session ready.")
    except Exception as e:
        print(f"[app] Session init failed: {e}")
    finally:
        _boot_done.set()
        sys.stdout = _original_stdout


def _is_authed() -> bool:
    """True if the current session carries an auth cookie."""
    return bool(_session) and any(c.name == "idsrvauth" for c in _session.cookies)


def _write_reauth_flag():
    """Drop the flag the host watcher reacts to (runs the headed login helper)."""
    flag = os.path.join(DATA_DIR, ".reauth_request")
    with open(flag, "w") as f:
        f.write(datetime.now().isoformat())


def trigger_host_reauth(timeout: int = 90) -> bool:
    """Ask the host to run its reliable headed-Chrome login (same as ./refresh-login),
    then wait for it to push fresh cookies back via /api/upload-cookies.

    This is preferred over headless force_reauth, which reCAPTCHA usually blocks.
    Returns True if a fresh cookie upload arrived within `timeout` seconds."""
    with _stats_lock:
        before_uploads = STATS.get("cookie_uploads", 0)
    bump("reauths")
    _write_reauth_flag()
    print(f"[reauth] Requested host headed-login; waiting up to {timeout}s for fresh cookies...")
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        with _stats_lock:
            if STATS.get("cookie_uploads", 0) > before_uploads:
                print("[reauth] Fresh cookies received from host login.")
                return True
        _time.sleep(2)
    print("[reauth] Host reauth did not complete in time (is the Mac logged in / profile warmed?).")
    return False


def force_reauth():
    """Headless re-auth fallback (often blocked by reCAPTCHA). Prefer trigger_host_reauth."""
    global _session
    bump("reauths")
    print("[reauth] Forcing re-authentication (headless fallback)...")
    with _session_lock:
        import requests as req
        _session = req.Session()
        _session.headers.update(HEADERS)
        email, password = get_credentials()
        cookies = login_with_playwright(email, password)
        apply_cookies(_session, cookies)
        # Save cookie jar
        import json as _json
        with open(COOKIE_JAR_FILE, "w") as f:
            _json.dump(cookies, f)
        has_auth = any(c.name == "idsrvauth" for c in _session.cookies)
        print(f"[reauth] Done. Has idsrvauth: {has_auth}")


def boot_auth_heal():
    """Shortly after startup, if we're not authenticated (e.g. cookie jar expired
    and the headless login was blocked), pull a fresh session from the host login."""
    _boot_done.wait()
    if not _is_authed():
        print("[reauth] Not authenticated after boot — requesting host reauth.")
        trigger_host_reauth(timeout=120)


def _session_valid() -> bool:
    """Quiet validity probe: do we have an idsrvauth cookie AND a working session?"""
    if not _is_authed():
        return False
    try:
        r = _session.get(f"{BASE_URL}/classic/mainclass?fl=true&tabID=7",
                         allow_redirects=False, timeout=15)
        return r.status_code == 200 and "resetSession" not in r.text and "classSchedule" in r.text
    except Exception:
        return False


def hourly_auth_check():
    """Once an hour, confirm the session is genuinely authenticated; if not, pull a
    fresh login from the host. Quiet on success to keep the log clean."""
    bump("auth_checks")
    if _session_valid():
        return
    print("[auth-check] Session not valid (missing/expired idsrvauth) — requesting host reauth.")
    bump("auth_heals")
    trigger_host_reauth(timeout=120)


def get_cached_classes(date="", location="0", category="0", force=False):
    """Return classes from cache if fresh, otherwise fetch and cache."""
    cache_key = (date, location, category)
    now = _time.time()

    with _cache_lock:
        cached = _class_cache.get(cache_key)
        if cached and not force and (now - cached["fetched_at"]) < CACHE_TTL:
            return cached["classes"]

    session = get_bot_session()
    classes = get_classes(session, date=date, location=location, class_type=category)

    with _cache_lock:
        _class_cache[cache_key] = {"classes": classes, "fetched_at": _time.time()}

    return classes


def refresh_default_cache():
    """Background job: keep the default view warm."""
    try:
        get_cached_classes(date="", location="0", category="0", force=True)
        bump("cache_refreshes")
    except Exception as e:
        print(f"[scheduler] Cache refresh error: {type(e).__name__}: {e}")


# --- Watchlist helpers ---

def load_watchlist() -> list[dict]:
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    return []


def save_watchlist(watchlist: list[dict]):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f, indent=2)


def load_log() -> list[dict]:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return []


def append_log(entry: dict):
    log = load_log()
    log.insert(0, entry)
    log = log[:100]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


# --- Precise booking scheduler ---

def parse_class_datetime(class_date: str, time_str: str) -> datetime | None:
    """Parse class_date (M/D/YYYY) and time (e.g. '9:30 am EDT') into a datetime."""
    if not class_date or not time_str:
        print(f"[parse] Empty date='{class_date}' or time='{time_str}'")
        return None
    try:
        time_clean = re.sub(r"[\xa0\s]+", " ", time_str).strip()
        time_clean = re.sub(r"\s+[A-Z]{2,4}$", "", time_clean).strip()
        result = datetime.strptime(f"{class_date} {time_clean}", "%m/%d/%Y %I:%M %p")
        return result
    except (ValueError, TypeError) as e:
        print(f"[parse] Failed to parse date='{class_date}' time='{time_str}': {e}")
        return None


def compute_open_time(class_dt: datetime) -> datetime:
    """Signup opens 1 week + 15 minutes before class start."""
    return class_dt - SIGNUP_OFFSET


def compute_reauth_time(open_time: datetime) -> datetime:
    """Re-auth 5 minutes before signup opens."""
    return open_time - REAUTH_BEFORE


def schedule_snag(watch: dict):
    """Schedule re-auth and rapid booking for a watched class."""
    class_dt = parse_class_datetime(watch.get("class_date", ""), watch.get("time", ""))
    if not class_dt:
        print(f"[snag] Cannot parse datetime for: {watch.get('class_name')}")
        return

    open_time = compute_open_time(class_dt)
    reauth_time = compute_reauth_time(open_time)
    now = datetime.now()

    watch_key = f"{watch['class_name']}_{watch.get('class_date','')}_{watch.get('time','')}"
    safe_id = re.sub(r"[^a-zA-Z0-9_]", "", watch_key)[:60]

    print(f"[snag] Class: {watch['class_name']} on {watch.get('class_date')} at {watch.get('time')}")
    print(f"[snag]   Class time:  {class_dt}")
    print(f"[snag]   Signup opens: {open_time}")
    print(f"[snag]   Re-auth at:  {reauth_time}")

    # Schedule re-auth (only if in the future)
    reauth_job_id = f"reauth_{safe_id}"
    if reauth_time > now:
        # Remove existing job if rescheduling
        try:
            scheduler.remove_job(reauth_job_id)
        except Exception:
            pass
        scheduler.add_job(
            trigger_host_reauth,
            trigger=DateTrigger(run_date=reauth_time),
            id=reauth_job_id,
            replace_existing=True,
        )
        print(f"[snag]   Re-auth scheduled for {reauth_time}")
    else:
        print(f"[snag]   Re-auth time already passed, skipping")

    # Schedule rapid booking attempts starting at open time
    snag_job_id = f"snag_{safe_id}"
    if open_time > now:
        try:
            scheduler.remove_job(snag_job_id)
        except Exception:
            pass
        scheduler.add_job(
            rapid_book,
            trigger=DateTrigger(run_date=open_time),
            args=[watch],
            id=snag_job_id,
            replace_existing=True,
        )
        print(f"[snag]   Booking scheduled for {open_time}")
    elif open_time > now - timedelta(minutes=5):
        # Signup just opened recently — try immediately
        print(f"[snag]   Signup just opened, attempting now...")
        threading.Thread(target=rapid_book, args=[watch], daemon=True).start()
    else:
        print(f"[snag]   Signup opened long ago — will try on next cache refresh")


def rapid_book(watch: dict):
    """
    Rapid-fire booking: try every 2 seconds for 60 seconds.
    The signup link may take a moment to appear after the open time.
    """
    class_name = watch.get("class_name", "?")
    class_date = watch.get("class_date", "")
    time_str = watch.get("time", "")
    print(f"[rapid_book] Starting rapid booking for {class_name} on {class_date} at {time_str}")

    append_log({
        "time": datetime.now().isoformat(),
        "action": "snag_started",
        "class": class_name,
        "date": class_date,
    })

    session = get_bot_session()
    attempts = 0
    max_attempts = 30  # 30 attempts * 2s = 60 seconds

    while attempts < max_attempts:
        attempts += 1
        now = datetime.now()
        print(f"[rapid_book] Attempt {attempts}/{max_attempts} at {now.strftime('%H:%M:%S')}")

        try:
            # Fetch fresh class list to find the class_id
            classes = get_classes(session, date=class_date, location="0", class_type="0")

            # Find our class
            for cls in classes:
                if (cls["has_signup"]
                        and cls["class_id"]
                        and cls["name"] == watch.get("class_name")
                        and cls["class_date"] == class_date
                        and cls["time"] == time_str):

                    print(f"[rapid_book] Found signup for {class_name}! class_id={cls['class_id']}")

                    result = signup_for_class(session, cls["class_id"], cls["class_date"])
                    print(f"[rapid_book] RESULT: {result}")
                    success = "successfully booked" in result.lower()

                    # Update watchlist
                    watchlist = load_watchlist()
                    for w in watchlist:
                        if (w["class_name"] == class_name
                                and w.get("class_date") == class_date
                                and w.get("time") == time_str):
                            w["status"] = "booked" if success else "book_failed"
                            w["result"] = result
                            w["booked_at"] = now.isoformat()
                            w["class_id"] = cls["class_id"]
                            break
                    save_watchlist(watchlist)

                    emailed = send_booking_email(
                        {"class_name": class_name, "class_date": class_date,
                         "time": time_str, "teacher": watch.get("teacher", "")},
                        result, success, source="auto-book at signup-open")
                    bump("booked" if success else "book_failed")
                    bump("emails_sent" if emailed else "emails_failed")

                    append_log({
                        "time": now.isoformat(),
                        "action": "snagged",
                        "class": class_name,
                        "date": class_date,
                        "result": result,
                        "attempts": attempts,
                    })
                    return

            print(f"[rapid_book] Signup not available yet...")

        except Exception as e:
            print(f"[rapid_book] Attempt {attempts} error: {e}")

        _time.sleep(2)

    # Exhausted attempts
    print(f"[rapid_book] Failed to snag {class_name} after {max_attempts} attempts")
    append_log({
        "time": datetime.now().isoformat(),
        "action": "snag_failed",
        "class": class_name,
        "date": class_date,
        "attempts": max_attempts,
    })


def schedule_all_watches():
    """On startup / when watchlist changes, schedule all pending watches."""
    watchlist = load_watchlist()
    for watch in watchlist:
        if watch.get("status") == "waiting":
            schedule_snag(watch)


def hourly_recap():
    """Print a once-an-hour summary of activity, then reset the counters."""
    with _stats_lock:
        s = dict(STATS)
        for k in STATS:
            STATS[k] = 0
    try:
        watchlist = load_watchlist()
        notify_active = sum(1 for w in watchlist if w.get("mode") == "notify" and w.get("status") == "watching")
        autobook_active = sum(1 for w in watchlist if w.get("mode") != "notify" and w.get("status") == "waiting")
    except Exception:
        notify_active = autobook_active = "?"
    print(
        f"[recap] Past hour — full-checks:{s['checks']} cache-refreshes:{s['cache_refreshes']} "
        f"spots-opened:{s['opens']} booked:{s['booked']}(failed {s['book_failed']}) "
        f"emails:{s['emails_sent']}(failed {s['emails_failed']}) "
        f"reauths:{s['reauths']} cookie-uploads:{s['cookie_uploads']} "
        f"auth-checks:{s['auth_checks']}(healed {s['auth_heals']}) | "
        f"active watches — notify:{notify_active} autobook:{autobook_active}"
    )


# --- "Notify when a full class opens up" ---

def _envq(key, default=""):
    """Read an env var, stripping one layer of surrounding quotes.
    Docker's --env-file keeps quotes literal (e.g. SMTP_PASS='"abc"'), so we
    normalize here to be resilient to however .env is quoted."""
    v = os.environ.get(key, default)
    if v and len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def _send_email(subject: str, body: str) -> bool:
    """Send an email via SMTP to all configured recipients (NOTIFY_EMAIL may be a
    comma-separated list; falls back to MB_EMAIL, then the sender). True on success."""
    smtp_user = _envq("SMTP_USER")
    smtp_pass = _envq("SMTP_PASS")
    smtp_host = _envq("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(_envq("SMTP_PORT", "587"))
    raw = _envq("NOTIFY_EMAIL") or _envq("MB_EMAIL") or smtp_user
    recipients = [a.strip() for a in raw.split(",") if a.strip()]

    if not smtp_user or not smtp_pass:
        print("[email] SMTP_USER/SMTP_PASS not set — cannot send email.")
        return False
    if not recipients:
        print("[email] No recipient address available.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"[email] Sent '{subject}' to {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"[email] Failed to send: {e}")
        return False


def _class_when(info: dict) -> str:
    return f"{info.get('date_label') or info.get('class_date', '')} at {info.get('time', '')}"


def send_open_email(watch: dict, open_spots: int) -> bool:
    """A watched class freed up but starts within 24h, so it was NOT auto-booked."""
    cls = watch.get("class_name", "class")
    return _send_email(
        f"Spot open (not booked — within 24h): {cls} — {_class_when(watch)}",
        f"A spot just opened in a class you're watching, but it starts in under 24 hours, "
        f"so it was NOT auto-booked.\n\n"
        f"Class:   {cls}\n"
        f"When:    {_class_when(watch)}\n"
        f"Teacher: {watch.get('teacher', '')}\n"
        f"Open spots: {open_spots}\n\n"
        f"Book it yourself: https://clients.mindbodyonline.com/classic/mainclass\n"
    )


def send_booking_email(info: dict, result: str, success: bool, source: str) -> bool:
    """Email a confirmation (or failure notice) for any booking the app makes."""
    cls = info.get("class_name") or info.get("name", "class")
    status = "Booked" if success else "Booking FAILED"
    return _send_email(
        f"{status}: {cls} — {_class_when(info)}",
        f"{'A class was booked' if success else 'A booking attempt failed'} ({source}).\n\n"
        f"Class:   {cls}\n"
        f"When:    {_class_when(info)}\n"
        f"Teacher: {info.get('teacher', '')}\n"
        f"Result:  {result}\n"
    )


def _find_cached_class(class_id: str, class_date: str) -> dict | None:
    """Look up class details (name/time/teacher) from the cache by id + date."""
    with _cache_lock:
        for entry in _class_cache.values():
            for c in entry.get("classes", []):
                if str(c.get("class_id")) == str(class_id) and c.get("class_date") == class_date:
                    return c
    return None


def schedule_next_full_check(delay: int | None = None):
    """(Re)schedule the full-class checker at a randomized 5-10 min interval."""
    if delay is None:
        delay = random.randint(FULL_CHECK_MIN, FULL_CHECK_MAX)
    run_at = datetime.now() + timedelta(seconds=delay)
    try:
        scheduler.add_job(
            check_full_watches,
            trigger=DateTrigger(run_date=run_at),
            id="full_watch_check",
            replace_existing=True,
        )
        pass  # next check scheduled (silent to reduce log noise)
    except Exception as e:
        print(f"[full_watch] Reschedule failed: {e}")


def check_full_watches():
    """Re-check every 'notify' watch: if a spot opened, email the user (no booking).
    Self-reschedules with a fresh random delay each run."""
    bump("checks")
    try:
        watchlist = load_watchlist()
        pending = [w for w in watchlist if w.get("mode") == "notify" and w.get("status") == "watching"]
        if not pending:
            return

        now = datetime.now()
        by_date = {}
        for w in pending:
            by_date.setdefault(w.get("class_date", ""), []).append(w)

        session = get_bot_session()
        changed = False
        for date, watches in by_date.items():
            try:
                classes = get_classes(session, date=date, location="0", class_type="0")
            except Exception as e:
                print(f"[full_watch] Fetch error for {date}: {e}")
                continue
            for w in watches:
                class_dt = parse_class_datetime(w.get("class_date", ""), w.get("time", ""))
                if class_dt and class_dt < now:
                    w["status"] = "expired"
                    changed = True
                    print(f"[full_watch] {w['class_name']} on {date} expired (class passed).")
                    continue
                match = next(
                    (c for c in classes
                     if c["name"] == w["class_name"]
                     and c["class_date"] == w.get("class_date")
                     and c["time"] == w.get("time")),
                    None,
                )
                if not match:
                    continue
                if match.get("has_signup") and match.get("open", 0) > 0:
                    bump("opens")
                    class_id = match.get("class_id") or w.get("class_id")
                    w["open_spots"] = match["open"]
                    w["class_id"] = class_id
                    changed = True

                    far_enough = class_dt is not None and (class_dt - now) >= BOOK_LEAD_TIME
                    if far_enough and class_id:
                        # >= 24h out → actually book it, then email the result.
                        print(f"[full_watch] OPEN: {w['class_name']} on {date} ({match['open']} spot) — booking (>=24h out).")
                        result = signup_for_class(session, class_id, match["class_date"])
                        if "session expired" in result.lower():
                            trigger_host_reauth()
                            session = get_bot_session()
                            result = signup_for_class(session, class_id, match["class_date"])
                        success = "successfully booked" in result.lower()
                        emailed = send_booking_email(w, result, success, source="notify auto-book")
                        bump("booked" if success else "book_failed")
                        bump("emails_sent" if emailed else "emails_failed")
                        w["status"] = "booked" if success else "book_failed"
                        w["result"] = result
                        w["booked_at"] = now.isoformat()
                        append_log({
                            "time": now.isoformat(), "action": "notify_book",
                            "class": w["class_name"], "date": w.get("class_date"),
                            "result": result, "success": success,
                        })
                    else:
                        # < 24h out (or no class_id) → notify only, don't book.
                        print(f"[full_watch] OPEN: {w['class_name']} on {date} ({match['open']} spot) — within 24h, emailing only.")
                        sent = send_open_email(w, match["open"])
                        bump("emails_sent" if sent else "emails_failed")
                        w["status"] = "opened"
                        w["opened_at"] = now.isoformat()
                        w["emailed"] = sent
                        append_log({
                            "time": now.isoformat(), "action": "notify_open",
                            "class": w["class_name"], "date": w.get("class_date"),
                            "open": match["open"], "emailed": sent,
                        })
                # else: still full — checked silently
        if changed:
            save_watchlist(watchlist)
    except Exception as e:
        print(f"[full_watch] Error: {e}")
    finally:
        schedule_next_full_check()


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/boot-log")
def api_boot_log():
    """SSE stream of boot/login progress messages."""
    def stream():
        sent = 0
        while not _boot_done.is_set():
            logs = list(_boot_log)
            for msg in logs[sent:]:
                yield f"data: {msg}\n\n"
            sent = len(logs)
            _time.sleep(0.3)
        # flush remaining
        logs = list(_boot_log)
        for msg in logs[sent:]:
            yield f"data: {msg}\n\n"
        yield "data: __DONE__\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/classes")
def api_classes():
    """Fetch all classes for the given week (served from cache)."""
    date = request.args.get("date", "")
    location = request.args.get("location", "0")
    category = request.args.get("category", "0")
    force = request.args.get("refresh", "") == "1"

    try:
        classes = get_cached_classes(date=date, location=location, category=category, force=force)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    watchlist = load_watchlist()
    watched_keys = {(w["class_name"], w.get("class_date", ""), w.get("time", "")) for w in watchlist}

    result = []
    for cls in classes:
        c = dict(cls)
        key = (c["name"], c.get("class_date", ""), c.get("time", ""))
        c["watched"] = key in watched_keys
        result.append(c)

    return jsonify(result)


@app.route("/api/watchlist")
def api_watchlist():
    watchlist = load_watchlist()
    now = datetime.now()
    # Enrich with computed open times and scheduler info
    for w in watchlist:
        if w.get("mode") == "notify":
            # Notify-watches don't have a signup-open countdown; phase tracks status.
            w["open_time"] = None
            w["reauth_time"] = None
            w["opens_in"] = None
            st = w.get("status")
            w["phase"] = {
                "booked": "done",
                "book_failed": "error",
                "opened": "opened",
                "expired": "expired",
            }.get(st, "notify")
            continue
        class_dt = parse_class_datetime(w.get("class_date", ""), w.get("time", ""))
        if class_dt:
            open_time = compute_open_time(class_dt)
            reauth_time = compute_reauth_time(open_time)
            w["open_time"] = open_time.isoformat()
            w["reauth_time"] = reauth_time.isoformat()
            w["opens_in"] = str(open_time - now).split(".")[0] if open_time > now else "now"
            # Scheduler phase
            if w.get("status") == "booked" or w.get("status") == "snagged":
                w["phase"] = "done"
            elif now >= open_time:
                w["phase"] = "booking"
            elif now >= reauth_time:
                w["phase"] = "reauth"
            else:
                w["phase"] = "scheduled"
        else:
            w["open_time"] = None
            w["reauth_time"] = None
            w["opens_in"] = "?"
            w["phase"] = "unknown"
    return jsonify(watchlist)


@app.route("/api/watch", methods=["POST"])
def api_watch():
    """Add a class to the watchlist and schedule its snag."""
    data = request.json
    watchlist = load_watchlist()

    # mode: "autobook" (snag when signup opens) or "notify" (email when a full class frees up)
    mode = data.get("mode", "autobook")

    entry = {
        "class_name": data["name"],
        "class_date": data.get("class_date", ""),
        "class_id": data.get("class_id"),
        "time": data.get("time", ""),
        "teacher": data.get("teacher", ""),
        "date_label": data.get("date", ""),
        "mode": mode,
        "status": "watching" if mode == "notify" else "waiting",
        "added_at": datetime.now().isoformat(),
    }

    # Don't add duplicates
    for w in watchlist:
        if (w["class_name"] == entry["class_name"]
                and w.get("class_date") == entry.get("class_date")
                and w.get("time") == entry.get("time")):
            return jsonify({"status": "already_watched"})

    watchlist.append(entry)
    save_watchlist(watchlist)

    if mode == "notify":
        # Picked up by the recurring checker; run a check soon so it works right away.
        schedule_next_full_check(delay=5)
        return jsonify({"status": "added", "mode": "notify"})

    # autobook: schedule the snag for this class
    schedule_snag(entry)
    class_dt = parse_class_datetime(entry["class_date"], entry["time"])
    open_time = compute_open_time(class_dt) if class_dt else None
    return jsonify({
        "status": "added",
        "mode": "autobook",
        "open_time": open_time.isoformat() if open_time else None,
    })


@app.route("/api/unwatch", methods=["POST"])
def api_unwatch():
    """Remove a class from the watchlist and cancel its scheduled jobs."""
    data = request.json
    watchlist = load_watchlist()

    # Find the watch to remove and cancel its jobs
    for w in watchlist:
        if (w["class_name"] == data["name"]
                and w.get("class_date") == data.get("class_date")
                and w.get("time") == data.get("time")):
            watch_key = f"{w['class_name']}_{w.get('class_date','')}_{w.get('time','')}"
            safe_id = re.sub(r"[^a-zA-Z0-9_]", "", watch_key)[:60]
            for prefix in ["reauth_", "snag_"]:
                try:
                    scheduler.remove_job(f"{prefix}{safe_id}")
                    print(f"[unwatch] Cancelled job {prefix}{safe_id}")
                except Exception:
                    pass
            break

    watchlist = [
        w for w in watchlist
        if not (w["class_name"] == data["name"]
                and w.get("class_date") == data.get("class_date")
                and w.get("time") == data.get("time"))
    ]
    save_watchlist(watchlist)
    return jsonify({"status": "removed"})


@app.route("/api/book", methods=["POST"])
def api_book():
    """Immediately book a class. Re-auths automatically if session expired."""
    data = request.json
    class_id = data.get("class_id")
    class_date = data.get("class_date")
    if not class_id or not class_date:
        return jsonify({"error": "Missing class_id or class_date"}), 400

    session = get_bot_session()
    try:
        result = signup_for_class(session, class_id, class_date)

        # If session expired, re-auth via the host headed login and retry once
        if "session expired" in result.lower():
            print("[api_book] Session expired, requesting host reauth and retrying...")
            trigger_host_reauth()
            session = get_bot_session()
            result = signup_for_class(session, class_id, class_date)

        success = "successfully booked" in result.lower()
        info = _find_cached_class(class_id, class_date) or {"class_id": class_id, "class_date": class_date}
        emailed = send_booking_email(info, result, success, source="manual book")
        bump("booked" if success else "book_failed")
        bump("emails_sent" if emailed else "emails_failed")

        append_log({
            "time": datetime.now().isoformat(),
            "action": "manual_book",
            "class_id": class_id,
            "date": class_date,
            "result": result,
        })
        return jsonify({"result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log")
def api_log():
    return jsonify(load_log())


@app.route("/api/upload-cookies", methods=["POST"])
def api_upload_cookies():
    """Upload cookies from local login to Railway's persistent storage."""
    global _session
    data = request.json
    if not data or not isinstance(data, dict):
        return jsonify({"error": "POST a JSON dict of cookies"}), 400

    # Save to persistent volume
    from bot import COOKIE_JAR_FILE
    with open(COOKIE_JAR_FILE, "w") as f:
        json.dump(data, f)

    # Apply to current session
    global _user_name
    _user_name = None  # re-resolve name for the new session
    with _session_lock:
        import requests as req
        _session = req.Session()
        _session.headers.update(HEADERS)
        apply_cookies(_session, data)

    bump("cookie_uploads")
    cookie_names = list(data.keys())
    has_auth = "idsrvauth" in cookie_names
    print(f"[upload-cookies] Received {len(data)} cookies. Has idsrvauth: {has_auth}")
    print(f"[upload-cookies] Cookie names: {cookie_names}")

    return jsonify({"status": "ok", "cookies": len(data), "has_idsrvauth": has_auth})


_user_name = None  # cached display name of the signed-in member


@app.route("/api/auth-status")
def api_auth_status():
    """Report whether the current session is truly authenticated (has idsrvauth),
    and the signed-in member's name."""
    global _user_name
    has = bool(_session) and any(c.name == "idsrvauth" for c in _session.cookies)
    if has and not _user_name:
        try:
            _user_name = get_user_name(_session)
        except Exception:
            _user_name = None
    return jsonify({"authenticated": has, "user": _user_name if has else None})


@app.route("/api/request-reauth", methods=["POST"])
def api_request_reauth():
    """Drop a flag in the shared /data volume. A host-side watcher picks this up
    and runs the headed Chrome login, then uploads fresh cookies. Lets you force
    a re-auth remotely (e.g. from your phone via the public URL)."""
    _write_reauth_flag()
    print("[reauth] Remote re-auth requested — flag written for host watcher.")
    return jsonify({"status": "reauth requested"})


# --- Eager session init ---
threading.Thread(target=_init_session_background, daemon=True).start()

# --- Scheduler startup ---
scheduler = BackgroundScheduler()

# Cache refresh every 5 min (delayed 10s on startup)
scheduler.add_job(refresh_default_cache, "interval", minutes=5, id="refresh_cache",
                  next_run_time=datetime.now() + timedelta(seconds=10))

# Schedule all existing watches on startup (delayed 15s)
scheduler.add_job(schedule_all_watches, trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=15)),
                  id="init_watches")

# Start the "notify when a full class opens" checker (first run 20s after boot,
# then self-reschedules every 5-10 min)
scheduler.add_job(check_full_watches, trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=20)),
                  id="full_watch_check")

# Hourly activity recap
scheduler.add_job(hourly_recap, "interval", hours=1, id="hourly_recap")

# Shortly after boot, self-heal auth via the host login if we're not authenticated
scheduler.add_job(boot_auth_heal, trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=25)),
                  id="boot_auth_heal")

# Hourly: verify we still have a valid idsrvauth session, reauth via host if not
scheduler.add_job(hourly_auth_check, "interval", hours=1, id="hourly_auth_check",
                  next_run_time=datetime.now() + timedelta(minutes=30))

scheduler.start()
print("Scheduler started — cache refresh in 10s, watch scheduling in 15s, full-watch check in 20s")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
