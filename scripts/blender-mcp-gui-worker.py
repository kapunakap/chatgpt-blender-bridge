#!/usr/bin/env python3
"""Start the installed Blender Lab MCP add-on on a dedicated GUI-worker port."""

from __future__ import annotations

import argparse
import bpy
import importlib
import json
import os
import sys


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"refusing non-loopback Blender MCP host: {args.host}")
    if not bpy.app.online_access:
        raise RuntimeError("Blender online access is required; launch the worker with --online-mode")

    addon_name = next(
        (name for name in bpy.context.preferences.addons.keys() if name == "mcp" or name.endswith(".mcp")),
        None,
    )
    if not addon_name:
        raise RuntimeError("Blender Lab MCP add-on is not enabled in this Blender profile")
    addon = importlib.import_module(addon_name)
    server = importlib.import_module(f"{addon_name}.mcp_to_blender_server")
    execute_interactive = importlib.import_module(f"{addon_name}.execute_interactive")

    autostart = getattr(addon, "_autostart_timer", None)
    if autostart is not None and bpy.app.timers.is_registered(autostart):
        bpy.app.timers.unregister(autostart)
    if server.is_running():
        server.stop()
    server.start(args.host, args.port)
    if not bpy.app.timers.is_registered(execute_interactive.run):
        bpy.app.timers.register(
            execute_interactive.run,
            first_interval=server.TIMER_INTERVAL_ACTIVE,
            persistent=True,
        )
    print(
        json.dumps(
            {
                "event": "native_gui_worker_ready",
                "worker_id": args.worker_id,
                "pid": os.getpid(),
                "host": args.host,
                "port": args.port,
                "addon": addon_name,
            },
            sort_keys=True,
        ),
        flush=True,
    )


main()
