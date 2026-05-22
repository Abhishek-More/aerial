import re
import os
import json
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://clients.mindbodyonline.com"
_DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
COOKIE_JAR_FILE = os.path.join(_DATA_DIR, ".cookie_jar.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/classic/mainclass",
}


def save_cookie_jar(session: requests.Session):
    """Save session cookies to disk for reuse."""
    cookies = {}
    for cookie in session.cookies:
        cookies[cookie.name] = {
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
        }
    with open(COOKIE_JAR_FILE, "w") as f:
        json.dump(cookies, f)


def load_cookie_jar(session: requests.Session) -> bool:
    """Load previously saved cookies. Returns True if loaded."""
    if not os.path.exists(COOKIE_JAR_FILE):
        return False
    with open(COOKIE_JAR_FILE) as f:
        cookies = json.load(f)
    for name, info in cookies.items():
        session.cookies.set(name, info["value"], domain=info["domain"], path=info["path"])
    return bool(cookies)


def login_with_playwright(email: str, password: str) -> dict[str, str]:
    """
    Use a headless browser to log in and bypass Cloudflare.
    Returns a dict of cookies to apply to a requests session.
    """
    from playwright.sync_api import sync_playwright

    print("[login] Launching headless browser...")
    with sync_playwright() as p:
        print("[login] Starting Chromium...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        # Step 1: Navigate to the studio page (Cloudflare challenge solved by real browser)
        print(f"[login] Navigating to {BASE_URL}/classic/ws?studioid=836167")
        page.goto(f"{BASE_URL}/classic/ws?studioid=836167", wait_until="networkidle", timeout=60000)
        print(f"[login] Page loaded. URL: {page.url}")
        print(f"[login] Page title: {page.title()}")

        # Step 2: Fill in login form and submit via the actual form POST
        print("[login] Waiting for login form (#su1UserName)...")
        page.wait_for_selector("#su1UserName", timeout=30000)
        print("[login] Login form found. Filling credentials...")
        page.fill("#su1UserName", email)
        page.fill("#su1Password", password)

        # Click login and wait for it to process
        print("[login] Clicking login button...")
        page.click("#btnSu1Login")

        # Give login time to process — wait a bit then grab cookies
        print("[login] Waiting for login to process...")
        page.wait_for_timeout(5000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        print(f"[login] Post-login URL: {page.url}")

        # Grab cookies immediately — no further navigation needed
        browser_cookies = context.cookies()
        print(f"[login] Extracted {len(browser_cookies)} cookies")
        cookie_names = [c["name"] for c in browser_cookies]
        print(f"[login] Cookie names: {cookie_names}")

        browser.close()

    # Convert to dict
    cookies = {}
    for c in browser_cookies:
        cookies[c["name"]] = {
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
        }
    return cookies


def apply_cookies(session: requests.Session, cookies: dict):
    """Apply a cookie dict to a requests session."""
    for name, info in cookies.items():
        session.cookies.set(name, info["value"], domain=info["domain"], path=info["path"])


def check_session(session: requests.Session) -> bool:
    """Check if the current session is still valid."""
    print("[session] Checking if session is valid...")
    try:
        resp = session.get(f"{BASE_URL}/classic/mainclass?fl=true&tabID=7", allow_redirects=False, timeout=15)
        print(f"[session] Status: {resp.status_code}, length: {len(resp.text)}")
        has_reset = "resetSession" in resp.text
        has_schedule = "classSchedule" in resp.text
        print(f"[session] Has resetSession: {has_reset}, has classSchedule: {has_schedule}")
        if resp.status_code == 200 and not has_reset and has_schedule:
            print("[session] Session is valid!")
            return True
    except Exception as e:
        print(f"[session] Check failed: {e}")
    print("[session] Session is invalid or expired.")
    return False


def get_credentials() -> tuple[str, str]:
    """Load credentials from environment variables."""
    email = os.environ.get("MB_EMAIL", "")
    password = os.environ.get("MB_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("Set MB_EMAIL and MB_PASSWORD environment variables.")
    return email, password


def get_session() -> requests.Session:
    print("[get_session] Creating new session...")
    session = requests.Session()
    session.headers.update(HEADERS)

    # Try 1: Load saved cookie jar from last successful session
    print("[get_session] Try 1: Loading saved cookie jar...")
    jar_loaded = load_cookie_jar(session)
    print(f"[get_session] Cookie jar loaded: {jar_loaded}, cookies: {len(session.cookies)}")
    if jar_loaded and check_session(session):
        print("[get_session] Restored session from saved cookies.")
        return session

    # Try 2: Log in with playwright (bypasses Cloudflare)
    print("[get_session] Try 2: Logging in with playwright...")
    session.cookies.clear()
    email, password = get_credentials()
    print(f"[get_session] Credentials loaded for: {email}")
    cookies = login_with_playwright(email, password)
    print(f"[get_session] Got {len(cookies)} cookies from browser")

    # Apply cookies to requests session
    apply_cookies(session, cookies)

    # Save for next time
    with open(COOKIE_JAR_FILE, "w") as f:
        json.dump(cookies, f)
    print("[get_session] Cookie jar saved")

    if check_session(session):
        print("[get_session] Session established via browser login.")
        return session

    raise RuntimeError("Login succeeded in browser but session check failed.")


def get_classes(
    session: requests.Session,
    date: str = "",
    location: str = "1",
    tab_id: str = "7",
    class_type: str = "28",
) -> list[dict]:
    """Fetch and parse the weekly class schedule."""
    if not date:
        from datetime import datetime
        date = datetime.now().strftime("%-m/%-d/%Y")
    url = f"{BASE_URL}/classic/mainclass"
    params = {"fl": "true", "tabID": tab_id}
    form_data = {
        "pageNum": "1",
        "requiredtxtUserName": "",
        "requiredtxtPassword": "",
        "optForwardingLink": "",
        "optRememberMe": "",
        "tabID": tab_id,
        "optView": "week",
        "useClassLogic": "true",
        "filterByClsSch": "",
        "prevFilterByClsSch": "-1",
        "prevFilterByClsSch2": "-2",
        "txtDate": date,
        "optLocation": location,
        "optTG": class_type,
        "optVT": "0",
        "optInstructor": "0",
    }

    resp = session.post(url, params=params, data=form_data, timeout=30)
    resp.raise_for_status()
    return parse_classes(resp.text)


def parse_classes(html: str) -> list[dict]:
    """Parse the MindBody class schedule HTML."""
    soup = BeautifulSoup(html, "html.parser")
    schedule_div = soup.find("div", id="classSchedule-mainTable")
    if not schedule_div:
        return []

    classes = []
    current_date = ""
    current_date_str = ""  # M/D/YYYY format for booking

    for element in schedule_div.children:
        if not hasattr(element, "get"):
            continue

        # Day header: <div class="header" id="an3"><b><span>Tue </span>May 26, 2026</b></div>
        if "header" in (element.get("class") or []):
            current_date = element.get_text(strip=True)
            # Parse "TueMay 26, 2026" or "Tue May 26, 2026" into M/D/YYYY
            # The month name may be glued to the day abbreviation (e.g. "FriMay")
            date_match = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d+),\s*(\d{4})", current_date)
            if date_match:
                from datetime import datetime as _dt
                try:
                    parsed = _dt.strptime(f"{date_match.group(1)} {date_match.group(2)}, {date_match.group(3)}", "%B %d, %Y")
                    current_date_str = parsed.strftime("%-m/%-d/%Y")
                    # Also fix the display date to have a space after day abbreviation
                    span = element.find("span", class_="headText")
                    if span:
                        current_date = span.get_text(strip=True) + " " + date_match.group(0)
                except ValueError:
                    current_date_str = ""
            continue

        # Class row: <div class="evenRow row"> or <div class="oddRow row">
        row_classes = element.get("class") or []
        if "row" not in row_classes:
            continue

        cols = element.find_all("div", class_="col")
        if len(cols) < 5:
            continue

        # Extract time from first col
        time = cols[0].get_text(strip=True)

        # Extract signup button and class_id/class_date from onClick
        class_id = None
        class_date = current_date_str  # default from day header
        has_signup = False
        btn = element.find("input", class_="SignupButton")
        if btn:
            has_signup = True
            onclick = btn.get("onclick", "")
            m = re.search(r"classId=(\d+)&classDate=([^&']+)", onclick)
            if m:
                class_id = m.group(1)
                class_date = m.group(2)

        # Extract availability from the text like "(6 Reserved, 0 Open)"
        avail_text = ""
        for div in element.find_all("div"):
            t = div.get_text(strip=True)
            if "Reserved" in t and "Open" in t:
                avail_text = t
                break

        reserved = 0
        open_spots = 0
        m = re.search(r"\((\d+)\s*Reserved,\s*(\d+)\s*Open\)", avail_text)
        if m:
            reserved = int(m.group(1))
            open_spots = int(m.group(2))

        # Class name from modalClassDesc link
        name_link = element.find("a", class_="modalClassDesc")
        class_name = name_link.get_text(strip=True) if name_link else ""

        # Teacher
        teacher_link = element.find("a", class_="modalBio")
        if teacher_link:
            teacher = teacher_link.get_text(strip=True)
        else:
            # Some teachers aren't links (no bio), just plain text in the col
            teacher = cols[3].get_text(strip=True) if len(cols) > 3 else ""

        # Location
        loc_link = element.find("a", class_="modalLocationInfo")
        location = loc_link.get_text(strip=True) if loc_link else ""

        # Duration
        duration = cols[4].get_text(strip=True) if len(cols) > 4 else ""

        classes.append({
            "date": current_date,
            "time": time,
            "name": class_name,
            "teacher": teacher,
            "location": location,
            "duration": duration,
            "reserved": reserved,
            "open": open_spots,
            "has_signup": has_signup,
            "class_id": class_id,
            "class_date": class_date,
        })

    # Filter out classes in the past
    from datetime import datetime as _dt2
    now = _dt2.now()
    future_classes = []
    for cls in classes:
        if not cls["class_date"]:
            future_classes.append(cls)
            continue
        try:
            # Normalize: replace &nbsp; and extra whitespace, strip timezone
            time_clean = re.sub(r"[\xa0\s]+", " ", cls["time"]).strip()
            time_clean = re.sub(r"\s+[A-Z]{2,4}$", "", time_clean).strip()
            cls_dt = _dt2.strptime(f"{cls['class_date']} {time_clean}", "%m/%d/%Y %I:%M %p")
            if cls_dt >= now:
                future_classes.append(cls)
        except ValueError as e:
            future_classes.append(cls)
    return future_classes


def signup_for_class(session: requests.Session, class_id: str, class_date: str, tg: str = "28", cls_loc: str = "1") -> str:
    """
    Book a class through the 3-step MindBody flow:
      1. res_a.asp    — reservation page (sets session state)
      2. res_deb.asp  — debit/confirm (actually books it)
      3. my_sch.asp   — confirmation page
    """
    # Step 1: Hit the reservation page
    res_a_url = f"{BASE_URL}/ASP/res_a.asp"
    res_a_params = {"tg": tg, "classId": class_id, "classDate": class_date, "clsLoc": cls_loc}
    resp = session.get(res_a_url, params=res_a_params, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text()

    # Check if class is full
    if "class is full" in page_text.lower():
        return f"Class {class_id} on {class_date} is FULL."

    # Step 2: Hit res_deb.asp to confirm the booking
    client_id = ""
    client_match = re.search(r"clientId=(\d+)", resp.text)
    if client_match:
        client_id = client_match.group(1)

    res_deb_url = f"{BASE_URL}/ASP/res_deb.asp"
    res_deb_params = {
        "classID": class_id,
        "courseid": "",
        "classDate": class_date,
        "pmtRefNo": "0",
        "clsLoc": cls_loc,
        "typeGroupID": tg,
        "recurring": "false",
        "unpd": "1",
        "wlID": "",
        "clientId": client_id,
        "enroll": "false",
    }
    resp2 = session.get(res_deb_url, params=res_deb_params, timeout=30)
    resp2.raise_for_status()

    soup2 = BeautifulSoup(resp2.text, "html.parser")
    page_text2 = soup2.get_text()

    # Step 3: Check for confirmation
    if "you've booked" in page_text2.lower() or "notifyBooking" in resp2.text:
        return f"Successfully booked class {class_id} on {class_date}!"

    # Sometimes res_deb redirects to my_sch.asp with confirmation
    sch_match = re.search(r"classSchIDs=([^&\"']+)", resp2.text)
    if sch_match:
        sch_ids = sch_match.group(1)
        my_sch_url = f"{BASE_URL}/ASP/my_sch.asp"
        my_sch_params = {
            "classSchIDs": sch_ids,
            "enroll": "false",
            "recurring": "false",
            "optResfor": "",
            "back": "no",
            "modal": "",
            "tabID": "2",
        }
        resp3 = session.get(my_sch_url, params=my_sch_params, timeout=30)
        resp3.raise_for_status()
        soup3 = BeautifulSoup(resp3.text, "html.parser")
        page_text3 = soup3.get_text()

        if "you've booked" in page_text3.lower():
            notify = soup3.find("div", id="notifyBooking")
            if notify:
                return notify.get_text(strip=True)
            return f"Successfully booked class {class_id} on {class_date}!"

    if "full" in page_text2.lower() or "waitlist" in page_text2.lower():
        return f"Class {class_id} on {class_date} is FULL."

    return f"Booking may have failed. Response snippet: {page_text2[:300]}"


if __name__ == "__main__":
    session = get_session()
    print("Fetching classes...")
    classes = get_classes(session)
    for cls in classes:
        print(f"  {cls['date']}  {cls['time']:>14}  {cls['name']:<45}  {cls['open']} open  (id={cls['class_id']})")
