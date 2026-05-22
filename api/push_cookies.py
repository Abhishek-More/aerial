#!/usr/bin/env python3
"""
Login locally (where playwright works) and push cookies to Railway.
Run this whenever Railway's session expires.

Usage: python push_cookies.py [RAILWAY_URL]
"""
import sys
import json
import requests
from bot import login_with_playwright, get_credentials

RAILWAY_URL = sys.argv[1] if len(sys.argv) > 1 else "https://aerial-production.up.railway.app"

print("Logging in locally via playwright...")
email, password = get_credentials()
cookies = login_with_playwright(email, password)

cookie_names = list(cookies.keys())
print(f"Got {len(cookies)} cookies: {cookie_names}")
has_auth = "idsrvauth" in cookie_names
print(f"Has idsrvauth: {has_auth}")

if not has_auth:
    print("WARNING: No idsrvauth cookie — booking won't work!")

print(f"\nPushing cookies to {RAILWAY_URL}/api/upload-cookies ...")
resp = requests.post(f"{RAILWAY_URL}/api/upload-cookies", json=cookies)
print(f"Response: {resp.status_code} {resp.json()}")
