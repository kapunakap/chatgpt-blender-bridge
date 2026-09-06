# Configuration

Only sanitized examples belong in this directory.

## Verified examples

The 2026-09-06 end-to-end repair produced two public-safe templates:

- `tunnel-client.yaml.example` — the verified single-user tunnel-client profile structure for a stdio Blender MCP server.
- `tunnel-client-multi-worker.yaml.example` — the Issue #4 router pattern that leases a native Blender MCP worker per stdio session.
- `launchd.plist.example` — the verified macOS user LaunchAgent pattern with `RunAtLoad` and `KeepAlive`.

`env.example` configures the local diagnostic/acceptance scripts.

## Placeholder rules

The tracked templates use obvious tokens such as:

- `__TUNNEL_ID__`
- `__RUNTIME_KEY_FILE__`
- `__BLENDER_MCP_COMMAND__`
- `__BLENDER_BIN__`
- `__BLENDER_WORKER_MCP_COMMAND__`
- `__WORKER_RUNTIME__`
- `__HEALTH_URL_FILE__`
- `__LOG_FILE__`
- `__HOME__`
- `__PROFILE_DIR__`
- `__LAUNCHD_LOG_DIR__`

They are **not** shell-expanded automatically inside YAML/plist files. Render or replace them locally before use.

Never commit the rendered private profile, tunnel ID/key material, private URLs, auth codes, or raw logs.

## Recommended local locations

A clean macOS installation can use:

```text
~/.config/tunnel-client/blender-mcp.yaml
~/.config/tunnel-client/keys/blender-mcp-runtime-key
~/Library/LaunchAgents/com.example.chatgpt-blender-bridge.plist
~/Library/Application Support/tunnel-client/blender-mcp.health.url
~/Library/Logs/chatgpt-blender-bridge/
```

Use restrictive permissions (`0600`) for the tunnel profile, key file, and installed LaunchAgent.

## Verification rule

Before calling a configuration working:

1. Blender must own the loopback MCP listener.
2. The local stdio MCP acceptance must initialize, discover tools, and execute a read-only Blender tool.
3. tunnel-client `/healthz` and `/readyz` must be healthy.
4. The real ChatGPT `@Blender` path must complete a live scene read and harmless Python operation without a 502.
