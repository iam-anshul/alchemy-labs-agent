import base64
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from api.workspace_outputs import decode_artifact, run_outputs


class WorkspaceOutputTests(unittest.TestCase):
    def test_database_artifacts_are_primary_and_keep_preview_contract(self):
        run_id = uuid4()
        artifact = {
            "rel_path": "outputs/report.md",
            "content_b64": base64.b64encode(b"# Report").decode("ascii"),
            "bytes": 8,
            "task_id": "t1",
            "filename": "report.md",
            "mime_type": "text/markdown",
            "modified_at": 1_718_352_000,
        }
        run = SimpleNamespace(
            query_id=run_id,
            workspace_id="Vendor risk",
            workspace="/missing",
            started_at=datetime.now(timezone.utc),
            produced_artifacts=[artifact],
        )

        outputs = run_outputs(run)

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].task_id, "t1")
        self.assertIn("/outputs/report.md?", outputs[0].preview_url)
        self.assertNotIn("/outputs/outputs/", outputs[0].preview_url)
        self.assertIn("disposition=inline", outputs[0].preview_url)
        self.assertIn("disposition=attachment", outputs[0].download_url)
        self.assertEqual(decode_artifact(artifact), b"# Report")

    def test_legacy_run_falls_back_to_filesystem_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            output = workspace / "outputs" / "report.txt"
            output.parent.mkdir()
            output.write_text("ready", encoding="utf-8")
            run = SimpleNamespace(
                query_id=uuid4(),
                workspace_id="Research",
                workspace=str(workspace),
                started_at=datetime.now(timezone.utc),
                produced_artifacts=None,
            )

            outputs = run_outputs(run)

            self.assertEqual([item.relative_path for item in outputs], [
                "outputs/report.txt",
            ])

    def test_new_empty_run_does_not_expose_restored_files_as_its_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            output = workspace / "outputs" / "prior-run.txt"
            output.parent.mkdir()
            output.write_text("old", encoding="utf-8")
            run = SimpleNamespace(
                query_id=uuid4(),
                workspace_id="Research",
                workspace=str(workspace),
                started_at=datetime.now(timezone.utc),
                produced_artifacts=[],
            )

            self.assertEqual(run_outputs(run), [])


if __name__ == "__main__":
    unittest.main()
