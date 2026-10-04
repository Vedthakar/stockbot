#!/bin/zsh
# Start the bot in PAPER mode from a scheduler (launchd/cron) or by hand.
#
# macOS note: launchd agents cannot open files under ~/Documents (TCC), so
# if you schedule this, keep the project outside ~/Documents (e.g. ~/stockbot).
# The retry loop below covers a wake-from-sleep race only.

PROJECT_DIR="${0:A:h}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') start_bot: $*" }

# Wait for the project volume to actually be readable (up to ~60s).
for attempt in {1..30}; do
  [[ -r "$PROJECT_DIR/main.py" ]] && break
  [[ $attempt -eq 1 ]] && log "project dir not readable yet (waking?); retrying"
  sleep 2
done

if ! cd "$PROJECT_DIR" 2>/dev/null || [[ ! -r main.py ]]; then
  log "FATAL: $PROJECT_DIR still unreadable after 60s; giving up"
  exit 1
fi

# If a bot is already running (pid file present and process alive), do nothing.
# The dashboard's own scheduler may have won the race; that is fine.
if [[ -f bot.pid ]] && kill -0 "$(cat bot.pid)" 2>/dev/null; then
  log "already running (pid $(cat bot.pid)); skipping"
  exit 0
fi

# Fresh morning session: clear any leftover "paused" state from yesterday.
echo "run" > bot.control

# Hold off idle sleep while the bot runs, so a mid-session nap cannot silently
# stop trading with a position open. -i is idle-sleep only: closing the lid
# still sleeps, and the hold dies with the bot.
log "launching main.py (idle sleep held until it exits)"
exec caffeinate -i .venv/bin/python main.py
