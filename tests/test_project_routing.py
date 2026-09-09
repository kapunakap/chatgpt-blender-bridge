from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from blender_bridge.mcp_proxy import project_tools, tool_result
from blender_bridge.project_routing import (
    ProjectBusyError,
    ProjectRouter,
    ProjectRoutingError,
    metadata_from_environment,
    normalize_project_metadata,
    project_key,
)


class ProjectRoutingUnitTests(unittest.TestCase):
    def test_worktree_is_strongest_identity_and_resolves_relative_blend(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw) / "feature"
            worktree.mkdir()
            metadata = normalize_project_metadata(
                {
                    "project": "Kapelica",
                    "repo": "kapunakap/gta-labin",
                    "branch": "feat/kapelica-district",
                    "worktree": str(worktree),
                    "blend_path": "assets/source/district.blend",
                }
            )
            self.assertEqual(metadata["worktree"], str(worktree.resolve()))
            self.assertEqual(
                metadata["blend_path"],
                str((worktree / "assets/source/district.blend").resolve()),
            )
            self.assertEqual(project_key(metadata), f"worktree:{worktree.resolve()}")

    def test_relative_blend_requires_worktree(self) -> None:
        with self.assertRaises(ProjectRoutingError):
            normalize_project_metadata(
                {"repo": "kapunakap/gta-labin", "blend_path": "assets/source/city.blend"}
            )

    def test_environment_metadata_is_optional(self) -> None:
        self.assertEqual(metadata_from_environment({}), {})
        metadata = metadata_from_environment(
            {
                "BLENDER_PROJECT": "Rasa",
                "BLENDER_PROJECT_REPO": "kapunakap/gta-labin",
                "BLENDER_PROJECT_BRANCH": "feat/rasa-mining-town",
            }
        )
        self.assertEqual(metadata["project"], "Rasa")
        self.assertEqual(metadata["branch"], "feat/rasa-mining-town")

    def test_first_attach_binds_current_worker_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = ProjectRouter(raw)
            metadata = {"repo": "kapunakap/gta-labin", "branch": "feat/plomin-coastal-town"}
            first = router.resolve(
                metadata,
                current_worker="worker-2",
                statuses=[
                    {"id": "worker-1", "healthy": True, "busy": False},
                    {"id": "worker-2", "healthy": True, "busy": True},
                ],
                session_id="session-a",
            )
            self.assertEqual(first["worker"], "worker-2")
            stored = router.get(metadata)
            self.assertIsNotNone(stored)
            self.assertEqual(stored["worker"], "worker-2")

            second = router.resolve(
                metadata,
                current_worker="worker-1",
                statuses=[
                    {"id": "worker-1", "healthy": True, "busy": True},
                    {"id": "worker-2", "healthy": True, "busy": False},
                ],
                session_id="session-b",
            )
            self.assertEqual(second["worker"], "worker-2")
            self.assertEqual(second["last_session"], "session-b")

    def test_existing_busy_project_is_not_silently_rebound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = ProjectRouter(raw)
            metadata = {"project": "Kapelica"}
            router.resolve(
                metadata,
                current_worker="worker-1",
                statuses=[{"id": "worker-1", "healthy": True, "busy": True}],
            )
            with self.assertRaises(ProjectBusyError):
                router.resolve(
                    metadata,
                    current_worker="worker-2",
                    statuses=[
                        {"id": "worker-1", "healthy": True, "busy": True},
                        {"id": "worker-2", "healthy": True, "busy": True},
                    ],
                )

    def test_dead_project_worker_recovers_to_current_worker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = ProjectRouter(raw)
            metadata = {"repo": "kapunakap/gta-labin", "branch": "feat/trget-harbour-integration"}
            router.resolve(
                metadata,
                current_worker="worker-1",
                statuses=[{"id": "worker-1", "healthy": True, "busy": True}],
            )
            recovered = router.resolve(
                metadata,
                current_worker="worker-3",
                statuses=[
                    {"id": "worker-1", "healthy": False, "busy": False},
                    {"id": "worker-3", "healthy": True, "busy": True},
                ],
            )
            self.assertEqual(recovered["worker"], "worker-3")
            self.assertEqual(recovered["recovered_from_worker"], "worker-1")

    def test_relative_blend_cannot_escape_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw) / "feature"
            worktree.mkdir()
            with self.assertRaises(ProjectRoutingError):
                normalize_project_metadata(
                    {
                        "worktree": str(worktree),
                        "blend_path": "../outside.blend",
                    }
                )

    def test_relative_blend_cannot_escape_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "feature"
            outside = root / "outside"
            worktree.mkdir()
            outside.mkdir()
            (worktree / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ProjectRoutingError):
                normalize_project_metadata(
                    {
                        "worktree": str(worktree),
                        "blend_path": "linked/city.blend",
                    }
                )

    def test_new_project_uses_unclaimed_worker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = ProjectRouter(raw)
            first = {"project": "Kapelica"}
            second = {"project": "Rasa"}
            router.resolve(
                first,
                current_worker="worker-1",
                statuses=[
                    {"id": "worker-1", "healthy": True, "busy": True},
                    {"id": "worker-2", "healthy": True, "busy": False},
                ],
            )
            route = router.resolve(
                second,
                current_worker="worker-1",
                statuses=[
                    {"id": "worker-1", "healthy": True, "busy": True},
                    {"id": "worker-2", "healthy": True, "busy": False},
                ],
            )
            self.assertEqual(route["worker"], "worker-2")

    def test_new_project_fails_when_every_healthy_worker_is_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = ProjectRouter(raw)
            router.resolve(
                {"project": "Kapelica"},
                current_worker="worker-1",
                statuses=[{"id": "worker-1", "healthy": True, "busy": True}],
            )
            with self.assertRaises(ProjectBusyError):
                router.resolve(
                    {"project": "Rasa"},
                    current_worker="worker-1",
                    statuses=[{"id": "worker-1", "healthy": True, "busy": True}],
                )

    def test_dead_project_recovery_skips_other_project_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = ProjectRouter(raw)
            kapelica = {"project": "Kapelica"}
            rasa = {"project": "Rasa"}
            router.resolve(
                kapelica,
                current_worker="worker-1",
                statuses=[
                    {"id": "worker-1", "healthy": True, "busy": True},
                    {"id": "worker-2", "healthy": True, "busy": False},
                    {"id": "worker-3", "healthy": True, "busy": False},
                ],
            )
            router.resolve(
                rasa,
                current_worker="worker-2",
                statuses=[
                    {"id": "worker-1", "healthy": True, "busy": False},
                    {"id": "worker-2", "healthy": True, "busy": True},
                    {"id": "worker-3", "healthy": True, "busy": False},
                ],
            )
            recovered = router.resolve(
                kapelica,
                current_worker="worker-2",
                statuses=[
                    {"id": "worker-1", "healthy": False, "busy": False},
                    {"id": "worker-2", "healthy": True, "busy": True},
                    {"id": "worker-3", "healthy": True, "busy": False},
                ],
            )
            self.assertEqual(recovered["worker"], "worker-3")
            self.assertEqual(recovered["recovered_from_worker"], "worker-1")

    def test_restore_if_current_rolls_back_failed_new_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = ProjectRouter(raw)
            metadata = {"project": "Plomin"}
            route = router.resolve(
                metadata,
                current_worker="worker-2",
                statuses=[{"id": "worker-2", "healthy": True, "busy": True}],
                session_id="session-a",
            )
            self.assertTrue(
                router.restore_if_current(
                    metadata,
                    expected_route=route,
                    previous_route=None,
                )
            )
            self.assertIsNone(router.get(metadata))

    def test_restore_if_current_does_not_overwrite_newer_route(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = ProjectRouter(raw)
            metadata = {"project": "Plomin"}
            route = router.resolve(
                metadata,
                current_worker="worker-1",
                statuses=[{"id": "worker-1", "healthy": True, "busy": True}],
                session_id="session-a",
            )
            newer = router.resolve(
                metadata,
                current_worker="worker-1",
                statuses=[{"id": "worker-1", "healthy": True, "busy": True}],
                session_id="session-b",
            )
            self.assertFalse(
                router.restore_if_current(
                    metadata,
                    expected_route=route,
                    previous_route=None,
                )
            )
            self.assertEqual(router.get(metadata), newer)

    def test_project_tools_are_added_without_worker_or_port_inputs(self) -> None:
        tools = {item["name"]: item for item in project_tools()}
        self.assertEqual(set(tools), {"project_attach", "project_status", "project_detach"})
        attach_schema = tools["project_attach"]["inputSchema"]
        self.assertNotIn("worker", attach_schema["properties"])
        self.assertNotIn("port", attach_schema["properties"])

    def test_tool_result_is_standard_mcp_call_result(self) -> None:
        response = tool_result(7, {"attached": True, "worker": "worker-2"})
        self.assertEqual(response["id"], 7)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["worker"], "worker-2")


if __name__ == "__main__":
    unittest.main()
