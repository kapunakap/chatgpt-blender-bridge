#!/usr/bin/env python3
"""Real local autoscaling acceptance for Issue #9.

Run this from an interactive macOS login session. It starts the supervisor as a
child of that user session, never from tunnel-client, and uses only a temporary
managed runtime/port range. The legacy 127.0.0.1:9876 endpoint is only probed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blender_bridge.project_routing import ProjectRouter  # noqa: E402
from blender_bridge.supervisor import CapacityBusyError, SupervisorClient  # noqa: E402
from blender_bridge.workers import WorkerBusyError, WorkerManager  # noqa: E402


def _legacy_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 9876), timeout=0.5):
            return True
    except OSError:
        return False


def _lease_child(runtime: str, socket_path: str, release, queue) -> None:
    manager = WorkerManager(runtime_dir=runtime)
    client = SupervisorClient(socket_path, timeout=40.0)
    last_error: str | None = None
    for _ in range(20):
        try:
            candidate = client.ensure_capacity()
            try:
                with manager.lease_worker(str(candidate["id"])) as worker:
                    queue.put(
                        {
                            "ok": True,
                            "worker": worker["id"],
                            "pid": worker["pid"],
                            "port": worker["port"],
                        }
                    )
                    release.wait(60.0)
                    return
            except WorkerBusyError as exc:
                last_error = str(exc)
                time.sleep(0.05)
        except CapacityBusyError as exc:
            queue.put({"ok": False, "code": "capacity_busy", "error": str(exc)})
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.1)
    queue.put({"ok": False, "code": "lease_failed", "error": last_error})


def _wait_for(predicate, timeout: float, description: str) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {description}; last={last!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-port", type=int, default=10070)
    parser.add_argument("--idle-timeout", type=float, default=2.0)
    parser.add_argument("--gc-interval", type=float, default=0.5)
    parser.add_argument("--keep-runtime", action="store_true")
    args = parser.parse_args()

    runtime = Path(tempfile.mkdtemp(prefix="chatgpt-blender-autoscale-", dir="/tmp")).resolve()
    socket_path = runtime / "supervisor.sock"
    env = os.environ.copy()
    env.update(
        {
            "CHATGPT_BLENDER_WORKER_RUNTIME": str(runtime),
            "BLENDER_WORKER_MIN_COUNT": "1",
            "BLENDER_WORKER_MAX_COUNT": "5",
            "BLENDER_WORKER_GUI_COUNT": "5",
            "BLENDER_WORKER_BASE_PORT": str(args.base_port),
            "BLENDER_WORKER_IDLE_TIMEOUT_SECONDS": str(args.idle_timeout),
            "BLENDER_WORKER_GC_INTERVAL_SECONDS": str(args.gc_interval),
            "BLENDER_JOB_RETENTION_SECONDS": "1",
            "BLENDER_LOG_RETENTION_SECONDS": "1",
            "BLENDER_PROJECT_AFFINITY_STALE_SECONDS": "2",
        }
    )
    legacy_before = _legacy_open()
    supervisor = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "blender-worker-supervisor.py"),
            "--runtime",
            str(runtime),
            "run",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    manager: WorkerManager | None = None
    report: dict[str, Any] = {"runtime": str(runtime), "legacy_9876_before": legacy_before}
    try:
        client = SupervisorClient(socket_path, timeout=40.0)

        def supervisor_status() -> dict[str, Any] | None:
            if not socket_path.exists():
                return None
            try:
                return client.status()
            except Exception:
                return None

        _wait_for(supervisor_status, 45.0, "supervisor control socket")
        manager = WorkerManager(runtime_dir=runtime)

        ctx = mp.get_context("spawn")
        release = ctx.Event()
        queue = ctx.Queue()
        holders = [
            ctx.Process(target=_lease_child, args=(str(runtime), str(socket_path), release, queue))
            for _ in range(5)
        ]
        for proc in holders:
            proc.start()
        leases = [queue.get(timeout=60.0) for _ in holders]
        if not all(item.get("ok") for item in leases):
            raise RuntimeError(f"failed to lease five workers: {leases}")
        worker_ids = {str(item["worker"]) for item in leases}
        pids = {int(item["pid"]) for item in leases}
        if len(worker_ids) != 5 or len(pids) != 5:
            raise RuntimeError(f"expected five isolated workers, got {leases}")
        report["five_concurrent"] = leases

        sixth = ctx.Process(target=_lease_child, args=(str(runtime), str(socket_path), release, queue))
        sixth.start()
        sixth_result = queue.get(timeout=20.0)
        if sixth_result.get("code") != "capacity_busy":
            raise RuntimeError(f"6th request did not hit capacity cap: {sixth_result}")
        report["sixth_request"] = sixth_result
        sixth.join(timeout=5.0)

        release.set()
        for proc in holders:
            proc.join(timeout=10.0)
            if proc.is_alive():
                proc.terminate()

        statuses = manager.status()
        if not statuses:
            raise RuntimeError("no managed worker survived for affinity test")
        target = sorted(statuses, key=lambda item: str(item["id"]))[0]
        router = ProjectRouter(runtime)
        route = router.resolve(
            {"project": "issue9-acceptance"},
            current_worker=str(target["id"]),
            statuses=statuses,
            session_id="acceptance",
        )
        candidate = client.ensure_capacity({"project": "issue9-acceptance"})
        if candidate["id"] != route["worker"]:
            raise RuntimeError(f"project affinity was not reused: route={route}, candidate={candidate}")
        report["project_affinity"] = {"route": route, "candidate": candidate["id"]}

        dead_target = sorted(manager.status(), key=lambda item: str(item["id"]))[-1]["id"]
        manager.kill_worker(str(dead_target))
        reconcile = client.request("reconcile")
        dead_ids = {item["id"] for item in reconcile["workers"]}
        if dead_target in dead_ids and not next(
            (item.get("process_alive") for item in reconcile["workers"] if item["id"] == dead_target),
            False,
        ):
            raise RuntimeError(f"dead worker record was not reconciled: {dead_target}")
        report["dead_reconcile"] = {"killed": dead_target, "result": reconcile["reconciliation"]}

        completed = manager.jobs_dir / "acceptance-complete"
        completed.mkdir(exist_ok=False)
        result_file = completed / "result.blend"
        result_file.write_bytes(b"acceptance placeholder")
        old = time.time() - 10
        os.utime(result_file, (old, old))
        active = manager.jobs_dir / "acceptance-active"
        active.mkdir(exist_ok=False)
        (active / "source.blend").write_bytes(b"must remain")
        stale_log = manager.logs_dir / "stale-acceptance.log"
        stale_log.write_text("old\n", encoding="utf-8")
        os.utime(stale_log, (old, old))
        gc_report = client.request("gc")
        if completed.exists() or not active.exists() or stale_log.exists():
            raise RuntimeError(f"GC safety failure: {gc_report}")
        report["gc"] = gc_report["gc"]

        deadline = time.monotonic() + args.idle_timeout + max(4.0, args.gc_interval * 4)
        scaled: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            scaled = manager.status()
            if len(scaled) == 1:
                break
            time.sleep(0.25)
        if len(scaled) != 1:
            raise RuntimeError(f"pool did not scale down to min=1: {scaled}")
        report["scaled_down_workers"] = scaled

        legacy_after = _legacy_open()
        report["legacy_9876_after"] = legacy_after
        report["legacy_9876_preserved"] = legacy_before == legacy_after
        if legacy_before and not legacy_after:
            raise RuntimeError("legacy 127.0.0.1:9876 endpoint was disrupted")

        print(json.dumps({"ok": True, **report}, indent=2, sort_keys=True))
        return 0
    finally:
        with contextlib.suppress(Exception):
            os.killpg(supervisor.pid, signal.SIGTERM)
        try:
            supervisor.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                os.killpg(supervisor.pid, signal.SIGKILL)
        if manager is not None:
            with contextlib.suppress(Exception):
                manager.stop_all()
        if not args.keep_runtime:
            shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
