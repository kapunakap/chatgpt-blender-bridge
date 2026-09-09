#!/usr/bin/env python3
"""Bind one stdio Blender MCP session to one isolated, project-aware managed worker."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blender_bridge.mcp_proxy import WorkerMcpProxy, run_stdio_proxy  # noqa: E402
from blender_bridge.project_routing import metadata_from_environment  # noqa: E402
from blender_bridge.workers import WorkerBusyError, WorkerError, WorkerManager  # noqa: E402


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Route one Blender MCP stdio session to an isolated project-aware worker"
    )
    parser.add_argument("--runtime", default=os.environ.get("CHATGPT_BLENDER_WORKER_RUNTIME"))
    parser.add_argument("--blender-bin", default=os.environ.get("BLENDER_BIN"))
    parser.add_argument("--worker", default=os.environ.get("BLENDER_WORKER_ID", "auto"))
    parser.add_argument("--mcp-command", default=os.environ.get("BLENDER_MCP_COMMAND"))
    parser.add_argument(
        "--ensure-count",
        type=int,
        default=int(os.environ.get("BLENDER_WORKER_COUNT", "0")),
        help="start/reuse this many workers before routing the MCP session",
    )
    parser.add_argument(
        "--gui-count",
        type=int,
        default=int(os.environ.get("BLENDER_WORKER_GUI_COUNT", "0")),
    )
    parser.add_argument(
        "--base-port",
        type=int,
        default=int(os.environ.get("BLENDER_WORKER_BASE_PORT", "9970")),
    )
    parser.add_argument("--project", default=os.environ.get("BLENDER_PROJECT"))
    parser.add_argument("--repo", default=os.environ.get("BLENDER_PROJECT_REPO"))
    parser.add_argument("--branch", default=os.environ.get("BLENDER_PROJECT_BRANCH"))
    parser.add_argument("--worktree", default=os.environ.get("BLENDER_PROJECT_WORKTREE"))
    parser.add_argument("--blend-path", default=os.environ.get("BLENDER_PROJECT_BLEND_PATH"))
    parser.add_argument(
        "--open-blend",
        action="store_true",
        default=_env_bool("BLENDER_PROJECT_OPEN_BLEND"),
        help="open the configured blend path after resolving startup project affinity",
    )
    args = parser.parse_args()

    startup_metadata = metadata_from_environment()
    for key, value in {
        "project": args.project,
        "repo": args.repo,
        "branch": args.branch,
        "worktree": args.worktree,
        "blend_path": args.blend_path,
    }.items():
        if value:
            startup_metadata[key] = value

    try:
        manager = WorkerManager(runtime_dir=args.runtime, blender_bin=args.blender_bin)
        if args.ensure_count:
            manager.start(
                count=args.ensure_count,
                gui_count=args.gui_count,
                base_port=args.base_port,
            )
        proxy = WorkerMcpProxy(
            manager,
            mcp_command=args.mcp_command,
            worker_id=args.worker,
            startup_metadata=startup_metadata or None,
            startup_open_blend=args.open_blend,
        )
        return run_stdio_proxy(proxy)
    except (WorkerBusyError, WorkerError, OSError) as exc:
        print(f"blender-worker-mcp: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
