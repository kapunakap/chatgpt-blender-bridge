#!/usr/bin/env python3
'''Real-Blender acceptance for Issue #7 automatic project-aware routing.'''

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import select
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
    wrapper, runtime, mcp_command, blender, base_port, project, branch, worktree, mode = sys.argv[1:]
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

            status_before = await session.call_tool("project_status", {})
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

            if mode == "expect_busy":
                if not bool(getattr(attach, "isError", False)):
                    raise RuntimeError(f"busy project_attach unexpectedly succeeded: {attach}")
                status_after = await session.call_tool("project_status", {})
                before_data = structured(status_before)
                after_data = structured(status_after)
                print(json.dumps({
                    "project": project,
                    "busy_error": structured(attach).get("error"),
                    "worker_before": (before_data.get("worker") or {}).get("id"),
                    "worker_after": (after_data.get("worker") or {}).get("id"),
                    "project_after": after_data.get("project"),
                }, sort_keys=True))
                return

            if bool(getattr(attach, "isError", False)):
                raise RuntimeError(f"project_attach failed: {attach}")
            attach_data = structured(attach)
            project_route = attach_data.get("project") or {}
            metadata = project_route.get("metadata") or {}
            blend_path = metadata.get("blend_path")
            if not blend_path:
                raise RuntimeError("resolved project metadata has no blend_path")

            marker_name = f"ROUTING_MARKER_{project}"
            wants_edit = project == "Kapelica"
            marker_code = (
                "import bpy, os\n"
                f"_marker={json.dumps(marker_name)}\n"
                f"_project={json.dumps(project)}\n"
                f"_blend={json.dumps(str(blend_path))}\n"
                "_before_markers=sorted(obj.name for obj in bpy.data.objects if obj.name.startswith('ROUTING_MARKER_'))\n"
                "_before_selected=sorted(obj.name for obj in bpy.context.selected_objects)\n"
                "_before_mode=bpy.context.mode\n"
                "if bpy.context.mode != 'OBJECT':\n"
                "    bpy.ops.object.mode_set(mode='OBJECT')\n"
                "for _selected in list(bpy.context.selected_objects):\n"
                "    _selected.select_set(False)\n"
                "_obj=bpy.data.objects.get(_marker)\n"
                "if _obj is None:\n"
                "    _mesh=bpy.data.meshes.new(_marker+'_mesh')\n"
                "    _obj=bpy.data.objects.new(_marker,_mesh)\n"
                "    bpy.context.scene.collection.objects.link(_obj)\n"
                "_obj.select_set(True)\n"
                "bpy.context.view_layer.objects.active=_obj\n"
                "bpy.context.scene['project_marker']=_project\n"
                + ("bpy.ops.object.mode_set(mode='EDIT')\n" if wants_edit else "")
                + "_mode_before_save=bpy.context.mode\n"
                "bpy.ops.wm.save_as_mainfile(filepath=_blend)\n"
                "result={'pid':os.getpid(),'marker':bpy.context.scene.get('project_marker'),"
                "'blend_file':bpy.data.filepath,'physical_file':os.path.isfile(_blend),"
                "'physical_size':os.path.getsize(_blend) if os.path.isfile(_blend) else 0,"
                "'before_markers':_before_markers,'before_selected':_before_selected,"
                "'before_mode':_before_mode,'mode_before_save':_mode_before_save,"
                "'mode':bpy.context.mode,'selected':sorted(obj.name for obj in bpy.context.selected_objects),"
                "'routing_markers':sorted(obj.name for obj in bpy.data.objects if obj.name.startswith('ROUTING_MARKER_'))}\n"
            )
            first = await session.call_tool("execute_blender_code", {"code": marker_code})
            second = await session.call_tool(
                "execute_blender_code",
                {
                    "code": (
                        "import bpy, os\n"
                        "result={'pid':os.getpid(),'marker':bpy.context.scene.get('project_marker'),"
                        "'blend_file':bpy.data.filepath,'mode':bpy.context.mode,"
                        "'selected':sorted(obj.name for obj in bpy.context.selected_objects),"
                        "'routing_markers':sorted(obj.name for obj in bpy.data.objects if obj.name.startswith('ROUTING_MARKER_'))}\n"
                    )
                },
            )
            status = await session.call_tool("project_status", {})
            if any(bool(getattr(item, "isError", False)) for item in (first, second, status)):
                raise RuntimeError("one or more routed tool calls failed")

            first_data = structured(first).get("result", {})
            second_data = structured(second).get("result", {})
            status_data = structured(status)
            before_data = structured(status_before)

            print(json.dumps({
                "project": project,
                "marker_object": marker_name,
                "initial_worker": (before_data.get("worker") or {}).get("id"),
                "worker": attach_data.get("worker"),
                "pid_first": first_data.get("pid"),
                "pid_second": second_data.get("pid"),
                "marker": second_data.get("marker"),
                "worktree": metadata.get("worktree"),
                "blend_path": metadata.get("blend_path"),
                "blend_file": second_data.get("blend_file"),
                "physical_file": first_data.get("physical_file"),
                "physical_size": first_data.get("physical_size"),
                "before_markers": first_data.get("before_markers"),
                "before_selected": first_data.get("before_selected"),
                "before_mode": first_data.get("before_mode"),
                "mode_before_save": first_data.get("mode_before_save"),
                "mode": second_data.get("mode"),
                "selected": second_data.get("selected"),
                "routing_markers": second_data.get("routing_markers"),
                "status_worker": (status_data.get("worker") or {}).get("id"),
            }, sort_keys=True))


