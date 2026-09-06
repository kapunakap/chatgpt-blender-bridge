# Architecture

## End-to-end path

```text
ChatGPT
  ↓
ChatGPT plugin / custom MCP app
  ↓
OpenAI Secure MCP Tunnel
  ↓
tunnel-client on the user's Mac
  ↓
localhost Blender MCP endpoint
  ↓
Blender MCP add-on
  ↓
Blender Python API / scene
```

OpenAI's current documentation states that ChatGPT does not connect directly to a local MCP server; Secure MCP Tunnel is the supported bridge for a private/local MCP server without exposing it directly to the public internet.

Reference: https://help.openai.com/en/articles/12584461

## Trust boundaries

### 1. ChatGPT ↔ Secure MCP Tunnel

Cloud-side connection and plugin/app configuration. A failure here may appear as connection expiry, authorization errors, discovery failures, or gateway errors.

### 2. Secure MCP Tunnel ↔ tunnel-client

The tunnel client must be authenticated, running, and connected to the correct local target. Public diagnostics must never disclose tunnel credentials.

### 3. tunnel-client ↔ Blender MCP

The local target should normally be loopback-only. The bridge should not require exposing Blender MCP directly to the public internet.

### 4. Blender MCP ↔ Blender

The add-on/server must be installed, enabled, running, and able to execute only the capabilities the user intends to expose.

## Failure isolation strategy

Diagnose from the inside out:

1. Is Blender running?
2. Is the Blender MCP server listening locally?
3. Can the local host reach the MCP port?
4. Is `tunnel-client` running and healthy?
5. Can the tunnel reach the local MCP target?
6. Can ChatGPT discover the MCP tools?
7. Can ChatGPT execute a real tool call?

A successful check at one layer does not prove later layers are healthy.

## Acceptance philosophy

The final acceptance test must be performed through ChatGPT itself. Local TCP checks, process checks, and config validation are diagnostics—not end-to-end proof.

The preferred harmless proof is:

1. read live scene metadata,
2. perform a reversible or ephemeral Python operation,
3. read back its result,
4. clean it up,
5. confirm the user's scene is unchanged.

## Current known regression

The first tracked end-to-end failure is Issue #1: ChatGPT receives a 502 when invoking the Blender plugin. The root cause is intentionally not guessed in this document; once verified, it should be added to `docs/troubleshooting.md` and, where possible, detected by `scripts/doctor.sh`.

## Isolated multi-worker control plane

Issue #4 adds a session router in front of multiple **native Blender Lab MCP** endpoints:

```text
ChatGPT / MCP client
  ↓
tunnel-client
  ↓ stdio
blender-worker-mcp.py
  ↓ one exclusive session lease
  ├─ blender-mcp -> 127.0.0.1:9970 -> Blender Lab MCP -> worker-1
  ├─ blender-mcp -> 127.0.0.1:9971 -> Blender Lab MCP -> worker-2
  └─ blender-mcp -> 127.0.0.1:9972 -> Blender Lab MCP -> worker-3
```

Background workers use Blender Lab MCP's built-in `blender --background --online-mode --command blender_mcp --host ... --port ...` path. The session router does not invent a second Blender protocol: it sets `BLENDER_MCP_HOST` and `BLENDER_MCP_PORT` for the normal `blender-mcp` stdio server.

One OS file lease is held for each routed MCP session or direct manager job, preventing two callers from using the same Blender process at the same time. Per-job working copies and a separate user-global source-file lock prevent accidental concurrent writes to the same canonical `.blend` file.

All managed Blender endpoints bind to loopback only. The existing single-user `127.0.0.1:9876` endpoint is reserved and remains supported.

See [`multi-worker.md`](multi-worker.md) for lifecycle, routing, locking, health/restart, and real-Blender acceptance.
