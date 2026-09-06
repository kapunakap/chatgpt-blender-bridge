#!/usr/bin/env python3
"""Real-Blender acceptance for Issue #4 multi-worker isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
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
    SourceBusyError,
    WorkerManager,
    detect_blender_bin,
    detect_blender_mcp_command,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_base_port(count: int) -> int:
    for _ in range(200):
        base = random.randint(14000, 26000)
        if base <= DEFAULT_SINGLE_USER_PORT <= base + count - 1:
            continue
        sockets = []
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
    raise RuntimeError("could not find 3 free consecutive loopback ports")


def create_blend(blender: Path, helper: Path, output: Path, object_name: str) -> None:
    proc = subprocess.run(
        [str(blender), "--background", "--factory-startup", "--python", str(helper), "--", str(output), object_name],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0 or not output.is_file():
        raise RuntimeError(f"failed to create fixture {output}: {proc.stderr[-3000:]}")


def write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def main() -> int:
    blender = detect_blender_bin()
    base_port = find_base_port(3)
    report: dict[str, object] = {
        "blender_bin": str(blender),
        "base_port": base_port,
        "single_user_port": DEFAULT_SINGLE_USER_PORT,
    }

    with tempfile.TemporaryDirectory(prefix="chatgpt-blender-workers-") as temp_raw:
        temp = Path(temp_raw)
        runtime = temp / "runtime"
        fixtures = temp / "fixtures"
        scripts = temp / "scripts"
        fixtures.mkdir()
        scripts.mkdir()

        creator = write(
            scripts / "create_fixture.py",
            """import bpy, sys\nfrom pathlib import Path\nargs=sys.argv[sys.argv.index('--')+1:]\nout=Path(args[0]); name=args[1]\nbpy.ops.wm.read_factory_settings(use_empty=True)\nbpy.ops.mesh.primitive_cube_add()\nobj=bpy.context.active_object; obj.name=name\nbpy.context.scene['fixture_name']=name\nbpy.ops.wm.save_as_mainfile(filepath=str(out))\n""",
        )
        source_a = fixtures / "source-a.blend"
        source_b = fixtures / "source-b.blend"
        source_c = fixtures / "source-c.blend"
        create_blend(blender, creator, source_a, "SourceA")
        create_blend(blender, creator, source_b, "SourceB")
        create_blend(blender, creator, source_c, "SourceC")
        initial_hashes = {str(p): sha256(p) for p in (source_a, source_b, source_c)}

        job_a = write(
            scripts / "job_a.py",
            """import bpy, time\nassert 'SourceA' in bpy.data.objects\nassert 'WorkerBOnly' not in bpy.data.objects\nbpy.ops.mesh.primitive_uv_sphere_add(location=(2,0,0))\nbpy.context.active_object.name='WorkerAOnly'\nbpy.context.scene['worker_marker']='A'\ntime.sleep(1.5)\n""",
        )
        job_b = write(
            scripts / "job_b.py",
            """import bpy, time\nassert 'SourceB' in bpy.data.objects\nassert 'WorkerAOnly' not in bpy.data.objects\nbpy.ops.mesh.primitive_cylinder_add(location=(-2,0,0))\nbpy.context.active_object.name='WorkerBOnly'\nbpy.context.scene['worker_marker']='B'\ntime.sleep(1.5)\n""",
        )
        job_c = write(
            scripts / "job_c_export.py",
            """import bpy\nfrom pathlib import Path\nassert 'SourceC' in bpy.data.objects\nbpy.ops.mesh.primitive_monkey_add(location=(0,2,0))\nbpy.context.active_object.name='ExportOnly'\nexport_path=Path(JOB_DIR)/'worker-3.glb'\nbpy.ops.export_scene.gltf(filepath=str(export_path), export_format='GLB', use_selection=False)\nassert export_path.is_file() and export_path.stat().st_size > 0\n""",
        )
        write_job = write(
            scripts / "write_source.py",
            """import bpy, time\nbpy.ops.mesh.primitive_cone_add(location=(0,0,2))\nbpy.context.active_object.name='PublishedByWriter'\ntime.sleep(2.0)\n""",
        )
        mcp_probe = write(
            scripts / "mcp_session_probe.py",
            r'''import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command=sys.argv[1],
        args=[
            "--runtime", sys.argv[2],
            "--worker", "auto",
            "--mcp-command", sys.argv[3],
            "--blender-bin", sys.argv[4],
            "--ensure-count", "3",
            "--base-port", sys.argv[5],
        ],
        env=None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            response = await session.call_tool(
                "execute_blender_code",
                {
                    "code": (
                        "import bpy, os, time\n"
                        "time.sleep(1.5)\n"
                        "result={'pid':os.getpid(),'background':bool(bpy.app.background)}"
                    )
                },
            )
            structured = getattr(response, "structuredContent", None) or {}
            worker_result = structured.get("result", {}) if isinstance(structured, dict) else {}
            print(json.dumps({
                "server": getattr(init.serverInfo, "name", None),
                "tool_count": len(tools.tools),
                "is_error": bool(getattr(response, "isError", False)),
                "pid": worker_result.get("pid"),
                "background": worker_result.get("background"),
            }, sort_keys=True))


asyncio.run(main())
''',
        )

        manager = WorkerManager(runtime_dir=runtime, blender_bin=blender)
        try:
            started = manager.start(count=3, gui_count=0, base_port=base_port, timeout=45)
            assert len(started) == 3
            assert all(item["healthy"] for item in started)
            ports = [int(item["port"]) for item in started]
            pids = [int(item["pid"]) for item in started]
            assert len(set(ports)) == 3 and len(set(pids)) == 3
            assert all(port != DEFAULT_SINGLE_USER_PORT for port in ports)
            assert all(item.get("protocol") == "blender-lab-mcp-socket" for item in started)
            assert all(item.get("busy") is False for item in started)
            report["three_workers"] = {
                "ports": ports,
                "pids": pids,
                "healthy": True,
                "protocol": "blender-lab-mcp-socket",
            }

            mcp_command = detect_blender_mcp_command()
            mcp_python = mcp_command.parent / "python"
            if not mcp_python.is_file():
                raise RuntimeError(f"MCP environment Python not found next to {mcp_command}")
            mcp_wrapper = ROOT / "scripts" / "blender-worker-mcp.py"
            probe_args = [
                str(mcp_python), str(mcp_probe), str(mcp_wrapper), str(runtime),
                str(mcp_command), str(blender), str(base_port),
            ]
            session_one = subprocess.Popen(
                probe_args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            time.sleep(0.1)
            session_two = subprocess.Popen(
                probe_args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            out_one, err_one = session_one.communicate(timeout=90)
            out_two, err_two = session_two.communicate(timeout=90)
            assert session_one.returncode == 0, err_one
            assert session_two.returncode == 0, err_two
            probe_one = json.loads(out_one.strip().splitlines()[-1])
            probe_two = json.loads(out_two.strip().splitlines()[-1])
            assert probe_one["server"] == "blender-mcp" and probe_two["server"] == "blender-mcp"
            assert not probe_one["is_error"] and not probe_two["is_error"]
            assert probe_one["background"] is True and probe_two["background"] is True
            assert int(probe_one["pid"]) in pids and int(probe_two["pid"]) in pids
            assert int(probe_one["pid"]) != int(probe_two["pid"])
            report["stdio_mcp_session_routing"] = {
                "server": "blender-mcp",
                "session_one_pid": int(probe_one["pid"]),
                "session_two_pid": int(probe_two["pid"]),
                "distinct_workers": True,
                "tool_count": min(int(probe_one["tool_count"]), int(probe_two["tool_count"])),
            }

            wall_start = time.time()
            with ThreadPoolExecutor(max_workers=3) as pool:
                fa = pool.submit(manager.run_job, "worker-1", source_a, job_a, "job-a", False, 120)
                fb = pool.submit(manager.run_job, "worker-2", source_b, job_b, "job-b", False, 120)
                fc = pool.submit(manager.run_job, "worker-3", source_c, job_c, "job-c", False, 120)
                ra, rb, rc = fa.result(), fb.result(), fc.result()
            wall_elapsed = time.time() - wall_start

            assert "WorkerAOnly" in ra["object_names"] and "WorkerBOnly" not in ra["object_names"]
            assert "WorkerBOnly" in rb["object_names"] and "WorkerAOnly" not in rb["object_names"]
            assert "ExportOnly" in rc["object_names"]
            assert max(ra["job_started_at"], rb["job_started_at"]) < min(ra["job_finished_at"], rb["job_finished_at"])
            export_path = Path(rc["result_path"]).parent / "worker-3.glb"
            assert export_path.is_file() and export_path.stat().st_size > 0
            report["parallel_isolation"] = {
                "job_a_worker": ra["worker_id"],
                "job_b_worker": rb["worker_id"],
                "overlap_proved": True,
                "wall_seconds": round(wall_elapsed, 3),
                "no_state_leakage": True,
                "third_worker_export": {"path": str(export_path), "bytes": export_path.stat().st_size},
            }

            for response, source in ((ra, source_a), (rb, source_b), (rc, source_c)):
                assert Path(response["working_copy"]).is_file()
                assert Path(response["result_path"]).is_file()
                assert Path(response["working_copy"]).resolve() != source.resolve()
                assert Path(response["result_path"]).resolve() != source.resolve()
                assert sha256(source) == initial_hashes[str(source)]
            report["working_copy_isolation"] = {"source_hashes_unchanged": True, "per_job_copies": True}

            before = {item["id"]: item for item in manager.status()}
            assert all(item["healthy"] for item in before.values())
            old_pid = int(before["worker-2"]["pid"])
            manager.kill_worker("worker-2")
            time.sleep(0.3)
            crashed = {item["id"]: item for item in manager.status()}
            assert not crashed["worker-2"]["healthy"]
            assert crashed["worker-1"]["healthy"] and crashed["worker-3"]["healthy"]
            assert int(crashed["worker-1"]["pid"]) == int(before["worker-1"]["pid"])
            assert int(crashed["worker-3"]["pid"]) == int(before["worker-3"]["pid"])
            restarted = manager.restart_worker("worker-2", timeout=45)
            assert restarted["healthy"] and int(restarted["pid"]) != old_pid
            after_restart = {item["id"]: item for item in manager.status()}
            assert after_restart["worker-1"]["healthy"] and after_restart["worker-3"]["healthy"]
            assert int(after_restart["worker-1"]["pid"]) == int(before["worker-1"]["pid"])
            assert int(after_restart["worker-3"]["pid"]) == int(before["worker-3"]["pid"])
            report["crash_recovery"] = {
                "crashed_worker": "worker-2",
                "old_pid": old_pid,
                "new_pid": int(restarted["pid"]),
                "unrelated_workers_unchanged": True,
            }

            cli = ROOT / "scripts" / "blender-workers.py"
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(cli),
                    "--runtime", str(runtime),
                    "--blender-bin", str(blender),
                    "run",
                    "--worker", "worker-1",
                    "--source", str(source_a),
                    "--script", str(write_job),
                    "--job-id", "writer-one",
                    "--write-source",
                    "--timeout", "120",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            lock_path = manager._source_lock_path(source_a)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if lock_path.exists() and "writer-one" in lock_path.read_text(encoding="utf-8", errors="ignore"):
                    break
                if first.poll() is not None:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("writer-one did not acquire source lock")
            second = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "--runtime", str(runtime),
                    "--blender-bin", str(blender),
                    "run",
                    "--worker", "worker-3",
                    "--source", str(source_a),
                    "--script", str(write_job),
                    "--job-id", "writer-two",
                    "--write-source",
                    "--timeout", "30",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            stdout1, stderr1 = first.communicate(timeout=120)
            assert first.returncode == 0, stderr1
            assert second.returncode == 73, (second.stdout, second.stderr)
            assert "source_busy" in second.stderr
            assert sha256(source_a) != initial_hashes[str(source_a)]
            report["exclusive_source_write"] = {
                "first_writer_rc": first.returncode,
                "second_writer_rc": second.returncode,
                "second_writer_rejected": True,
                "published_source_changed": True,
            }

            final_status = manager.status()
            assert len(final_status) == 3 and all(item["healthy"] for item in final_status)
            assert all(not item.get("busy", False) for item in final_status)
            assert all(item.get("protocol") == "blender-lab-mcp-socket" for item in final_status)
            report["health_observable"] = final_status
            report["single_user_compatibility"] = {
                "legacy_port": DEFAULT_SINGLE_USER_PORT,
                "worker_ports": ports,
                "legacy_port_untouched": DEFAULT_SINGLE_USER_PORT not in ports,
            }
        finally:
            manager.stop_all()

    report["ok"] = True
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
