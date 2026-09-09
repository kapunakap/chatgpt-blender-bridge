# Local Blender worker autoscaling and garbage collection

Issue #9 adds a **user-session worker supervisor** on top of the existing isolated
worker leases and project/worktree affinity routing.

The important macOS rule is unchanged: **the Secure MCP Tunnel / `tunnel-client`
process does not launch Blender**. `tunnel-client` only starts the stdio router.
The router asks a local Unix-socket supervisor for capacity, then takes the same
exclusive worker `flock` used by the static multi-worker implementation.

```text
ChatGPT session
  ↓
tunnel-client
  ↓ stdio
scripts/blender-worker-mcp.py
  ↓ local Unix socket: ensure_capacity
user LaunchAgent supervisor
  ↓ owns start/stop/reconcile/GC
managed Blender workers on 127.0.0.1:9970+
```

The original single-user Blender MCP endpoint on `127.0.0.1:9876` is never used
as managed capacity and is explicitly rejected if a managed port range would
overlap it.

## Configuration

Recommended starting point:

```bash
export CHATGPT_BLENDER_WORKER_RUNTIME="$HOME/.cache/chatgpt-blender-bridge/workers"
export BLENDER_WORKER_AUTOSCALE=1
export BLENDER_WORKER_MIN_COUNT=1
export BLENDER_WORKER_MAX_COUNT=5
export BLENDER_WORKER_IDLE_TIMEOUT_SECONDS=1200
export BLENDER_WORKER_GC_INTERVAL_SECONDS=300
export BLENDER_JOB_RETENTION_SECONDS=86400
export BLENDER_LOG_RETENTION_SECONDS=604800
export BLENDER_PROJECT_AFFINITY_STALE_SECONDS=86400
export BLENDER_WORKER_BASE_PORT=9970

# For interactive GUI workers on macOS:
export BLENDER_WORKER_GUI_COUNT=5
```

`BLENDER_WORKER_COUNT` / `--ensure-count` belongs to the older static-pool mode.
Do not set it when `BLENDER_WORKER_AUTOSCALE=1`; the router intentionally refuses
that combination so Blender cannot be spawned from the tunnel process coalition.

Optional controls:

- `BLENDER_WORKER_SUPERVISOR_SOCKET` — override the default
  `<runtime>/supervisor.sock`.
- `BLENDER_WORKER_SPAWN_TIMEOUT_SECONDS` — worker startup health deadline.
- `BLENDER_WORKER_SUPERVISOR_TIMEOUT_SECONDS` — router wait time for a supervisor
  response.

## Capacity behavior

For each new autoscaled stdio session:

1. The router asks the supervisor for a candidate.
2. The supervisor reconciles dead/unhealthy managed worker state.
3. It reuses a healthy, idle, unclaimed worker if one exists.
4. If none exists and live worker count is below `max_workers`, the supervisor
   grows the pool.
5. The router takes the existing exclusive worker lease.
6. If another session wins the lease race first, the router asks the supervisor
   again.
7. At the hard cap, the request fails explicitly with `capacity_busy`.

The supervisor serializes capacity decisions in one local process. Managed
endpoints remain loopback-only.

### Project affinity

Active project/worktree affinity records reserve their worker from unrelated
autoscaled sessions. If startup project metadata is already known, the router
passes it to the supervisor so the matching idle worker can be reused.

An affinity is considered stale only after
`BLENDER_PROJECT_AFFINITY_STALE_SECONDS`. A stale affinity can be evicted only
while its worker is not leased/busy. Fresh affinities protect idle workers from
scale-down.

## Scale-down

A worker becomes an idle-GC candidate only when all of the following are true:

- it is healthy,
- it is not leased/busy,
- it is not protected by a fresh project affinity,
- it has remained observed-idle for at least
  `BLENDER_WORKER_IDLE_TIMEOUT_SECONDS`,
- removing it would not take the pool below `BLENDER_WORKER_MIN_COUNT`.

Before termination the supervisor re-checks the current lease/health/affinity
state. It never asks `WorkerManager.stop_worker()` for a busy candidate.

Idle timestamps are persisted in `supervisor-state.json`. After a supervisor
restart, current busy workers clear any stale idle observation before scale-down.

## Reconciliation

Each control request and periodic GC cycle can reconcile managed state:

- dead process records are removed,
- alive but unhealthy and unleased workers are restarted,
- `min_workers` is restored when necessary,
- a busy worker is never restarted merely to change GUI/background mode.

Project routing can then recover an affinity onto healthy unclaimed capacity
using the existing Issue #7 rules.

## Garbage collection

GC is deliberately conservative.

### Completed job directories

`jobs/*` is eligible only when:

- no managed worker is currently leased/busy, and
- the job has an old `job.json` marker with `status=completed|failed`, **or**
  contains an old `result.blend`.

A directory containing only `source.blend` is treated as active/incomplete and
is not removed. GC never follows symlinked job directories and never deletes a
source `.blend` outside the private worker runtime.

### Session lease files

An old lease file is removed only when its worker no longer exists and the file
can be locked non-blockingly. Lease files for known workers are retained.

### Logs

Old logs are deleted only if their path is not the current log for any managed
worker.

### Project affinities

A route older than `BLENDER_PROJECT_AFFINITY_STALE_SECONDS` can be removed only
when its recorded worker is not busy.

## macOS LaunchAgent

Use the sanitized template:

```bash
cp config/blender-worker-supervisor.launchd.plist.example \
  "$HOME/Library/LaunchAgents/com.example.chatgpt-blender-worker-supervisor.plist"
```

Replace every `__PLACEHOLDER__` with local absolute paths. In particular:

- `__PYTHON_BIN__`
- `__BRIDGE_DIR__`
- `__HOME__`
- `__WORKER_RUNTIME__`
- `__BLENDER_BIN__`
- `__BLENDER_MCP_COMMAND__`
- `__LAUNCHD_LOG_DIR__`

Then load it in the **GUI user domain**:

```bash
launchctl bootout "gui/$(id -u)/com.example.chatgpt-blender-worker-supervisor" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.example.chatgpt-blender-worker-supervisor.plist"
launchctl kickstart -k "gui/$(id -u)/com.example.chatgpt-blender-worker-supervisor"
```

Check it:

```bash
python3 scripts/blender-worker-supervisor.py status
```

The LaunchAgent owns Blender process lifecycle and survives individual ChatGPT
or tunnel stdio disconnects.

## Tunnel configuration

The ChatGPT-facing command only enables autoscale routing:

```text
BLENDER_WORKER_AUTOSCALE=1 scripts/blender-worker-mcp.py --worker auto
```

Do not add `--ensure-count` to the tunnel command on macOS.

See `config/tunnel-client-multi-worker.yaml.example`.

## Real acceptance

Run from an interactive macOS login session:

```bash
python3 scripts/autoscale-acceptance.py
```

The harness uses a temporary managed runtime and a non-legacy base port. It
checks:

- five concurrent isolated worker leases,
- explicit rejection of a sixth request at `max_workers=5`,
- project-affinity reuse,
- dead worker reconciliation,
- bounded job/log/lease/affinity GC,
- idle scale-down back to `min_workers=1`,
- `127.0.0.1:9876` listener state before and after.

It cleans up only the temporary managed worker runtime it created.
