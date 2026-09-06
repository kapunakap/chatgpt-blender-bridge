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

Issue #4 adds a separate local worker-manager path for concurrent modelling, export, render, and validation jobs:

```text
worker manager / router
  ├─ 127.0.0.1:9970 -> persistent Blender worker
  ├─ 127.0.0.1:9971 -> persistent Blender worker
  └─ 127.0.0.1:9972 -> persistent Blender worker
```

This control plane is intentionally separate from the verified single-user Blender Lab MCP endpoint on `127.0.0.1:9876`. Managed worker ports are loopback-only and authenticated with per-worker random tokens kept in the private runtime directory.

Jobs route explicitly by worker ID. Each job opens a private working copy and writes a separate `result.blend`. Publishing a result back to a mutable source requires an OS-level exclusive source lock, so two workers cannot concurrently write the same canonical `.blend` file.

See [`multi-worker.md`](multi-worker.md) for lifecycle, routing, file isolation, health/restart behavior, and the real-Blender acceptance gate.
