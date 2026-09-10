from __future__ import annotations

import contextlib
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from .project_routing import ProjectRouter, normalize_project_metadata, project_key
from .workers import (
    DEFAULT_BASE_PORT,
    DEFAULT_SINGLE_USER_PORT,
    WorkerBusyError,
    WorkerError,
    WorkerManager,
)

DEFAULT_MIN_WORKERS = 1
DEFAULT_MAX_WORKERS = 5
DEFAULT_IDLE_TIMEOUT_SECONDS = 1200.0
DEFAULT_GC_INTERVAL_SECONDS = 300.0
DEFAULT_JOB_RETENTION_SECONDS = 86400.0
DEFAULT_LOG_RETENTION_SECONDS = 604800.0
DEFAULT_AFFINITY_STALE_SECONDS = 86400.0
MAX_CONTROL_MESSAGE_BYTES = 64 * 1024


class CapacityBusyError(WorkerBusyError):
    """Raised when autoscaling reached the configured worker cap."""


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(data), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise WorkerError(f"{name} must be an integer") from exc


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise WorkerError(f"{name} must be numeric") from exc


def _worker_number(worker_id: str) -> int:
    prefix = "worker-"
    if worker_id.startswith(prefix):
        with contextlib.suppress(ValueError):
            return int(worker_id[len(prefix):])
    return 0


