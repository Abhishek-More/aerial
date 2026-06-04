#!/bin/bash
# One command to refresh the app's MindBody session.
# Opens a real Chrome window; log in there, and your session is uploaded to the running app.
set -e
cd "$(dirname "$0")"
VENV="$HOME/.aerial-helper-venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "Helper environment missing. Run: python3 -m venv $VENV && $VENV/bin/pip install playwright"
  exit 1
fi
exec "$VENV/bin/python" login_helper.py
