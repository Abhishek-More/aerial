import json
import os
import threading
import time as _time
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, redirect, render_template, request, url_for

from bot import get_classes, get_session, signup_for_class

app = Flask(__name__)

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "booking_log.json")

# Global session — reused across requests and scheduler
_session = None
_session_lock = threading.Lock()

# In-memory class cache — refreshed every 5 minutes by the scheduler
_class_cache = {}  # key: (date, location, category) -> {"classes": [...], "fetched_at": float}
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


def get_cached_classes(date="", location="1", category="28", force=False):
    """Return classes from cache if fresh, otherwise fetch and cache."""
    cache_key = (date, location, category)
    now = _time.time()

    with _cache_lock:
        cached = _class_cache.get(cache_key)
        if cached and not force and (now - cached["fetched_at"]) < CACHE_TTL:
            age = int(now - cached["fetched_at"])
            print(f"[cache] HIT for {cache_key} (age: {age}s, {len(cached['classes'])} classes)")
            return cached["classes"]

    print(f"[cache] MISS for {cache_key}, force={force}. Fetching from MindBody...")
    session = get_bot_session()
    print(f"[cache] Session ready. Fetching classes...")
    classes = get_classes(session, date=date, location=location, class_type=category)
    print(f"[cache] Fetched {len(classes)} classes")

    with _cache_lock:
        _class_cache[cache_key] = {"classes": classes, "fetched_at": _time.time()}

    return classes


def refresh_default_cache():
    """Background job: keep the default view warm."""
    print(f"[scheduler] Cache refresh starting...")
    try:
        classes = get_cached_classes(date="", location="1", category="28", force=True)
        print(f"[scheduler] Cache refreshed: {len(classes)} classes")
    except Exception as e:
        print(f"[scheduler] Cache refresh error: {type(e).__name__}: {e}")


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
    # Keep last 100 entries
    log = log[:100]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def check_and_book():
    """Scheduler job: check watchlist and book classes whose signup is now available."""
    watchlist = load_watchlist()
    if not watchlist:
        return

    now = datetime.now()

    # Use cached classes (refreshed separately by refresh_default_cache)
    try:
        classes = get_cached_classes(force=True)
    except Exception as e:
        append_log({"time": now.isoformat(), "action": "fetch_error", "error": str(e)})
        return

    # Build lookup: (class_name, class_date_str, time) -> class_info
    # Also build by class_id for direct matching
    available = {}
    for cls in classes:
        if cls["has_signup"] and cls["class_id"]:
            available[cls["class_id"]] = cls

    updated = False
    remaining = []
    for watch in watchlist:
        # If already booked, skip
        if watch.get("status") == "booked":
            remaining.append(watch)
            continue

        # Check if this watched class now has a signup link
        cid = watch.get("class_id")
        if cid and cid in available:
            cls = available[cid]
            try:
                result = signup_for_class(session, cls["class_id"], cls["class_date"])
                watch["status"] = "booked"
                watch["result"] = result
                watch["booked_at"] = now.isoformat()
                updated = True
                append_log({
                    "time": now.isoformat(),
                    "action": "booked",
                    "class": watch["class_name"],
                    "date": watch["class_date"],
                    "result": result,
                })
                print(f"[{now}] BOOKED: {watch['class_name']} on {watch['class_date']} -> {result}")
            except Exception as e:
                append_log({
                    "time": now.isoformat(),
                    "action": "book_error",
                    "class": watch["class_name"],
                    "error": str(e),
                })
                print(f"[{now}] ERROR booking {watch['class_name']}: {e}")
        else:
            # Not yet available — try matching by name/date/time if class_id is unknown
            if not cid:
                for avail_cls in classes:
                    if (avail_cls["has_signup"]
                            and avail_cls["class_id"]
                            and avail_cls["name"] == watch.get("class_name")
                            and avail_cls["class_date"] == watch.get("class_date")
                            and avail_cls["time"] == watch.get("time")):
                        # Found it — update the watch with the real class_id and book
                        watch["class_id"] = avail_cls["class_id"]
                        try:
                            result = signup_for_class(session, avail_cls["class_id"], avail_cls["class_date"])
                            watch["status"] = "booked"
                            watch["result"] = result
                            watch["booked_at"] = now.isoformat()
                            updated = True
                            append_log({
                                "time": now.isoformat(),
                                "action": "booked",
                                "class": watch["class_name"],
                                "date": watch["class_date"],
                                "result": result,
                            })
                            print(f"[{now}] BOOKED: {watch['class_name']} on {watch['class_date']} -> {result}")
                        except Exception as e:
                            append_log({
                                "time": now.isoformat(),
                                "action": "book_error",
                                "class": watch["class_name"],
                                "error": str(e),
                            })
                        break

        remaining.append(watch)

    if updated:
        save_watchlist(remaining)


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/classes")
def api_classes():
    """Fetch all classes for the given week (served from cache)."""
    date = request.args.get("date", "")
    location = request.args.get("location", "1")
    category = request.args.get("category", "28")
    force = request.args.get("refresh", "") == "1"

    try:
        classes = get_cached_classes(date=date, location=location, category=category, force=force)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    watchlist = load_watchlist()
    watched_keys = {(w["class_name"], w.get("class_date", ""), w.get("time", "")) for w in watchlist}

    # Return a copy so we don't mutate the cache
    result = []
    for cls in classes:
        c = dict(cls)
        key = (c["name"], c.get("class_date", ""), c.get("time", ""))
        c["watched"] = key in watched_keys
        result.append(c)

    return jsonify(result)


@app.route("/api/watchlist")
def api_watchlist():
    return jsonify(load_watchlist())


@app.route("/api/watch", methods=["POST"])
def api_watch():
    """Add a class to the watchlist."""
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
    return jsonify({"status": "added"})


@app.route("/api/unwatch", methods=["POST"])
def api_unwatch():
    """Remove a class from the watchlist."""
    data = request.json
    watchlist = load_watchlist()
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
    """Immediately book a class."""
    data = request.json
    class_id = data.get("class_id")
    class_date = data.get("class_date")
    if not class_id or not class_date:
        return jsonify({"error": "Missing class_id or class_date"}), 400

    session = get_bot_session()
    try:
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


# Start the scheduler at import time (works with both gunicorn and direct run)
# Delay first run by 10s so gunicorn worker can finish booting and accept requests
scheduler = BackgroundScheduler()
scheduler.add_job(refresh_default_cache, "interval", minutes=5, id="refresh_cache",
                  next_run_time=datetime.now() + timedelta(seconds=10))
scheduler.add_job(check_and_book, "interval", seconds=30, id="check_and_book",
                  next_run_time=datetime.now() + timedelta(seconds=15))
scheduler.start()
print("Scheduler started — first cache refresh in 10s")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