@dataclass(frozen=True)
class SupervisorConfig:
    runtime_dir: Path
    min_workers: int = DEFAULT_MIN_WORKERS
    max_workers: int = DEFAULT_MAX_WORKERS
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    gc_interval_seconds: float = DEFAULT_GC_INTERVAL_SECONDS
    job_retention_seconds: float = DEFAULT_JOB_RETENTION_SECONDS
    log_retention_seconds: float = DEFAULT_LOG_RETENTION_SECONDS
    affinity_stale_seconds: float = DEFAULT_AFFINITY_STALE_SECONDS
    base_port: int = DEFAULT_BASE_PORT
    gui_count: int = 0
    spawn_timeout_seconds: float = 30.0
    control_socket: Path | None = None

    @property
    def socket_path(self) -> Path:
        return self.control_socket or (self.runtime_dir / "supervisor.sock")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        runtime_dir: str | Path | None = None,
    ) -> "SupervisorConfig":
        source = os.environ if env is None else env
        raw_runtime = runtime_dir or source.get("CHATGPT_BLENDER_WORKER_RUNTIME")
        root = (
            Path(raw_runtime).expanduser().resolve()
            if raw_runtime
            else Path.home() / ".cache" / "chatgpt-blender-bridge" / "workers"
        )
        socket_raw = source.get("BLENDER_WORKER_SUPERVISOR_SOCKET")
        config = cls(
            runtime_dir=root,
            min_workers=_env_int(source, "BLENDER_WORKER_MIN_COUNT", DEFAULT_MIN_WORKERS),
            max_workers=_env_int(source, "BLENDER_WORKER_MAX_COUNT", DEFAULT_MAX_WORKERS),
            idle_timeout_seconds=_env_float(
                source, "BLENDER_WORKER_IDLE_TIMEOUT_SECONDS", DEFAULT_IDLE_TIMEOUT_SECONDS
            ),
            gc_interval_seconds=_env_float(
                source, "BLENDER_WORKER_GC_INTERVAL_SECONDS", DEFAULT_GC_INTERVAL_SECONDS
            ),
            job_retention_seconds=_env_float(
                source, "BLENDER_JOB_RETENTION_SECONDS", DEFAULT_JOB_RETENTION_SECONDS
            ),
            log_retention_seconds=_env_float(
                source, "BLENDER_LOG_RETENTION_SECONDS", DEFAULT_LOG_RETENTION_SECONDS
            ),
            affinity_stale_seconds=_env_float(
                source, "BLENDER_PROJECT_AFFINITY_STALE_SECONDS", DEFAULT_AFFINITY_STALE_SECONDS
            ),
            base_port=_env_int(source, "BLENDER_WORKER_BASE_PORT", DEFAULT_BASE_PORT),
            gui_count=_env_int(source, "BLENDER_WORKER_GUI_COUNT", 0),
            spawn_timeout_seconds=_env_float(source, "BLENDER_WORKER_SPAWN_TIMEOUT_SECONDS", 30.0),
            control_socket=Path(socket_raw).expanduser().resolve() if socket_raw else None,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.min_workers < 1:
            raise WorkerError("BLENDER_WORKER_MIN_COUNT must be >= 1")
        if self.max_workers < self.min_workers:
            raise WorkerError("BLENDER_WORKER_MAX_COUNT must be >= BLENDER_WORKER_MIN_COUNT")
        if self.gui_count < 0 or self.gui_count > self.max_workers:
            raise WorkerError("BLENDER_WORKER_GUI_COUNT must be between 0 and max workers")
        for name, value in (
            ("BLENDER_WORKER_IDLE_TIMEOUT_SECONDS", self.idle_timeout_seconds),
            ("BLENDER_WORKER_GC_INTERVAL_SECONDS", self.gc_interval_seconds),
            ("BLENDER_JOB_RETENTION_SECONDS", self.job_retention_seconds),
            ("BLENDER_LOG_RETENTION_SECONDS", self.log_retention_seconds),
            ("BLENDER_PROJECT_AFFINITY_STALE_SECONDS", self.affinity_stale_seconds),
            ("BLENDER_WORKER_SPAWN_TIMEOUT_SECONDS", self.spawn_timeout_seconds),
        ):
            if value < 0:
                raise WorkerError(f"{name} must be >= 0")
        if self.base_port < 1024 or self.base_port + self.max_workers - 1 > 65535:
            raise WorkerError("managed worker port range is outside 1024..65535")
        if self.base_port <= DEFAULT_SINGLE_USER_PORT <= self.base_port + self.max_workers - 1:
            raise WorkerError(
                f"managed worker range must not include legacy port {DEFAULT_SINGLE_USER_PORT}"
            )


class SupervisorClient:
    def __init__(self, socket_path: str | Path, timeout: float = 35.0) -> None:
        self.socket_path = Path(socket_path).expanduser().resolve()
        self.timeout = timeout

    @classmethod
    def for_runtime(
        cls,
        runtime_dir: str | Path,
        *,
        socket_path: str | Path | None = None,
        timeout: float = 35.0,
    ) -> "SupervisorClient":
        root = Path(runtime_dir).expanduser().resolve()
        return cls(socket_path or (root / "supervisor.sock"), timeout=timeout)

    def request(self, op: str, **payload: Any) -> dict[str, Any]:
        request = {"op": op, **payload}
        raw = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.socket_path))
                sock.sendall(raw)
                response = bytearray()
                while b"\n" not in response:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if len(response) > MAX_CONTROL_MESSAGE_BYTES:
                        raise WorkerError("worker supervisor response exceeded 64 KiB")
        except OSError as exc:
            raise WorkerError(
                f"worker autoscaling supervisor unavailable at {self.socket_path}: {exc}"
            ) from exc
        line = bytes(response).split(b"\n", 1)[0]
        if not line:
            raise WorkerError("worker autoscaling supervisor closed without a response")
        try:
            decoded = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError(f"invalid worker supervisor response: {exc}") from exc
        if not isinstance(decoded, dict):
            raise WorkerError("invalid worker supervisor response type")
        if not decoded.get("ok"):
            message = str(decoded.get("error") or "worker supervisor request failed")
            if decoded.get("code") == "capacity_busy":
                raise CapacityBusyError(message)
            raise WorkerError(message)
        return decoded

    def ensure_capacity(
        self, project_metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        response = self.request("ensure_capacity", project=dict(project_metadata or {}))
        worker = response.get("worker")
        if not isinstance(worker, dict) or not worker.get("id"):
            raise WorkerError("worker supervisor returned no candidate worker")
        return worker

    def status(self) -> dict[str, Any]:
        return self.request("status")


class WorkerSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        *,
        manager: WorkerManager | Any | None = None,
        router: ProjectRouter | Any | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        config.validate()
        self.config = config
        self.manager = manager or WorkerManager(runtime_dir=config.runtime_dir)
        self.router = router or ProjectRouter(config.runtime_dir)
        self.now = now
        self.state_path = config.runtime_dir / "supervisor-state.json"
        self.lock_path = config.runtime_dir / "supervisor.lock"
        self._operation_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._idle_since: dict[str, float] = {}
        self._load_supervisor_state()

    def _load_supervisor_state(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        idle = data.get("idle_since") if isinstance(data, dict) else None
        if isinstance(idle, dict):
            for worker_id, value in idle.items():
                with contextlib.suppress(TypeError, ValueError):
                    self._idle_since[str(worker_id)] = float(value)

    def _save_supervisor_state(self) -> None:
        _atomic_json(
            self.state_path,
            {"version": 1, "idle_since": self._idle_since, "updated_at": self.now()},
        )

    def _routes(self) -> list[dict[str, Any]]:
        routes = self.router.list_routes()
        return [dict(item) for item in routes if isinstance(item, Mapping)]

    def _active_claims(self, now: float) -> dict[str, set[str]]:
        claims: dict[str, set[str]] = {}
        cutoff = now - self.config.affinity_stale_seconds
        for route in self._routes():
            worker = str(route.get("worker") or "")
            key = str(route.get("key") or "")
            updated = float(route.get("updated_at") or 0.0)
            if worker and key and updated > cutoff:
                claims.setdefault(worker, set()).add(key)
        return claims

    @staticmethod
    def _status_by_id(statuses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {str(item.get("id")): item for item in statuses if item.get("id")}

    def _project_key(self, metadata: Mapping[str, Any] | None) -> str | None:
        if not metadata:
            return None
        normalized = normalize_project_metadata(metadata)
        return project_key(normalized)

    def _eligible_free(
        self,
        statuses: list[dict[str, Any]],
        *,
        project_key_hint: str | None,
        active_claims: Mapping[str, set[str]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for status in statuses:
            worker_id = str(status.get("id") or "")
            if not worker_id or not status.get("healthy") or status.get("busy"):
                continue
            claims = active_claims.get(worker_id, set())
            if claims and (project_key_hint is None or any(key != project_key_hint for key in claims)):
                continue
            result.append(status)
        result.sort(key=lambda item: (_worker_number(str(item.get("id") or "")), str(item.get("id"))))
        return result

    def _reconcile_workers(self) -> dict[str, list[str]]:
        removed: list[str] = []
        restarted: list[str] = []
        statuses = self.manager.status()
        for status in statuses:
            worker_id = str(status.get("id") or "")
            if not worker_id or status.get("busy"):
                continue
            if not status.get("process_alive"):
                self.manager.stop_worker(worker_id)
                self._idle_since.pop(worker_id, None)
                removed.append(worker_id)
            elif not status.get("healthy"):
                self.manager.restart_worker(worker_id, timeout=self.config.spawn_timeout_seconds)
                self._idle_since.pop(worker_id, None)
                restarted.append(worker_id)
        return {"removed_dead": removed, "restarted_unhealthy": restarted}

    def _assert_busy_modes_compatible(
        self, statuses: list[dict[str, Any]], desired_count: int
    ) -> None:
        for status in statuses:
            if not status.get("busy"):
                continue
            worker_id = str(status.get("id") or "")
            index = _worker_number(worker_id)
            if index < 1 or index > desired_count:
                continue
            desired_mode = "gui" if index <= self.config.gui_count else "background"
            actual_mode = str(status.get("mode") or "")
            if actual_mode and actual_mode != desired_mode:
                raise WorkerBusyError(
                    f"refusing to reconfigure busy {worker_id} from {actual_mode} to {desired_mode}"
                )

    def _ensure_minimum(self) -> list[str]:
        statuses = self.manager.status()
        healthy = [item for item in statuses if item.get("healthy")]
        if len(healthy) >= self.config.min_workers:
            return []
        self._assert_busy_modes_compatible(statuses, self.config.min_workers)
        before = {str(item.get("id")) for item in statuses if item.get("id")}
        self.manager.start(
            count=self.config.min_workers,
            gui_count=min(self.config.gui_count, self.config.min_workers),
            base_port=self.config.base_port,
            timeout=self.config.spawn_timeout_seconds,
        )
        after = {str(item.get("id")) for item in self.manager.status() if item.get("id")}
        return sorted(after - before, key=_worker_number)

    def _spawn_one(self, statuses: list[dict[str, Any]]) -> list[str]:
        live = [item for item in statuses if item.get("process_alive")]
        if len(live) >= self.config.max_workers:
            raise CapacityBusyError(
                f"Blender worker capacity busy: max_workers={self.config.max_workers}"
            )
        desired = min(
            self.config.max_workers,
            max(
                len(live) + 1,
                max((_worker_number(str(item.get("id") or "")) for item in live), default=0),
            ),
        )
        self._assert_busy_modes_compatible(statuses, desired)
        before = {str(item.get("id")) for item in statuses if item.get("id")}
        self.manager.start(
            count=desired,
            gui_count=min(self.config.gui_count, desired),
            base_port=self.config.base_port,
            timeout=self.config.spawn_timeout_seconds,
        )
        after = {str(item.get("id")) for item in self.manager.status() if item.get("id")}
        return sorted(after - before, key=_worker_number)

    def ensure_capacity(
        self, project_metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._operation_lock:
            self._reconcile_workers()
            self._ensure_minimum()
            now = self.now()
            statuses = self.manager.status()
            claims = self._active_claims(now)
            hint = self._project_key(project_metadata)

            free = self._eligible_free(
                statuses, project_key_hint=hint, active_claims=claims
            )
            if free:
                return dict(free[0])

            self._spawn_one(statuses)
            statuses = self.manager.status()
            claims = self._active_claims(now)
            free = self._eligible_free(
                statuses, project_key_hint=hint, active_claims=claims
            )
            if free:
                return dict(free[0])
            raise CapacityBusyError(
                f"Blender worker capacity busy: max_workers={self.config.max_workers}"
            )

    def _refresh_idle(self, statuses: list[dict[str, Any]], now: float) -> None:
        live_ids = {str(item.get("id")) for item in statuses if item.get("id")}
        for worker_id in list(self._idle_since):
            if worker_id not in live_ids:
                self._idle_since.pop(worker_id, None)
        for status in statuses:
            worker_id = str(status.get("id") or "")
            if not worker_id:
                continue
            if status.get("healthy") and not status.get("busy"):
                self._idle_since.setdefault(worker_id, now)
            else:
                self._idle_since.pop(worker_id, None)

    def _scale_down(
        self,
        statuses: list[dict[str, Any]],
        now: float,
        active_claims: Mapping[str, set[str]],
    ) -> list[str]:
        live = [item for item in statuses if item.get("process_alive")]
        excess = max(0, len(live) - self.config.min_workers)
        if excess == 0:
            return []
        candidates: list[tuple[float, int, str]] = []
        for status in live:
            worker_id = str(status.get("id") or "")
            if (
                not worker_id
                or status.get("busy")
                or not status.get("healthy")
                or active_claims.get(worker_id)
            ):
                continue
            idle_since = self._idle_since.get(worker_id, now)
            if now - idle_since < self.config.idle_timeout_seconds:
                continue
            candidates.append((idle_since, -_worker_number(worker_id), worker_id))
        candidates.sort()
        stopped: list[str] = []
        for _, _, worker_id in candidates[:excess]:
            latest = self._status_by_id(self.manager.status()).get(worker_id)
            if not latest or latest.get("busy") or not latest.get("healthy"):
                continue
            if self._active_claims(now).get(worker_id):
                continue
            self.manager.stop_worker(worker_id)
            self._idle_since.pop(worker_id, None)
            stopped.append(worker_id)
        return stopped

    def _gc_stale_affinities(
        self, statuses: list[dict[str, Any]], now: float
    ) -> list[str]:
        state_path = Path(getattr(self.router, "state_path", self.config.runtime_dir / "projects.json"))
        lock_path = Path(getattr(self.router, "lock_path", self.config.runtime_dir / "projects.lock"))
        if not state_path.is_file():
            return []
        busy = {
            str(item.get("id"))
            for item in statuses
            if item.get("id") and item.get("busy")
        }
        cutoff = now - self.config.affinity_stale_seconds
        removed: list[str] = []
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return []
                projects = state.get("projects") if isinstance(state, dict) else None
                if not isinstance(projects, dict):
                    return []
                for key, route in list(projects.items()):
                    if not isinstance(route, dict):
                        continue
                    worker = str(route.get("worker") or "")
                    updated = float(route.get("updated_at") or 0.0)
                    if updated > cutoff or worker in busy:
                        continue
                    projects.pop(key, None)
                    removed.append(str(key))
                if removed:
                    _atomic_json(state_path, state)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return sorted(removed)

    def _gc_jobs(self, statuses: list[dict[str, Any]], now: float) -> list[str]:
        if any(item.get("busy") for item in statuses):
            return []
        jobs_dir = Path(getattr(self.manager, "jobs_dir", self.config.runtime_dir / "jobs"))
        if not jobs_dir.is_dir():
            return []
        removed: list[str] = []
        for job_dir in jobs_dir.iterdir():
            if not job_dir.is_dir() or job_dir.is_symlink():
                continue
            result_file = job_dir / "result.blend"
            marker = job_dir / "job.json"
            completed_at: float | None = None
            if marker.is_file():
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
                if isinstance(data, dict) and data.get("status") in {"completed", "failed"}:
                    with contextlib.suppress(TypeError, ValueError):
                        completed_at = float(data.get("completed_at"))
            if completed_at is None and result_file.is_file():
                with contextlib.suppress(OSError):
                    completed_at = result_file.stat().st_mtime
            if completed_at is None or now - completed_at < self.config.job_retention_seconds:
                continue
            shutil.rmtree(job_dir)
            removed.append(job_dir.name)
        return sorted(removed)

    def _gc_logs(self, statuses: list[dict[str, Any]], now: float) -> list[str]:
        logs_dir = Path(getattr(self.manager, "logs_dir", self.config.runtime_dir / "logs"))
        if not logs_dir.is_dir():
            return []
        active_logs = {
            str(Path(str(item.get("log_path"))).resolve())
            for item in statuses
            if item.get("log_path")
        }
        removed: list[str] = []
        for path in logs_dir.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            if str(path.resolve()) in active_logs:
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age < self.config.log_retention_seconds:
                continue
            path.unlink(missing_ok=True)
            removed.append(path.name)
        return sorted(removed)

    def _gc_session_leases(self, statuses: list[dict[str, Any]], now: float) -> list[str]:
        lease_dir = Path(
            getattr(self.manager, "session_leases_dir", self.config.runtime_dir / "session-leases")
        )
        if not lease_dir.is_dir():
            return []
        known_workers = {str(item.get("id")) for item in statuses if item.get("id")}
        removed: list[str] = []
        for path in lease_dir.glob("*.lock"):
            worker_id = path.stem
            if worker_id in known_workers:
                continue
            try:
                if now - path.stat().st_mtime < self.config.gc_interval_seconds:
                    continue
                handle = path.open("r+", encoding="utf-8")
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed.append(path.name)
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        return sorted(removed)

    def reconcile_and_gc(self) -> dict[str, Any]:
        with self._operation_lock:
            now = self.now()
            reconciliation = self._reconcile_workers()
            started = self._ensure_minimum()
            statuses = self.manager.status()
            self._refresh_idle(statuses, now)
            claims = self._active_claims(now)
            stopped = self._scale_down(statuses, now, claims)
            statuses = self.manager.status()
            stale_affinities = self._gc_stale_affinities(statuses, now)
            jobs = self._gc_jobs(statuses, now)
            logs = self._gc_logs(statuses, now)
            leases = self._gc_session_leases(statuses, now)
            self._save_supervisor_state()
            return {
                "ok": True,
                "time": now,
                "reconciliation": reconciliation,
                "started_for_minimum": started,
                "scaled_down": stopped,
                "gc": {
                    "stale_affinities": stale_affinities,
                    "jobs": jobs,
                    "logs": logs,
                    "session_leases": leases,
                },
                "workers": self.manager.status(),
            }

    def status(self) -> dict[str, Any]:
        with self._operation_lock:
            return {
                "ok": True,
                "socket": str(self.config.socket_path),
                "config": {
                    "min_workers": self.config.min_workers,
                    "max_workers": self.config.max_workers,
                    "idle_timeout_seconds": self.config.idle_timeout_seconds,
                    "gc_interval_seconds": self.config.gc_interval_seconds,
                    "base_port": self.config.base_port,
                    "gui_count": self.config.gui_count,
                },
                "workers": self.manager.status(),
                "projects": self._routes(),
            }

    def handle_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        try:
            if op == "ensure_capacity":
                project = request.get("project")
                metadata = project if isinstance(project, Mapping) else None
                return {"ok": True, "worker": self.ensure_capacity(metadata)}
            if op == "status":
                return self.status()
            if op in {"reconcile", "gc"}:
                return self.reconcile_and_gc()
            return {"ok": False, "code": "invalid_request", "error": f"unknown operation: {op!r}"}
        except CapacityBusyError as exc:
            return {"ok": False, "code": "capacity_busy", "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "code": "supervisor_error", "error": str(exc)}

    def _serve_connection(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        data = bytearray()
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_CONTROL_MESSAGE_BYTES:
                response = {"ok": False, "code": "invalid_request", "error": "request exceeded 64 KiB"}
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
                return
        line = bytes(data).split(b"\n", 1)[0]
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            response = self.handle_request(request)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            response = {"ok": False, "code": "invalid_request", "error": str(exc)}
        conn.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))

    def stop(self) -> None:
        self._stop_event.set()

    def serve_forever(self) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(PermissionError):
            os.chmod(self.config.runtime_dir, 0o700)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        with self.lock_path.open("r+", encoding="utf-8") as lock_handle:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkerError("another Blender worker supervisor is already running") from exc

            self.reconcile_and_gc()
            socket_path = self.config.socket_path
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            if socket_path.exists():
                socket_path.unlink()

            previous_handlers: dict[int, Any] = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    previous_handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, lambda *_: self.stop())

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(socket_path))
                os.chmod(socket_path, 0o600)
                server.listen(64)
                server.settimeout(min(max(self.config.gc_interval_seconds, 0.25), 1.0))
                next_gc = time.monotonic() + self.config.gc_interval_seconds
                while not self._stop_event.is_set():
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        conn = None
                    if conn is not None:
                        with conn:
                            self._serve_connection(conn)
                    if time.monotonic() >= next_gc:
                        self.reconcile_and_gc()
                        next_gc = time.monotonic() + self.config.gc_interval_seconds
            finally:
                server.close()
                with contextlib.suppress(FileNotFoundError):
                    socket_path.unlink()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
