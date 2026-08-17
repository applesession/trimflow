import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.pipeline import build_output_artifacts, cleanup_cancelled_job_artifacts, cleanup_job_artifacts
from shared.helpers import build_job_workspace_name


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

    def test_cleanup_preserves_resumable_temp_after_render_failure(self):
        tmp_dir = self.make_workspace_temp_dir()
        temp_dir = tmp_dir / "temp" / "title"
        temp_dir.mkdir(parents=True)

        cleanup_job_artifacts(
            {"temp": True},
            temp_dir=temp_dir,
            render_completed=False,
            preserve_temp_on_failure=True,
        )

        self.assertTrue(temp_dir.exists())

    def test_stop_preserves_all_local_artifacts(self):
        tmp_dir = self.make_workspace_temp_dir()
        download_dir = tmp_dir / "downloads" / "title"
        temp_dir = tmp_dir / "temp" / "title"
        output_dir = tmp_dir / "output" / "title"
        for path in (download_dir, temp_dir, output_dir):
            path.mkdir(parents=True, exist_ok=True)

        cleanup_job_artifacts(
            {"downloads": True, "temp": True, "output": True},
            download_dir=download_dir,
            temp_dir=temp_dir,
            job_output_dir=output_dir,
            render_completed=True,
            job_completed=True,
            cancellation_requested=True,
        )

        self.assertTrue(download_dir.exists())
        self.assertTrue(temp_dir.exists())
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

    def test_cleanup_removes_only_current_job_outputs(self):
        tmp_dir = self.make_workspace_temp_dir()
        output_dir = tmp_dir / "output" / "title"
        current = output_dir / "current.mkv"
        sibling = output_dir / "sibling.mkv"
        output_dir.mkdir(parents=True)
        current.touch()
        sibling.touch()

        cleanup_job_artifacts(
            {"output": True},
            job_output_dir=output_dir,
            output_files=[current],
            render_completed=True,
            job_completed=True,
        )

        self.assertFalse(current.exists())
        self.assertTrue(sibling.exists())
        self.assertTrue(output_dir.exists())

    def test_cancelled_job_cleanup_removes_all_local_artifacts(self):
        tmp_dir = self.make_workspace_temp_dir()
        download_dir = tmp_dir / "downloads" / "Title"
        temp_root = tmp_dir / "temp"
        output_root = tmp_dir / "output"
        for path in (download_dir, temp_root / "Title", output_root / "Title"):
            path.mkdir(parents=True)

        job = {
            "title": "Title",
            "source": {"type": "magnet", "download_dir": str(download_dir)},
            "output_dir": str(output_root),
        }
        with patch("lib.pipeline.TEMP_ROOT", temp_root):
            cleanup_cancelled_job_artifacts(job)

        self.assertFalse(download_dir.exists())
        self.assertFalse((temp_root / "Title").exists())
        self.assertFalse((output_root / "Title").exists())

    def test_cancelled_job_cleanup_preserves_sibling_range_artifacts(self):
        tmp_dir = self.make_workspace_temp_dir()
        temp_root = tmp_dir / "temp"
        output_root = tmp_dir / "output"
        first = {
            "title": "Title",
            "season": 1,
            "episodes_range": "001-010",
            "source": {"type": "magnet", "download_dir": str(tmp_dir / "downloads" / "first")},
            "output_dir": str(output_root),
        }
        sibling = {
            **first,
            "episodes_range": "011-020",
            "source": {"type": "magnet", "download_dir": str(tmp_dir / "downloads" / "sibling")},
        }
        first_temp = temp_root / build_job_workspace_name(first)
        sibling_temp = temp_root / build_job_workspace_name(sibling)
        first_artifacts = build_output_artifacts(first, output_root)
        sibling_artifacts = build_output_artifacts(sibling, output_root)
        for path in (
            Path(first["source"]["download_dir"]),
            Path(sibling["source"]["download_dir"]),
            first_temp,
            sibling_temp,
            first_artifacts["job_output_dir"],
        ):
            path.mkdir(parents=True, exist_ok=True)
        for artifacts in (first_artifacts, sibling_artifacts):
            for key in ("output_video", "output_txt", "output_manifest"):
                artifacts[key].touch()

        with patch("lib.pipeline.TEMP_ROOT", temp_root):
            cleanup_cancelled_job_artifacts(first)

        self.assertFalse(Path(first["source"]["download_dir"]).exists())
        self.assertTrue(Path(sibling["source"]["download_dir"]).exists())
        self.assertFalse(first_temp.exists())
        self.assertTrue(sibling_temp.exists())
        self.assertFalse(first_artifacts["output_video"].exists())
        self.assertTrue(sibling_artifacts["output_video"].exists())


if __name__ == "__main__":
    unittest.main()
