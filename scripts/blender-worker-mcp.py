#!/usr/bin/env python3
"""Bind one stdio Blender MCP session to one isolated managed worker."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blender_bridge.workers import WorkerBusyError, WorkerError, WorkerManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Route a Blender MCP stdio session to an isolated worker")
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
    args = parser.parse_args()
    try:
        manager = WorkerManager(runtime_dir=args.runtime, blender_bin=args.blender_bin)
        if args.ensure_count:
            manager.start(
                count=args.ensure_count,
                gui_count=args.gui_count,
                base_port=args.base_port,
            )
        return manager.run_mcp_session(worker_id=args.worker, mcp_command=args.mcp_command)
    except (WorkerBusyError, WorkerError) as exc:
        print(f"blender-worker-mcp: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