asyncio.run(main())
''',
        encoding="utf-8",
    )
    return path


def write_holder(path: Path) -> Path:
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


async def snapshot(session, event):
    status = await session.call_tool("project_status", {})
    state = await session.call_tool(
        "execute_blender_code",
        {"code": "import bpy, os\nresult={'pid':os.getpid(),'marker':bpy.context.scene.get('project_marker'),'mode':bpy.context.mode,'selected':sorted(obj.name for obj in bpy.context.selected_objects)}\n"},
    )
    if bool(getattr(status, "isError", False)) or bool(getattr(state, "isError", False)):
        raise RuntimeError("holder snapshot failed")
    status_data = structured(status)
    state_data = structured(state).get("result", {})
    return {
        "event": event,
        "worker": (status_data.get("worker") or {}).get("id"),
        "pid": state_data.get("pid"),
        "marker": state_data.get("marker"),
        "mode": state_data.get("mode"),
        "selected": state_data.get("selected"),
    }


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
                raise RuntimeError(f"holder project_attach failed: {attach}")
            print(json.dumps(await snapshot(session, "ready"), sort_keys=True), flush=True)
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line or line.strip() == "close":
                    return
                if line.strip() == "probe":
                    print(json.dumps(await snapshot(session, "probe"), sort_keys=True), flush=True)


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
    mode: str = "normal",
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
            mode,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{project} probe failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _read_holder_line(proc: subprocess.Popen[str], timeout: float = 60.0) -> dict[str, object]:
    if proc.stdout is None:
        raise RuntimeError("holder stdout unavailable")
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError("timed out waiting for holder output")
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        raise RuntimeError(f"holder exited before response: {stderr}")
    return json.loads(line)


def start_holder(
    mcp_python: Path,
    holder: Path,
    wrapper: Path,
    runtime: Path,
    mcp_command: Path,
    blender: Path,
    base_port: int,
    project: str,
    branch: str,
    worktree: Path,
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    proc = subprocess.Popen(
        [
            str(mcp_python),
            str(holder),
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
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    return proc, _read_holder_line(proc)


def holder_command(proc: subprocess.Popen[str], command: str) -> dict[str, object]:
    if proc.stdin is None:
        raise RuntimeError("holder stdin unavailable")
    proc.stdin.write(command + "\n")
    proc.stdin.flush()
    return _read_holder_line(proc)


def stop_holder(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if proc.stdin is not None:
        try:
            proc.stdin.write("close\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass
    try:
        proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)


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
        holder = write_holder(temp / "project_holder.py")
        projects = [
            ("Kapelica", "feat/kapelica-district", temp / "kapelica"),
            ("Rasa", "feat/rasa-mining-town", temp / "rasa"),
            ("Plomin", "feat/plomin-coastal-town", temp / "plomin"),
        ]
        for _, _, worktree in projects:
            (worktree / "assets/source").mkdir(parents=True)

        manager = WorkerManager(runtime_dir=runtime, blender_bin=blender)
        holders: list[subprocess.Popen[str]] = []
        try:
            started = manager.start(count=3, gui_count=3, base_port=base_port, timeout=45)
            assert len(started) == 3
            assert all(item["healthy"] and item["mode"] == "gui" for item in started)
            assert len({int(item["pid"]) for item in started}) == 3
            assert len({int(item["port"]) for item in started}) == 3
            assert all(int(item["port"]) != DEFAULT_SINGLE_USER_PORT for item in started)
            report["workers"] = [
                {"id": item["id"], "pid": item["pid"], "host": item["host"], "port": item["port"]}
                for item in started
            ]

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
            all_marker_names = {str(item["marker_object"]) for item in first_round}
            for item in first_round:
                marker = str(item["marker_object"])
                assert item["pid_first"] == item["pid_second"]
                assert item["worker"] == item["status_worker"]
                assert item["marker"] == item["project"]
                assert item["before_markers"] == []
                assert item["routing_markers"] == [marker]
                assert item["selected"] == [marker]
                expected_mode = "EDIT_MESH" if item["project"] == "Kapelica" else "OBJECT"
                assert item["mode_before_save"] == expected_mode
                assert item["mode"] == expected_mode
                assert not (set(item["routing_markers"]) & (all_marker_names - {marker}))
                assert bool(item["physical_file"])
                assert int(item["physical_size"]) > 0

            blend_paths = [str(item["blend_path"]) for item in first_round]
            assert len(set(blend_paths)) == 3
            assert all(path.endswith("/assets/source/city.blend") for path in blend_paths)
            physical_files = []
            for item in first_round:
                path = Path(str(item["blend_path"]))
                assert path.is_file()
                assert path.stat().st_size == int(item["physical_size"])
                with path.open("rb") as handle:
                    magic = handle.read(7)
                assert (
                    magic.startswith(b"BLENDER")
                    or magic.startswith(b"\x1f\x8b")
                    or magic.startswith(b"\x28\xb5\x2f\xfd")
                )
                assert str(path.resolve()) == str(item["blend_file"])
                physical_files.append(
                    {"path": str(path.resolve()), "size": path.stat().st_size, "magic": magic.hex()}
                )
            report["parallel_project_sessions"] = first_round
            report["relative_blend_paths_isolated"] = True
            report["physical_blend_files"] = physical_files
            report["scene_selection_mode_isolated"] = True

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
            assert reused["routing_markers"] == [kapelica["marker_object"]]
            report["sticky_reuse"] = {
                "worker": reused["worker"],
                "pid": reused["pid_first"],
                "same_worker": True,
            }

            kap_holder, kap_ready = start_holder(
                mcp_python,
                holder,
                wrapper,
                runtime,
                mcp_command,
                blender,
                base_port,
                *kapelica_project,
            )
            holders.append(kap_holder)
            assert kap_ready["worker"] == kapelica["worker"]
            busy = run_probe(
                mcp_python,
                probe,
                wrapper,
                runtime,
                mcp_command,
                blender,
                base_port,
                *kapelica_project,
                mode="expect_busy",
            )
            assert busy["busy_error"]
            assert "busy" in str(busy["busy_error"]).lower()
            assert busy["worker_before"] == busy["worker_after"]
            assert busy["project_after"] is None
            report["busy_affinity_fail_closed"] = busy
            stop_holder(kap_holder)
            holders.remove(kap_holder)

            rasa_project = projects[1]
            rasa = next(item for item in first_round if item["project"] == "Rasa")
            rasa_holder, rasa_ready = start_holder(
                mcp_python,
                holder,
                wrapper,
                runtime,
                mcp_command,
                blender,
                base_port,
                *rasa_project,
            )
            holders.append(rasa_holder)
            assert rasa_ready["worker"] == rasa["worker"]
            assert rasa_ready["pid"] == rasa["pid_first"]
            assert rasa_ready["marker"] == "Rasa"

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
            assert recovered["worker"] == victim_worker
            rasa_after = holder_command(rasa_holder, "probe")
            assert rasa_after["worker"] == rasa_ready["worker"]
            assert rasa_after["pid"] == rasa_ready["pid"]
            assert rasa_after["marker"] == "Rasa"
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
                "unrelated_active_session": {
                    "worker": rasa_after["worker"],
                    "pid": rasa_after["pid"],
                    "marker": rasa_after["marker"],
                },
            }
            stop_holder(rasa_holder)
            holders.remove(rasa_holder)

            report["manual_and_legacy_note"] = (
                "Run scripts/multi-worker-acceptance.py after this gate to re-prove manual --worker "
                "routing and that managed workers do not consume the legacy 9876 endpoint."
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        finally:
            for proc in holders:
                stop_holder(proc)
            manager.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
