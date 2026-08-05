import json
import os
import random
import re
import sys
import threading
import time as _time
import urllib.request
from collections import deque
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from flask import Flask, Response, jsonify, render_template, request

from bot import get_classes, get_session, signup_for_class, login_with_playwright, get_credentials, apply_cookies, save_cookie_jar, load_cookie_jar, check_session, get_user_name, get_client_id, get_server_time, RateLimited, HEADERS, COOKIE_JAR_FILE, BASE_URL

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
LOG_FILE = os.path.join(DATA_DIR, "booking_log.json")


# --- Accounts (switchable; only the selected account is active at a time) ---
def _envq(key, default=""):
    """Read an env var, stripping one layer of surrounding quotes (Docker's
    --env-file keeps quotes literal)."""
    v = os.environ.get(key, default)
    if v and len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def _load_accounts():
    """Accounts come from MB1_*, MB2_, ... env vars (falls back to MB_* as one account)."""
    accts = []
    for i in range(1, 6):
        email = _envq(f"MB{i}_EMAIL")
        pw = _envq(f"MB{i}_PASSWORD")
        if email and pw:
            accts.append({"id": str(i), "name": _envq(f"MB{i}_NAME") or email.split("@")[0],
                          "email": email, "password": pw})
    if not accts:
        email, pw = _envq("MB_EMAIL"), _envq("MB_PASSWORD")
        if email and pw:
            accts.append({"id": "1", "name": email.split("@")[0], "email": email, "password": pw})
    return accts


ACCOUNTS = _load_accounts()
ACCOUNTS_BY_ID = {a["id"]: a for a in ACCOUNTS}
_ACTIVE_FILE = os.path.join(DATA_DIR, "active_account")


def cookie_jar_path(aid):
    return os.path.join(DATA_DIR, f"cookie_jar_{aid}.json")


def watchlist_path(aid):
    return os.path.join(DATA_DIR, f"watchlist_{aid}.json")


def _read_active_id():
    try:
        with open(_ACTIVE_FILE) as f:
            aid = f.read().strip()
        if aid in ACCOUNTS_BY_ID:
            return aid
    except Exception:
        pass
    return ACCOUNTS[0]["id"] if ACCOUNTS else "1"


def active_account():
    return ACCOUNTS_BY_ID.get(_active_id) or (ACCOUNTS[0] if ACCOUNTS
                                              else {"id": "1", "name": "?", "email": "", "password": ""})


def _migrate_legacy_files():
    """Move pre-multi-account files to the default account's per-account files."""
    if not ACCOUNTS:
        return
    did = ACCOUNTS[0]["id"]
    for legacy, dest in [(os.path.join(DATA_DIR, ".cookie_jar.json"), cookie_jar_path(did)),
                         (os.path.join(DATA_DIR, "watchlist.json"), watchlist_path(did))]:
        if os.path.exists(legacy) and not os.path.exists(dest):
            try:
                os.rename(legacy, dest)
                print(f"[account] Migrated {os.path.basename(legacy)} -> account {did}")
            except Exception as e:
                print(f"[account] migration of {legacy} failed: {e}")


_migrate_legacy_files()
_active_id = _read_active_id()

# Signup opens exactly 1 week + 15 minutes before class start
SIGNUP_OFFSET = timedelta(weeks=1, minutes=15)
# Re-auth 5 minutes before signup opens
REAUTH_BEFORE = timedelta(minutes=5)
# Fast-snag tuning: start polling this early, keep trying this long past open.
# The burst's job is to win the instant spots exist at open; later drops are
# handled by the notify fallback, so the window stays short to limit requests.
SNAG_LEAD = timedelta(seconds=3)
SNAG_WINDOW = timedelta(seconds=12)

# "Notify when a full class opens up" — re-check on a randomized interval
FULL_CHECK_MIN = 300  # 5 min
FULL_CHECK_MAX = 600  # 10 min
# Auto-book a freed-up watched class only if it starts at least this far out.
BOOK_LEAD_TIME = timedelta(hours=24)

# Lightweight activity counters, summarized hourly (see hourly_recap)
_stats_lock = threading.Lock()
STATS = {"checks": 0, "cache_refreshes": 0, "opens": 0, "booked": 0, "book_failed": 0,
         "texts_sent": 0, "texts_failed": 0, "reauths": 0, "cookie_uploads": 0,
         "auth_checks": 0, "auth_heals": 0}


