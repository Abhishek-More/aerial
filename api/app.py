import json
import os
import re
import threading
import time as _time
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from flask import Flask, jsonify, render_template, request

from bot import get_classes, get_session, signup_for_class, login_with_playwright, get_credentials, apply_cookies, save_cookie_jar, check_session, HEADERS, COOKIE_JAR_FILE

app = Flask(__name__)

DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
LOG_FILE = os.path.join(DATA_DIR, "booking_log.json")

# Signup opens exactly 1 week + 15 minutes before class start
SIGNUP_OFFSET = timedelta(weeks=1, minutes=15)
# Re-auth 5 minutes before signup opens
REAUTH_BEFORE = timedelta(minutes=5)

# Global session
_session = None
_session_lock = threading.Lock()

# In-memory class cache
_class_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def get_bot_session():
    global _session
    with _session_lock:
        if _session is None:
            print("[app] Initializing bot session (first time)...")
            _session = get_session()
            print("[app] Bot session ready.")
        return _session


def force_reauth():
    """Force a fresh login to get new cookies."""
    global _session
    print("[reauth] Forcing re-authentication...")
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


def get_cached_classes(date="", location="0", category="0", force=False):
    """Return classes from cache if fresh, otherwise fetch and cache."""
    cache_key = (date, location, category)
    now = _time.time()

    with _cache_lock:
        cached = _class_cache.get(cache_key)
        if cached and not force and (now - cached["fetched_at"]) < CACHE_TTL:
            return cached["classes"]

    print(f"[cache] MISS for {cache_key}, force={force}. Fetching...")
    session = get_bot_session()
    classes = get_classes(session, date=date, location=location, class_type=category)
    print(f"[cache] Fetched {len(classes)} classes")

    with _cache_lock:
        _class_cache[cache_key] = {"classes": classes, "fetched_at": _time.time()}

    return classes


def refresh_default_cache():
    """Background job: keep the default view warm."""
    try:
        classes = get_cached_classes(date="", location="0", category="0", force=True)
        print(f"[scheduler] Cache refreshed: {len(classes)} classes")
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
            force_reauth,
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

                    # Update watchlist
                    watchlist = load_watchlist()
                    for w in watchlist:
                        if (w["class_name"] == class_name
                                and w.get("class_date") == class_date
                                and w.get("time") == time_str):
                            w["status"] = "booked"
                            w["result"] = result
                            w["booked_at"] = now.isoformat()
                            w["class_id"] = cls["class_id"]
                            break
                    save_watchlist(watchlist)

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


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


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

    entry = {
        "class_name": data["name"],
        "class_date": data.get("class_date", ""),
        "class_id": data.get("class_id"),
        "time": data.get("time", ""),
        "teacher": data.get("teacher", ""),
        "date_label": data.get("date", ""),
        "status": "waiting",
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

    # Schedule the snag for this class
    schedule_snag(entry)

    class_dt = parse_class_datetime(entry["class_date"], entry["time"])
    open_time = compute_open_time(class_dt) if class_dt else None

    return jsonify({
        "status": "added",
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

        # If session expired, re-auth and retry once
        if "session expired" in result.lower():
            print("[api_book] Session expired, re-authing and retrying...")
            force_reauth()
            session = get_bot_session()
            result = signup_for_class(session, class_id, class_date)

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
    with _session_lock:
        import requests as req
        _session = req.Session()
        _session.headers.update(HEADERS)
        apply_cookies(_session, data)

    cookie_names = list(data.keys())
    has_auth = "idsrvauth" in cookie_names
    print(f"[upload-cookies] Received {len(data)} cookies. Has idsrvauth: {has_auth}")
    print(f"[upload-cookies] Cookie names: {cookie_names}")

    return jsonify({"status": "ok", "cookies": len(data), "has_idsrvauth": has_auth})


# --- Scheduler startup ---
scheduler = BackgroundScheduler()

# Cache refresh every 5 min (delayed 10s on startup)
scheduler.add_job(refresh_default_cache, "interval", minutes=5, id="refresh_cache",
                  next_run_time=datetime.now() + timedelta(seconds=10))

# Schedule all existing watches on startup (delayed 15s)
scheduler.add_job(schedule_all_watches, trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=15)),
                  id="init_watches")

scheduler.start()
print("Scheduler started — cache refresh in 10s, watch scheduling in 15s")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
