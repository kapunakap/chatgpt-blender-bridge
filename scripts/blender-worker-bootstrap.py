#!/usr/bin/env python3
"""Persistent Blender worker endpoint used by blender_bridge.workers.

This file is executed by Blender, not regular CPython. Every worker owns one
Blender process and one loopback TCP endpoint. Requests are authenticated with
a random token stored in a mode-0600 file in the private runtime directory.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time
import traceback

import bpy


def _args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token-file", required=True)
    return parser.parse_args(argv)


def _require_loopback(host: str) -> None:
    candidate = "127.0.0.1" if host == "localhost" else host
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise RuntimeError(f"worker host must be an IP loopback address, got {host!r}") from exc
    if not address.is_loopback:
        raise RuntimeError(f"refusing non-loopback worker host: {host}")


def _read_request(conn: socket.socket) -> dict:
    chunks = []
    total = 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > 32 * 1024 * 1024:
            raise RuntimeError("request exceeded 32 MiB")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise RuntimeError("empty request")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("request must be a JSON object")
    return value


def _send(conn: socket.socket, value: dict) -> None:
    conn.sendall(json.dumps(value, sort_keys=True, default=str).encode("utf-8") + b"\n")


def _summary(worker_id: str, port: int) -> dict:
    active = bpy.context.view_layer.objects.active
    return {
        "worker_id": worker_id,
        "pid": os.getpid(),
        "port": port,
        "background": bool(bpy.app.background),
        "blend_file": bpy.data.filepath,
        "scene": bpy.context.scene.name if bpy.context.scene else None,
        "active_object": active.name if active else None,
        "object_names": sorted(obj.name for obj in bpy.data.objects),
        "object_count": len(bpy.data.objects),
    }


def _run_script(request: dict, worker_id: str, port: int) -> dict:
    blend_file = Path(str(request["blend_file"])).expanduser().resolve()
    result_path = Path(str(request["result_path"])).expanduser().resolve()
    job_dir = Path(str(request["job_dir"])).expanduser().resolve()
    code = str(request["code"])
    script_path = str(request.get("script_path") or "<worker-job>")
    job_id = str(request.get("job_id") or "job")

    if not blend_file.is_file() or blend_file.suffix.lower() != ".blend":
        raise RuntimeError(f"invalid blend file: {blend_file}")
    job_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    bpy.ops.wm.open_mainfile(filepath=str(blend_file))
    namespace = {
        "__name__": "__blender_worker_job__",
        "__file__": script_path,
        "bpy": bpy,
        "Path": Path,
        "JOB_ID": job_id,
        "JOB_DIR": str(job_dir),
        "RESULT_PATH": str(result_path),
        "WORKER_ID": worker_id,
        "WORKER_PORT": port,
    }
    exec(compile(code, script_path, "exec"), namespace, namespace)
    bpy.ops.wm.save_as_mainfile(filepath=str(result_path))
    response = _summary(worker_id, port)
    response.update(
        {
            "ok": True,
            "job_id": job_id,
            "result_path": str(result_path),
            "job_started_at": started,
            "job_finished_at": time.time(),
        }
    )
    return response


def _handle(request: dict, token: str, worker_id: str, port: int) -> tuple[dict, bool]:
    supplied = str(request.get("token") or "")
    if not secrets.compare_digest(supplied, token):
        return {"ok": False, "error": "unauthorized"}, False
    command = request.get("cmd")
    if command == "ping":
        response = _summary(worker_id, port)
        response["ok"] = True
        return response, False
    if command == "run_script":
        return _run_script(request, worker_id, port), False
    if command == "shutdown":
        return {"ok": True, "worker_id": worker_id, "shutdown": True}, True
    return {"ok": False, "error": f"unknown command: {command!r}"}, False


def _serve_connection(conn: socket.socket, token: str, worker_id: str, port: int) -> bool:
    shutdown = False
    with conn:
        conn.settimeout(600.0)
        try:
            request = _read_request(conn)
            response, shutdown = _handle(request, token, worker_id, port)
        except Exception as exc:
            traceback.print_exc()
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            _send(conn, response)
        except OSError:
            pass
    return shutdown


def main() -> None:
    args = _args()
    _require_loopback(args.host)
    token_file = Path(args.token_file).expanduser().resolve()
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("worker token file is empty")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(16)
    print(
        json.dumps(
            {
                "event": "worker_ready",
                "worker_id": args.worker_id,
                "pid": os.getpid(),
                "host": args.host,
                "port": args.port,
                "background": bool(bpy.app.background),
            },
            sort_keys=True,
        ),
        flush=True,
    )

    if bpy.app.background:
        server.settimeout(0.5)
        shutdown = False
        while not shutdown:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            shutdown = _serve_connection(conn, token, args.worker_id, args.port)
        server.close()
        return

    server.setblocking(False)
    state = {"shutdown": False}

    def poll() -> float | None:
        if state["shutdown"]:
            server.close()
            try:
                bpy.ops.wm.quit_blender()
            except Exception:
                os._exit(0)
            return None
        try:
            conn, _ = server.accept()
        except BlockingIOError:
            return 0.05
        state["shutdown"] = _serve_connection(conn, token, args.worker_id, args.port)
        return 0.01

    bpy.app.timers.register(poll, first_interval=0.05, persistent=True)


main()
