#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

BOT="$PROJECT_DIR/bot.py"
LOG="$PROJECT_DIR/bot.log"
PID_FILE="$PROJECT_DIR/bot.pid"
LABEL="com.myu.ai-telegram-bot"
PLIST_TEMPLATE="$PROJECT_DIR/scripts/$LABEL.plist"
RUNTIME_PLIST="$PROJECT_DIR/scripts/.${LABEL}.runtime.plist"
DOMAIN="gui/$(id -u)"

cd "$PROJECT_DIR"

# Match only this project's venv-launched bot.py, not any same-named bot.py elsewhere on the machine.
find_bot_pids() {
  pgrep -f "^$PROJECT_DIR/.venv/bin/python .*$BOT" || true
}

echo "Stopping existing bot.py processes for $PROJECT_DIR ..."
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true

# Dynamically generate runtime plist with current PROJECT_DIR
sed "s|/Users/myu/github/ai_telegram|$PROJECT_DIR|g" "$PLIST_TEMPLATE" > "$RUNTIME_PLIST"

pids=($(find_bot_pids))
if (( ${#pids[@]} > 0 )); then
  kill "${pids[@]}" 2>/dev/null || true
  sleep 2
fi

remaining=($(find_bot_pids))
if (( ${#remaining[@]} > 0 )); then
  kill -9 "${remaining[@]}" 2>/dev/null || true
  sleep 1
fi

echo "Starting bot.py ..."
touch "$LOG"
start_line_count="$(wc -l < "$LOG" | tr -d ' ')"
launchctl bootstrap "$DOMAIN" "$RUNTIME_PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

for _ in {1..45}; do
  if tail -n +"$((start_line_count + 1))" "$LOG" | grep -q "Application started"; then
    pid="$(find_bot_pids | head -n 1 || true)"
    if [[ -n "$pid" ]]; then
      echo "$pid" > "$PID_FILE"
      echo "Bot started. PID: $pid"
    else
      echo "Bot reported started. PID lookup did not return a process."
    fi
    echo "Log: $LOG"
    exit 0
  fi

  sleep 1
done

echo "Bot did not confirm startup within 45 seconds." >&2
echo "Log: $LOG"
tail -n 120 "$LOG" || true
exit 1
