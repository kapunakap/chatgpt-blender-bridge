# Automatic project-aware Blender routing

Issue #7 extends the isolated worker pool so normal ChatGPT usage does not require choosing worker IDs, MCP ports, or one permanent plugin profile per feature.

## Recommended architecture

Keep one ChatGPT-facing Blender integration and point it at the project-aware wrapper:

```text
ChatGPT thread / MCP session
          |
          v
scripts/blender-worker-mcp.py --worker auto
          |
          +--> project_attach(...) [once when project metadata is known]
          |
          v
persistent project affinity router
          |
          +--> worker-1 127.0.0.1:9970 -> Blender A
          +--> worker-2 127.0.0.1:9971 -> Blender B
          +--> worker-3 127.0.0.1:9972 -> Blender C
```

The user does not need to know `worker-1`, `9970`, or any other internal routing detail.

## Start a GUI worker pool

For three interactive Blender windows:

```bash
python3 scripts/blender-workers.py start \
  --count 3 \
  --gui-count 3 \
  --base-port 9970

python3 scripts/blender-workers.py status
```

The existing single-user endpoint on `127.0.0.1:9876` remains reserved and separate.

For tunnel integration, keep using one command with automatic worker selection:

```bash
python3 scripts/blender-worker-mcp.py \
  --ensure-count 3 \
  --gui-count 3 \
  --base-port 9970 \
  --worker auto
```

## Session behavior

Every wrapper process represents one MCP stdio session.

At session startup it leases one healthy free worker and holds that exclusive lease until the stdio session closes. This preserves scene, selection, mode, undo, save, export, and render isolation even before project metadata is attached.

The proxy exposes three additional MCP tools alongside the normal Blender MCP tools:

- `project_attach`
- `project_status`
- `project_detach`

### `project_attach`

Call this once when the agent has trustworthy project/worktree metadata:

```text
project_attach(
  project="Kapelica",
  repo="kapunakap/gta-labin",
  branch="feat/kapelica-district",
  worktree="/Users/onin/dev/gta-labin/.worktrees/kapelica-district-20260908",
  blend_path="assets/source/city.blend"
)
```

`blend_path` is optional. A relative `blend_path` is only accepted when `worktree` is present, and is canonicalized underneath that worktree. Traversal (including an existing symlink that resolves outside the worktree) is rejected. That means two Git worktrees may use the same repository-relative path without becoming the same physical source file.

`open_blend=true` is optional and explicitly opens the resolved file after routing. It is false by default so attaching metadata alone does not mutate Blender file state.

### Affinity rules

Project identity is chosen in this order:

1. canonical worktree path,
2. repository + branch,
3. repository + project name,
4. repository,
5. project name.

The persistent affinity state lives in the private worker runtime:

```text
~/.cache/chatgpt-blender-bridge/workers/projects.json
```

with a separate lock file. The runtime remains local-only and should stay user-private.

Routing behavior is fail-closed:

- First attachment keeps the session's already-leased worker only when that worker is not reserved by another project affinity. Otherwise the router selects another healthy, free, unclaimed worker and transparently rebinds.
- A healthy worker already claimed by one project is never silently claimed by a second project. If the pool has no safe unclaimed capacity, attachment fails explicitly instead of sharing Blender state.
- A later session for the same project reuses the existing worker when that worker is healthy and free.
- If that project's worker is currently busy, the second session receives an explicit project-busy tool error instead of being silently routed to a different Blender state.
- If the recorded worker is missing or unhealthy, recovery chooses only a healthy worker that is not claimed by another project. If no such worker exists, the router fails closed; the worker manager may instead restart the project's original worker on its existing endpoint.
- If a live rebind races with another session and the target lease cannot be acquired, the proxy rolls the newly written affinity back when it is still the current route, then restores its previous worker where possible.

## Startup metadata

When trustworthy metadata is already known before the MCP session starts, it can be supplied through CLI flags:

```bash
python3 scripts/blender-worker-mcp.py \
  --worker auto \
  --project Kapelica \
  --repo kapunakap/gta-labin \
  --branch feat/kapelica-district \
  --worktree /absolute/path/to/worktree \
  --blend-path assets/source/city.blend
```

or equivalent environment variables:

```text
BLENDER_PROJECT
BLENDER_PROJECT_REPO
BLENDER_PROJECT_BRANCH
BLENDER_PROJECT_WORKTREE
BLENDER_PROJECT_BLEND_PATH
BLENDER_PROJECT_OPEN_BLEND
BLENDER_SESSION_ID
```

Do not statically set project metadata in a shared one-profile tunnel unless that tunnel is intentionally dedicated to one project. For the normal shared integration, leave project metadata unset and let the agent call `project_attach` once per logical feature session.

`BLENDER_SESSION_ID` is optional. If the tunnel/runtime can propagate a stable session identifier, provide it. Otherwise the wrapper uses its own process identity for diagnostics; routing correctness does not depend on ChatGPT UI thread IDs.

## One integration, many feature threads

Example concurrent use:

```text
Kapelica thread --project_attach--> worker-2 / Blender PID A
Raša thread     --project_attach--> worker-1 / Blender PID B
Plomin thread   --project_attach--> worker-3 / Blender PID C
```

All three threads use the same ChatGPT Blender integration and the same tunnel command. Worker and port selection are internal.

## Detach behavior

`project_detach()` clears project metadata from the current MCP session but intentionally preserves the persistent project-to-worker affinity. It does not release the worker lease early; the lease is released when the MCP stdio session ends.

This makes detach safe for diagnostics without making future sessions lose the project's preferred Blender instance.

## Real acceptance

Run the Issue #7 gate on a machine with the verified Blender Lab MCP stack:

```bash
python3 scripts/project-routing-acceptance.py
```

It proves:

- three real GUI Blender worker processes with distinct PIDs and loopback endpoints,
- three concurrent sessions through the same wrapper,
- automatic distinct-worker allocation,
- `project_attach` / `project_status` availability,
- repeated calls stay on the same Blender PID,
- scene marker, selection, and actual Blender mode state do not leak between projects,
- identical repository-relative `.blend` paths are saved as three distinct physical Blender files in separate worktrees,
- a later session reuses the project's worker affinity,
- a second session is rejected while that project's worker is actively leased,
- a killed project worker is restarted/recovered while an unrelated MCP session remains live and keeps the same PID/state.

Then rerun the original multi-worker gate:

```bash
python3 scripts/multi-worker-acceptance.py
```

That re-proves manual `--worker <id>` / `--worker auto` compatibility, native Blender Lab MCP isolation, source locking, per-job copies, export behavior, and preservation of the legacy `9876` path.

## Security notes

- Worker endpoints stay on `127.0.0.1` only.
- Project/worktree paths are local metadata and are not tunnel credentials.
- `projects.json` is runtime state, not a repository artifact; do not commit it.
- The router never derives project identity from ambiguous natural-language chat text. It uses explicit MCP arguments, CLI flags, or environment metadata.
- Same-source write safety for managed jobs remains enforced by the existing user-global source locks. Interactive Blender sessions should still use separate worktree-backed files when they may save directly.
