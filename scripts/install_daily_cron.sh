#!/usr/bin/env bash
#
# Install (or update / remove) a cron job that posts one moodful post per day.
#
# Default: a RANDOM time between 6am and midnight (local). It does this by
# firing every hour in the window and letting `post-daily` roll whether this is
# the hour (each hour equally likely; the last hour is guaranteed), then
# scattering a random number of minutes within that hour. Robust to the machine
# sleeping — the next awake hour just re-rolls.
#
# Usage:
#   scripts/install_daily_cron.sh                    random time, 6am–midnight (default)
#   scripts/install_daily_cron.sh --window=8-22      random time, 8am–10pm
#   scripts/install_daily_cron.sh 09:00              fixed time instead (every day at 9:00)
#   scripts/install_daily_cron.sh --dry-run [...]    print the crontab line, change nothing
#   scripts/install_daily_cron.sh --uninstall        remove the job
#
# The job cd's into the repo so the CLI auto-loads .env (your Bluesky creds);
# output is appended to logs/daily-post.log. cron uses your machine's LOCAL
# time. To switch which post goes out by calendar date instead of a running
# count, add `--start YYYY-MM-DD` to the command below.
#
set -euo pipefail

MARKER="# moodful-daily-post"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi

UNINSTALL=0
DRYRUN=0
WINDOW="6-24"
FIXED=""
for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    --dry-run)   DRYRUN=1 ;;
    --window=*)  WINDOW="${arg#--window=}" ;;
    [0-2][0-9]:[0-5][0-9]) FIXED="$arg" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

current_crontab() { crontab -l 2>/dev/null || true; }
without_marker()  { current_crontab | grep -vF "$MARKER" || true; }

if [ "$UNINSTALL" -eq 1 ]; then
  without_marker | crontab -
  echo "Removed the moodful daily-post cron job (if it was installed)."
  exit 0
fi

if [ -n "$FIXED" ]; then
  HH=$((10#${FIXED%%:*}))
  MM=$((10#${FIXED##*:}))
  SCHEDULE="$MM $HH * * *"
  CMD="$PY -m moodful_responder post-daily"
  DESC=$(printf 'every day at %02d:%02d local' "$HH" "$MM")
else
  WS="${WINDOW%-*}"
  WE="${WINDOW#*-}"
  if ! [[ "$WS" =~ ^[0-9]+$ && "$WE" =~ ^[0-9]+$ ]] || [ "$WS" -ge "$WE" ] || [ "$WE" -gt 24 ]; then
    echo "invalid --window='$WINDOW' (use START-END hours, e.g. 6-24)" >&2
    exit 2
  fi
  SCHEDULE="0 ${WS}-$((WE - 1)) * * *"
  CMD="$PY -m moodful_responder post-daily --window ${WINDOW}"
  DESC="a random time between ${WS}:00 and ${WE}:00 local"
fi

CRON_LINE="$SCHEDULE cd $REPO && $CMD >> $REPO/logs/daily-post.log 2>&1 $MARKER"

if [ "$DRYRUN" -eq 1 ]; then
  echo "Would install this crontab line ($DESC); nothing changed:"
  echo "  $CRON_LINE"
  exit 0
fi

[ -n "$PY" ] || { echo "No python found — create the venv first (python3 -m venv .venv)." >&2; exit 1; }
mkdir -p "$REPO/logs"

{ without_marker; echo "$CRON_LINE"; } | crontab -

echo "Installed: one moodful post at $DESC."
echo "  $CRON_LINE"
echo
echo "verify:  crontab -l"
echo "logs:    tail -f $REPO/logs/daily-post.log"
echo "test:    cd $REPO && $PY -m moodful_responder post-daily --dry-run"
echo "remove:  $0 --uninstall"
