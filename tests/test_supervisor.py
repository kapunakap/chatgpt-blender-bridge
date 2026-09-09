import json
import os
from pathlib import Path
import tempfile
import unittest

from blender_bridge.supervisor import CapacityBusyError, SupervisorConfig, WorkerSupervisor
from blender_bridge.workers import WorkerError


class FakeManager:
    def __init__(self, root, workers=None):
        self.root = Path(root)
        self.jobs_dir = self.root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.session_leases_dir = self.root / "session-leases"
        self.session_leases_dir.mkdir(parents=True, exist_ok=True)
        self.workers = {}
        for worker in workers or []:
            self.workers[worker["id"]] = dict(worker)
        self.stopped = []
        self.started = []
        self.restarted = []

    def status(self):
        result = []
        for worker_id in sorted(self.workers):
            worker = dict(self.workers[worker_id])
            worker.setdefault("process_alive", True)
            worker.setdefault("healthy", True)
            worker.setdefault("busy", False)
            worker.setdefault("mode", "background")
            worker.setdefault("port", 9970 + int(worker_id.split("-")[1]) - 1)
            worker.setdefault("pid", 1000 + int(worker_id.split("-")[1]))
            worker.setdefault("log_path", str(self.logs_dir / (worker_id + ".log")))
            result.append(worker)
        return result

    def start(self, count, gui_count, base_port, timeout):
        self.started.append((count, gui_count, base_port))
        for index in range(1, count + 1):
            worker_id = f"worker-{index}"
            if worker_id not in self.workers:
                self.workers[worker_id] = {
                    "id": worker_id,
                    "healthy": True,
                    "busy": False,
                    "process_alive": True,
                    "mode": "gui" if index <= gui_count else "background",
                    "port": base_port + index - 1,
                    "pid": 1000 + index,
                    "log_path": str(self.logs_dir / (worker_id + ".log")),
                }
        return self.status()

    def stop_worker(self, worker_id, grace=5.0):
        self.stopped.append(worker_id)
        self.workers.pop(worker_id, None)

    def restart_worker(self, worker_id, timeout=30):
        self.restarted.append(worker_id)
        self.workers[worker_id]["healthy"] = True
        self.workers[worker_id]["process_alive"] = True
        return [worker for worker in self.status() if worker["id"] == worker_id][0]


class FakeRouter:
    def __init__(self, root, routes=None):
        self.root = Path(root)
        self.state_path = self.root / "projects.json"
        self.lock_path = self.root / "projects.lock"
        self.routes = list(routes or [])
        state = {"version": 1, "projects": {route["key"]: dict(route) for route in self.routes}}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def list_routes(self):
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [dict(value, key=key) for key, value in data.get("projects", {}).items()]
        return []


