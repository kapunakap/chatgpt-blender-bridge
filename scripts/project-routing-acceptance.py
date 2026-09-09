#!/usr/bin/env python3
'''Real-Blender acceptance for Issue #7 automatic project-aware routing.'''

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blender_bridge.workers import (  # noqa: E402
    DEFAULT_SINGLE_USER_PORT,
    WorkerManager,
    detect_blender_bin,
    detect_blender_mcp_command,
)


def find_base_port(count: int) -> int:
    for _ in range(200):
        base = random.randint(14000, 26000)
        if base <= DEFAULT_SINGLE_USER_PORT <= base + count - 1:
            continue
        sockets: list[socket.socket] = []
        try:
            for port in range(base, base + count):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", port))
                sockets.append(sock)
            return base
        except OSError:
            pass
        finally:
            for sock in sockets:
                sock.close()
    raise RuntimeError("could not find free consecutive worker ports")


def write_probe(path: Path) -> Path:
    path.write_text(
        r'''import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def structured(response):
    value = getattr(response, "structuredContent", None)
    if isinstance(value, dict):
        return value
    content = getattr(response, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


async def main() -> None:
    wrapper, runtime, mcp_command, blender, base_port, project, branch, worktree = sys.argv[1:]
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            wrapper,
            "--runtime", runtime,
            "--worker", "auto",
            "--mcp-command", mcp_command,
            "--blender-bin", blender,
            "--ensure-count", "3",
            "--gui-count", "3",
            "--base-port", base_port,
        ],
        env=None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            required = {"project_attach", "project_status", "project_detach", "execute_blender_code"}
            missing = sorted(required - names)
            if missing:
                raise RuntimeError(f"missing tools: {missing}")

            attach = await session.call_tool(
                "project_attach",
                {
                    "project": project,
                    "repo": "kapunakap/gta-labin",
                    "branch": branch,
                    "worktree": worktree,
                    "blend_path": "assets/source/city.blend",
                },
            )
            if bool(getattr(attach, "isError", False)):
                raise RuntimeError(f"project_attach failed: {attach}")
            attach_data = structured(attach)

            marker_code = (
                "import bpy, os\n"
                f"bpy.context.scene['project_marker']={json.dumps(project)}\n"
                "result={'pid':os.getpid(),'marker':bpy.context.scene.get('project_marker'),"
                "'blend_file':bpy.data.filepath}\n"
            )
            first = await session.call_tool("execute_blender_code", {"code": marker_code})
            second = await session.call_tool(
                "execute_blender_code",
                {
                    "code": (
                        "import bpy, os\n"
                        "result={'pid':os.getpid(),'marker':bpy.context.scene.get('project_marker'),"
                        "'blend_file':bpy.data.filepath}\n"
                    )
                },
            )
            status = await session.call_tool("project_status", {})
            if any(bool(getattr(item, "isError", False)) for item in (first, second, status)):
                raise RuntimeError("one or more routed tool calls failed")

            first_data = structured(first).get("result", {})
            second_data = structured(second).get("result", {})
            status_data = structured(status)
            project_route = attach_data.get("project") or {}
            metadata = project_route.get("metadata") or {}

            print(json.dumps({
                "project": project,
                "worker": attach_data.get("worker"),
                "pid_first": first_data.get("pid"),
                "pid_second": second_data.get("pid"),
                "marker": second_data.get("marker"),
                "worktree": metadata.get("worktree"),
                "blend_path": metadata.get("blend_path"),
                "status_worker": (status_data.get("worker") or {}).get("id"),
            }, sort_keys=True))


asyncio.run(main())
''',
        encoding="utf-8",
    )
    return path


