# Troubleshooting

Use the architecture layers instead of changing multiple components at once.

## Start here

```bash
./scripts/doctor.sh
```

Then identify the first failing boundary below.

## Blender is not running

Symptoms:

- doctor cannot find a Blender process,
- the MCP port is closed.

Action:

- start Blender,
- enable/start the MCP add-on/server,
- rerun the doctor.

## Blender runs but the MCP port is closed

Symptoms:

- Blender process is present,
- TCP connection to `BLENDER_MCP_HOST:BLENDER_MCP_PORT` fails.

Check:

- MCP add-on is enabled,
- MCP server was explicitly started if required,
- host/port match `.env`,
- another process is not using the configured port.

## Local Blender socket works but ChatGPT returns 502

This was the first real regression fixed by this repository.

### Verified failure signature

The failing machine had all of these at the same time:

- Blender itself was healthy and owned `127.0.0.1:9876`.
- A direct local Blender MCP socket probe succeeded.
- The Blender MCP stdio server could initialize, list tools, and execute a read-only tool locally.
- The ChatGPT Personal plugin was installed/enabled but real tool calls returned upstream HTTP 502.
- The active Blender tunnel was an old unmanaged long-lived tunnel-client/stdin-stdout session rather than a persistent user service.
- Tunnel logs correlated ChatGPT requests with `client_internal` 502 responses and no upstream MCP response.

This localized the failure to the tunnel-runtime/stdio layer rather than Blender or the scene.

### Verified repair

1. Keep Blender and the add-on unchanged if their direct local checks are green.
2. Start a fresh tunnel-client using the same verified remote tunnel identity and the same Blender MCP stdio target.
3. Confirm `/healthz` is `live` and `/readyz` is `ready`.
4. Confirm a real ChatGPT `@Blender` call succeeds through the fresh client.
5. Install that profile under a user LaunchAgent with `RunAtLoad` + `KeepAlive`.
6. Retire the stale unmanaged tunnel process.
7. Repeat the ChatGPT live scene read and harmless Python acceptance with only the persistent service left.

No Blender reinstall, add-on reinstall, tunnel-client source patch, or tunnel-client binary upgrade was required for the verified incident.

## Multiple matching tunnel-client processes

The doctor warns if more than one process matches `TUNNEL_PROFILE_NAME`. Redundant tunnel clients are supported by the tunnel protocol, so multiple processes are not automatically wrong; however, an unexpected extra copy can hide a stale runtime and make diagnosis confusing.

For a normal single-Mac Blender setup, prefer one persistent LaunchAgent-managed client. If you intentionally run redundant clients, document that choice and correlate requests by client logs before treating the warning as a failure.

## LaunchAgent not running

When `TUNNEL_LAUNCHD_LABEL` is configured, the doctor checks the user service.

Inspect it with:

```bash
launchctl print "gui/$(id -u)/${TUNNEL_LAUNCHD_LABEL}"
```

Expected properties for the verified pattern:

- `state = running`
- `keepalive`
- `runatload`

If the service is missing, install/bootstrap the rendered `config/launchd.plist.example` pattern from [`installation.md`](installation.md).

## Tunnel process exists but health/readiness is bad

A process existing is weaker evidence than a healthy service.

If `TUNNEL_HEALTH_URL_FILE` is configured, read the local base URL from that file and check:

```bash
curl -fsS "${HEALTH_BASE}/healthz"
curl -fsS "${HEALTH_BASE}/readyz"
```

Expected bodies are `live` and `ready`.

If the health URL file points at an old dead port, treat it as stale runtime metadata and inspect the service/profile that owns the file.

## TCP works but local MCP acceptance fails

Run:

```bash
bash scripts/acceptance-test.sh
```

The acceptance test exercises the stdio MCP server rather than only the socket. If it fails after the TCP check passes, investigate the MCP executable/venv, MCP initialization compatibility, and Blender-MCP bridge protocol before touching the cloud tunnel.

## Tool discovery succeeds but execution fails

Treat discovery and invocation as separate gates. Tool schemas being visible does not prove that an invocation can traverse the tunnel and reach Blender.

Capture sanitized evidence for:

- discovery result,
- invocation error,
- tunnel health,
- local MCP health,
- time correlation between the attempted call and local logs.

## Port conflict

To inspect the configured example port on macOS:

```bash
lsof -nP -iTCP:${BLENDER_MCP_PORT:-9876} -sTCP:LISTEN
```

Confirm the listener belongs to Blender.

## Before filing a public issue

Include:

- macOS version,
- Blender version,
- Blender MCP upstream project/version,
- tunnel-client version,
- sanitized doctor output,
- exact error text with secrets removed,
- which acceptance step failed.

Never include raw credentials, tokens, private URLs, authorization codes, real tunnel IDs, or full unredacted configs/logs.
