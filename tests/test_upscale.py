import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from lib import reset_test_db
from core.runner import run_jobs
from core.upscale import build_video2x_command, process_upscale_job
from shared.db import claim_job, insert_one_job, load_jobs, recover_running_jobs


class UpscaleTests(unittest.TestCase):
    def setUp(self):
        reset_test_db()
        self.root = Path(".test_tmp") / f"upscale_{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def make_job(self, episodes_range="001-002"):
        return {
            "title": "Test Anime",
            "season": 1,
            "episodes_range": episodes_range,
            "processing_mode": "upscale_4k",
            "source": {
                "type": "magnet",
                "magnet": "magnet:?xt=urn:btih:test",
                "download_dir": str(self.root / "downloads"),
            },
            "output_dir": str(self.root / "output"),
            "cleanup": {"downloads": True, "output": True},
        }

    def test_normal_recovery_does_not_touch_running_upscale_job(self):
        insert_one_job({
            "title": "Normal", "season": 1, "episodes_range": "001",
            "processing_mode": "compilation", "source": {"type": "local", "input_dir": "input"},
        })
        insert_one_job(self.make_job("001"))
        jobs = load_jobs(status="pending")
        for job in jobs:
            self.assertTrue(claim_job(job["_queue_id"]))

        recovered = recover_running_jobs(exclude_processing_modes={"upscale_4k"})

        self.assertEqual([job["title"] for job in recovered], ["Normal"])
        self.assertEqual([job["title"] for job in load_jobs(status="running")], ["Test Anime"])
        self.assertEqual([job["title"] for job in load_jobs(status="pending")], ["Normal"])

    @patch("core.runner.validate_required_files")
    @patch("core.runner.validate_required_tools")
    @patch("core.runner.validate_required_env")
    @patch("core.runner.process_job", return_value={"output_video": "normal.mkv", "delivery_summary": {}})
    def test_normal_runner_never_claims_upscale_job(self, _process, _env, _tools, _files):
        insert_one_job({
            "title": "Normal", "season": 1, "episodes_range": "001",
            "processing_mode": "compilation", "source": {"type": "local", "input_dir": "input"},
        })
        insert_one_job(self.make_job("001"))
        normal_jobs = load_jobs(status="pending", exclude_processing_modes={"upscale_4k"})

        summary = run_jobs({}, normal_jobs, exclude_processing_modes={"upscale_4k"})

        self.assertEqual(summary["jobs_processed"], 1)
        self.assertEqual([job["title"] for job in load_jobs(status="pending")], ["Test Anime"])

    def test_video2x_command_matches_smoke_configuration(self):
        executable = self.root / "Video2X.AppImage"
        executable.write_bytes(b"appimage")

        command = build_video2x_command(
            {"upscale": {"video2x_path": str(executable)}},
            "source.mkv",
            "output.work.mkv",
        )

        self.assertIn("--appimage-extract-and-run", command)
        self.assertIn("realesr-animevideov3", command)
        self.assertIn("h264_nvenc", command)
        self.assertIn("preset=fast", command)
        self.assertIn("cq=23", command)
        self.assertNotIn("watermark", " ".join(command).lower())

    @patch("core.upscale.validate_upscale_output", return_value={"duration": 120, "video": {"width": 3840}})
    @patch("core.upscale.validate_upscale_source", return_value={"duration": 120, "video": {"width": 1920}})
    @patch("core.upscale.validate_upscale_environment")
    @patch("core.upscale.publish_video_to_vk")
    @patch("core.upscale.run_video2x")
    @patch("core.upscale.download_magnet")
    def test_delivery_retry_reuses_episode_checkpoint(
        self,
        _mock_download,
        mock_video2x,
        mock_publish,
        _mock_environment,
        _mock_source,
        _mock_validate,
    ):
        job = self.make_job()
        download_dir = Path(job["source"]["download_dir"])
        download_dir.mkdir(parents=True)
        episode_files = []
        for episode in (1, 2):
            path = download_dir / f"episode_{episode:02d}.mkv"
            path.write_bytes(f"episode-{episode}".encode())
            episode_files.append((episode, path))

        def fake_render(_config, _source, output):
            Path(output).write_bytes(b"4k-video")

        mock_video2x.side_effect = fake_render
        success = {
            "video_uploaded": True,
            "post_created": True,
            "video_url": "https://vk.test/video",
            "errors_by_stage": {},
        }
        mock_publish.side_effect = [success, RuntimeError("vk down")]

        with patch("core.upscale.find_episode_files", return_value=(episode_files, [])):
            with self.assertRaisesRegex(RuntimeError, "vk down"):
                process_upscale_job({}, job)

            self.assertEqual(mock_video2x.call_count, 2)
            mock_video2x.reset_mock()
            mock_publish.reset_mock()
            mock_publish.side_effect = None
            mock_publish.return_value = success

            result = process_upscale_job({}, job)

        self.assertTrue(result["completed"])
        mock_video2x.assert_not_called()
        mock_publish.assert_called_once()
        self.assertEqual(mock_publish.call_args.kwargs["privacy_view"], 5)
        self.assertFalse(download_dir.exists())


if __name__ == "__main__":
    unittest.main()
