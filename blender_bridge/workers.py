from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
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
MCP_PROTOCOL = "blender-lab-mcp-socket"


class WorkerError(RuntimeError):
    """Base worker-manager error."""


class SourceBusyError(WorkerError):
    """Raised when a mutable source file already has a writer."""


class WorkerBusyError(WorkerError):
    """Raised when an MCP session cannot obtain a worker lease."""


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


def detect_blender_mcp_command(explicit: str | None = None) -> Path:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("BLENDER_MCP_COMMAND"):
        candidates.append(os.environ["BLENDER_MCP_COMMAND"])
    which = shutil.which("blender-mcp")
    if which:
        candidates.append(which)
    candidates.extend(
        [
            str(Path.home() / ".local/share/blender-mcp/v1.0.0-venv/bin/blender-mcp"),
            str(Path.home() / ".local/bin/blender-mcp"),
        ]
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    raise WorkerError("blender-mcp executable not found; set BLENDER_MCP_COMMAND")


def _read_null_json(sock: socket.socket) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > 32 * 1024 * 1024:
            raise WorkerError("Blender MCP response exceeded 32 MiB")
        if b"\0" in chunk:
            break
    raw = b"".join(chunks).split(b"\0", 1)[0]
    if not raw:
        raise WorkerError("Blender MCP endpoint closed without a response")
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise WorkerError("invalid Blender MCP response")
    return response


def _pid_alive(pid: int) -> bool:
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
    port = str(record.get("port") or "")
    mode = str(record.get("mode") or "")
    if mode == "background":
        return (
            "--command" in command
            and "blender_mcp" in command
            and "--port" in command
            and port in command
        )
    worker_id = str(record.get("id") or "")
    return (
        "blender-mcp-gui-worker.py" in command
        and "--worker-id" in command
        and worker_id in command
        and port in command
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
        self.session_leases_dir = self.runtime_dir / "session-leases"
        lock_override = os.environ.get("CHATGPT_BLENDER_SOURCE_LOCK_DIR")
        self.locks_dir = (
            Path(lock_override).expanduser().resolve()
            if lock_override
            else Path.home() / ".cache" / "chatgpt-blender-bridge" / "source-locks"
        )
        self.logs_dir = self.runtime_dir / "logs"
        for path in (self.jobs_dir, self.session_leases_dir, self.locks_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
        for path in (self.runtime_dir, self.session_leases_dir, self.locks_dir):
            with contextlib.suppress(PermissionError):
                os.chmod(path, 0o700)
        self.blender_bin = detect_blender_bin(str(blender_bin) if blender_bin else None)
        self.gui_bootstrap = repo_root() / "scripts" / "blender-mcp-gui-worker.py"
        if not self.gui_bootstrap.is_file():
            raise WorkerError(f"GUI worker bootstrap missing: {self.gui_bootstrap}")

    @contextlib.contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        self.state_lock_path.touch(mode=0o600, exist_ok=True)
        with self.state_lock_path.open("r+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            state: dict[str, Any] = {"version": 2, "workers": {}}
            if self.state_path.exists():
                try:
                    loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        state = loaded
                except (json.JSONDecodeError, OSError):
                    pass
            state["version"] = 2
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

    def _record(self, worker_id: str) -> dict[str, Any]:
        state = self._load_state()
        record = state.get("workers", {}).get(worker_id)
        if not record:
            raise WorkerError(f"unknown worker: {worker_id}")
        return record

    @staticmethod
    def _request_record(
        record: dict[str, Any],
        code: str,
        timeout: float,
        strict_json: bool = True,
    ) -> dict[str, Any]:
        message = {"type": "execute", "code": code, "strict_json": strict_json}
        try:
            with socket.create_connection(
                (str(record["host"]), int(record["port"])), timeout=min(timeout, 5.0)
            ) as sock:
                sock.settimeout(timeout)
                sock.sendall(json.dumps(message).encode("utf-8") + b"\0")
                response = _read_null_json(sock)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerError(f"worker {record.get('id')} MCP request failed: {exc}") from exc
        if response.get("status") != "ok":
            raise WorkerError(str(response.get("message") or "Blender MCP request failed"))
        result = response.get("result")
        if strict_json and not isinstance(result, dict):
            raise WorkerError("Blender MCP response result must be a JSON object")
        return response

    def execute_code(
        self,
        worker_id: str,
        code: str,
        timeout: float = 30.0,
        strict_json: bool = True,
    ) -> dict[str, Any]:
        return self._request_record(self._record(worker_id), code, timeout, strict_json)

    def _worker_lease_path(self, worker_id: str) -> Path:
        return self.session_leases_dir / f"{worker_id}.lock"

    def _worker_busy(self, worker_id: str) -> bool:
        lease_path = self._worker_lease_path(worker_id)
        lease_path.touch(mode=0o600, exist_ok=True)
        handle = lease_path.open("r+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            handle.close()

    def _health(self, record: dict[str, Any]) -> dict[str, Any]:
        pid = int(record.get("pid", 0) or 0)
        process_alive = pid > 0 and _pid_alive(pid)
        busy = self._worker_busy(str(record.get("id") or ""))
        response: dict[str, Any] | None = None
        error: str | None = None
        if process_alive and not busy:
            try:
                response = self._request_record(
                    record,
                    "import bpy, os\nresult={"
                    "'pid':os.getpid(),"
                    "'background':bool(bpy.app.background),"
                    "'blend_file':bpy.data.filepath,"
                    "'scene':bpy.context.scene.name if bpy.context.scene else None,"
                    "'object_count':len(bpy.data.objects),"
                    "'object_names':sorted(obj.name for obj in bpy.data.objects)"
                    "}",
                    timeout=1.5,
                )
            except Exception as exc:  # diagnostic path
                error = str(exc)
        result = response.get("result") if response else None
        endpoint_pid = int(result.get("pid", 0)) if isinstance(result, dict) else 0
        if busy:
            healthy = bool(process_alive and _pid_matches_worker(record))
        else:
            healthy = bool(process_alive and response and endpoint_pid == pid)
        return {
            "id": record.get("id"),
            "pid": pid,
            "host": record.get("host"),
            "port": record.get("port"),
            "mode": record.get("mode"),
            "protocol": record.get("protocol", MCP_PROTOCOL),
            "healthy": healthy,
            "busy": busy,
            "process_alive": process_alive,
            "started_at": record.get("started_at"),
            "log_path": record.get("log_path"),
            "ping": result,
            "error": error,
        }

    def status(self) -> list[dict[str, Any]]:
        state = self._load_state()
        return [self._health(record) for _, record in sorted(state.get("workers", {}).items())]

    def _spawn_worker(self, worker_id: str, port: int, mode: str) -> dict[str, Any]:
        self._validate_worker_id(worker_id)
        self._validate_port(port)
        if mode not in {"background", "gui"}:
            raise WorkerError(f"unsupported worker mode: {mode}")
        if not self._port_available(port):
            raise WorkerError(f"worker port already in use: {LOOPBACK_HOST}:{port}")

        log_path = self.logs_dir / f"{worker_id}.log"
        if mode == "background":
            command = [
                str(self.blender_bin),
                "--background",
                "--online-mode",
                "--command",
                "blender_mcp",
                "--host",
                LOOPBACK_HOST,
                "--port",
                str(port),
            ]
        else:
            command = [
                str(self.blender_bin),
                "--online-mode",
                "--python",
                str(self.gui_bootstrap),
                "--",
                "--worker-id",
                worker_id,
                "--host",
                LOOPBACK_HOST,
                "--port",
                str(port),
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
            "protocol": MCP_PROTOCOL,
            "started_at": time.time(),
            "log_path": str(log_path),
            "blender_bin": str(self.blender_bin),
        }

    def _wait_healthy(self, worker_id: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error = "not ready"
        while time.monotonic() < deadline:
            record = self._record(worker_id)
            health = self._health(record)
            if health["healthy"]:
                return health
            last_error = health.get("error") or "worker not healthy"
            if not health["process_alive"]:
                break
            time.sleep(0.2)
        raise WorkerError(f"worker {worker_id} failed to become healthy: {last_error}")

    @staticmethod
    def _terminate_record(record: dict[str, Any], grace: float = 3.0) -> None:
        pid = int(record.get("pid", 0) or 0)
        if not pid or not _pid_alive(pid):
            return
        if not _pid_matches_worker(record):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_alive(pid) and _pid_matches_worker(record):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 2.0
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)

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
                if (
                    existing
                    and int(existing.get("port", -1)) == port
                    and existing.get("mode") == desired_mode
                    and existing.get("protocol") == MCP_PROTOCOL
                    and self._health(existing)["healthy"]
                ):
                    records.append(existing)
                    continue
                if existing:
                    self._terminate_record(existing)
                    workers.pop(worker_id, None)
                record = self._spawn_worker(worker_id, port, desired_mode)
                workers[worker_id] = record
                records.append(record)
        return [self._wait_healthy(record["id"], timeout=timeout) for record in records]

    def stop_worker(self, worker_id: str, grace: float = 5.0) -> None:
        state = self._load_state()
        record = state.get("workers", {}).get(worker_id)
        if not record:
            return
        self._terminate_record(record, grace=grace)
        with self._locked_state() as locked:
            locked.get("workers", {}).pop(worker_id, None)

    def stop_all(self) -> None:
        state = self._load_state()
        for worker_id in list(state.get("workers", {})):
            self.stop_worker(worker_id)

    def restart_worker(self, worker_id: str, timeout: float = 30.0) -> dict[str, Any]:
        old = self._record(worker_id)
        port = int(old["port"])
        mode = str(old["mode"])
        self.stop_worker(worker_id)
        with self._locked_state() as state:
            record = self._spawn_worker(worker_id, port, mode)
            state["workers"][worker_id] = record
        return self._wait_healthy(worker_id, timeout=timeout)

    def kill_worker(self, worker_id: str) -> None:
        record = self._record(worker_id)
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
        user_code = script_path.read_text(encoding="utf-8")
        worker_code = (
            "import bpy, os, time\n"
            "from pathlib import Path\n"
            f"_blend_file={str(working_copy)!r}\n"
            f"_result_path={str(result_path)!r}\n"
            f"_job_dir={str(job_dir)!r}\n"
            f"_job_id={job_id!r}\n"
            f"_worker_id={worker_id!r}\n"
            f"_worker_port={int(self._record(worker_id)['port'])!r}\n"
            f"_script_path={str(script_path)!r}\n"
            f"_user_code={user_code!r}\n"
            "_started=time.time()\n"
            "bpy.ops.wm.open_mainfile(filepath=_blend_file)\n"
            "_ns={'__name__':'__blender_worker_job__','__file__':_script_path,'bpy':bpy,'Path':Path,"
            "'JOB_ID':_job_id,'JOB_DIR':_job_dir,'RESULT_PATH':_result_path,'WORKER_ID':_worker_id,'WORKER_PORT':_worker_port}\n"
            "exec(compile(_user_code,_script_path,'exec'),_ns,_ns)\n"
            "bpy.ops.wm.save_as_mainfile(filepath=_result_path)\n"
            "result={'ok':True,'job_id':_job_id,'worker_id':_worker_id,'pid':os.getpid(),"
            "'port':_worker_port,'job_started_at':_started,'job_finished_at':time.time(),"
            "'result_path':_result_path,'object_names':sorted(obj.name for obj in bpy.data.objects),"
            "'object_count':len(bpy.data.objects)}\n"
        )

        def execute() -> dict[str, Any]:
            started = time.time()
            with self.lease_worker(worker_id):
                full = self.execute_code(worker_id, worker_code, timeout=timeout)
            response = dict(full["result"])
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
            publish_tmp = source_path.with_name(f".{source_path.name}.{job_id}.{os.getpid()}.tmp")
            shutil.copy2(result_path, publish_tmp)
            os.replace(publish_tmp, source_path)
            response["published_to_source"] = True
            response["source_lock"] = str(lock_path)
            return response

    @contextlib.contextmanager
    def lease_worker(self, worker_id: str = "auto") -> Iterator[dict[str, Any]]:
        statuses = [item for item in self.status() if item["healthy"] and not item.get("busy", False)]
        if worker_id != "auto":
            statuses = [item for item in statuses if item["id"] == worker_id]
            if not statuses:
                raise WorkerBusyError(f"worker is unavailable or unhealthy: {worker_id}")
        if not statuses:
            raise WorkerBusyError("no healthy workers available")
        for status in statuses:
            lease_path = self._worker_lease_path(str(status["id"]))
            lease_path.touch(mode=0o600, exist_ok=True)
            handle = lease_path.open("r+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                handle.seek(0)
                handle.truncate()
                json.dump(
                    {"worker_id": status["id"], "session_pid": os.getpid(), "acquired_at": time.time()},
                    handle,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                yield status
            finally:
                with contextlib.suppress(OSError):
                    handle.seek(0)
                    handle.truncate()
                    handle.flush()
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            return
        target = worker_id if worker_id != "auto" else "all healthy workers"
        raise WorkerBusyError(f"no free MCP session slot for {target}")

    def run_mcp_session(
        self,
        worker_id: str = "auto",
        mcp_command: str | Path | None = None,
    ) -> int:
        command = detect_blender_mcp_command(str(mcp_command) if mcp_command else None)
        with self.lease_worker(worker_id) as worker:
            env = os.environ.copy()
            env["BLENDER_MCP_HOST"] = str(worker["host"])
            env["BLENDER_MCP_PORT"] = str(worker["port"])
            env["BLENDER_WORKER_ID_RESOLVED"] = str(worker["id"])
            proc = subprocess.run(
                [str(command), "--transport", "stdio"],
                stdin=None,
                stdout=None,
                stderr=None,
                env=env,
                check=False,
            )
            return int(proc.returncode)


def _manager_from_args(args: argparse.Namespace) -> WorkerManager:
    return WorkerManager(runtime_dir=args.runtime, blender_bin=args.blender_bin)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage isolated native Blender MCP workers")
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

    run = sub.add_parser("run", help="route a Blender Python job to one worker")
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
            result: Any = manager.start(count=args.count, gui_count=args.gui_count, base_port=args.base_port)
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
    except (WorkerBusyError, WorkerError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
