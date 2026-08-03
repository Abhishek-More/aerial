FROM python:3.12-slim

# Playwright/Chromium system dependencies (same set the main app uses).
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates fonts-liberation libasound2 libatk-bridge2.0-0 \
    libatk1.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 \
    libnss3 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 xdg-utils \
    libpango-1.0-0 libcairo2 libxshmfence1 tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=America/New_York
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

# bot.py comes from the parent dir (login flow + constants reused by monitor.py).
# Build from the repo root:  docker build -f monitor/Dockerfile -t aerial-monitor .
COPY api/bot.py ./bot.py
COPY monitor/monitor.py ./monitor.py

# Long-running job: one check immediately, then every CHECK_INTERVAL_MINUTES.
CMD ["python", "monitor.py"]
