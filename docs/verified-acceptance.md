# Verified end-to-end acceptance — 2026-09-06

This file records sanitized evidence from the first successful real ChatGPT → Blender repair.

No tunnel IDs, runtime keys, workspace/org IDs, auth URLs/codes, private config contents, process command secrets, or user-specific absolute paths are included.

## Stack

- Blender: `5.2.1 LTS`
- Blender Lab MCP: `v1.0.0`
- tunnel-client: `0.0.13+4b5267f823be0b046bb883aacb51603cfde3a0ea`
- Blender MCP socket: loopback `127.0.0.1:9876`
- tunnel transport to Blender MCP: stdio
- macOS persistence: user LaunchAgent, `RunAtLoad=true`, `KeepAlive=true`

## Failure before repair

The real ChatGPT Personal plugin repeatedly returned upstream HTTP 502.

Independent local checks proved Blender was not the failing component:

- Blender owned the loopback MCP socket.
- A direct null-byte-delimited Blender add-on `execute` request returned `status=ok`.
- Blender reported version `5.2.1 LTS` and an unsaved scene.
- The Blender MCP stdio server initialized successfully, exposed 26 tools, and executed a read-only Blender summary tool.

The failure boundary was therefore between the Secure MCP Tunnel client runtime and its long-lived stdio child. The active Blender tunnel was an old unmanaged runtime with repeated `client_internal` 502s and no MCP upstream response. There was no persistent Blender LaunchAgent protecting the setup from stale-session/restart problems.

## Repair

- Reused the existing verified remote tunnel identity; no credentials were rotated or published.
- Started a fresh tunnel-client with the same Blender MCP stdio target.
- Confirmed the fresh client's `/healthz` and `/readyz` endpoints returned HTTP 200 (`live` / `ready`).
- Confirmed the fresh client handled real ChatGPT control-plane commands successfully.
- Installed a user LaunchAgent with `RunAtLoad` + `KeepAlive`.
- Retired the stale unmanaged tunnel runtime and the temporary recovery runtime.
- Repeated acceptance with only the persistent LaunchAgent-managed tunnel left.

No Blender reinstall, add-on reinstall, tunnel-client source change, or tunnel-client binary upgrade was required.

## Final real ChatGPT proof

### Live Blender/scene read

A real `@Blender` `get_blendfile_summary_datablocks` call returned:

- scene: `Scene`
- object datablocks: `3`
- render engine: `BLENDER_EEVEE`
- active workspace: `Layout`

A separate `get_objects_summary` call reported the standard unsaved startup objects (`Camera`, `Cube`, `Light`).

### Harmless Python execution

A real `@Blender` `execute_blender_code` call returned a unique acceptance marker plus:

- Blender version: `5.2.1 LTS`
- scene: `Scene`
- object count: `3`
- active workspace: `Layout`
- filepath: empty (unsaved scene)

The locally observed running Blender process matched the instance that answered through the plugin; the ephemeral PID value is intentionally omitted here.

### Persistent service proof

After stale/recovery processes were removed:

- the user LaunchAgent remained `state = running`,
- `RunAtLoad` and `KeepAlive` were active,
- `/healthz` returned HTTP 200 `live`,
- `/readyz` returned HTTP 200 `ready`,
- the LaunchAgent tunnel log showed real ChatGPT MCP commands being forwarded,
- zero new internal 502s were observed on the persistent client.

### Scene preservation

The acceptance used read-only queries plus Python that only constructed a JSON result. No objects were added, deleted, moved, renamed, or saved. Object count remained `3`, scene remained `Scene`, and filepath remained empty.

## Acceptance result

- [x] `@Blender` connects without 502.
- [x] Live scene information is retrieved.
- [x] Harmless Blender Python executes through the real plugin.
- [x] Response is tied to the currently running Blender instance.
- [x] Scene remains unchanged.
- [x] Tunnel persistence is installed and healthy.

**END-TO-END STATUS: PASS**
