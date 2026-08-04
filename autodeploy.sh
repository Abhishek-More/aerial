#!/bin/sh
# Rebuild and restart the container whenever a new commit lands on origin/main
# or origin/master.
#
# Two callers, same script: deploy-hook.sh on a push event (the fast path) and
# launchd every 15 minutes (~/Library/LaunchAgents/com.aerial.deploy.plist) as
# the backstop for pushes missed while the forwarder was down. The SHA
# comparison below is what makes running twice a no-op.
set -eu
cd "$(dirname "$0")"
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

# A failed deploy has to reach a phone: this box runs headless with a locked
# screen, where `osascript display notification` draws to nobody.
imsg() {
  secret=$(sed -n 's/^IMSG_PATH=//p' "$HOME/Code/imsg/.env" 2>/dev/null || true)
  [ -n "${secret:-}" ] || return 0
  curl -fsS -m 10 -X POST -H "Title: deploy" -d "$1" \
    "http://127.0.0.1:8779/$secret" >/dev/null 2>&1 || true
}

branch=${1:-main}
case "$branch" in
  main|master) ;;
  *) echo "unsupported deploy branch: $branch" >&2; exit 2 ;;
esac

# Credentials come from the repo-local `gh auth git-credential` helper, so a
# fetch works headless (over ssh, no keychain) as well as under launchd.
if ! git fetch -q origin "$branch"; then
  imsg "aerial: git fetch failed, deploys are stalled"
  exit 1
fi
want=$(git rev-parse "origin/$branch")
state=.git/autodeploy-sha    # untracked, survives reset; seeded at install time
if [ -f "$state" ] && [ "$(cat "$state")" = "$want" ]; then exit 0; fi

echo "=== $(date) deploying $want from $branch"

# origin is the source of truth here. Anything edited on the box directly is
# parked in a stash (recover with `git stash list` / `git stash show -p`)
# rather than silently discarded or merged into the deploy.
git stash push -u -m "autodeploy $(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null || true
git reset --hard -q "$want"

if ! ./deploy.sh; then
  imsg "aerial deploy failed on $(git log -1 --format=%s "$want" | cut -c1-60)"
  exit 1    # no state write, so the next tick retries
fi

echo "$want" > "$state"
