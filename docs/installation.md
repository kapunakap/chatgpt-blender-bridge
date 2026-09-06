# Installation

This guide documents the macOS pattern proven end to end on 2026-09-06.

## Verified versions

- Blender `5.2.1 LTS`
- Blender Lab MCP `v1.0.0`
- tunnel-client `0.0.13+4b5267f823be0b046bb883aacb51603cfde3a0ea`

The examples are sanitized. They intentionally do not contain a real tunnel ID, runtime key, private endpoint, auth code, or machine-specific path.

## 1. Install Blender

Install Blender from its official distribution channel and start it once.

## 2. Install Blender Lab MCP

Install the upstream Blender Lab MCP add-on/server. The verified setup used the add-on on:

```text
127.0.0.1:9876
```

Keep the add-on bound to loopback. Do not expose the Blender MCP socket publicly.

The verified v1.0.0 MCP server executable was launched with:

```text
blender-mcp --transport stdio
```

and received `BLENDER_MCP_HOST=127.0.0.1` plus `BLENDER_MCP_PORT=9876` in its environment.

## 3. Prepare local script configuration

```bash
cp config/env.example .env
```

Edit `.env` locally if your MCP installation path, port, LaunchAgent label, or health URL file differs. `.env` is ignored by Git.

## 4. Create/attach the Secure MCP Tunnel

Use OpenAI's Secure MCP Tunnel setup flow for your account/workspace. This repository does not create or publish your credentials.

After the setup flow gives you a tunnel identity and runtime credential, keep the runtime key in a local file with restrictive permissions:

```bash
chmod 600 /path/to/blender-mcp-runtime-key
```

Never paste the key or real tunnel ID into this public repository.

## 5. Render the tunnel-client profile

Start from:

```text
config/tunnel-client.yaml.example
```

Replace these tokens locally:

- `__TUNNEL_ID__`
- `__RUNTIME_KEY_FILE__`
- `__HEALTH_URL_FILE__`
- `__LOG_FILE__`
- `__BLENDER_MCP_COMMAND__`

A normal durable destination is:

```text
~/.config/tunnel-client/blender-mcp.yaml
```

Set the private rendered profile to mode `0600`.

Before creating a background service, validate the profile using the tunnel-client doctor/status commands available in your installed version, then start it once and confirm both health endpoints are good.

## 6. Install the persistent user LaunchAgent

The 502 repair was made durable by running the Blender tunnel as a macOS user LaunchAgent with `RunAtLoad=true` and `KeepAlive=true`.

Start from:

```text
config/launchd.plist.example
```

Replace:

- `__HOME__`
- `__PROFILE_DIR__`
- `__LAUNCHD_LOG_DIR__`

The profile directory should contain `blender-mcp.yaml` and the LaunchAgent passes `--profile blender-mcp`.

Validate the rendered plist before installing it:

```bash
plutil -lint /path/to/rendered.plist
```

Install it:

```bash
mkdir -p "$HOME/Library/LaunchAgents"
install -m 600 /path/to/rendered.plist \
  "$HOME/Library/LaunchAgents/com.example.chatgpt-blender-bridge.plist"
```

Load it for the logged-in user:

```bash
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.example.chatgpt-blender-bridge.plist"
```

If it is already loaded and you intentionally changed its config, use a controlled restart rather than starting a second unmanaged copy.

Verify:

```bash
launchctl print "gui/$(id -u)/com.example.chatgpt-blender-bridge"
```

The verified service reported `state = running`, `keepalive`, and `runatload`.

## 7. Verify tunnel health/readiness

The tunnel profile writes a local health base URL to its configured health URL file. Read that file locally, then verify:

```bash
curl -fsS "${HEALTH_BASE}/healthz"
curl -fsS "${HEALTH_BASE}/readyz"
```

The verified result was HTTP 200 with bodies `live` and `ready`.

## 8. Run local diagnostics

```bash
bash scripts/doctor.sh
```

The doctor checks the Blender listener, matching tunnel processes, LaunchAgent state when configured, and tunnel health/readiness.

## 9. Exercise the real local MCP boundary

```bash
bash scripts/acceptance-test.sh
```

This does more than test TCP. It launches the configured Blender MCP stdio server, performs MCP initialization/tool discovery, and executes the read-only `get_blendfile_summary_datablocks` tool against the running Blender instance.

## 10. Final ChatGPT acceptance

From a normal ChatGPT chat with the Blender Personal plugin enabled, prove:

1. `@Blender` connects without a 502.
2. A live scene read succeeds.
3. A harmless `execute_blender_code` call succeeds.
4. The result reports the same Blender version/running instance observed locally.
5. Object count/scene state remains unchanged.

The verified 2026-09-06 run passed all five. See [`verified-acceptance.md`](verified-acceptance.md).

## Avoid the first regression

Do not leave the Blender tunnel as a one-off unmanaged terminal process. A long-lived stale stdio/tunnel-client process was the failing boundary in the first 502 incident. Use the persistent user service, check `/healthz` and `/readyz`, and avoid accidental competing copies of the same Blender tunnel profile.
