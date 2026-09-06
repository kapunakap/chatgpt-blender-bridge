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

## Local MCP port works but ChatGPT fails

A successful local port test proves only that something is listening locally. Continue outward:

1. verify the `tunnel-client` process/service is running,
2. inspect sanitized tunnel health logs,
3. verify the tunnel targets the same local host/port,
4. verify ChatGPT can discover the expected MCP tools,
5. execute a real tool call through ChatGPT.

Do not paste unredacted tunnel logs into a public issue.

## HTTP 502 from ChatGPT

Tracked by Issue #1.

A 502 is an end-to-end symptom, not a root-cause diagnosis. Possible failure boundaries include the cloud tunnel, tunnel-client, local target routing, transport/protocol mismatch, or the MCP server itself.

Do **not** close Issue #1 after merely making local checks green. Closure requires the real ChatGPT-side acceptance criteria in the issue.

Once the current root cause is proven, add the exact signature and fix here.

## Tool discovery succeeds but execution fails

Treat discovery and invocation as separate gates. Tool schemas being visible does not prove that an invocation can traverse the tunnel and reach Blender.

Capture sanitized evidence for:

- discovery result,
- invocation error,
- tunnel health,
- local MCP health,
- time correlation between the attempted call and local logs.

## Connection/reconnect loops

Test from a normal ChatGPT chat as well as any specialized mode you were using, because product surfaces can differ in custom MCP support. If the same plugin behaves differently between surfaces, record that as a separate boundary rather than changing the local Blender setup blindly.

## Port conflict

To inspect the configured example port on macOS:

```bash
lsof -nP -iTCP:${BLENDER_MCP_PORT:-9876} -sTCP:LISTEN
```

Confirm the listener belongs to the process you expect.

## Before filing a public issue

Include:

- macOS version,
- Blender version,
- Blender MCP upstream project/version,
- tunnel-client version,
- sanitized doctor output,
- exact error text with secrets removed,
- which acceptance step failed.

Never include raw credentials, tokens, private URLs, authorization codes, or full unredacted configs.
