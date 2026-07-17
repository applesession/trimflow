import shutil
import tempfile
import unittest
from pathlib import Path

from lib.pipeline import cleanup_job_artifacts


class PipelineCleanupTests(unittest.TestCase):
    def make_workspace_temp_dir(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(dir=root))
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def test_cleanup_preserves_downloads_after_render_failure(self):
        tmp_dir = self.make_workspace_temp_dir()
        download_dir = tmp_dir / "downloads" / "title"
        temp_dir = tmp_dir / "temp" / "title"
        output_dir = tmp_dir / "output" / "title"

        download_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        cleanup_job_artifacts(
            {"downloads": True, "temp": True, "output": True},
            download_dir=download_dir,
            temp_dir=temp_dir,
            job_output_dir=output_dir,
            render_completed=False,
            job_completed=False,
        )

        self.assertTrue(download_dir.exists())
        self.assertFalse(temp_dir.exists())
        self.assertTrue(output_dir.exists())

    def test_cleanup_removes_downloads_but_keeps_output_after_delivery_failure(self):
        tmp_dir = self.make_workspace_temp_dir()
        download_dir = tmp_dir / "downloads" / "title"
        output_dir = tmp_dir / "output" / "title"
        download_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        cleanup_job_artifacts(
            {"downloads": True, "temp": True, "output": True},
            download_dir=download_dir,
            job_output_dir=output_dir,
            render_completed=True,
            job_completed=False,
        )

        self.assertFalse(download_dir.exists())
        self.assertTrue(output_dir.exists())

    def test_cleanup_can_remove_output_after_success(self):
        tmp_dir = self.make_workspace_temp_dir()
        output_dir = tmp_dir / "output" / "title"
        output_dir.mkdir(parents=True, exist_ok=True)

        cleanup_job_artifacts(
            {"downloads": True, "temp": True, "output": True},
            job_output_dir=output_dir,
            render_completed=True,
            job_completed=True,
        )

        self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
