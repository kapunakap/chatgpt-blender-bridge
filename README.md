# ChatGPT Blender Bridge

A reproducible, security-conscious reference setup for connecting ChatGPT to a local Blender instance through an MCP bridge.

> **Status:** verified working on macOS. On 2026-09-06 the full ChatGPT → OpenAI Secure MCP Tunnel → tunnel-client → Blender MCP → running Blender path passed live read and harmless Python execution acceptance with no 502.

## Verified stack

- Blender: **5.2.1 LTS**
- Blender Lab MCP: **v1.0.0**
- tunnel-client: **0.0.13+4b5267f823be0b046bb883aacb51603cfde3a0ea**
- Blender MCP target: **127.0.0.1:9876**

See [`versions.env`](versions.env) and [`docs/verified-acceptance.md`](docs/verified-acceptance.md).

## Architecture

```text
ChatGPT
  ↓ Personal plugin / custom MCP app
OpenAI Secure MCP Tunnel
  ↓
tunnel-client on macOS
  ↓ stdio MCP
Blender MCP server
  ↓ loopback JSON socket
Blender MCP add-on on 127.0.0.1:9876
  ↓
running Blender
```

The goal is not merely to document settings. The setup should be **reproducible, diagnosable, persistent, and testable**.

## Scope

The currently verified target is **macOS**. Other operating systems should only be documented after they are genuinely tested.

This repository intentionally does **not** contain:

- tunnel secrets or credentials,
- raw private tunnel configuration,
- ChatGPT authentication material,
- user-specific machine identifiers,
- arbitrary `.blend` project files,
- third-party Blender add-ons unless their license explicitly permits redistribution.

## Quick start

1. Install Blender and Blender Lab MCP.
2. Configure the Blender add-on on loopback (`127.0.0.1:9876`).
3. Install `tunnel-client` and create/attach the tunnel using OpenAI's Secure MCP Tunnel setup flow.
4. Copy `config/env.example` to `.env` and edit only your local values.
5. Render `config/tunnel-client.yaml.example` with your private tunnel ID/key-file path and local MCP executable path.
6. Install the sanitized LaunchAgent pattern from `config/launchd.plist.example` so the tunnel survives login/reboot and restarts after crashes.
7. Start Blender and its MCP endpoint.
8. Run `bash scripts/doctor.sh`.
9. Run `bash scripts/acceptance-test.sh` to exercise the real local stdio MCP boundary.
10. Run the final acceptance through `@Blender` in ChatGPT.

See [`docs/installation.md`](docs/installation.md) for the exact verified pattern and safety notes.

## Definition of working

The verified setup satisfies all of these:

- [x] ChatGPT connects to the Blender plugin without a 502.
- [x] ChatGPT retrieves live scene information from the running Blender instance.
- [x] ChatGPT executes a harmless Blender Python operation through the plugin.
- [x] The returned state proves the response came from that running Blender instance.
- [x] The acceptance test leaves the scene unchanged.
- [x] The tunnel runs under a user LaunchAgent with `RunAtLoad` and `KeepAlive`.
- [x] Tunnel `/healthz` and `/readyz` return HTTP 200.

## Diagnostics

Run:

```bash
bash scripts/doctor.sh
```

The doctor checks the Blender process/listener, the configured tunnel process/profile, the user LaunchAgent where configured, and tunnel health/readiness. It also warns when multiple matching tunnel clients are running, which is useful for detecting a stale competing runtime.

A green doctor still does not replace the final ChatGPT-side test.

## Local MCP acceptance

Run:

```bash
bash scripts/acceptance-test.sh
```

Unlike the original TCP-only gate, this launches the configured Blender MCP stdio server, performs MCP initialization/tool discovery, and executes the read-only `get_blendfile_summary_datablocks` tool against the running Blender instance.

## Isolated multi-worker sessions

For concurrent users/jobs, the worker manager starts independent Blender processes on distinct **native Blender Lab MCP** loopback endpoints. `scripts/blender-worker-mcp.py` leases one worker for each stdio MCP session, so concurrent ChatGPT sessions do not share Blender selection, mode, scene, undo, save, export, or render state.

```bash
python3 scripts/blender-workers.py start --count 3 --base-port 9970
python3 scripts/blender-workers.py status
python3 scripts/multi-worker-acceptance.py
```

For tunnel integration, point the local MCP command at the router wrapper instead of directly at `blender-mcp`. The wrapper can `--ensure-count 3` and route each session with `--worker auto`. The original interactive endpoint on `127.0.0.1:9876` remains unchanged.

See [`docs/multi-worker.md`](docs/multi-worker.md) and [`config/tunnel-client-multi-worker.yaml.example`](config/tunnel-client-multi-worker.yaml.example).

## Configuration

Copy the local target example:

```bash
cp config/env.example .env
```

`.env` is ignored by Git.

The repository also includes:

- [`config/tunnel-client.yaml.example`](config/tunnel-client.yaml.example) — sanitized structure from the verified tunnel profile.
- [`config/launchd.plist.example`](config/launchd.plist.example) — sanitized user LaunchAgent pattern from the verified persistent service.

The placeholders are intentionally not valid secrets. Replace them locally and never commit the rendered private files.

## Security

This repository is public. Read [`SECURITY.md`](SECURITY.md) and [`docs/security.md`](docs/security.md) before sharing logs, configs, screenshots, or issue comments.

If you accidentally expose a credential, rotate/revoke it first, then clean up the public artifact. Removing it from the latest commit alone is not sufficient.

## Troubleshooting

See [`docs/troubleshooting.md`](docs/troubleshooting.md). The first 502 regression and its stale-runtime signature are documented there as a regression case.

## License

MIT. See [`LICENSE`](LICENSE).
