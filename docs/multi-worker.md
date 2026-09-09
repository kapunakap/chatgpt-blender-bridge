# Multi-worker Blender sessions

Issue #4 adds a worker manager and MCP session router for concurrent Blender work without sharing one mutable interactive scene.

The existing verified single-user path on `127.0.0.1:9876` remains supported. Multi-worker mode uses additional loopback-only ports and the **same native Blender Lab MCP socket protocol** that the normal `blender-mcp` stdio server already uses.

## Architecture

```text
ChatGPT / MCP clients
        |
        v
OpenAI Secure MCP Tunnel / tunnel-client
        |
        v
scripts/blender-worker-mcp.py
(one stdio session -> one worker lease)
        |
        +--> worker-1 127.0.0.1:9970 -> native Blender Lab MCP -> Blender process
        +--> worker-2 127.0.0.1:9971 -> native Blender Lab MCP -> Blender process
        +--> worker-3 127.0.0.1:9972 -> native Blender Lab MCP -> Blender process

Existing single-user path stays separate:
ChatGPT -> tunnel-client -> blender-mcp -> 127.0.0.1:9876 -> interactive Blender
```

Background workers are launched with Blender Lab MCP's native background command:

```bash
blender --background --online-mode \
  --command blender_mcp \
  --host 127.0.0.1 \
  --port 9970
```

An optional GUI worker uses the installed Blender Lab MCP add-on in a normal Blender process and moves its listener to the assigned worker port.

## Security boundary

Worker endpoints are intentionally bound to `127.0.0.1` only. They are not public services and must not be port-forwarded or bound to `0.0.0.0`.

Blender Lab MCP's local socket is trusted-localhost infrastructure; it does not add a separate authentication layer per worker. The public/cloud boundary remains the existing OpenAI Secure MCP Tunnel + local `tunnel-client` model.

The manager stores state and worker-session locks in a private runtime directory (`0700` where supported). Source-file locks are also local OS locks.

## Start or reuse a pool

The default runtime directory is:

```text
~/.cache/chatgpt-blender-bridge/workers
```

Start three background workers:

```bash
python3 scripts/blender-workers.py start --count 3 --base-port 9970
```

Start one GUI worker plus two background workers:

```bash
python3 scripts/blender-workers.py start \
  --count 3 \
  --gui-count 1 \
  --base-port 9970
```

Starting the same pool again reuses healthy workers. Port `9876` is reserved by the manager so multi-worker mode cannot accidentally consume the verified single-user endpoint.

## Route a real MCP session

`scripts/blender-worker-mcp.py` is a stdio MCP routing wrapper. It acquires an exclusive worker lease for the entire MCP session, sets `BLENDER_MCP_HOST` / `BLENDER_MCP_PORT`, and launches the installed `blender-mcp --transport stdio` server.

Route automatically to the first healthy free worker:

```bash
BLENDER_MCP_COMMAND="$HOME/.local/share/blender-mcp/v1.0.0-venv/bin/blender-mcp" \
python3 scripts/blender-worker-mcp.py --worker auto
```

Pin the session to one worker:

```bash
python3 scripts/blender-worker-mcp.py --worker worker-2
```

The wrapper can also ensure the pool exists before accepting the stdio session **when its parent process is allowed to launch Blender**:

```bash
python3 scripts/blender-worker-mcp.py \
  --ensure-count 3 \
  --base-port 9970 \
  --worker auto
```

On macOS, do not use `--ensure-count` from the Secure MCP Tunnel / `tunnel-client` process. Blender 5.2 can abort during AppKit application registration or Metal initialization when launched from that process coalition. Start or reuse the pool from an interactive GUI login session first, then configure the tunnel wrapper without `--ensure-count`; the tunnel should only lease and route already-running workers.

Two concurrent `auto` stdio sessions cannot acquire the same worker lease. A direct manager job uses the same lease mechanism, so automation jobs and ChatGPT sessions also cannot mutate one Blender process concurrently.

