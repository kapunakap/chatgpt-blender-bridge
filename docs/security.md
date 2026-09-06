# Security Model

## Principle

The bridge exists so ChatGPT can reach a local MCP server **without directly publishing that local server to the public internet**. Keep the Blender MCP listener on loopback whenever the upstream server supports it.

OpenAI's current MCP guidance recommends Secure MCP Tunnel for local/private MCP servers rather than exposing them directly:
https://help.openai.com/en/articles/12584461

## Threats to consider

### Credential leakage

Tunnel credentials can grant access to the bridge. Never commit them, print them from helper scripts, or paste them into public issues.

### Over-broad Blender capabilities

A Blender MCP server may expose Python execution or other powerful actions. Treat it as code execution on your workstation. Enable only software you trust and understand.

### Prompt injection / untrusted content

Custom MCP integrations can increase exposure to prompt-injection and unsafe-action risks. Avoid feeding untrusted instructions into workflows that have write or execution capabilities without reviewing the action.

### Public diagnostics

Logs, screenshots, shell history, process command lines, and launchd files may accidentally reveal tokens or private URLs. Sanitize before sharing.

## Repository rules

Tracked examples use placeholders only.

Local secret-bearing files should use ignored paths such as:

```text
.env
config/tunnel-client.local.yaml
```

Helper scripts must not use shell tracing (`set -x`) around secret-bearing commands.

## Least privilege

- keep Blender MCP bound to loopback,
- run the tunnel client as the user rather than root unless there is a demonstrated requirement,
- avoid unrelated filesystem/network permissions,
- keep destructive acceptance operations out of the default test path.

## Acceptance-test safety

The end-to-end test should use a reversible/ephemeral Blender change, verify it, and remove it. Do not use the user's production scene contents as a test fixture.

A failed cleanup step must be reported explicitly.