class SupervisorTests(unittest.TestCase):
    def cfg(self, root, **kwargs):
        data = dict(
            runtime_dir=Path(root),
            min_workers=1,
            max_workers=5,
            idle_timeout_seconds=10,
            gc_interval_seconds=1,
            job_retention_seconds=10,
            log_retention_seconds=10,
            affinity_stale_seconds=50,
            base_port=9970,
            gui_count=0,
            spawn_timeout_seconds=1,
        )
        data.update(kwargs)
        return SupervisorConfig(**data)

    def test_reuse_then_scale_and_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = FakeManager(temp_dir)
            router = FakeRouter(temp_dir)
            supervisor = WorkerSupervisor(
                self.cfg(temp_dir, max_workers=2), manager=manager, router=router
            )
            worker1 = supervisor.ensure_capacity()
            self.assertEqual(worker1["id"], "worker-1")
            manager.workers["worker-1"]["busy"] = True
            worker2 = supervisor.ensure_capacity()
            self.assertEqual(worker2["id"], "worker-2")
            manager.workers["worker-2"]["busy"] = True
            with self.assertRaises(CapacityBusyError):
                supervisor.ensure_capacity()
            self.assertNotIn("worker-3", manager.workers)

    def test_project_claim_spawns_instead_of_stealing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            router = FakeRouter(
                temp_dir,
                [{"key": "project:A", "worker": "worker-1", "updated_at": 100, "metadata": {"project": "A"}}],
            )
            manager = FakeManager(temp_dir, [{"id": "worker-1"}])
            supervisor = WorkerSupervisor(
                self.cfg(temp_dir, max_workers=2), manager=manager, router=router, now=lambda: 100
            )
            worker = supervisor.ensure_capacity({"project": "B"})
            self.assertEqual(worker["id"], "worker-2")
            affinity_worker = supervisor.ensure_capacity({"project": "A"})
            self.assertEqual(affinity_worker["id"], "worker-1")

    def test_idle_scale_down_preserves_active_affinity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [100.0]
            manager = FakeManager(
                temp_dir,
                [{"id": "worker-1"}, {"id": "worker-2"}, {"id": "worker-3"}],
            )
            router = FakeRouter(
                temp_dir,
                [{"key": "project:A", "worker": "worker-2", "updated_at": 100, "metadata": {"project": "A"}}],
            )
            supervisor = WorkerSupervisor(
                self.cfg(temp_dir, min_workers=1, idle_timeout_seconds=5, affinity_stale_seconds=50),
                manager=manager,
                router=router,
                now=lambda: now[0],
            )
            supervisor.reconcile_and_gc()
            now[0] = 106
            report = supervisor.reconcile_and_gc()
            self.assertIn("worker-2", manager.workers)
            self.assertEqual(set(manager.workers), {"worker-2"})
            self.assertEqual(len(report["scaled_down"]), 2)

    def test_dead_reconciled_and_minimum_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = FakeManager(
                temp_dir,
                [{"id": "worker-1", "healthy": False, "process_alive": False}],
            )
            router = FakeRouter(temp_dir)
            supervisor = WorkerSupervisor(
                self.cfg(temp_dir, min_workers=1), manager=manager, router=router
            )
            report = supervisor.reconcile_and_gc()
            self.assertIn("worker-1", manager.workers)
            self.assertIn("worker-1", report["reconciliation"]["removed_dead"])

    def test_gc_completed_only_and_stale_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = 1000.0
            manager = FakeManager(temp_dir, [{"id": "worker-1"}])
            router = FakeRouter(
                temp_dir,
                [
                    {"key": "project:old", "worker": "worker-1", "updated_at": 900, "metadata": {"project": "old"}},
                    {"key": "project:new", "worker": "worker-1", "updated_at": 990, "metadata": {"project": "new"}},
                ],
            )
            completed = manager.jobs_dir / "done"
            completed.mkdir()
            result = completed / "result.blend"
            result.write_bytes(b"x")
            os.utime(result, (900, 900))
            active = manager.jobs_dir / "active"
            active.mkdir()
            (active / "source.blend").write_bytes(b"source")
            stale_log = manager.logs_dir / "worker-9.log"
            stale_log.write_text("old", encoding="utf-8")
            os.utime(stale_log, (900, 900))
            active_log = manager.logs_dir / "worker-1.log"
            active_log.write_text("active", encoding="utf-8")
            os.utime(active_log, (900, 900))
            lease = manager.session_leases_dir / "worker-9.lock"
            lease.write_text("", encoding="utf-8")
            os.utime(lease, (900, 900))
            supervisor = WorkerSupervisor(
                self.cfg(
                    temp_dir,
                    job_retention_seconds=50,
                    log_retention_seconds=50,
                    affinity_stale_seconds=50,
                ),
                manager=manager,
                router=router,
                now=lambda: now,
            )
            report = supervisor.reconcile_and_gc()
            self.assertFalse(completed.exists())
            self.assertTrue(active.exists())
            self.assertFalse(stale_log.exists())
            self.assertTrue(active_log.exists())
            self.assertFalse(lease.exists())
            self.assertIn("project:old", report["gc"]["stale_affinities"])
            self.assertNotIn("project:new", report["gc"]["stale_affinities"])

    def test_managed_range_cannot_touch_legacy_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(WorkerError):
                self.cfg(temp_dir, base_port=9875, max_workers=3).validate()


if __name__ == "__main__":
    unittest.main()
