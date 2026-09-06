# ChatGPT Blender Bridge

A reproducible, security-conscious reference setup for connecting ChatGPT to a local Blender instance through an MCP bridge.

> **Status:** early public work in progress. The first end-to-end regression is tracked in [Issue #1](https://github.com/kapunakap/chatgpt-blender-bridge/issues/1). Do not assume the bridge is working until the real ChatGPT acceptance test passes.

## What this repository is for

This project documents and automates the path:

```text
┌───────────┐
│  ChatGPT  │
└─────┬─────┘
      │ Personal plugin / custom MCP app
      ▼
┌─────────────────────────┐
│ OpenAI Secure MCP Tunnel│
└────────────┬────────────┘
             │
             ▼
┌─────────────────┐
│  tunnel-client  │
│    user's Mac   │
└────────┬────────┘
         │ localhost
         ▼
┌─────────────────┐
│   Blender MCP   │
│     add-on      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│     Blender     │
└─────────────────┘
```

The goal is not merely to document settings. The goal is to make the setup **reproducible, diagnosable, and testable**.

## Scope

The initial supported target is **macOS**. Other operating systems should only be documented after they are genuinely tested.

This repository intentionally does **not** contain:

- tunnel secrets or credentials,
- raw private tunnel configuration,
- ChatGPT authentication material,
- user-specific machine identifiers,
- arbitrary `.blend` project files,
- third-party Blender add-ons unless their license explicitly permits redistribution.

## Quick start

The exact installation flow is still being hardened. The intended workflow is:

1. Install Blender.
2. Install and enable a compatible Blender MCP add-on.
3. Install/configure `tunnel-client` through OpenAI Secure MCP Tunnel.
4. Copy `config/env.example` to `.env` and set the local Blender MCP target.
5. Start Blender and its MCP endpoint.
6. Run `bash scripts/doctor.sh`.
7. Run `bash scripts/acceptance-test.sh` for the locally testable portion.
8. Perform the final acceptance test from ChatGPT through the real Blender plugin.

See [`docs/installation.md`](docs/installation.md) before attempting setup.

## Definition of working

Configuration files existing is **not** success. A working installation must satisfy all of these:

- [ ] ChatGPT connects to the Blender plugin without a 502.
- [ ] ChatGPT retrieves live scene information from the running Blender instance.
- [ ] ChatGPT executes a harmless Blender Python operation through the plugin.
- [ ] The returned state proves the response came from that running Blender instance.
- [ ] The acceptance test leaves the scene unchanged.

## Diagnostics

Run:

```bash
bash scripts/doctor.sh
```

The doctor is intentionally conservative. It checks local prerequisites and connectivity without printing secrets. A green local doctor does **not** replace the final ChatGPT-side acceptance test.

## Local acceptance test

Run:

```bash
bash scripts/acceptance-test.sh
```

This checks the local Blender MCP boundary that can be safely validated from the machine. The final ChatGPT → tunnel → Blender proof must still be executed from ChatGPT itself.

## Configuration

Copy the local target example instead of editing tracked files with machine-specific values:

```bash
cp config/env.example .env
```

`.env` is ignored by Git.

A runnable tunnel-client YAML is **not published yet**. The current schema/version will be added only after Issue #1 is fixed and the configuration is proven end to end. See [`config/README.md`](config/README.md).

## Security

This repository is public. Read [`SECURITY.md`](SECURITY.md) and [`docs/security.md`](docs/security.md) before sharing logs, configs, screenshots, or issue comments.

If you accidentally expose a credential, rotate/revoke it first, then clean up the public artifact. Removing it from the latest commit alone is not sufficient.

## Troubleshooting

See [`docs/troubleshooting.md`](docs/troubleshooting.md). The current 502 regression is tracked in [Issue #1](https://github.com/kapunakap/chatgpt-blender-bridge/issues/1), and its eventual root cause will be turned into a doctor check and regression note where practical.

## Project maturity

The first milestone is **v0.1 — reproducible macOS setup**. It should not be tagged until a real ChatGPT-side acceptance test succeeds end to end.

## License

MIT. See [`LICENSE`](LICENSE).