def bump(key, n=1):
    with _stats_lock:
        STATS[key] = STATS.get(key, 0) + n


# Snapshot of the most recent check_full_watches run, surfaced in the Debug tab.
_full_check_lock = threading.Lock()
_last_full_check = {"ran_at": None, "duration_ms": None, "note": "", "items": []}

# clientId (MindBody member ID) is account-stable, so fetch it once per account and
# cache it. Needed by res_deb now that the res_a step (which used to supply it) is gone.
_client_id_lock = threading.Lock()
_client_id_cache = {}  # account_id -> clientId


def resolve_client_id(session):
    """Cached clientId for the active account; fetches from main_info.asp on first use."""
    aid = _active_id
    with _client_id_lock:
        cid = _client_id_cache.get(aid)
    if cid:
        return cid
    cid = get_client_id(session) or ""
    if cid:
        with _client_id_lock:
            _client_id_cache[aid] = cid
        print(f"[client-id] cached {cid} for account {aid}")
    return cid

# Global session
_session = None
_session_lock = threading.Lock()

# In-memory class cache
_class_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def _build_session_for_active():
    acct = active_account()
    print(f"[app] Initializing session for {acct['name']} ({acct['email']})...")
    s = get_session(email=acct["email"], password=acct["password"],
                    cookie_jar_file=cookie_jar_path(acct["id"]))
    print("[app] Bot session ready.")
    return s


def get_bot_session():
    global _session
    _boot_done.wait()  # block until startup init finishes
    with _session_lock:
        if _session is None:
            _session = _build_session_for_active()
        return _session


def _init_session_background():
    """Run session init eagerly on startup so boot logs stream in real time."""
    global _session
    try:
        with _session_lock:
            _session = _build_session_for_active()
    except Exception as e:
        print(f"[app] Session init failed: {e}")
    finally:
        _boot_done.set()
        sys.stdout = _original_stdout


def _session_from_jar(acct):
    """Build a session from an account's saved cookie jar only (no login). Returns
    the session if the jar is valid, else None. Keeps account-switching fast."""
    import requests as req
    s = req.Session()
    s.headers.update(HEADERS)
    if load_cookie_jar(s, cookie_jar_path(acct["id"])) and check_session(s):
        return s
    return None


def set_active_account(aid: str) -> bool:
    """Switch the active account: rebuild the session from the new account's saved
    cookie jar (so the lock is accurate immediately; full login happens lazily only
    if needed), persist the choice, and reschedule autobook snags for the new
    account's watchlist (only the active account is monitored)."""
    global _active_id, _session, _user_name
    if aid not in ACCOUNTS_BY_ID:
        return False
    with _session_lock:
        _active_id = aid
        _session = _session_from_jar(ACCOUNTS_BY_ID[aid])
    _user_name = None
    try:
        with open(_ACTIVE_FILE, "w") as f:
            f.write(aid)
    except Exception as e:
        print(f"[account] Failed to persist active account: {e}")
    print(f"[account] Switched active account -> {active_account()['name']}")
    _reschedule_active_snags()
    return True


def _reschedule_active_snags():
    """Cancel all snag/reauth jobs (from the previous account) and reschedule for the
    now-active account's watchlist."""
    try:
        for job in scheduler.get_jobs():
            if job.id.startswith("snag_") or job.id.startswith("reauth_"):
                scheduler.remove_job(job.id)
    except Exception as e:
        print(f"[account] snag cleanup error: {e}")
    schedule_all_watches()


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
    acct = active_account()
    print(f"[reauth] Forcing headless re-auth for {acct['name']} (fallback)...")
    with _session_lock:
        import requests as req
        _session = req.Session()
        _session.headers.update(HEADERS)
        cookies = login_with_playwright(acct["email"], acct["password"])
        apply_cookies(_session, cookies)
        # Save cookie jar
        import json as _json
        with open(cookie_jar_path(acct["id"]), "w") as f:
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
    cache_key = (_active_id, date, location, category)
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

# Serialize all watchlist/log file writes; multiple booking threads can run at once.
_io_lock = threading.Lock()


def _read_json_list(path) -> list:
    """Read a JSON array, tolerating corruption (e.g. an interrupted concurrent write)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        try:  # salvage the first valid array, ignore trailing garbage
            with open(path) as f:
                obj, _ = json.JSONDecoder().raw_decode(f.read().lstrip())
            return obj if isinstance(obj, list) else []
        except Exception:
            return []


def _write_json(path, data):
    """Atomic write (temp file + rename) so a reader never sees a half-written file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_watchlist() -> list[dict]:
    with _io_lock:
        return _read_json_list(watchlist_path(_active_id))


