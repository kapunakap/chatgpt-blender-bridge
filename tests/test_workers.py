from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from blender_bridge.workers import (
    DEFAULT_SINGLE_USER_PORT,
    WorkerError,
    WorkerManager,
    detect_blender_bin,
)


class WorkerManagerUnitTests(unittest.TestCase):
    def test_reserved_single_user_port_is_rejected(self) -> None:
        with self.assertRaises(WorkerError):
            WorkerManager._validate_port(DEFAULT_SINGLE_USER_PORT)
        WorkerManager._validate_port(9970)

    def test_job_and_worker_ids_are_path_safe(self) -> None:
        WorkerManager._validate_job_id("job-001.city")
        WorkerManager._validate_worker_id("worker-3")
        for bad in ("", "../escape", "/absolute", "has space", "x" * 129):
            with self.assertRaises(WorkerError, msg=bad):
                WorkerManager._validate_job_id(bad)

    def test_detect_blender_honors_explicit_executable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake = Path(raw) / "blender"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            self.assertEqual(detect_blender_bin(str(fake)), fake.resolve())

    def test_source_write_lock_rejects_other_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            source = temp / "source.blend"
            source.write_bytes(b"fixture")
            manager = WorkerManager.__new__(WorkerManager)
            manager.locks_dir = temp / "locks"
            manager.locks_dir.mkdir()
            child_code = """
import json
from pathlib import Path
import sys
from blender_bridge.workers import SourceBusyError, WorkerManager
m = WorkerManager.__new__(WorkerManager)
m.locks_dir = Path(sys.argv[1])
try:
    with m.source_write_lock(Path(sys.argv[2]), 'child'):
        pass
except SourceBusyError as exc:
    print(json.dumps({'busy': True, 'error': str(exc)}))
    raise SystemExit(73)
raise SystemExit(0)
"""
            with manager.source_write_lock(source, "parent") as lock_path:
                metadata = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["job_id"], "parent")
                proc = subprocess.run(
                    [sys.executable, "-c", child_code, str(manager.locks_dir), str(source)],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 73, proc.stderr)
                self.assertIn('"busy": true', proc.stdout.lower())
            with manager.source_write_lock(source, "after-release"):
                pass


if __name__ == "__main__":
    unittest.main()