Environment equivalents are available for tunnel/service configuration:

```text
CHATGPT_BLENDER_WORKER_RUNTIME
BLENDER_BIN
BLENDER_MCP_COMMAND
BLENDER_WORKER_ID
BLENDER_WORKER_COUNT
BLENDER_WORKER_GUI_COUNT
BLENDER_WORKER_BASE_PORT
```

See `config/tunnel-client-multi-worker.yaml.example` for a sanitized tunnel command pattern.

## Observe health and occupancy

```bash
python3 scripts/blender-workers.py status
```

Status reports:

- worker ID, PID, mode, host, and port,
- protocol (`blender-lab-mcp-socket`),
- process and endpoint health,
- `busy` session/job lease state,
- current blend path/object summary when the worker is idle,
- log path.

A busy worker is considered healthy from its verified process identity without injecting a diagnostic Python request into a job that is currently running.

## Route a file job

A manager job is a Blender Python script plus a source `.blend`. Before execution, the manager creates:

```text
<runtime>/jobs/<job-id>/source.blend
<runtime>/jobs/<job-id>/result.blend
```

Example:

```bash
python3 scripts/blender-workers.py run \
  --worker worker-2 \
  --source assets/source/city.blend \
  --script scripts/jobs/build-city.py \
  --job-id city-20260907
```

The job script receives:

- `bpy`
- `Path`
- `JOB_ID`
- `JOB_DIR`
- `RESULT_PATH`
- `WORKER_ID`
- `WORKER_PORT`

Normal jobs never overwrite the source file. Different source files/jobs can run on different workers in parallel.

## Exclusive source publishing

Publishing back to the mutable source is explicit:

```bash
python3 scripts/blender-workers.py run \
  --worker worker-1 \
  --source assets/source/city.blend \
  --script scripts/jobs/update-city.py \
  --job-id city-publish \
  --write-source
```

`--write-source` holds an OS-level exclusive lock for the canonical source path for the full job and atomic publish. A second writer is rejected with exit code `73` and error code `source_busy`.

Source locks are user-global by default:

```text
~/.cache/chatgpt-blender-bridge/source-locks
```

That prevents separate worker runtime directories from bypassing the same-file write rule. Override with `CHATGPT_BLENDER_SOURCE_LOCK_DIR` only when all cooperating processes use the same alternate lock root.

## Crash recovery

Worker state is persisted in `<runtime>/workers.json`. A free worker is healthy only when its recorded PID exists and its native Blender Lab MCP endpoint returns the same PID. A leased/busy worker is checked by process identity without sending an extra request into the active job.

Inspect and restart one worker without touching the others:

```bash
python3 scripts/blender-workers.py status
python3 scripts/blender-workers.py restart --worker worker-2
```

A deliberate hard-failure test is available:

```bash
python3 scripts/blender-workers.py kill --worker worker-2
python3 scripts/blender-workers.py status
python3 scripts/blender-workers.py restart --worker worker-2
```

Stop all managed workers:

```bash
python3 scripts/blender-workers.py stop
```

Before signaling a persisted PID, the manager verifies its process command still matches the expected Blender worker. This avoids killing an unrelated process after PID reuse.

## Real acceptance

Run:

```bash
python3 scripts/multi-worker-acceptance.py
```

This is a **real Blender** gate. It proves:

- three distinct Blender processes on three distinct native Blender Lab MCP loopback endpoints,
- two concurrent real `blender-mcp` stdio sessions routed to different worker PIDs,
- two independent modelling jobs overlap without state leakage,
- a third worker exports a real GLB while other workers are busy,
- normal jobs operate on per-job copies and leave source hashes unchanged,
- concurrent write access to the same source is rejected,
- health/occupancy is observable,
- a hard-killed worker restarts without changing unrelated worker PIDs,
- managed ports do not consume legacy port `9876`.

The optional GUI-worker path is also directly testable with `--gui-count 1`. The existing single-user ChatGPT acceptance remains separate and should still pass through the live `@Blender` plugin.