def save_watchlist(watchlist: list[dict]):
    with _io_lock:
        _write_json(watchlist_path(_active_id), watchlist)


def load_log() -> list[dict]:
    with _io_lock:
        return _read_json_list(LOG_FILE)


def append_log(entry: dict):
    with _io_lock:
        log = _read_json_list(LOG_FILE)
        log.insert(0, entry)
        _write_json(LOG_FILE, log[:100])


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

    # Schedule rapid booking — start SNAG_LEAD before open so we're already polling
    # the instant the signup button flips.
    snag_job_id = f"snag_{safe_id}"
    snag_start = open_time - SNAG_LEAD
    if snag_start > now:
        try:
            scheduler.remove_job(snag_job_id)
        except Exception:
            pass
        scheduler.add_job(
            rapid_book,
            trigger=DateTrigger(run_date=snag_start),
            args=[watch],
            id=snag_job_id,
            replace_existing=True,
        )
        print(f"[snag]   Booking burst scheduled for {snag_start} (open {open_time})")
    elif class_dt > now:
        # Signup is already open (or within the lead) and the class is upcoming → go now.
        print(f"[snag]   Signup already open — attempting to book now...")
        threading.Thread(target=rapid_book, args=[watch], daemon=True).start()
    else:
        print(f"[snag]   Class already passed — not booking.")


def _sleep_until(target):
    """Block until the container wall-clock reaches `target` (naive local datetime).
    Coarse-sleeps to within ~10ms then busy-spins the remainder for tight precision,
    so a scheduled get_classes() call goes out at the instant we intend."""
    while True:
        rem = (target - datetime.now()).total_seconds()
        if rem <= 0:
            return
        if rem > 0.02:
            _time.sleep(rem - 0.01)
        # else: tight spin through the final ~10-20ms


def _clone_session(src):
    """Shallow copy of a requests.Session (headers, cookies, proxies) so a dedicated
    thread can issue requests concurrently without sharing one Session across threads
    (requests.Session isn't safe for simultaneous use from multiple threads)."""
    import requests as req
    s = req.Session()
    s.headers.update(dict(src.headers))
    for c in src.cookies:
        s.cookies.set_cookie(c)
    s.proxies.update(dict(src.proxies))
    return s


