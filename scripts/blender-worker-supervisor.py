#!/usr/bin/env python3
"""Run or query the local user-session Blender worker autoscaling supervisor."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blender_bridge.supervisor import SupervisorClient, SupervisorConfig, WorkerSupervisor  # noqa: E402
from blender_bridge.workers import WorkerError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Blender worker autoscaling supervisor")
    parser.add_argument("--runtime", default=os.environ.get("CHATGPT_BLENDER_WORKER_RUNTIME"))
    parser.add_argument("--socket", default=os.environ.get("BLENDER_WORKER_SUPERVISOR_SOCKET"))
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "status", "reconcile"),
        default="run",
    )
    args = parser.parse_args()

    try:
        config = SupervisorConfig.from_env(runtime_dir=args.runtime)
        if args.socket:
            config = SupervisorConfig(
                **{
                    **config.__dict__,
                    "control_socket": Path(args.socket).expanduser().resolve(),
                }
            )
        if args.command == "run":
            WorkerSupervisor(config).serve_forever()
            return 0

        client = SupervisorClient(config.socket_path)
        result = client.status() if args.command == "status" else client.request("reconcile")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (WorkerError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
