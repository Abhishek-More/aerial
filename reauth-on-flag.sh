#!/bin/bash
# Fired by launchd (WatchPaths) when the app drops a remote re-auth flag in the
# shared /data volume. Runs the headed auto-login, then clears the flag.
cd "$(dirname "$0")"
FLAG="$HOME/.aerial-data/.reauth_request"
[ -f "$FLAG" ] || exit 0
echo "$(date) — reauth flag detected, running auto-login"
"$HOME/.aerial-helper-venv/bin/python" login_helper.py --auto
rm -f "$FLAG"
