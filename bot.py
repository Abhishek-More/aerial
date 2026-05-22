import re
import os
import json
import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://clients.mindbodyonline.com"
CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json")
COOKIE_JAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cookie_jar.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/classic/mainclass",
}


def save_cookie_jar(session: cloudscraper.CloudScraper):
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


def load_cookie_jar(session: cloudscraper.CloudScraper) -> bool:
    """Load previously saved cookies. Returns True if loaded."""
    if not os.path.exists(COOKIE_JAR_FILE):
        return False
    with open(COOKIE_JAR_FILE) as f:
        cookies = json.load(f)
    for name, info in cookies.items():
        session.cookies.set(name, info["value"], domain=info["domain"], path=info["path"])
    return bool(cookies)


def login(session: cloudscraper.CloudScraper, email: str, password: str) -> bool:
    """
    Log in to MindBody via the identity flow.
    Returns True on success.
    """
    print("Logging in...")

    # Step 1: Hit the main page to get initial session cookies
    resp = session.get(f"{BASE_URL}/classic/ws?studioid=836167", allow_redirects=True)
    resp.raise_for_status()

    # Step 2: Post credentials to the login endpoint
    login_url = f"{BASE_URL}/ASP/login_p.asp"
    login_data = {
        "requiredtxtUserName": email,
        "requiredtxtPassword": password,
        "tg": "",
        "vt": "",
        "lvl": "",
        "stype": "",
        "qParam": "",
        "view": "",
        "trn": "0",
        "page": "",
        "catid": "",
        "prodid": "",
        "prodGroupId": "",
        "date": "",
        "classid": "0",
        "sSU": "",
        "optForwardingLink": "",
        "isAsync": "false",
    }
    resp2 = session.post(login_url, data=login_data, allow_redirects=True)
    resp2.raise_for_status()

    # Check if login succeeded by looking for "signed in" or the welcome message
    if "you're signed in" in resp2.text.lower() or "you&#39;re signed in" in resp2.text.lower():
        print("Login successful!")
        save_cookie_jar(session)
        return True

    # Sometimes MindBody uses identity/OAuth login instead of the classic form.
    if "IdentityLogin" in resp2.url or "identity" in resp2.url.lower():
        print("This studio uses Identity login (OAuth). Classic login not supported.")
        return False

    # Check for login errors
    soup = BeautifulSoup(resp2.text, "html.parser")
    error_div = soup.find("div", id="LoginError") or soup.find("div", class_="LoginErrorDiv")
    if error_div and error_div.get_text(strip=True):
        print(f"Login failed: {error_div.get_text(strip=True)}")
        return False

    print("Login status unclear — checking if session is valid...")
    return check_session(session)


def check_session(session: cloudscraper.CloudScraper) -> bool:
    """Check if the current session is still valid."""
    resp = session.get(f"{BASE_URL}/classic/mainclass?fl=true&tabID=7", allow_redirects=False)
    # If we get a 200 with actual class content, session is good
    # If we get a redirect or a resetSession page, it's expired
    if resp.status_code == 200 and "resetSession" not in resp.text and "classSchedule" in resp.text:
        return True
    return False



def get_credentials() -> tuple[str, str]:
    """Load or prompt for credentials."""
    if os.path.exists(CREDS_FILE):
        with open(CREDS_FILE) as f:
            creds = json.load(f)
        return creds["email"], creds["password"]

    print("No saved credentials found.")
    email = input("Email: ").strip()
    password = input("Password: ").strip()

    save = input("Save credentials for next time? (y/n): ").strip().lower()
    if save == "y":
        with open(CREDS_FILE, "w") as f:
            json.dump({"email": email, "password": password}, f)
        os.chmod(CREDS_FILE, 0o600)
        print(f"Saved to {CREDS_FILE}")

    return email, password


def get_session() -> cloudscraper.CloudScraper:
    session = cloudscraper.create_scraper()
    session.headers.update(HEADERS)

    # Try 1: Load saved cookie jar from last successful session
    if load_cookie_jar(session) and check_session(session):
        print("Restored session from saved cookies.")
        return session

    # Try 2: Log in with credentials
    session.cookies.clear()
    email, password = get_credentials()
    if login(session, email, password):
        return session

    print("Login failed. Check your credentials and try again.")
    exit(1)


def get_classes(
    session: cloudscraper.CloudScraper,
    date: str = "",
    location: str = "0",
    tab_id: str = "7",
    class_type: str = "0",
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

    resp = session.post(url, params=params, data=form_data)
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
            date_match = re.search(r"(\w+)\s+(\d+),\s*(\d{4})", current_date)
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

    return classes


def signup_for_class(session: cloudscraper.CloudScraper, class_id: str, class_date: str, tg: str = "28", cls_loc: str = "1") -> str:
    """
    Book a class through the 3-step MindBody flow:
      1. res_a.asp    — reservation page (sets session state)
      2. res_deb.asp  — debit/confirm (actually books it)
      3. my_sch.asp   — confirmation page
    """
    # Step 1: Hit the reservation page
    res_a_url = f"{BASE_URL}/ASP/res_a.asp"
    res_a_params = {"tg": tg, "classId": class_id, "classDate": class_date, "clsLoc": cls_loc}
    resp = session.get(res_a_url, params=res_a_params)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text()

    # Check if class is full
    if "class is full" in page_text.lower():
        return f"Class {class_id} on {class_date} is FULL."

    # Step 2: Hit res_deb.asp to confirm the booking
    # Extract clientId from the page if available, fallback to finding it in scripts
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
    resp2 = session.get(res_deb_url, params=res_deb_params)
    resp2.raise_for_status()

    soup2 = BeautifulSoup(resp2.text, "html.parser")
    page_text2 = soup2.get_text()

    # Step 3: Check for confirmation — look for the classSchIDs redirect or confirmation text
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
        resp3 = session.get(my_sch_url, params=my_sch_params)
        resp3.raise_for_status()
        soup3 = BeautifulSoup(resp3.text, "html.parser")
        page_text3 = soup3.get_text()

        if "you've booked" in page_text3.lower():
            # Extract what was booked
            notify = soup3.find("div", id="notifyBooking")
            if notify:
                return notify.get_text(strip=True)
            return f"Successfully booked class {class_id} on {class_date}!"

    # If we got here, check for errors
    if "full" in page_text2.lower() or "waitlist" in page_text2.lower():
        return f"Class {class_id} on {class_date} is FULL."

    return f"Booking may have failed. Response snippet: {page_text2[:300]}"


if __name__ == "__main__":
    session = get_session()
    print("Fetching classes...")
    classes = get_classes(session)
    for cls in classes:
        print(f"  {cls['date']}  {cls['time']:>14}  {cls['name']:<45}  {cls['open']} open  (id={cls['class_id']})")
