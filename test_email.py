#!/usr/bin/env python3
"""
Send a test email using the SMTP settings in api/.env.

Usage:
  python3 test_email.py                 # sends to NOTIFY_EMAIL / MB_EMAIL
  python3 test_email.py you@example.com # sends to a specific address

Tries the app password as-is, then with spaces stripped (Gmail shows app
passwords with spaces; both should work, but we try both to be safe).
"""
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

env = {}
env_path = Path(__file__).parent / "api" / ".env"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

user = env.get("SMTP_USER", "")
pw = env.get("SMTP_PASS", "")
host = env.get("SMTP_HOST", "smtp.gmail.com")
port = int(env.get("SMTP_PORT", "587"))
to = sys.argv[1] if len(sys.argv) > 1 else (env.get("NOTIFY_EMAIL") or env.get("MB_EMAIL") or user)

print(f"From: {user}")
print(f"To:   {to}")
print(f"SMTP: {host}:{port}")

if not user or not pw:
    print("ERROR: SMTP_USER and SMTP_PASS must be set in api/.env")
    sys.exit(1)


def try_send(password, label):
    try:
        msg = EmailMessage()
        msg["Subject"] = "Aerial test email"
        msg["From"] = user
        msg["To"] = to
        msg.set_content("This is a test email from your aerial watcher. "
                        "If you received this, SMTP is working.")
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, password)
            s.send_message(msg)
        print(f"[{label}] SUCCESS — email sent to {to}")
        return True
    except Exception as e:
        print(f"[{label}] FAILED: {e}")
        return False


ok = try_send(pw, "password as-is")
if not ok:
    stripped = pw.replace(" ", "")
    if stripped != pw:
        ok = try_send(stripped, "password without spaces")

if not ok:
    print("\nStill failing. Common causes:")
    print("  - 2-Step Verification not enabled on", user)
    print("  - App Password was created for a different Google account than SMTP_USER")
    print("  - App Password typo / was revoked")
    print("  - Using your normal Gmail password instead of an App Password")
    sys.exit(1)
