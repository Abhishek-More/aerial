#!/bin/sh
# Deploy the moment a push lands on main or master, instead of waiting for the
# poller.
#
# `gh webhook forward` opens a websocket to GitHub's webhook forwarder and
# streams push events here, so this needs no public endpoint, no tunnel, no
# receiver process and no shared secret. Every event for main or master runs
# autodeploy.sh, which compares SHAs before it builds, so a duplicate or
# replayed event is a no-op.
#
# Runs under launchd with KeepAlive
# (~/Library/LaunchAgents/com.aerial.hook.plist). The 15-minute poller stays as
# the backstop for events missed while this is down.
set -eu
cd "$(dirname "$0")"
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

REPO=Abhishek-More/aerial

# Each forward run creates its own repo webhook and only deletes it on a clean
# exit. GitHub caps a repo at 20 hooks, so reap the leftovers from earlier runs
# first or the pipeline eventually stops being able to register one.
gh api "repos/$REPO/hooks" \
  --jq '.[] | select(.config.url | test("webhook-forwarder.github.com")) | .id' |
  while read -r id; do
    echo "=== $(date) removing stale forwarder hook $id"
    gh api -X DELETE "repos/$REPO/hooks/$id" >/dev/null || true
  done

echo "=== $(date) listening for pushes to $REPO"
gh webhook forward --events=push --repo="$REPO" | while IFS= read -r line; do
  case $line in
    *refs/heads/main*) branch=main ;;
    *refs/heads/master*) branch=master ;;
    *) continue ;;
  esac
  echo "=== $(date) push to $branch"
  sh ./autodeploy.sh "$branch" || true    # keep listening even if one deploy fails
done
