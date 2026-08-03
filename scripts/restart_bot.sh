#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON="$PROJECT_DIR/.venv/bin/python"
BOT="$PROJECT_DIR/bot.py"
LOG="$PROJECT_DIR/bot.log"
PID_FILE="$PROJECT_DIR/bot.pid"
LABEL="com.myu.ai-telegram-bot"
PLIST_TEMPLATE="$PROJECT_DIR/scripts/$LABEL.plist"
RUNTIME_PLIST="/tmp/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

cd "$PROJECT_DIR"

echo "Stopping existing bot.py processes for $PROJECT_DIR ..."
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true

# Dynamically generate runtime plist with current PROJECT_DIR
sed "s|/Users/myu/github/ai_telegram|$PROJECT_DIR|g" "$PLIST_TEMPLATE" > "$RUNTIME_PLIST"

pids=()
while IFS= read -r line; do
  pid="$(awk '{print $2}' <<< "$line")"
  command="$line"
  if [[ "$command" == *"$BOT"* || "$command" == *" $PROJECT_DIR/bot.py"* || "$command" == *" bot.py"* ]]; then
    pids+=("$pid")
  fi
done < <(ps auxww)

if (( ${#pids[@]} > 0 )); then
  kill "${pids[@]}"
  sleep 2
fi

remaining=()
while IFS= read -r line; do
  pid="$(awk '{print $2}' <<< "$line")"
  command="$line"
  if [[ "$command" == *"$BOT"* || "$command" == *" $PROJECT_DIR/bot.py"* || "$command" == *" bot.py"* ]]; then
    remaining+=("$pid")
  fi
done < <(ps auxww)

if (( ${#remaining[@]} > 0 )); then
  kill -TERM "${remaining[@]}"
  sleep 1
fi

echo "Starting bot.py ..."
touch "$LOG"
start_line_count="$(wc -l < "$LOG" | tr -d ' ')"
launchctl bootstrap "$DOMAIN" "$RUNTIME_PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

for _ in {1..45}; do
  if tail -n +"$((start_line_count + 1))" "$LOG" | grep -q "Application started"; then
    pid="$(pgrep -f "$BOT" | head -n 1 || true)"
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

echo "Bot is still running, but startup confirmation was not seen within 45 seconds."
echo "Log: $LOG"
tail -n 120 "$LOG" || true