def rapid_book(watch: dict):
    """
    Fast auto-book at signup-open. Polls the schedule back-to-back (no fixed 2s
    gap), aligned to MindBody's clock, from ~SNAG_LEAD before open until
    SNAG_WINDOW after. The instant the signup button appears it books with the
    real tg/clsLoc. Relies on the re-auth scheduled 5 min before open (no
    validity round-trip on the hot path). If it still doesn't get a spot, the
    watch is converted to a notify watch so it keeps polling for openings.

    Timing guarantee: regardless of poll cadence or backoff, a get_classes() call
    is fired at exactly open-0.2s and at open (container clock), each on its own
    thread, so we catch the button the instant it flips. We're only waiting on
    get_classes() to hand us the class_id/tg/clsLoc needed to fire the booking.
    """
    class_name = watch.get("class_name", "?")
    class_date = watch.get("class_date", "")
    time_str = watch.get("time", "")
    print(f"[rapid_book] Auto-book burst for {class_name} on {class_date} at {time_str}")

    append_log({
        "time": datetime.now().isoformat(), "action": "snag_started",
        "class": class_name, "date": class_date,
    })

    session = get_bot_session()  # auth ensured by the 5-min-before reauth job

    class_dt = parse_class_datetime(class_date, time_str)
    open_time = compute_open_time(class_dt) if class_dt else datetime.now()

    # Align to MindBody's clock so the burst lands right as signup opens.
    server_offset = timedelta(0)
    st = get_server_time(session)
    if st:
        server_offset = st - datetime.now()
        print(f"[rapid_book] server clock offset {server_offset.total_seconds():+.1f}s")
    # Poll until SNAG_WINDOW past open (server-aligned), but always at least
    # SNAG_WINDOW from now (covers the already-open catch-up case).
    deadline_local = max((open_time + SNAG_WINDOW) - server_offset,
                         datetime.now() + SNAG_WINDOW)

    # Resolve clientId now (during the lead) so the booking step doesn't need res_a.
    client_id = resolve_client_id(session)
    print(f"[rapid_book] using clientId={client_id!r}")

    # Shared booking state across the main loop and the dedicated aligned-shot threads.
    book_lock = threading.Lock()
    booked_evt = threading.Event()
    state = {"last_reason": "signup row never appeared", "found": {}, "attempts": 0}
    book_info = {"class_name": class_name, "class_date": class_date,
                 "time": time_str, "teacher": watch.get("teacher", "")}

    def _record_success(result):
        now = datetime.now()
        watchlist = load_watchlist()
        for w in watchlist:
            if (w["class_name"] == class_name
                    and w.get("class_date") == class_date
                    and w.get("time") == time_str):
                w["status"] = "booked"
                w["result"] = result
                w["booked_at"] = now.isoformat()
                if state["found"].get("class_id"):
                    w["class_id"] = state["found"]["class_id"]
                break
        save_watchlist(watchlist)
        texted = alert_booking(book_info, result, True, source="auto-book at signup-open")
        bump("booked")
        bump("texts_sent" if texted else "texts_failed")
        append_log({
            "time": now.isoformat(), "action": "snagged", "class": class_name,
            "date": class_date, "result": result, "attempts": state["attempts"],
        })

    def attempt(sess, label):
        """One get_classes() probe; books under lock if a spot is open. Returns an
        outcome tag (SUCCESS/DONE/FULL/NO_AUTH/OTHER/WAIT). Safe to call concurrently
        from the aligned-shot threads and the main loop. May raise (RateLimited /
        network) — callers handle backoff."""
        if booked_evt.is_set():
            return "DONE"
        state["attempts"] += 1
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if label:
            print(f"[rapid_book] {ts} ALIGNED SHOT ({label}) — firing get_classes")
        classes = get_classes(sess, date=class_date, location="0", class_type="0")
        match = next(
            (c for c in classes
             if c["name"] == class_name
             and c["class_date"] == class_date
             and c["time"] == time_str),
            None,
        )
        if match is None:
            state["last_reason"] = "class not listed in schedule"
            return "WAIT"
        if not (match.get("has_signup") and match.get("class_id")):
            state["last_reason"] = "signup not open yet (no signup button)"
            return "WAIT"
        tg = match.get("tg") or "28"
        cls_loc = match.get("cls_loc") or "1"
        state["found"] = {"class_id": match["class_id"], "tg": tg, "cls_loc": cls_loc}
        open_n = match.get("open", 0)
        if open_n <= 0:
            state["last_reason"] = "FULL"
            return "FULL"
        # A spot exists — serialize so the concurrent shots can't double-book.
        with book_lock:
            if booked_evt.is_set():
                return "DONE"
            who = label or "poll"
            print(f"[rapid_book] {ts} TRYING open={open_n} id={match['class_id']} "
                  f"tg={tg} clsLoc={cls_loc}  <{who}>")
            result = signup_for_class(sess, match["class_id"], match["class_date"],
                                      tg=tg, cls_loc=cls_loc, client_id=client_id)
            tag = _classify_book_result(result)
            print(f"[rapid_book] {ts} [{tag}] {result}  <{who}>")
            state["last_reason"] = tag
            if tag == "SUCCESS":
                _record_success(result)
                booked_evt.set()
            return tag

    # Dedicated aligned-shot threads: fire get_classes at EXACTLY open-0.2s and open
    # (container clock), each on its own cloned session, so the call goes out on time
    # regardless of what the main loop is doing mid-request.
    def aligned_shot(target, label, sess):
        _sleep_until(target)
        if booked_evt.is_set():
            return
        try:
            attempt(sess, label)
        except RateLimited as e:
            print(f"[rapid_book] aligned {label} rate-limited: {e}")
        except Exception as e:
            print(f"[rapid_book] aligned {label} error: {type(e).__name__}: {e}")

    shot_threads = []
    for target, label in ((open_time - timedelta(seconds=0.2), "open-0.2s"),
                          (open_time, "open+0.0s")):
        if target > datetime.now():
            th = threading.Thread(target=aligned_shot,
                                  args=(target, label, _clone_session(session)),
                                  daemon=True)
            th.start()
            shot_threads.append(th)

    # Main fallback poll loop: BASE_GAP cadence + exponential backoff. The aligned
    # threads own the exact open instants; this loop covers the approach and the
    # post-open window (later drops, or a retry if a shot's booking failed).
    BASE_GAP = 0.4
    FULL_GAP = 0.6   # signup open but full — schedule-only polling, go a bit slower
    MAX_BACKOFF = 8.0
    backoff_n = 0
    while not booked_evt.is_set() and datetime.now() < deadline_local:
        gap = BASE_GAP
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        try:
            tag = attempt(session, None)
            backoff_n = 0  # clean read — clear any accumulated backoff
            if tag in ("SUCCESS", "DONE"):
                break
            elif tag == "FULL":
                gap = FULL_GAP
            elif tag == "NO_AUTH":
                print("[rapid_book] Session expired mid-snag — requesting host reauth...")
                trigger_host_reauth(timeout=120)
                session = get_bot_session()
            elif tag == "OTHER":
                gap = 1.0  # raced and lost between fetch and book — brief back-off
        except RateLimited as e:
            backoff_n += 1
            gap = min(BASE_GAP * (2 ** backoff_n), MAX_BACKOFF)
            state["last_reason"] = str(e)
            print(f"[rapid_book] {ts} RATE-LIMITED — backing off to {gap:.1f}s ({e})")
        except Exception as e:
            backoff_n += 1
            gap = min(BASE_GAP * (2 ** backoff_n), MAX_BACKOFF)
            state["last_reason"] = f"error: {type(e).__name__}: {e}"
            print(f"[rapid_book] {ts} ERROR (backoff to {gap:.1f}s): {e}")
        _time.sleep(gap)

    # Let any in-flight aligned shot finish its booking before we decide the outcome.
    for th in shot_threads:
        th.join(timeout=2.0)
    if booked_evt.is_set():
        return

    # Didn't get a spot at open → convert to a notify watch so it keeps polling
    # for openings (and auto-books if a spot frees while it's still >=24h out).
    last_reason = state["last_reason"]
    found = state["found"]
    reason = {
        "FULL": "full at signup-open (didn't get a spot)",
        "NO_AUTH": "not authenticated (reauth didn't recover in time)",
        "OTHER": f"unexpected response: {last_reason}",
        "class not listed in schedule": "class never appeared in the schedule",
        "signup not open yet (no signup button)": "signup never opened during the window",
        "signup row never appeared": "signup never opened during the window",
    }.get(last_reason, last_reason)
    print(f"[rapid_book] No spot for {class_name} after {state['attempts']} attempts ({reason}). "
          f"Converting to notify watch to keep polling for openings.")

    watchlist = load_watchlist()
    for w in watchlist:
        if (w["class_name"] == class_name
                and w.get("class_date") == class_date
                and w.get("time") == time_str):
            w["mode"] = "notify"
            w["status"] = "watching"
            w["result"] = f"Missed at signup-open ({reason}); now watching for openings."
            if found:
                w["class_id"] = found["class_id"]
                w["tg"] = found["tg"]
                w["cls_loc"] = found["cls_loc"]
            break
    save_watchlist(watchlist)
    schedule_next_full_check(delay=5)  # start polling it soon

    # No text: a watch that is merely still watching is not news on a phone.
    # You asked for the watch; the text you want is the one that says a spot
    # opened or that it got booked.
    _notify(
        f"Missed at open, now watching: {class_name} — {_class_when(book_info)}",
        f"Couldn't grab {class_name} the moment signup opened ({reason}).\n\n"
        f"It's now on your watchlist in notify mode — I'll keep checking for openings and "
        f"auto-book if a spot frees while it's 24h+ out (or text you if it opens within 24h).\n",
        text=None,
    )
    bump("book_failed")
    append_log({
        "time": datetime.now().isoformat(), "action": "snag_failed_now_watching",
        "class": class_name, "date": class_date, "attempts": attempts, "reason": reason,
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
        f"texts:{s['texts_sent']}(failed {s['texts_failed']}) "
        f"reauths:{s['reauths']} cookie-uploads:{s['cookie_uploads']} "
        f"auth-checks:{s['auth_checks']}(healed {s['auth_heals']}) | "
        f"active watches — notify:{notify_active} autobook:{autobook_active}"
    )


# --- "Notify when a full class opens up" ---

def _send_imsg(text: str) -> bool:
    """Text the line through imsg, the host's iMessage service. Unset IMSG_URL
    means the channel is off, which is not a failure."""
    url = _envq("IMSG_URL")
    if not url:
        return False
    req = urllib.request.Request(url, data=text.encode(), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("Title", "aerial")   # imsg prefixes the message with this
    try:
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as e:
        print(f"[imsg] Failed to send: {e}")
        return False


def _notify(subject: str, body: str, text: str | None = "") -> bool:
    """Every alert this bot raises goes through here: it prints the detail (so it
    still lands in `docker logs`), and an iMessage for the buzz. `text` is what
    gets texted, because a log-style subject line reads badly on a phone; it
    defaults to the subject, and `text=None` means don't text, for news that
    does not deserve a buzz. True when the text went out."""
    print(f"[notify] {subject}\n{body}")
    if text is None:
        return False
    return _send_imsg(text or subject)


def _booking_succeeded(result: str) -> bool:
    """signup_for_class returns different success strings depending on the flow:
    'Successfully booked ...' (direct) or the raw confirmation 'You've Booked: ...'
    (via my_sch.asp). Match either, so a real booking isn't reported as failed."""
    r = (result or "").lower()
    return "successfully booked" in r or "you've booked" in r


def _classify_book_result(result: str) -> str:
    """Categorize a signup_for_class result: SUCCESS / FULL / NO_AUTH / OTHER."""
    r = (result or "").lower()
    if _booking_succeeded(result):
        return "SUCCESS"
    if "is full" in r:
        return "FULL"
    if "session expired" in r:
        return "NO_AUTH"
    return "OTHER"


def _class_when(info: dict) -> str:
    return f"{info.get('date_label') or info.get('class_date', '')} at {info.get('time', '')}"


_TZ_TAIL = re.compile(r"\s*\b[A-Z]{2,4}T\b\s*$")           # the " EDT" on a time
_DATE_LABEL = re.compile(r"^(\w{3})\w* (\w{3})\w* (\d{1,2}),? \d{4}$")


def _short_when(info: dict) -> str:
    """'Thu June 11, 2026' + '7:30\\xa0pm  EDT' -> 'Thu Jun 11 at 7:30 pm'.

    A text is read at a glance, so the year and the timezone are noise, and the
    scraped values arrive with non-breaking and doubled spaces in them. Falls
    back to whatever it was handed if the shape surprises it.
    """
    label = " ".join((info.get("date_label") or info.get("class_date") or "").split())
    m = _DATE_LABEL.match(label)
    if m:
        label = " ".join(m.groups())
    when = _TZ_TAIL.sub("", " ".join((info.get("time") or "").split()))
    return f"{label} at {when}" if label and when else label or when


def alert_open(watch: dict, open_spots: int) -> bool:
    """A watched class freed up but starts within 24h, so it was NOT auto-booked."""
    cls = watch.get("class_name", "class")
    return _notify(
        f"Spot open (not booked — within 24h): {cls} — {_class_when(watch)}",
        f"A spot just opened in a class you're watching, but it starts in under 24 hours, "
        f"so it was NOT auto-booked.\n\n"
        f"Class:   {cls}\n"
        f"When:    {_class_when(watch)}\n"
        f"Teacher: {watch.get('teacher', '')}\n"
        f"Open spots: {open_spots}\n\n"
        f"Book it yourself: https://clients.mindbodyonline.com/classic/mainclass\n",
        text=f"A spot just opened in {cls}, {_short_when(watch)}. It's under 24h out so "
             f"I didn't book it, go grab it.",
    )


def alert_booking(info: dict, result: str, success: bool, source: str,
                  quiet: bool = False) -> bool:
    """Text a confirmation (or failure notice) for any booking the app makes.
    `quiet` drops the text, for a booking you are watching happen in the UI."""
    cls = info.get("class_name") or info.get("name", "class")
    status = "Booked" if success else "Booking FAILED"
    return _notify(
        f"{status}: {cls} — {_class_when(info)}",
        f"{'A class was booked' if success else 'A booking attempt failed'} ({source}).\n\n"
        f"Class:   {cls}\n"
        f"When:    {_class_when(info)}\n"
        f"Teacher: {info.get('teacher', '')}\n"
        f"Result:  {result}\n",
        text=None if quiet else (
            f"You're in: {cls}, {_short_when(info)}." if success else
            f"Couldn't get you into {cls}, {_short_when(info)}. {result.strip()[:90]}"),
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
    """Re-check every 'notify' watch: if a spot opened, text the user (no booking).
    Self-reschedules with a fresh random delay each run."""
    bump("checks")
    started = _time.time()
    run_items = []  # per-watch outcome for this run (surfaced in the Debug tab)
    note = ""
    try:
        watchlist = load_watchlist()
        pending = [w for w in watchlist if w.get("mode") == "notify" and w.get("status") == "watching"]
        if not pending:
            note = "no active notify watches"
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
                for w in watches:
                    run_items.append({"name": w.get("class_name"), "date": w.get("class_date"),
                                      "time": w.get("time"), "status": "fetch_error", "open": 0})
                continue
            for w in watches:
                class_dt = parse_class_datetime(w.get("class_date", ""), w.get("time", ""))
                if class_dt and class_dt < now:
                    w["status"] = "expired"
                    changed = True
                    run_items.append({"name": w.get("class_name"), "date": w.get("class_date"),
                                      "time": w.get("time"), "status": "expired", "open": 0})
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
                    run_items.append({"name": w.get("class_name"), "date": w.get("class_date"),
                                      "time": w.get("time"), "status": "not_found", "open": 0})
                    continue
                open_n = match.get("open", 0) or 0
                is_open = bool(match.get("has_signup")) and open_n > 0
                run_items.append({"name": w.get("class_name"), "date": w.get("class_date"),
                                  "time": w.get("time"), "status": "open" if is_open else "full",
                                  "open": open_n})
                if is_open:
                    bump("opens")
                    class_id = match.get("class_id") or w.get("class_id")
                    w["open_spots"] = match["open"]
                    w["class_id"] = class_id
                    changed = True

                    far_enough = class_dt is not None and (class_dt - now) >= BOOK_LEAD_TIME
                    if far_enough and class_id:
                        # >= 24h out → actually book it, then text the result.
                        tg = match.get("tg") or "28"
                        cls_loc = match.get("cls_loc") or "1"
                        print(f"[full_watch] OPEN: {w['class_name']} on {date} ({match['open']} spot) — booking (>=24h out, tg={tg} clsLoc={cls_loc}).")
                        cid = resolve_client_id(session)
                        result = signup_for_class(session, class_id, match["class_date"], tg=tg, cls_loc=cls_loc, client_id=cid)
                        if "session expired" in result.lower():
                            trigger_host_reauth()
                            session = get_bot_session()
                            result = signup_for_class(session, class_id, match["class_date"], tg=tg, cls_loc=cls_loc, client_id=cid)
                        success = _booking_succeeded(result)
                        texted = alert_booking(w, result, success, source="notify auto-book")
                        bump("booked" if success else "book_failed")
                        bump("texts_sent" if texted else "texts_failed")
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
                        print(f"[full_watch] OPEN: {w['class_name']} on {date} ({match['open']} spot) — within 24h, texting only.")
                        texted = alert_open(w, match["open"])
                        bump("texts_sent" if texted else "texts_failed")
                        w["status"] = "opened"
                        w["opened_at"] = now.isoformat()
                        w["texted"] = texted
                        append_log({
                            "time": now.isoformat(), "action": "notify_open",
                            "class": w["class_name"], "date": w.get("class_date"),
                            "open": match["open"], "texted": texted,
                        })
                # else: still full — checked silently
        if changed:
            save_watchlist(watchlist)
    except Exception as e:
        print(f"[full_watch] Error: {e}")
        note = note or f"error: {e}"
    finally:
        with _full_check_lock:
            _last_full_check["ran_at"] = datetime.now().isoformat()
            _last_full_check["duration_ms"] = round((_time.time() - started) * 1000)
            _last_full_check["note"] = note
            _last_full_check["items"] = run_items
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


@app.route("/api/debug/jobs")
def api_debug_jobs():
    """List all scheduled APScheduler jobs with their next run time. Used by the
    Debug tab to confirm jobs are registered and firing as expected."""
    now = datetime.now()
    jobs = []
    for job in scheduler.get_jobs():
        nrt = job.next_run_time  # None if the job is paused / has no future run
        secs = (nrt - now.astimezone(nrt.tzinfo)).total_seconds() if nrt else None
        jobs.append({
            "id": job.id,
            "name": job.name,
            "trigger": str(job.trigger),
            "next_run": nrt.isoformat() if nrt else None,
            "seconds_until": round(secs) if secs is not None else None,
            "paused": nrt is None,
        })
    jobs.sort(key=lambda j: (j["seconds_until"] is None, j["seconds_until"] or 0))
    return jsonify({
        "running": scheduler.running,
        "now": now.isoformat(),
        "count": len(jobs),
        "jobs": jobs,
    })


@app.route("/api/debug/full-check")
def api_debug_full_check():
    """Results of the most recent check_full_watches run: per-item open/full status.
    Used by the Debug tab."""
    with _full_check_lock:
        snap = dict(_last_full_check)
        snap["items"] = [dict(i) for i in _last_full_check["items"]]
    items = snap["items"]
    snap["summary"] = {
        "total": len(items),
        "open": sum(1 for i in items if i["status"] == "open"),
        "full": sum(1 for i in items if i["status"] == "full"),
        "other": sum(1 for i in items if i["status"] not in ("open", "full")),
    }
    return jsonify(snap)


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
        w["class_dt"] = class_dt.isoformat() if class_dt else None  # sort key for the UI
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

    # mode: "autobook" (snag when signup opens) or "notify" (text when a full class frees up)
    mode = data.get("mode", "autobook")

    entry = {
        "class_name": data["name"],
        "class_date": data.get("class_date", ""),
        "class_id": data.get("class_id"),
        "tg": data.get("tg"),
        "cls_loc": data.get("cls_loc"),
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
    tg = data.get("tg") or "28"
    cls_loc = data.get("cls_loc") or "1"

    session = get_bot_session()
    try:
        cid = resolve_client_id(session)
        result = signup_for_class(session, class_id, class_date, tg=tg, cls_loc=cls_loc, client_id=cid)

        # If session expired, re-auth via the host headed login and retry once
        if "session expired" in result.lower():
            print("[api_book] Session expired, requesting host reauth and retrying...")
            trigger_host_reauth()
            session = get_bot_session()
            result = signup_for_class(session, class_id, class_date, tg=tg, cls_loc=cls_loc, client_id=cid)

        success = _booking_succeeded(result)
        info = _find_cached_class(class_id, class_date) or {"class_id": class_id, "class_date": class_date}
        alert_booking(info, result, success, source="manual book", quiet=True)
        bump("booked" if success else "book_failed")

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
    """Receive cookies from the host headed login and store them for the given
    account (defaults to the active one). Refreshes the live session if it's for
    the active account."""
    global _session, _user_name
    data = request.json
    if not data or not isinstance(data, dict):
        return jsonify({"error": "POST a JSON dict of cookies"}), 400

    aid = request.args.get("account") or _active_id
    if aid not in ACCOUNTS_BY_ID:
        aid = _active_id

    with open(cookie_jar_path(aid), "w") as f:
        json.dump(data, f)

    has_auth = "idsrvauth" in data
    if aid == _active_id:
        _user_name = None  # re-resolve name for the refreshed session
        with _session_lock:
            import requests as req
            _session = req.Session()
            _session.headers.update(HEADERS)
            apply_cookies(_session, data)

    bump("cookie_uploads")
    print(f"[upload-cookies] account {aid}: {len(data)} cookies, idsrvauth={has_auth}")
    return jsonify({"status": "ok", "account": aid, "cookies": len(data), "has_idsrvauth": has_auth})


_user_name = None  # cached display name of the signed-in member


@app.route("/api/auth-status")
def api_auth_status():
    """Report whether the active account's session is authenticated (has idsrvauth),
    the signed-in member's name, and which account is active."""
    global _user_name
    acct = active_account()
    has = bool(_session) and any(c.name == "idsrvauth" for c in _session.cookies)
    if has and not _user_name:
        try:
            _user_name = get_user_name(_session)
        except Exception:
            _user_name = None
    return jsonify({"authenticated": has, "user": _user_name if has else None,
                    "account_id": acct["id"], "account_name": acct["name"]})


@app.route("/api/accounts")
def api_accounts():
    """List configured accounts and which is active (for the switcher dropdown)."""
    return jsonify({
        "active": _active_id,
        "accounts": [{"id": a["id"], "name": a["name"], "email": a["email"],
                      "active": a["id"] == _active_id} for a in ACCOUNTS],
    })


@app.route("/api/active-account")
def api_active_account():
    """The active account's id/name/email — used by the host login helper."""
    a = active_account()
    return jsonify({"id": a["id"], "name": a["name"], "email": a["email"]})


@app.route("/api/switch-account", methods=["POST"])
def api_switch_account():
    """Switch the active account."""
    data = request.json or {}
    aid = str(data.get("id", ""))
    if not set_active_account(aid):
        return jsonify({"error": f"unknown account id {aid}"}), 400
    return jsonify({"status": "switched", "active": _active_id, "name": active_account()["name"]})


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
