from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterable, Mapping

from .workers import WorkerBusyError, WorkerError


PROJECT_STATE_VERSION = 1
PROJECT_METADATA_KEYS = ("project", "repo", "branch", "worktree", "blend_path")


class ProjectRoutingError(WorkerError):
    """Base project-routing error."""


class ProjectBusyError(WorkerBusyError, ProjectRoutingError):
    """Raised when a project is already attached to a busy worker."""


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


def normalize_project_metadata(raw: Mapping[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key in PROJECT_METADATA_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            metadata[key] = text

    worktree = metadata.get("worktree")
    worktree_root: Path | None = None
    if worktree:
        worktree_root = Path(worktree).expanduser().resolve(strict=False)
        metadata["worktree"] = str(worktree_root)

    blend_path = metadata.get("blend_path")
    if blend_path:
        path = Path(blend_path).expanduser()
        if not path.is_absolute():
            if worktree_root is None:
                raise ProjectRoutingError("relative blend_path requires worktree")
            path = (worktree_root / path).resolve(strict=False)
            try:
                path.relative_to(worktree_root)
            except ValueError as exc:
                raise ProjectRoutingError("relative blend_path must stay underneath worktree") from exc
        else:
            path = path.resolve(strict=False)
        metadata["blend_path"] = str(path)

    if not any(metadata.get(key) for key in ("worktree", "repo", "project")):
        raise ProjectRoutingError("project metadata requires worktree, repo, or project")
    return metadata


def project_key(metadata: Mapping[str, str]) -> str:
    if metadata.get("worktree"):
        return f"worktree:{metadata['worktree']}"
    if metadata.get("repo") and metadata.get("branch"):
        return f"repo:{metadata['repo']}#branch:{metadata['branch']}"
    if metadata.get("repo") and metadata.get("project"):
        return f"repo:{metadata['repo']}#project:{metadata['project']}"
    if metadata.get("repo"):
        return f"repo:{metadata['repo']}"
    return f"project:{metadata['project']}"


def metadata_from_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    raw = {
        "project": source.get("BLENDER_PROJECT"),
        "repo": source.get("BLENDER_PROJECT_REPO"),
        "branch": source.get("BLENDER_PROJECT_BRANCH"),
        "worktree": source.get("BLENDER_PROJECT_WORKTREE"),
        "blend_path": source.get("BLENDER_PROJECT_BLEND_PATH"),
    }
    if not any(raw.values()):
        return {}
    return normalize_project_metadata(raw)


class ProjectRouter:
    """Persistent project -> worker affinity stored inside the private worker runtime."""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.runtime_dir / "projects.json"
        self.lock_path = self.runtime_dir / "projects.lock"
        with contextlib.suppress(PermissionError):
            os.chmod(self.runtime_dir, 0o700)

    @contextlib.contextmanager
    def _locked_state(self):
        self.lock_path.touch(mode=0o600, exist_ok=True)
        with self.lock_path.open("r+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            state: dict[str, Any] = {"version": PROJECT_STATE_VERSION, "projects": {}}
            if self.state_path.exists():
                try:
                    loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        state = loaded
                except (json.JSONDecodeError, OSError):
                    pass
            state["version"] = PROJECT_STATE_VERSION
            state.setdefault("projects", {})
            yield state
            _atomic_json(self.state_path, state)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _status_by_id(statuses: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
        return {str(item.get("id")): item for item in statuses if item.get("id")}

    @staticmethod
    def _claims_by_worker(projects: Mapping[str, Any]) -> dict[str, set[str]]:
        claims: dict[str, set[str]] = {}
        for key, route in projects.items():
            if not isinstance(route, Mapping):
                continue
            worker = str(route.get("worker") or "")
            if worker:
                claims.setdefault(worker, set()).add(str(key))
        return claims

    @staticmethod
    def _other_claims(claims: Mapping[str, set[str]], worker: str, key: str) -> list[str]:
        return sorted(item for item in claims.get(worker, set()) if item != key)

    def _select_unclaimed_worker(
        self,
        *,
        key: str,
        current_worker: str,
        status_by_id: Mapping[str, Mapping[str, Any]],
        claims: Mapping[str, set[str]],
    ) -> str:
        current_status = status_by_id.get(current_worker)
        if (
            current_status
            and bool(current_status.get("healthy"))
            and not self._other_claims(claims, current_worker, key)
        ):
            return current_worker

        for worker_id, status in sorted(status_by_id.items()):
            if worker_id == current_worker:
                continue
            if not bool(status.get("healthy")) or bool(status.get("busy")):
                continue
            if self._other_claims(claims, worker_id, key):
                continue
            return worker_id

        raise ProjectBusyError(f"project {key} has no unclaimed healthy worker available")

    def resolve(
        self,
        raw_metadata: Mapping[str, Any],
        *,
        current_worker: str,
        statuses: Iterable[Mapping[str, Any]],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        metadata = normalize_project_metadata(raw_metadata)
        key = project_key(metadata)
        status_by_id = self._status_by_id(statuses)
        now = time.time()

        with self._locked_state() as state:
            projects = state["projects"]
            existing = projects.get(key)
            claims = self._claims_by_worker(projects)
            if not isinstance(existing, dict):
                target_worker = self._select_unclaimed_worker(
                    key=key,
                    current_worker=current_worker,
                    status_by_id=status_by_id,
                    claims=claims,
                )
                route = {
                    "key": key,
                    "worker": target_worker,
                    "metadata": metadata,
                    "updated_at": now,
                }
                if session_id:
                    route["last_session"] = session_id
                projects[key] = route
                return json.loads(json.dumps(route))

            target_worker = str(existing.get("worker") or "")
            target_status = status_by_id.get(target_worker)
            if target_status and bool(target_status.get("healthy")):
                other_claims = self._other_claims(claims, target_worker, key)
                if other_claims:
                    raise ProjectBusyError(
                        f"project {key} worker {target_worker} is also claimed by {', '.join(other_claims)}"
                    )
                if target_worker == current_worker:
                    existing["metadata"] = metadata
                    existing["updated_at"] = now
                    if session_id:
                        existing["last_session"] = session_id
                    return json.loads(json.dumps(existing))
                if bool(target_status.get("busy")):
                    raise ProjectBusyError(
                        f"project {key} is already attached to busy worker {target_worker}"
                    )
                existing["metadata"] = metadata
                existing["updated_at"] = now
                if session_id:
                    existing["last_session"] = session_id
                return json.loads(json.dumps(existing))

            replacement_worker = self._select_unclaimed_worker(
                key=key,
                current_worker=current_worker,
                status_by_id=status_by_id,
                claims=claims,
            )
            replacement = {
                "key": key,
                "worker": replacement_worker,
                "metadata": metadata,
                "updated_at": now,
                "recovered_from_worker": target_worker or None,
            }
            if session_id:
                replacement["last_session"] = session_id
            projects[key] = replacement
            return json.loads(json.dumps(replacement))

    def restore_if_current(
        self,
        raw_metadata: Mapping[str, Any],
        *,
        expected_route: Mapping[str, Any],
        previous_route: Mapping[str, Any] | None,
    ) -> bool:
        metadata = normalize_project_metadata(raw_metadata)
        key = project_key(metadata)
        expected = json.loads(json.dumps(expected_route))
        previous = json.loads(json.dumps(previous_route)) if previous_route is not None else None
        with self._locked_state() as state:
            projects = state["projects"]
            current = projects.get(key)
            if not isinstance(current, dict) or current != expected:
                return False
            if previous is None:
                projects.pop(key, None)
            else:
                projects[key] = previous
            return True

    def get(self, raw_metadata: Mapping[str, Any]) -> dict[str, Any] | None:
        metadata = normalize_project_metadata(raw_metadata)
        key = project_key(metadata)
        with self._locked_state() as state:
            route = state["projects"].get(key)
            return json.loads(json.dumps(route)) if isinstance(route, dict) else None

    def list_routes(self) -> list[dict[str, Any]]:
        with self._locked_state() as state:
            routes = []
            for key, route in sorted(state["projects"].items()):
                if isinstance(route, dict):
                    item = json.loads(json.dumps(route))
                    item.setdefault("key", key)
                    routes.append(item)
            return routes