def run_probe(
    mcp_python: Path,
    probe: Path,
    wrapper: Path,
    runtime: Path,
    mcp_command: Path,
    blender: Path,
    base_port: int,
    project: str,
    branch: str,
    worktree: Path,
) -> dict[str, object]:
    proc = subprocess.run(
        [
            str(mcp_python),
            str(probe),
            str(wrapper),
            str(runtime),
            str(mcp_command),
            str(blender),
            str(base_port),
            project,
            branch,
            str(worktree),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{project} probe failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    blender = detect_blender_bin()
    mcp_command = detect_blender_mcp_command()
    mcp_python = mcp_command.parent / "python"
    if not mcp_python.is_file():
        raise RuntimeError(f"MCP environment Python not found next to {mcp_command}")

    wrapper = ROOT / "scripts" / "blender-worker-mcp.py"
    base_port = find_base_port(3)
    report: dict[str, object] = {
        "base_port": base_port,
        "single_user_port": DEFAULT_SINGLE_USER_PORT,
        "blender": str(blender),
        "mcp_command": str(mcp_command),
    }

    with tempfile.TemporaryDirectory(prefix="chatgpt-blender-project-routing-") as raw:
        temp = Path(raw)
        runtime = temp / "runtime"
        probe = write_probe(temp / "project_probe.py")
        projects = [
            ("Kapelica", "feat/kapelica-district", temp / "kapelica"),
            ("Rasa", "feat/rasa-mining-town", temp / "rasa"),
            ("Plomin", "feat/plomin-coastal-town", temp / "plomin"),
        ]
        for _, _, worktree in projects:
            (worktree / "assets/source").mkdir(parents=True)

        manager = WorkerManager(runtime_dir=runtime, blender_bin=blender)
        try:
            started = manager.start(count=3, gui_count=3, base_port=base_port, timeout=45)
            assert len(started) == 3
            assert all(item["healthy"] and item["mode"] == "gui" for item in started)
            assert len({int(item["pid"]) for item in started}) == 3
            assert all(int(item["port"]) != DEFAULT_SINGLE_USER_PORT for item in started)

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    pool.submit(
                        run_probe,
                        mcp_python,
                        probe,
                        wrapper,
                        runtime,
                        mcp_command,
                        blender,
                        base_port,
                        project,
                        branch,
                        worktree,
                    )
                    for project, branch, worktree in projects
                ]
                first_round = [future.result() for future in futures]

            pids = [int(item["pid_first"]) for item in first_round]
            workers = [str(item["worker"]) for item in first_round]
            assert len(set(pids)) == 3
            assert len(set(workers)) == 3
            for item in first_round:
                assert item["pid_first"] == item["pid_second"]
                assert item["worker"] == item["status_worker"]
                assert item["marker"] == item["project"]

            blend_paths = [str(item["blend_path"]) for item in first_round]
            assert len(set(blend_paths)) == 3
            assert all(path.endswith("/assets/source/city.blend") for path in blend_paths)
            report["parallel_project_sessions"] = first_round
            report["relative_blend_paths_isolated"] = True

            kapelica = next(item for item in first_round if item["project"] == "Kapelica")
            kapelica_project = projects[0]
            reused = run_probe(
                mcp_python,
                probe,
                wrapper,
                runtime,
                mcp_command,
                blender,
                base_port,
                *kapelica_project,
            )
            assert reused["worker"] == kapelica["worker"]
            assert reused["pid_first"] == kapelica["pid_first"]
            report["sticky_reuse"] = {
                "worker": reused["worker"],
                "pid": reused["pid_first"],
                "same_worker": True,
            }

            victim_worker = str(reused["worker"])
            victim_pid = int(reused["pid_first"])
            before_recovery = {str(item["id"]): int(item["pid"]) for item in manager.status()}
            manager.kill_worker(victim_worker)
            time.sleep(0.4)

            recovered = run_probe(
                mcp_python,
                probe,
                wrapper,
                runtime,
                mcp_command,
                blender,
                base_port,
                *kapelica_project,
            )
            assert int(recovered["pid_first"]) != victim_pid
            after_recovery = {str(item["id"]): int(item["pid"]) for item in manager.status()}
            for worker_id, pid in before_recovery.items():
                if worker_id != victim_worker:
                    assert after_recovery[worker_id] == pid
            report["dead_affinity_recovery"] = {
                "old_worker": victim_worker,
                "old_pid": victim_pid,
                "new_worker": recovered["worker"],
                "new_pid": recovered["pid_first"],
                "unrelated_worker_pids_unchanged": True,
            }

            report["manual_and_legacy_note"] = (
                "Run scripts/multi-worker-acceptance.py after this gate to re-prove manual --worker "
                "routing and that managed workers do not consume the legacy 9876 endpoint."
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        finally:
            manager.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
