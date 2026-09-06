#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 1

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

BLENDER_MCP_HOST="${BLENDER_MCP_HOST:-127.0.0.1}"
BLENDER_MCP_PORT="${BLENDER_MCP_PORT:-9876}"
BLENDER_MCP_COMMAND="${BLENDER_MCP_COMMAND:-}"
BLENDER_PROCESS_PATTERN="${BLENDER_PROCESS_PATTERN:-Blender}"
TUNNEL_PROCESS_PATTERN="${TUNNEL_PROCESS_PATTERN:-tunnel-client}"
TUNNEL_PROFILE_NAME="${TUNNEL_PROFILE_NAME:-blender-mcp}"
TUNNEL_LAUNCHD_LABEL="${TUNNEL_LAUNCHD_LABEL:-}"
TUNNEL_HEALTH_URL_FILE="${TUNNEL_HEALTH_URL_FILE:-}"

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

os_name="$(uname -s)"
if [[ "$os_name" == "Darwin" ]]; then
  pass "macOS detected"
else
  warn "initial supported path is macOS; detected $os_name"
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

if [[ -n "$BLENDER_MCP_COMMAND" ]]; then
  if [[ -x "$BLENDER_MCP_COMMAND" ]]; then
    pass "Blender MCP stdio command is executable"
  else
    warn "configured Blender MCP command is not executable: $BLENDER_MCP_COMMAND"
  fi
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

tunnel_matches="$(pgrep -fl -- "$TUNNEL_PROCESS_PATTERN" 2>/dev/null || true)"
if [[ -n "$TUNNEL_PROFILE_NAME" ]]; then
  profile_matches="$(printf '%s\n' "$tunnel_matches" | grep -F -- "$TUNNEL_PROFILE_NAME" || true)"
else
  profile_matches="$tunnel_matches"
fi
profile_count="$(printf '%s\n' "$profile_matches" | awk 'NF {count++} END {print count+0}')"

if (( profile_count == 0 )); then
  warn "no tunnel-client process matched profile name '$TUNNEL_PROFILE_NAME'"
elif (( profile_count == 1 )); then
  pass "exactly one tunnel-client process matches profile '$TUNNEL_PROFILE_NAME'"
else
  warn "multiple tunnel-client processes ($profile_count) match profile '$TUNNEL_PROFILE_NAME'; check for a stale competing runtime"
fi

if [[ "$os_name" == "Darwin" && -n "$TUNNEL_LAUNCHD_LABEL" ]]; then
  launchd_status="$(launchctl print "gui/$(id -u)/$TUNNEL_LAUNCHD_LABEL" 2>/dev/null || true)"
  if [[ -z "$launchd_status" ]]; then
    warn "LaunchAgent is not loaded: $TUNNEL_LAUNCHD_LABEL"
  else
    if printf '%s\n' "$launchd_status" | grep -q 'state = running'; then
      pass "LaunchAgent is running ($TUNNEL_LAUNCHD_LABEL)"
    else
      warn "LaunchAgent is loaded but not reported running ($TUNNEL_LAUNCHD_LABEL)"
    fi
    if printf '%s\n' "$launchd_status" | grep -q 'keepalive' && printf '%s\n' "$launchd_status" | grep -q 'runatload'; then
      pass "LaunchAgent has KeepAlive and RunAtLoad"
    else
      warn "LaunchAgent does not show both KeepAlive and RunAtLoad"
    fi
  fi
fi

if [[ -n "$TUNNEL_HEALTH_URL_FILE" ]]; then
  if [[ -r "$TUNNEL_HEALTH_URL_FILE" ]]; then
    health_base="$(tr -d '\r\n' < "$TUNNEL_HEALTH_URL_FILE")"
    case "$health_base" in
      http://127.0.0.1:*|http://localhost:*)
        pass "tunnel health URL file points to loopback"
        ;;
      *)
        warn "tunnel health URL is not an expected loopback HTTP address"
        ;;
    esac

    if command -v curl >/dev/null 2>&1; then
      health_body="$(curl --fail --silent --show-error --max-time 2 "$health_base/healthz" 2>/dev/null || true)"
      ready_body="$(curl --fail --silent --show-error --max-time 2 "$health_base/readyz" 2>/dev/null || true)"
      if [[ "$health_body" == "live" ]]; then
        pass "tunnel /healthz is live"
      else
        warn "tunnel /healthz did not return 'live'"
      fi
      if [[ "$ready_body" == "ready" ]]; then
        pass "tunnel /readyz is ready"
      else
        warn "tunnel /readyz did not return 'ready'"
      fi
    else
      warn "curl is unavailable; skipped tunnel health/readiness HTTP checks"
    fi
  else
    warn "tunnel health URL file is not readable: $TUNNEL_HEALTH_URL_FILE"
  fi
fi

printf '\nSummary: %d passed, %d warning(s), %d failure(s)\n' "$passes" "$warnings" "$failures"

if (( failures > 0 )); then
  printf 'RESULT: FAILED LOCAL CHECKS\n'
  exit 1
fi

printf 'RESULT: LOCAL CHECKS PASS\n'
printf 'Note: this does not prove the ChatGPT → Secure MCP Tunnel → Blender path.\n'
printf 'Run scripts/acceptance-test.sh, then the real @Blender ChatGPT acceptance.\n'
