#!/usr/bin/env bash
# Start everything a live interview needs, with one command: an ngrok tunnel to the API (Vapi
# has to reach it from the internet), the API itself, and the web app.
#
#   scripts/dev.sh            tunnel, API and web app: open http://localhost:5173 in Chrome
#   scripts/dev.sh --no-web   tunnel and API only: what scripts/smoke_call.py runs against
#
# Needs a filled-in .env (see .env.example), ngrok signed in to your account, uv and npm.
# Ctrl-C stops everything it started. It prints setting names and addresses only, never a value
# from .env: the ngrok address is the one thing it shows, and that is not a secret.
#
# This file is plain bash 3.2 (the one macOS ships): no `wait -n`, no associative arrays.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

API_PORT=8000
WEB_PORT=5173
NGROK_PORT=4040 # ngrok's local API, where the tunnel's address is read from
REQUIRED=(ANTHROPIC_API_KEY VAPI_API_KEY BRAIN_SECRET WEBHOOK_SECRET OWNER_KEY VISITOR_SALT)

fail() {
  echo "dev.sh: $1" >&2
  exit 1
}

with_web=1
for arg in "$@"; do
  case "$arg" in
    --no-web) with_web=0 ;;
    -h | --help)
      echo "usage: scripts/dev.sh [--no-web]"
      exit 0
      ;;
    *)
      echo "dev.sh: unknown option $arg (the only one is --no-web)" >&2
      exit 2
      ;;
  esac
done

# --- the settings ---------------------------------------------------------------------------

# A setting's value: from the environment if it has one, as for the server's own settings, else
# from .env. An empty value counts as not set. Only ever used to ask "is there one?", never
# printed.
setting() {
  local name="$1" value="${!1:-}"
  if [[ -z "$value" ]]; then
    value="$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?${name}[[:space:]]*=[[:space:]]*//p" .env | tail -n 1)"
    value="${value%"${value##*[![:space:]]}"}" # no trailing spaces
    case "$value" in
      \"*\")
        value="${value#\"}"
        value="${value%\"}"
        ;;
      \'*\')
        value="${value#\'}"
        value="${value%\'}"
        ;;
    esac
  fi
  printf '%s' "$value"
}

[[ -f .env ]] || fail ".env is missing. Copy .env.example to .env and fill it in."

missing=()
for name in "${REQUIRED[@]}"; do
  [[ -n "$(setting "$name")" ]] || missing+=("$name")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  fail "these settings are missing or empty in .env: ${missing[*]}"
fi

for command in ngrok uv curl; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is not installed or not on your PATH."
done
if [[ $with_web == 1 ]]; then
  command -v npm >/dev/null 2>&1 || fail "npm is not installed or not on your PATH."
  [[ -d web/node_modules ]] || fail "web/node_modules is missing. Run: npm --prefix web ci"
fi

# --- the ports ------------------------------------------------------------------------------

port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
  fi
}

busy=0
check_port() { # check_port PORT WHAT
  if port_in_use "$1"; then
    echo "dev.sh: port $1 ($2) is already in use. Stop what is using it and run this again." >&2
    busy=1
  fi
}
check_port "$API_PORT" "the API"
check_port "$NGROK_PORT" "ngrok's local page"
# 5173 is the web app's, so it only matters when the web app is going to start.
if [[ $with_web == 1 ]]; then
  check_port "$WEB_PORT" "the web app"
fi
[[ $busy == 0 ]] || exit 1

# --- starting, and stopping ------------------------------------------------------------------

pids=()
names=()
track() { # track NAME PID: remember a child so the trap can stop it
  names+=("$1")
  pids+=("$2")
}

descendants() { # every child, grandchild and so on of a pid (npm and uv start their own)
  local child
  for child in $(pgrep -P "$1" 2>/dev/null || true); do
    descendants "$child"
    echo "$child"
  done
}

cleanup() {
  trap - EXIT INT TERM
  local all="" pid waited alive
  for pid in ${pids[@]+"${pids[@]}"}; do
    all="$all $(descendants "$pid") $pid"
  done
  [[ -n "${all// /}" ]] || return 0
  # TERM first: it lets the API hang up a call that is still running, so Vapi stops billing.
  kill $all 2>/dev/null || true
  for _ in $(seq 1 50); do # up to 10 s
    alive=0
    for pid in $all; do
      ! kill -0 "$pid" 2>/dev/null || alive=1
    done
    [[ $alive == 0 ]] && break
    sleep 0.2
  done
  for pid in $all; do
    ! kill -0 "$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --- ngrok, then the API, then the web app ------------------------------------------------

echo "==> Starting the ngrok tunnel to port $API_PORT"
ngrok http "$API_PORT" >/dev/null 2>&1 &
ngrok_pid=$!
track ngrok "$ngrok_pid"

public_url=""
for _ in $(seq 1 40); do # 20 s
  kill -0 "$ngrok_pid" 2>/dev/null ||
    fail "ngrok stopped. Run 'ngrok http $API_PORT' by hand to see why (often it is not signed in)."
  public_url="$(curl -fsS --max-time 2 "http://127.0.0.1:$NGROK_PORT/api/tunnels" 2>/dev/null |
    grep -o '"public_url": *"https://[^"]*"' | head -n 1 | cut -d'"' -f4 || true)"
  [[ -z "$public_url" ]] || break
  sleep 0.5
done
[[ -n "$public_url" ]] || fail "ngrok gave no https address within 20 s."
# Vapi calls the brain and the webhook on this address. It overrides PUBLIC_BASE_URL in .env.
export PUBLIC_BASE_URL="$public_url"

echo "==> Starting the API on port $API_PORT"
# --no-access-log: the access log would record the secret URL paths (the brain and webhook).
uv run uvicorn mockingbird.app:app --factory --port "$API_PORT" --no-access-log &
api_pid=$!
track api "$api_pid"

ready=0
for _ in $(seq 1 60); do # 30 s
  kill -0 "$api_pid" 2>/dev/null || fail "the API stopped before it was ready. Its output is above."
  health="$(curl -fsS --max-time 2 "http://127.0.0.1:$API_PORT/healthz" 2>/dev/null || true)"
  if [[ "$health" == *'"ok":true'* ]]; then
    ready=1
    break
  fi
  sleep 0.5
done
[[ $ready == 1 ]] || fail "the API did not answer /healthz within 30 s."

echo "PUBLIC_BASE_URL: $PUBLIC_BASE_URL"
if [[ -n "$(setting DATABASE_URL)" ]]; then
  echo "Store: Postgres (DATABASE_URL is set)"
else
  echo "Store: in memory (DATABASE_URL is empty), so interviews are lost when the API stops"
fi

if [[ $with_web == 1 ]]; then
  echo "==> Starting the web app"
  npm --prefix web run dev &
  track web "$!"
  echo "Open http://localhost:$WEB_PORT in Chrome. Use headphones."
else
  echo "Web app not started (--no-web). In another shell: uv run python scripts/smoke_call.py"
fi
echo "Press Ctrl-C to stop everything."

# Stop if any child dies, so a half-running stack is never left behind.
while true; do
  for i in "${!pids[@]}"; do
    kill -0 "${pids[$i]}" 2>/dev/null || fail "${names[$i]} stopped, so everything is being stopped."
  done
  sleep 1
done
