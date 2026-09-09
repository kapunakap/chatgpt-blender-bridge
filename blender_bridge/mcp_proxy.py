from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

from .project_routing import (
    ProjectBusyError,
    ProjectRouter,
    ProjectRoutingError,
    normalize_project_metadata,
)
from .workers import WorkerBusyError, WorkerError, WorkerManager, detect_blender_mcp_command


PROJECT_TOOL_NAMES = {"project_attach", "project_status", "project_detach"}


def project_tools() -> list[dict[str, Any]]:
    identity_properties = {
        "project": {"type": "string", "description": "Human project or feature name."},
        "repo": {"type": "string", "description": "Repository, for example kapunakap/gta-labin."},
        "branch": {"type": "string", "description": "Git branch for this feature/worktree."},
        "worktree": {"type": "string", "description": "Absolute Git worktree path."},
        "blend_path": {
            "type": "string",
            "description": "Optional .blend path. Relative paths are resolved under worktree.",
        },
    }
    return [
        {
            "name": "project_attach",
            "description": (
                "Attach this MCP session to a logical project/worktree. The router reuses the "
                "project's healthy Blender worker when safe, or binds the current isolated worker "
                "on first use. Worker IDs and ports stay internal."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **identity_properties,
                    "open_blend": {
                        "type": "boolean",
                        "default": False,
                        "description": "Open blend_path in the routed worker after attaching.",
                    },
                },
                "anyOf": [
                    {"required": ["worktree"]},
                    {"required": ["repo"]},
                    {"required": ["project"]},
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": "project_status",
            "description": "Show this MCP session's resolved Blender worker and project affinity.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "project_detach",
            "description": (
                "Detach project metadata from this MCP session without destroying the persistent "
                "project-to-worker affinity. The worker lease remains isolated for this session."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def tool_result(request_id: Any, payload: Mapping[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    data = json.loads(json.dumps(payload))
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(data, sort_keys=True)}],
            "structuredContent": data,
            "isError": bool(is_error),
        },
    }


def jsonrpc_error(request_id: Any, message: str, code: int = -32000) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": int(code), "message": str(message)},
    }


class WorkerMcpProxy:
    """One stdio MCP session, one leased Blender worker, with project-aware affinity."""

    def __init__(
        self,
        manager: WorkerManager,
        *,
        mcp_command: str | Path | None = None,
        worker_id: str = "auto",
        startup_metadata: Mapping[str, Any] | None = None,
        startup_open_blend: bool = False,
    ) -> None:
        self.manager = manager
        self.mcp_command = detect_blender_mcp_command(str(mcp_command) if mcp_command else None)
        self.router = ProjectRouter(manager.runtime_dir)
        self.session_id = os.environ.get("BLENDER_SESSION_ID") or f"mcp-pid-{os.getpid()}"
        self.current_project: dict[str, Any] | None = None
        self._lease_context = None
        self.worker: dict[str, Any] | None = None
        self.child: subprocess.Popen[str] | None = None
        self.initialize_request: dict[str, Any] | None = None
        self.initialized_notification: dict[str, Any] | None = None

        self._acquire(worker_id)
        if startup_metadata:
            previous_route = self.router.get(startup_metadata)
            route = self.router.resolve(
                startup_metadata,
                current_worker=self.worker_id,
                statuses=self.manager.status(),
                session_id=self.session_id,
            )
            target = str(route["worker"])
            if target != self.worker_id:
                self._release()
                try:
                    self._acquire(target)
                except Exception:
                    self.router.restore_if_current(
                        startup_metadata,
                        expected_route=route,
                        previous_route=previous_route,
                    )
                    raise
            self.current_project = route
        self._start_child()
        if startup_metadata and startup_open_blend:
            self._open_blend_for_route(self.current_project or {})

    @property
    def worker_id(self) -> str:
        if not self.worker:
            raise WorkerError("MCP worker lease is not active")
        return str(self.worker["id"])

    def _acquire(self, worker_id: str) -> None:
        context = self.manager.lease_worker(worker_id)
        worker = context.__enter__()
        self._lease_context = context
        self.worker = worker

    def _release(self) -> None:
        context = self._lease_context
        self._lease_context = None
        self.worker = None
        if context is not None:
            context.__exit__(None, None, None)

    def _start_child(self) -> None:
        if not self.worker:
            raise WorkerError("cannot start blender-mcp without a worker lease")
        env = os.environ.copy()
        env["BLENDER_MCP_HOST"] = str(self.worker["host"])
        env["BLENDER_MCP_PORT"] = str(self.worker["port"])
        env["BLENDER_WORKER_ID_RESOLVED"] = str(self.worker["id"])
        self.child = subprocess.Popen(
            [str(self.mcp_command), "--transport", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
            env=env,
        )

    def _stop_child(self) -> None:
        child = self.child
        self.child = None
        if child is None:
            return
        if child.stdin:
            with contextlib.suppress(OSError):
                child.stdin.close()
        try:
            child.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                child.terminate()
            try:
                child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    child.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=2.0)

    def close(self) -> None:
        self._stop_child()
        self._release()

    def _write_child(self, message: Mapping[str, Any]) -> None:
        if not self.child or not self.child.stdin or self.child.poll() is not None:
            raise WorkerError("blender-mcp child is not running")
        self.child.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.child.stdin.flush()

    def _read_child_for(self, request_id: Any) -> dict[str, Any]:
        if not self.child or not self.child.stdout:
            raise WorkerError("blender-mcp child stdout is unavailable")
        while True:
            line = self.child.stdout.readline()
            if not line:
                code = self.child.poll()
                raise WorkerError(f"blender-mcp child exited while awaiting response (code={code})")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkerError(f"invalid JSON from blender-mcp child: {exc}") from exc
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
            _write_stdout(message)

    def forward(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        self._write_child(message)
        if "id" not in message:
            return None
        return self._read_child_for(message.get("id"))

    def _reinitialize_child(self) -> None:
        if not self.initialize_request:
            return
        response = self.forward(self.initialize_request)
        if not response or "error" in response:
            raise WorkerError(f"failed to initialize rebound blender-mcp child: {response}")
        if self.initialized_notification:
            self.forward(self.initialized_notification)

    def _rebind(self, target_worker: str) -> None:
        if target_worker == self.worker_id:
            return
        old_worker = self.worker_id
        self._stop_child()
        self._release()
        try:
            self._acquire(target_worker)
            self._start_child()
            self._reinitialize_child()
        except Exception:
            self._stop_child()
            self._release()
            try:
                self._acquire(old_worker)
                self._start_child()
                self._reinitialize_child()
            except Exception as rollback_exc:
                raise WorkerError(
                    f"failed to bind project to {target_worker}; rollback to {old_worker} also failed: "
                    f"{rollback_exc}"
                ) from rollback_exc
            raise

    def _open_blend_for_route(self, route: Mapping[str, Any]) -> dict[str, Any] | None:
        metadata = route.get("metadata")
        if not isinstance(metadata, Mapping):
            return None
        blend_path = metadata.get("blend_path")
        if not blend_path:
            return None
        path = Path(str(blend_path)).expanduser().resolve(strict=False)
        if not path.is_file():
            raise ProjectRoutingError(f"blend_path does not exist: {path}")
        code = (
            "import bpy\n"
            f"_project_blend={json.dumps(str(path))}\n"
            "bpy.ops.wm.open_mainfile(filepath=_project_blend)\n"
            "result={'blend_file':bpy.data.filepath,'scene':bpy.context.scene.name if bpy.context.scene else None}\n"
        )
        response = self.manager.execute_code(self.worker_id, code, timeout=60.0)
        result = response.get("result")
        return result if isinstance(result, dict) else None

    def attach_project(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        metadata = normalize_project_metadata(arguments)
        previous_route = self.router.get(metadata)
        route = self.router.resolve(
            metadata,
            current_worker=self.worker_id,
            statuses=self.manager.status(),
            session_id=self.session_id,
        )
        target = str(route["worker"])
        if target != self.worker_id:
            try:
                self._rebind(target)
            except Exception:
                self.router.restore_if_current(
                    metadata,
                    expected_route=route,
                    previous_route=previous_route,
                )
                raise
        self.current_project = route
        opened = None
        if bool(arguments.get("open_blend", False)):
            opened = self._open_blend_for_route(route)
        return {
            "attached": True,
            "session_id": self.session_id,
            "worker": self.worker_id,
            "project": route,
            "opened": opened,
        }

    def project_status(self) -> dict[str, Any]:
        worker = self.worker or {}
        return {
            "session_id": self.session_id,
            "worker": {
                "id": worker.get("id"),
                "pid": worker.get("pid"),
                "host": worker.get("host"),
                "port": worker.get("port"),
                "mode": worker.get("mode"),
            },
            "project": self.current_project,
        }

    def detach_project(self) -> dict[str, Any]:
        previous = self.current_project
        self.current_project = None
        return {
            "detached": bool(previous),
            "worker": self.worker_id,
            "previous_project": previous,
            "affinity_preserved": True,
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            self.initialize_request = json.loads(json.dumps(message))
            return self.forward(message)

        if method == "notifications/initialized":
            self.initialized_notification = json.loads(json.dumps(message))
            return self.forward(message)

        if method == "tools/list" and "id" in message:
            response = self.forward(message)
            if not response or "error" in response:
                return response
            result = response.get("result")
            if isinstance(result, dict):
                tools = result.get("tools")
                if isinstance(tools, list):
                    result["tools"] = [
                        tool
                        for tool in tools
                        if not (isinstance(tool, dict) and tool.get("name") in PROJECT_TOOL_NAMES)
                    ] + project_tools()
            return response

        if method == "tools/call" and "id" in message:
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            name = params.get("name")
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            if name in PROJECT_TOOL_NAMES:
                try:
                    if name == "project_attach":
                        payload = self.attach_project(arguments)
                    elif name == "project_status":
                        payload = self.project_status()
                    else:
                        payload = self.detach_project()
                    return tool_result(request_id, payload)
                except (ProjectBusyError, ProjectRoutingError, WorkerBusyError, WorkerError) as exc:
                    return tool_result(
                        request_id,
                        {"ok": False, "error": str(exc), "tool": name},
                        is_error=True,
                    )

        return self.forward(message)


def _write_stdout(message: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run_stdio_proxy(proxy: WorkerMcpProxy) -> int:
    try:
        for raw_line in sys.stdin:
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                _write_stdout(jsonrpc_error(None, f"invalid JSON-RPC input: {exc}", code=-32700))
                continue
            if not isinstance(message, dict):
                _write_stdout(jsonrpc_error(None, "JSON-RPC message must be an object", code=-32600))
                continue
            try:
                response = proxy.handle(message)
            except (WorkerBusyError, WorkerError, OSError, json.JSONDecodeError) as exc:
                if "id" in message:
                    response = jsonrpc_error(message.get("id"), str(exc))
                else:
                    print(f"blender-worker-mcp: {exc}", file=sys.stderr)
                    return 1
            if response is not None:
                _write_stdout(response)
        return 0
    finally:
        proxy.close()
