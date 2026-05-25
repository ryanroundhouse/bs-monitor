#!/usr/bin/env bash
#
# Install (or update / remove) a daily cron job that posts one moodful post
# to Bluesky.
#
# Usage:
#   scripts/install_daily_cron.sh [HH:MM]            install/update (default 09:00 local)
#   scripts/install_daily_cron.sh --dry-run [HH:MM]  print the crontab line, change nothing
#   scripts/install_daily_cron.sh --uninstall        remove the job
#
# Notes:
#   - The job cd's into the repo so the CLI auto-loads .env (your Bluesky
#     credentials) and finds the bundled posts. Output is appended to
#     logs/daily-post.log.
#   - It runs `post-daily` in count mode: the next unposted post each day,
#     tracked in responder.db, with a built-in guard against double-posting
#     on the same day. To switch to deterministic calendar mode, add
#     `--start YYYY-MM-DD` to the command below.
#   - cron uses your machine's LOCAL time and only fires while the machine is
#     awake. (For a laptop that sleeps, a small always-on host is more reliable.)
#
set -euo pipefail

MARKER="# moodful-daily-post"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi

MODE="install"
TIME="09:00"
for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE="uninstall" ;;
    --dry-run)   MODE="dryrun" ;;
    [0-2][0-9]:[0-5][0-9]) TIME="$arg" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

current_crontab() { crontab -l 2>/dev/null || true; }
without_marker()  { current_crontab | grep -vF "$MARKER" || true; }

if [ "$MODE" = "uninstall" ]; then
  without_marker | crontab -
  echo "Removed the moodful daily-post cron job (if it was installed)."
  exit 0
fi

HH=$((10#${TIME%%:*}))
MM=$((10#${TIME##*:}))
CRON_LINE="$MM $HH * * * cd $REPO && $PY -m moodful_responder post-daily >> $REPO/logs/daily-post.log 2>&1 $MARKER"

if [ "$MODE" = "dryrun" ]; then
  echo "Would install this crontab line (nothing changed):"
  echo "  $CRON_LINE"
  exit 0
fi

[ -n "$PY" ] || { echo "No python found — create the venv first (python3 -m venv .venv)." >&2; exit 1; }
mkdir -p "$REPO/logs"

# Replace any existing moodful line, then add the new one.
{ without_marker; echo "$CRON_LINE"; } | crontab -

printf 'Installed: one moodful post daily at %02d:%02d local time.\n' "$HH" "$MM"
echo "  $CRON_LINE"
echo
echo "verify:  crontab -l"
echo "logs:    tail -f $REPO/logs/daily-post.log"
echo "test:    cd $REPO && $PY -m moodful_responder post-daily --dry-run"
echo "remove:  $0 --uninstall"
