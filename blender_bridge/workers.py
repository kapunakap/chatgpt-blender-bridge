from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_BASE_PORT = 9970
DEFAULT_SINGLE_USER_PORT = 9876
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class WorkerError(RuntimeError):
    """Base worker-manager error."""


class SourceBusyError(WorkerError):
    """Raised when a mutable source file already has a writer."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_runtime_dir() -> Path:
    raw = os.environ.get("CHATGPT_BLENDER_WORKER_RUNTIME")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".cache" / "chatgpt-blender-bridge" / "workers"


def detect_blender_bin(explicit: str | None = None) -> Path:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("BLENDER_BIN"):
        candidates.append(os.environ["BLENDER_BIN"])
    which = shutil.which("blender")
    if which:
        candidates.append(which)
    candidates.extend(
        [
            "/Applications/Blender.app/Contents/MacOS/Blender",
            "/Applications/Blender 5.2.app/Contents/MacOS/Blender",
        ]
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    raise WorkerError("Blender executable not found; set BLENDER_BIN")


def _json_line(sock: socket.socket) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > 32 * 1024 * 1024:
            raise WorkerError("worker response exceeded 32 MiB")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise WorkerError("worker closed connection without a response")
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise WorkerError("invalid worker response")
    return response


def _pid_alive(pid: int) -> bool:
    # Reap an exited worker when this manager process is its parent. Without
    # this, a killed child remains a zombie and kill(pid, 0) incorrectly
    # reports it as alive until the parent exits. Managers in later CLI
    # processes are not the parent, so waitpid simply raises ChildProcessError.
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_command(pid: int) -> str | None:
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.is_file():
        try:
            return proc_cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        except OSError:
            pass
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    command = proc.stdout.strip()
    return command or None


def _pid_matches_worker(record: dict[str, Any]) -> bool:
    pid = int(record.get("pid", 0) or 0)
    if not pid or not _pid_alive(pid):
        return False
    command = _pid_command(pid)
    if not command:
        return False
    worker_id = str(record.get("id") or "")
    return (
        "blender-worker-bootstrap.py" in command
        and "--worker-id" in command
        and worker_id in command
    )


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


class WorkerManager:
    def __init__(
        self,
        runtime_dir: str | Path | None = None,
        blender_bin: str | Path | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir or default_runtime_dir()).expanduser().resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(PermissionError):
            os.chmod(self.runtime_dir, 0o700)
        self.state_path = self.runtime_dir / "workers.json"
        self.state_lock_path = self.runtime_dir / "workers.lock"
        self.jobs_dir = self.runtime_dir / "jobs"
        lock_override = os.environ.get("CHATGPT_BLENDER_SOURCE_LOCK_DIR")
        self.locks_dir = (
            Path(lock_override).expanduser().resolve()
            if lock_override
            else Path.home() / ".cache" / "chatgpt-blender-bridge" / "source-locks"
        )
        self.logs_dir = self.runtime_dir / "logs"
        for path in (self.jobs_dir, self.locks_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(PermissionError):
            os.chmod(self.locks_dir, 0o700)
        self.blender_bin = detect_blender_bin(str(blender_bin) if blender_bin else None)
        self.bootstrap = repo_root() / "scripts" / "blender-worker-bootstrap.py"
        if not self.bootstrap.is_file():
            raise WorkerError(f"worker bootstrap missing: {self.bootstrap}")

    @contextlib.contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        self.state_lock_path.touch(mode=0o600, exist_ok=True)
        with self.state_lock_path.open("r+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            state: dict[str, Any] = {"version": 1, "workers": {}}
            if self.state_path.exists():
                try:
                    loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        state = loaded
                except (json.JSONDecodeError, OSError):
                    pass
            state.setdefault("version", 1)
            state.setdefault("workers", {})
            yield state
            _atomic_json(self.state_path, state)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _load_state(self) -> dict[str, Any]:
        with self._locked_state() as state:
            return json.loads(json.dumps(state))

    @staticmethod
    def _validate_port(port: int) -> None:
        if not 1024 <= int(port) <= 65535:
            raise WorkerError(f"invalid worker port: {port}")
        if int(port) == DEFAULT_SINGLE_USER_PORT:
            raise WorkerError(
                f"port {DEFAULT_SINGLE_USER_PORT} is reserved for the existing single-user Blender MCP workflow"
            )

    @staticmethod
    def _validate_worker_id(worker_id: str) -> None:
        if not JOB_ID_RE.fullmatch(worker_id):
            raise WorkerError(f"invalid worker id: {worker_id!r}")

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not JOB_ID_RE.fullmatch(job_id):
            raise WorkerError(f"invalid job id: {job_id!r}")

    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((LOOPBACK_HOST, port))
            except OSError:
                return False
        return True

    def _token_for(self, record: dict[str, Any]) -> str:
        token_path = Path(record["token_file"])
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise WorkerError(f"empty token file for worker {record['id']}")
        return token

    def request(
        self,
        worker_id: str,
        payload: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        state = self._load_state()
        record = state.get("workers", {}).get(worker_id)
        if not record:
            raise WorkerError(f"unknown worker: {worker_id}")
        message = dict(payload)
        message["token"] = self._token_for(record)
        try:
            with socket.create_connection(
                (record["host"], int(record["port"])), timeout=min(timeout, 5.0)
            ) as sock:
                sock.settimeout(timeout)
                sock.sendall(json.dumps(message).encode("utf-8") + b"\n")
                response = _json_line(sock)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerError(f"worker {worker_id} request failed: {exc}") from exc
        if not response.get("ok"):
            raise WorkerError(str(response.get("error") or "worker request failed"))
        return response

    def _health(self, record: dict[str, Any]) -> dict[str, Any]:
        pid = int(record.get("pid", 0) or 0)
        process_alive = pid > 0 and _pid_alive(pid)
        response: dict[str, Any] | None = None
        error: str | None = None
        if process_alive:
            try:
                message = {"cmd": "ping", "token": self._token_for(record)}
                with socket.create_connection(
                    (record["host"], int(record["port"])), timeout=0.5
                ) as sock:
                    sock.settimeout(1.0)
                    sock.sendall(json.dumps(message).encode("utf-8") + b"\n")
                    response = _json_line(sock)
            except Exception as exc:  # health must remain diagnostic
                error = str(exc)
        healthy = bool(process_alive and response and response.get("ok"))
        return {
            "id": record.get("id"),
            "pid": pid,
            "host": record.get("host"),
            "port": record.get("port"),
            "mode": record.get("mode"),
            "healthy": healthy,
            "process_alive": process_alive,
            "started_at": record.get("started_at"),
            "log_path": record.get("log_path"),
            "ping": response,
            "error": error,
        }

    def status(self) -> list[dict[str, Any]]:
        state = self._load_state()
        return [
            self._health(record)
            for _, record in sorted(state.get("workers", {}).items())
        ]

    def _spawn_worker(
        self,
        worker_id: str,
        port: int,
        mode: str,
    ) -> dict[str, Any]:
        self._validate_worker_id(worker_id)
        self._validate_port(port)
        if mode not in {"background", "gui"}:
            raise WorkerError(f"unsupported worker mode: {mode}")
        if not self._port_available(port):
            raise WorkerError(f"worker port already in use: {LOOPBACK_HOST}:{port}")

        token_file = self.runtime_dir / f"{worker_id}.token"
        token_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        os.chmod(token_file, 0o600)
        log_path = self.logs_dir / f"{worker_id}.log"
        command = [str(self.blender_bin)]
        if mode == "background":
            command += ["--background", "--factory-startup"]
        else:
            command += ["--factory-startup"]
        command += [
            "--python",
            str(self.bootstrap),
            "--",
            "--worker-id",
            worker_id,
            "--host",
            LOOPBACK_HOST,
            "--port",
            str(port),
            "--token-file",
            str(token_file),
        ]
        log_handle = log_path.open("ab", buffering=0)
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        return {
            "id": worker_id,
            "pid": proc.pid,
            "host": LOOPBACK_HOST,
            "port": port,
            "mode": mode,
            "started_at": time.time(),
            "token_file": str(token_file),
            "log_path": str(log_path),
            "blender_bin": str(self.blender_bin),
        }

    def _wait_healthy(self, worker_id: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error = "not ready"
        while time.monotonic() < deadline:
            state = self._load_state()
            record = state.get("workers", {}).get(worker_id)
            if not record:
                raise WorkerError(f"worker disappeared while starting: {worker_id}")
            health = self._health(record)
            if health["healthy"]:
                return health
            last_error = health.get("error") or "worker not healthy"
            if not health["process_alive"]:
                break
            time.sleep(0.2)
        raise WorkerError(f"worker {worker_id} failed to become healthy: {last_error}")

    def start(
        self,
        count: int = 1,
        gui_count: int = 0,
        base_port: int = DEFAULT_BASE_PORT,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        if count < 1:
            raise WorkerError("count must be >= 1")
        if gui_count < 0 or gui_count > count:
            raise WorkerError("gui_count must be between 0 and count")
        records: list[dict[str, Any]] = []
        with self._locked_state() as state:
            workers = state["workers"]
            for index in range(count):
                worker_id = f"worker-{index + 1}"
                port = int(base_port) + index
                self._validate_port(port)
                desired_mode = "gui" if index < gui_count else "background"
                existing = workers.get(worker_id)
                if existing and int(existing.get("port", -1)) == port and existing.get("mode") == desired_mode:
                    if self._health(existing)["healthy"]:
                        records.append(existing)
                        continue
                if existing:
                    old_pid = int(existing.get("pid", 0) or 0)
                    if old_pid and _pid_matches_worker(existing):
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(old_pid, signal.SIGTERM)
                        deadline = time.monotonic() + 3.0
                        while _pid_alive(old_pid) and time.monotonic() < deadline:
                            time.sleep(0.05)
                        if _pid_alive(old_pid) and _pid_matches_worker(existing):
                            with contextlib.suppress(ProcessLookupError, PermissionError):
                                os.killpg(old_pid, signal.SIGKILL)
                    workers.pop(worker_id, None)
                record = self._spawn_worker(worker_id, port, desired_mode)
                workers[worker_id] = record
                records.append(record)
        results = []
        for record in records:
            results.append(self._wait_healthy(record["id"], timeout=timeout))
        return results

    def stop_worker(self, worker_id: str, grace: float = 5.0) -> None:
        state = self._load_state()
        record = state.get("workers", {}).get(worker_id)
        if not record:
            return
        pid = int(record.get("pid", 0) or 0)
        if pid and _pid_alive(pid):
            try:
                self.request(worker_id, {"cmd": "shutdown"}, timeout=2.0)
            except WorkerError:
                pass
            deadline = time.monotonic() + grace
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _pid_alive(pid) and _pid_matches_worker(record):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 2.0
                while _pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.1)
            if _pid_alive(pid) and _pid_matches_worker(record):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)
        with self._locked_state() as locked:
            locked.get("workers", {}).pop(worker_id, None)
        with contextlib.suppress(FileNotFoundError):
            Path(record["token_file"]).unlink()

    def stop_all(self) -> None:
        state = self._load_state()
        for worker_id in list(state.get("workers", {})):
            self.stop_worker(worker_id)

    def restart_worker(self, worker_id: str, timeout: float = 30.0) -> dict[str, Any]:
        state = self._load_state()
        old = state.get("workers", {}).get(worker_id)
        if not old:
            raise WorkerError(f"unknown worker: {worker_id}")
        port = int(old["port"])
        mode = str(old["mode"])
        self.stop_worker(worker_id)
        with self._locked_state() as state2:
            record = self._spawn_worker(worker_id, port, mode)
            state2["workers"][worker_id] = record
        return self._wait_healthy(worker_id, timeout=timeout)

    def kill_worker(self, worker_id: str) -> None:
        state = self._load_state()
        record = state.get("workers", {}).get(worker_id)
        if not record:
            raise WorkerError(f"unknown worker: {worker_id}")
        pid = int(record["pid"])
        if _pid_alive(pid):
            if not _pid_matches_worker(record):
                raise WorkerError(f"refusing to signal PID {pid}: process identity does not match {worker_id}")
            os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)

    def _source_lock_path(self, source: Path) -> Path:
        digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()
        return self.locks_dir / f"{digest}.lock"

    @contextlib.contextmanager
    def source_write_lock(self, source: str | Path, job_id: str) -> Iterator[Path]:
        source_path = Path(source).expanduser().resolve()
        lock_path = self._source_lock_path(source_path)
        lock_path.touch(mode=0o600, exist_ok=True)
        handle = lock_path.open("r+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SourceBusyError(f"source already has an active writer: {source_path}") from exc
            handle.seek(0)
            handle.truncate()
            json.dump(
                {"source": str(source_path), "job_id": job_id, "pid": os.getpid(), "acquired_at": time.time()},
                handle,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            yield lock_path
        finally:
            with contextlib.suppress(OSError):
                handle.seek(0)
                handle.truncate()
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def run_job(
        self,
        worker_id: str,
        source: str | Path,
        script: str | Path,
        job_id: str,
        write_source: bool = False,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        self._validate_job_id(job_id)
        source_path = Path(source).expanduser().resolve()
        script_path = Path(script).expanduser().resolve()
        if not source_path.is_file():
            raise WorkerError(f"source blend file not found: {source_path}")
        if source_path.suffix.lower() != ".blend":
            raise WorkerError(f"source must be a .blend file: {source_path}")
        if not script_path.is_file():
            raise WorkerError(f"job script not found: {script_path}")

        job_dir = self.jobs_dir / job_id
        if job_dir.exists():
            raise WorkerError(f"job already exists: {job_id}")
        job_dir.mkdir(parents=True, mode=0o700)
        working_copy = job_dir / "source.blend"
        result_path = job_dir / "result.blend"
        shutil.copy2(source_path, working_copy)
        code = script_path.read_text(encoding="utf-8")

        def execute() -> dict[str, Any]:
            started = time.time()
            response = self.request(
                worker_id,
                {
                    "cmd": "run_script",
                    "job_id": job_id,
                    "blend_file": str(working_copy),
                    "result_path": str(result_path),
                    "job_dir": str(job_dir),
                    "script_path": str(script_path),
                    "code": code,
                },
                timeout=timeout,
            )
            response["worker_id"] = worker_id
            response["job_id"] = job_id
            response["source"] = str(source_path)
            response["working_copy"] = str(working_copy)
            response["result_path"] = str(result_path)
            response["manager_started_at"] = started
            response["manager_finished_at"] = time.time()
            return response

        if not write_source:
            return execute()

        with self.source_write_lock(source_path, job_id) as lock_path:
            response = execute()
            publish_tmp = source_path.with_name(
                f".{source_path.name}.{job_id}.{os.getpid()}.tmp"
            )
            shutil.copy2(result_path, publish_tmp)
            os.replace(publish_tmp, source_path)
            response["published_to_source"] = True
            response["source_lock"] = str(lock_path)
            return response


def _manager_from_args(args: argparse.Namespace) -> WorkerManager:
    return WorkerManager(runtime_dir=args.runtime, blender_bin=args.blender_bin)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage isolated local Blender workers")
    parser.add_argument("--runtime", help="private runtime directory")
    parser.add_argument("--blender-bin", help="path to Blender executable")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start/reuse worker pool")
    start.add_argument("--count", type=int, default=1)
    start.add_argument("--gui-count", type=int, default=0)
    start.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)

    sub.add_parser("status", help="show worker health")
    sub.add_parser("stop", help="stop all workers")

    restart = sub.add_parser("restart", help="restart one worker on its existing endpoint")
    restart.add_argument("--worker", required=True)

    kill = sub.add_parser("kill", help="hard-kill one worker (for recovery testing)")
    kill.add_argument("--worker", required=True)

    run = sub.add_parser("run", help="route a script job to one worker")
    run.add_argument("--worker", required=True)
    run.add_argument("--source", required=True)
    run.add_argument("--script", required=True)
    run.add_argument("--job-id", required=True)
    run.add_argument("--write-source", action="store_true")
    run.add_argument("--timeout", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manager = _manager_from_args(args)
        if args.command == "start":
            result: Any = manager.start(
                count=args.count, gui_count=args.gui_count, base_port=args.base_port
            )
        elif args.command == "status":
            result = manager.status()
        elif args.command == "stop":
            manager.stop_all()
            result = {"stopped": True}
        elif args.command == "restart":
            result = manager.restart_worker(args.worker)
        elif args.command == "kill":
            manager.kill_worker(args.worker)
            result = {"killed": args.worker}
        elif args.command == "run":
            result = manager.run_job(
                worker_id=args.worker,
                source=args.source,
                script=args.script,
                job_id=args.job_id,
                write_source=args.write_source,
                timeout=args.timeout,
            )
        else:  # pragma: no cover
            parser.error("unknown command")
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except SourceBusyError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "code": "source_busy"}), file=sys.stderr)
        return 73
    except WorkerError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
