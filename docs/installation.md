# Installation

> **Status:** this installation guide is being hardened against the live end-to-end regression in Issue #1. Steps that depend on the final verified tunnel/client invocation are intentionally conservative rather than speculative.

## Requirements

- macOS for the initial supported path,
- Blender,
- a compatible Blender MCP add-on/server,
- OpenAI Secure MCP Tunnel access,
- `tunnel-client`,
- a ChatGPT plugin/custom MCP app configured for the tunnel.

OpenAI reference for local MCP connectivity through Secure MCP Tunnel:
https://help.openai.com/en/articles/12584461

## 1. Install Blender

Install Blender from its official distribution channel and start it once.

Record the tested Blender version in `versions.env` when validating a release of this repository.

## 2. Install the Blender MCP add-on

Install the MCP add-on from its upstream project rather than copying an unknown local add-on into this repository.

After installation:

1. enable the add-on,
2. start its MCP server,
3. keep it bound to loopback/local access unless the upstream project explicitly requires something else,
4. note its local host and port.

The default example configuration in this repository uses `127.0.0.1:9876`, but treat that as a configurable example, not a universal Blender MCP standard.

## 3. Prepare local configuration

```bash
cp config/env.example .env
cp config/tunnel-client.yaml.example config/tunnel-client.local.yaml
```

Edit only the ignored local files.

Never place real credentials in the tracked example files.

## 4. Configure Secure MCP Tunnel / tunnel-client

Configure `tunnel-client` so the tunnel targets the Blender MCP endpoint on loopback.

The exact credential and client bootstrap flow is deliberately not duplicated here until it is verified against the current tunnel-client release. Follow the OpenAI-provided Secure MCP Tunnel setup flow available to your ChatGPT account/workspace, then map its local target to the values in `.env`.

Expected logical mapping:

```text
remote tunnel endpoint
        ↓
tunnel-client
        ↓
127.0.0.1:${BLENDER_MCP_PORT}
```

Do not expose Blender MCP directly on a public interface merely to make ChatGPT reach it.

## 5. Start Blender MCP

Start Blender and ensure the MCP endpoint is running.

Then run:

```bash
./scripts/doctor.sh
```

The local MCP port should pass before debugging the cloud/tunnel side.

## 6. Start the tunnel client

Start `tunnel-client` using the credentials/configuration produced by the Secure MCP Tunnel setup flow.

If you install it as a background service, prefer a user-level service with explicit logs and least privilege. A sanitized launchd template will be added only after the currently working command line is re-verified.

## 7. Local smoke test

Run:

```bash
./scripts/acceptance-test.sh
```

This verifies only the locally testable boundary. It intentionally does not claim to prove the ChatGPT cloud path.

## 8. Final ChatGPT acceptance

From a normal ChatGPT chat with the Blender plugin enabled, prove all of the following:

1. the plugin connects without a 502,
2. a live scene read succeeds,
3. a harmless Blender Python operation succeeds,
4. the result proves it came from the currently running Blender instance,
5. the test cleans up after itself and leaves the scene unchanged.

Only this final step makes the end-to-end setup `PASS`.

## Updating this guide

When Issue #1 is resolved, replace any provisional wording with the exact verified commands, upstream version pins, and sanitized examples used in the successful acceptance run.
