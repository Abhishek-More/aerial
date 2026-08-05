# aerial — agent notes

A MindBody class booker: a Flask app plus an APScheduler worker that watches
classes, snags them the second signup opens, and alerts either way.

## Deploy: push to main, that is the whole pipeline

`origin/main` is the source of truth. The always-on box
(`computer.tail50b400.ts.net`, macOS) deploys a push within seconds:

```
launchd com.aerial.hook   (KeepAlive)             <- the fast path
  -> deploy-hook.sh
       gh webhook forward --events=push   (websocket to GitHub, no public port)
       line mentions refs/heads/main?  -> autodeploy.sh

launchd com.aerial.deploy (StartInterval 900)     <- the backstop
  -> autodeploy.sh                     catches pushes missed while the
                                       forwarder was down

autodeploy.sh
  git fetch origin main
  new commit?  -> git stash -u  ->  git reset --hard origin/main  ->  ./deploy.sh
  -> deploy.sh: docker compose up -d --build + health check on /api/boot-log
  -> log for all of it: ~/Library/Logs/aerial-deploy.log
```

This is the same pipeline concierge runs, script for script.

Consequences worth knowing before you touch anything:

- **Merged to main means shipped**, in seconds. There is no staging.
- **Never edit the checkout on the box.** The next deploy stashes whatever is
  there and hard-resets to `origin/main`. This bit once already: the box carried
  1029 uncommitted lines that only existed on that one machine, and they had to
  be committed (`b8263df`) before autodeploy could be turned on at all. Recover a
  bad day with `git stash list` in `~/Code/aerial` on the box.
- **The state file is `.git/autodeploy-sha`** (untracked): the last SHA that
  deployed successfully. A failed `deploy.sh` leaves it alone, so the next tick
  retries. Force a redeploy with `rm .git/autodeploy-sha`.
- **A failed deploy texts you** through `imsg` (see below), because the box runs
  headless with a locked screen where a desktop notification draws to nobody.
- **`api/.env` is not in git** and is never touched by a deploy: `MB*_EMAIL` /
  `MB*_PASSWORD`, `IMSG_URL`. Every variable in it
  reaches the container via `env_file`, so a new knob needs no compose change.
- **State lives in `~/.aerial-data`**, bind-mounted to `/data`: cookie jars,
  per-account watchlists, the booking log. The image is disposable; that
  directory is not. `DATA_DIR` falls back to `api/` when `/data` is absent, which
  is what makes a local run work.
- **The image is expensive to build** (playwright + chromium). Only `COPY api/`
  sits below the heavy layers, so an app-only change rebuilds in seconds as long
  as the Docker cache survives.
- **No `--preload` on gunicorn.** Startup work (session-init thread,
  APScheduler) runs at import time; preloading runs it in the master where it
  dies on fork.
- **`monitor/`** is a separate, currently unused watchdog with its own compose
  file. Nothing deploys it.

## Alerts

Everything the bot wants to tell you goes through `_notify()` in `api/app.py`:
it prints the detail to stdout (so it lands in `docker logs`), and an iMessage
for the buzz.

The iMessage half is [`imsg`](https://github.com/Abhishek-More/imsg), a small
shared service on the host (launchd `com.abhishek.imsg`, also used by
concierge). `IMSG_URL` in `api/.env` points at it:

```sh
IMSG_URL=http://host.docker.internal:8779/<the imsg secret>
```

Unset means the channel is off — no alert reaches anyone, which is not an error.
Host-side scripts use `127.0.0.1` instead of `host.docker.internal`.

## Checks

There is no test suite. What exists:

```sh
python3 -m py_compile api/app.py api/bot.py    # syntax
./deploy.sh                                    # builds, starts, health checks
curl -s localhost:5050/api/boot-log            # what the app did on startup
```

A push is a deploy. Run something before pushing.
