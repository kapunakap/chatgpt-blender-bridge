#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 1

if [[ -f .env ]]; then
  # Local file is user-controlled and intentionally ignored by Git.
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

BLENDER_MCP_HOST="${BLENDER_MCP_HOST:-127.0.0.1}"
BLENDER_MCP_PORT="${BLENDER_MCP_PORT:-9876}"
BLENDER_PROCESS_PATTERN="${BLENDER_PROCESS_PATTERN:-Blender}"
TUNNEL_PROCESS_PATTERN="${TUNNEL_PROCESS_PATTERN:-tunnel-client}"

passes=0
warnings=0
failures=0

pass() {
  printf '✓ %s\n' "$1"
  passes=$((passes + 1))
}

warn() {
  printf '! %s\n' "$1"
  warnings=$((warnings + 1))
}

fail() {
  printf '✗ %s\n' "$1"
  failures=$((failures + 1))
}

printf 'ChatGPT Blender Bridge Doctor\n'
printf '=============================\n\n'

if [[ "$(uname -s)" == "Darwin" ]]; then
  pass "macOS detected"
else
  warn "initial supported path is macOS; detected $(uname -s)"
fi

if [[ -d "/Applications/Blender.app" ]] || command -v blender >/dev/null 2>&1; then
  pass "Blender installation found"
else
  warn "Blender application was not found in /Applications or PATH"
fi

if pgrep -fi -- "$BLENDER_PROCESS_PATTERN" >/dev/null 2>&1; then
  pass "Blender process appears to be running"
else
  fail "Blender process not found (pattern: $BLENDER_PROCESS_PATTERN)"
fi

if pgrep -fi -- "$TUNNEL_PROCESS_PATTERN" >/dev/null 2>&1; then
  pass "tunnel-client process appears to be running"
else
  warn "tunnel-client process not found (pattern: $TUNNEL_PROCESS_PATTERN)"
fi

if [[ "$BLENDER_MCP_HOST" != "127.0.0.1" && "$BLENDER_MCP_HOST" != "localhost" && "$BLENDER_MCP_HOST" != "::1" ]]; then
  warn "Blender MCP host is not loopback: $BLENDER_MCP_HOST"
else
  pass "Blender MCP target is loopback-only ($BLENDER_MCP_HOST)"
fi

if command -v nc >/dev/null 2>&1; then
  if nc -z -w 2 "$BLENDER_MCP_HOST" "$BLENDER_MCP_PORT" >/dev/null 2>&1; then
    pass "TCP connection to Blender MCP target succeeds ($BLENDER_MCP_HOST:$BLENDER_MCP_PORT)"
  else
    fail "cannot connect to Blender MCP target ($BLENDER_MCP_HOST:$BLENDER_MCP_PORT)"
  fi
elif command -v python3 >/dev/null 2>&1; then
  if python3 - "$BLENDER_MCP_HOST" "$BLENDER_MCP_PORT" <<'PY' >/dev/null 2>&1
import socket
import sys
host = sys.argv[1]
port = int(sys.argv[2])
with socket.create_connection((host, port), timeout=2):
    pass
PY
  then
    pass "TCP connection to Blender MCP target succeeds ($BLENDER_MCP_HOST:$BLENDER_MCP_PORT)"
  else
    fail "cannot connect to Blender MCP target ($BLENDER_MCP_HOST:$BLENDER_MCP_PORT)"
  fi
else
  warn "neither nc nor python3 is available; skipped TCP connectivity test"
fi

printf '\nSummary: %d passed, %d warning(s), %d failure(s)\n' "$passes" "$warnings" "$failures"

if (( failures > 0 )); then
  printf 'RESULT: FAILED LOCAL CHECKS\n'
  exit 1
fi

printf 'RESULT: LOCAL CHECKS PASS\n'
printf 'Note: this does not prove the ChatGPT → Secure MCP Tunnel → Blender path.\n'
printf 'Run the real ChatGPT plugin acceptance test before declaring end-to-end success.\n'
