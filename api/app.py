import json
import os
import threading
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


def get_bot_session():
    global _session
    with _session_lock:
        if _session is None:
            _session = get_session()
        return _session


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

    session = get_bot_session()
    now = datetime.now()

    # Fetch current classes to see which have signup links
    try:
        classes = get_classes(session)
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
    """Fetch all classes for the given week."""
    date = request.args.get("date", "")
    location = request.args.get("location", "1")
    category = request.args.get("category", "28")

    session = get_bot_session()
    try:
        classes = get_classes(session, date=date, location=location, class_type=category)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    watchlist = load_watchlist()
    watched_keys = {(w["class_name"], w.get("class_date", ""), w.get("time", "")) for w in watchlist}

    for cls in classes:
        key = (cls["name"], cls.get("class_date", ""), cls.get("time", ""))
        cls["watched"] = key in watched_keys

    return jsonify(classes)


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


if __name__ == "__main__":
    # Start the scheduler — checks every 30 seconds
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_book, "interval", seconds=30, id="check_and_book")
    scheduler.start()
    print("Scheduler started — checking watchlist every 30 seconds")

    app.run(debug=False, port=5050)
