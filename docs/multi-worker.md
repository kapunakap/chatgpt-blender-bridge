# Multi-worker Blender sessions

Issue #4 adds a local worker manager for isolated concurrent Blender jobs. It is designed to sit **behind** the existing private/tunnel model and does not replace the verified single-user Blender Lab MCP endpoint on `127.0.0.1:9876`.

## Architecture

```text
ChatGPT / automation jobs
        |
        v
scripts/blender-workers.py
(worker manager / router)
        |
        +--> 127.0.0.1:9970 -> worker-1 -> Blender process -> job copy A
        +--> 127.0.0.1:9971 -> worker-2 -> Blender process -> job copy B
        +--> 127.0.0.1:9972 -> worker-3 -> Blender process -> job copy C

Existing interactive path remains separate:
ChatGPT -> Secure MCP Tunnel -> Blender Lab MCP -> 127.0.0.1:9876 -> GUI Blender
```

Each managed worker is a real persistent Blender process. Background workers run with `blender --background --factory-startup`. An optional GUI worker can be requested for manual inspection while the remaining workers stay headless.

The managed worker protocol is an internal control plane, not a public MCP service. Every worker:

- binds only to `127.0.0.1`,
- has a distinct TCP port,
- has a random authentication token stored in a mode-`0600` file,
- stores state/logs under a private mode-`0700` runtime directory,
- executes Blender Python only after the manager authenticates the request.

Do not expose worker ports through a public listener or port-forward them to another network.

## Start a pool

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
python3 scripts/blender-workers.py start --count 3 --gui-count 1 --base-port 9970
```

Port `9876` is reserved by the manager so the existing single-user Blender Lab MCP workflow cannot be accidentally replaced.

Use another private runtime when multiple independent manager pools are required:

```bash
python3 scripts/blender-workers.py \
  --runtime "$HOME/.cache/chatgpt-blender-bridge/pool-b" \
  start --count 3 --base-port 9980
```

## Observe health

```bash
python3 scripts/blender-workers.py status
```

Status is JSON and includes worker ID, PID, mode, host, port, process state, endpoint health, active `.blend` path, object count, and log path.

A healthy pool can be started again with the same count/base port and reused. To recover a failed or stale worker without touching unrelated workers:

```bash
python3 scripts/blender-workers.py restart --worker worker-2
```

For a deliberate hard-failure test:

```bash
python3 scripts/blender-workers.py kill --worker worker-2
python3 scripts/blender-workers.py status
python3 scripts/blender-workers.py restart --worker worker-2
```

Stop the managed pool:

```bash
python3 scripts/blender-workers.py stop
```

## Route a job

A job is a Blender Python script plus a source `.blend` file. The worker manager always creates a private working copy first:

```text
<runtime>/jobs/<job-id>/source.blend
<runtime>/jobs/<job-id>/result.blend
```

Run a modelling/export/render script on a specific worker:

```bash
python3 scripts/blender-workers.py run \
  --worker worker-2 \
  --source assets/source/city.blend \
  --script scripts/jobs/build-city.py \
  --job-id city-20260907
```

Inside the job script these globals are provided:

- `bpy`
- `JOB_ID`
- `JOB_DIR`
- `RESULT_PATH`
- `WORKER_ID`
- `WORKER_PORT`
- `Path`

The default job never overwrites the source file. This is the preferred mode for agent work because jobs can safely operate on independent copies in parallel.

## Source write ownership

Publishing a result back to the mutable source is explicit:

```bash
python3 scripts/blender-workers.py run \
  --worker worker-1 \
  --source assets/source/city.blend \
  --script scripts/jobs/update-city.py \
  --job-id city-publish \
  --write-source
```

`--write-source` holds an OS-level exclusive lock for the canonical source path for the complete job and atomic publish. Source locks live in a user-global private directory (`~/.cache/chatgpt-blender-bridge/source-locks` by default), so separate worker pools cannot bypass one another by using different runtime directories. A second writer to the same source is rejected immediately with exit code `73` and JSON error code `source_busy`. Set `CHATGPT_BLENDER_SOURCE_LOCK_DIR` only when you intentionally need a different shared lock root.

Different source files can be written concurrently. Read/copy jobs do not need the source write lock because they mutate only their per-job copies.

## Crash and stale-worker model

Worker state is persisted in `<runtime>/workers.json`. A worker is healthy only when both are true:

1. its PID still exists, and
2. its authenticated loopback endpoint answers `ping`.

`status` makes stale/crashed workers visible. `restart --worker <id>` stops any surviving stale process, preserves the worker's port/mode assignment, creates a fresh token, and starts a replacement Blender process. Unrelated workers are not restarted.

## Real acceptance

Run the macOS real-Blender gate:

```bash
python3 scripts/multi-worker-acceptance.py
```

It uses the installed Blender executable and proves all of the following with real processes/files rather than mocks:

- three distinct Blender PIDs on three distinct loopback ports,
- two independent jobs overlap in time without object/state leakage,
- a third worker exports a real GLB while other workers are busy,
- source files remain byte-identical for normal per-job-copy work,
- a concurrent second source writer is rejected,
- worker health is observable,
- a hard-killed worker restarts on the same endpoint while the other worker PIDs remain unchanged,
- managed worker ports do not consume the legacy single-user port `9876`.

The final existing single-user ChatGPT/Blender plugin acceptance remains separate: use `scripts/acceptance-test.sh` and the live `@Blender` path as documented in `docs/verified-acceptance.md`.
