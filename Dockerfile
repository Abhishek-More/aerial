FROM python:3.12-slim

# Install playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates fonts-liberation libasound2 libatk-bridge2.0-0 \
    libatk1.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 \
    libnss3 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 xdg-utils \
    libpango-1.0-0 libcairo2 libxshmfence1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install playwright chromium browser
RUN playwright install chromium

ENV PYTHONUNBUFFERED=1

COPY api/ ./api/

WORKDIR /app/api

# NOTE: no --preload. Startup code (session-init thread + APScheduler) runs at
# import time; with --preload it runs in the master and dies on fork, leaving the
# worker with an unset _boot_done and empty cache. Importing per-worker fixes that.
CMD sh -c "gunicorn app:app --bind 0.0.0.0:${PORT:-5050} --workers 1 --threads 2 --timeout 120"
