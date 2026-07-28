import json
import shutil
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from lib import reset_test_db
from core.runner import run_jobs
from core.upscale import build_video2x_command, cleanup_cancelled_upscale_job, process_upscale_job
from shared.db import claim_job, insert_one_job, load_jobs, recover_running_jobs
from shared.helpers import JobCancelled, cancellation_scope, run


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
        self.assertEqual(command[command.index("--pix-fmt") + 1], "yuv420p")
        self.assertIn("preset=fast", command)
        self.assertIn("cq=23", command)
        self.assertNotIn("watermark", " ".join(command).lower())

    def test_cancellation_stops_render_and_prefetch_subprocesses_and_cleans_slots(self):
        job = self.make_job()
        download_dir = Path(job["source"]["download_dir"])
        output_dir = Path(job["output_dir"]) / "Test_Anime"
        (download_dir / "episode_001").mkdir(parents=True)
        (download_dir / "episode_002").mkdir(parents=True)
        output_dir.mkdir(parents=True)

        with cancellation_scope(lambda: True):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(run, [sys.executable, "-c", "import time; time.sleep(30)"])
                    for _ in range(2)
                ]
                for future in futures:
                    with self.assertRaises(JobCancelled):
                        future.result(timeout=5)

        cleanup_cancelled_upscale_job(job)
        self.assertFalse(download_dir.exists())
        self.assertFalse(output_dir.exists())

    def _selected(self, count):
        return [
            {
                "episode": episode,
                "index": episode * 10,
                "path": f"Release/Show [{episode:03d}].mkv",
            }
            for episode in range(1, count + 1)
        ]

    def _fake_source_download(self, _torrent, download_dir, selected, **_kwargs):
        episode = int(selected["episode"])
        source = Path(download_dir) / f"episode_{episode:03d}" / f"episode_{episode:03d}.mkv"
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            source.write_bytes(f"episode-{episode}".encode())
        return source

    @patch("core.upscale.validate_upscale_output", return_value={"duration": 120, "video": {"width": 3840}})
    @patch("core.upscale.validate_upscale_source", return_value={"duration": 120, "video": {"width": 1920}})
    @patch("core.upscale.validate_upscale_environment")
    @patch("core.upscale.publish_video_to_vk")
    @patch("core.upscale.run_video2x")
    @patch("core.upscale._download_upscale_source")
    @patch("core.upscale.prepare_torrent_episode_downloads")
    def test_delivery_retry_reuses_prefetched_source_and_output_checkpoint(
        self,
        mock_prepare,
        mock_download,
        mock_video2x,
        mock_publish,
        _mock_environment,
        _mock_source,
        _mock_validate,
    ):
        job = self.make_job()
        download_dir = Path(job["source"]["download_dir"])
        download_dir.mkdir(parents=True)
        mock_prepare.return_value = (download_dir / "release.torrent", self._selected(2))
        mock_download.side_effect = self._fake_source_download

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

        with self.assertRaisesRegex(RuntimeError, "vk down"):
            process_upscale_job({}, job)

        self.assertEqual(mock_video2x.call_count, 2)
        self.assertFalse((download_dir / "episode_001").exists())
        self.assertTrue((download_dir / "episode_002").exists())
        mock_video2x.reset_mock()
        mock_publish.reset_mock()
        mock_publish.side_effect = None
        mock_publish.return_value = success
        job["processing"] = {
            "naming": {"navigation_label": "Перерождение", "source": "manual"},
        }

        result = process_upscale_job({}, job)

        self.assertTrue(result["completed"])
        mock_video2x.assert_not_called()
        mock_publish.assert_called_once()
        self.assertIn("Перерождение", mock_publish.call_args.args[1])
        self.assertEqual(mock_publish.call_args.kwargs["privacy_view"], 5)
        self.assertFalse(download_dir.exists())

    @patch("core.upscale.validate_upscale_output", return_value={"duration": 120, "video": {"width": 3840}})
    @patch("core.upscale.validate_upscale_source", return_value={"duration": 120, "video": {"width": 1920}})
    @patch("core.upscale.validate_upscale_environment")
    @patch("core.upscale.publish_video_to_vk", return_value={"video_uploaded": True})
    @patch("core.upscale.run_video2x")
    @patch("core.upscale._download_upscale_source")
    @patch("core.upscale.prepare_torrent_episode_downloads")
    def test_prefetch_keeps_only_one_episode_ahead(
        self,
        mock_prepare,
        mock_download,
        mock_video2x,
        _mock_publish,
        _mock_environment,
        _mock_source,
        _mock_validate,
    ):
        job = self.make_job("001-003")
        download_dir = Path(job["source"]["download_dir"])
        mock_prepare.return_value = (download_dir / "release.torrent", self._selected(3))
        events = []
        episode_two_ready = threading.Event()

        def fake_download(torrent, root, selected, **kwargs):
            episode = int(selected["episode"])
            events.append(f"download:{episode}")
            source = self._fake_source_download(torrent, root, selected)
            if episode == 2:
                episode_two_ready.set()
            return source

        def fake_render(_config, source, output):
            episode = int(Path(source).stem.rsplit("_", 1)[-1])
            events.append(f"render_start:{episode}")
            if episode == 1:
                self.assertTrue(episode_two_ready.wait(timeout=1))
                self.assertNotIn("download:3", events)
            if episode == 2:
                self.assertFalse((download_dir / "episode_001").exists())
            Path(output).write_bytes(b"4k")
            events.append(f"render_end:{episode}")

        mock_download.side_effect = fake_download
        mock_video2x.side_effect = fake_render

        result = process_upscale_job({}, job)

        self.assertTrue(result["completed"])
        self.assertLess(events.index("download:2"), events.index("render_end:1"))
        self.assertGreater(events.index("download:3"), events.index("render_end:1"))
        self.assertFalse(download_dir.exists())

    @patch("core.upscale.validate_upscale_output", return_value={"duration": 120, "video": {"width": 3840}})
    @patch("core.upscale.validate_upscale_source", return_value={"duration": 120, "video": {"width": 1920}})
    @patch("core.upscale.validate_upscale_environment")
    @patch("core.upscale.publish_video_to_vk", return_value={"video_uploaded": True})
    @patch("core.upscale.run_video2x")
    @patch("core.upscale._download_upscale_source")
    @patch("core.upscale.prepare_torrent_episode_downloads")
    def test_prefetch_failure_does_not_cancel_current_publish(
        self,
        mock_prepare,
        mock_download,
        mock_video2x,
        mock_publish,
        _mock_environment,
        _mock_source,
        _mock_validate,
    ):
        job = self.make_job()
        download_dir = Path(job["source"]["download_dir"])
        mock_prepare.return_value = (download_dir / "release.torrent", self._selected(2))

        def fake_download(torrent, root, selected, **kwargs):
            if int(selected["episode"]) == 2:
                raise RuntimeError("prefetch failed")
            return self._fake_source_download(torrent, root, selected)

        mock_download.side_effect = fake_download
        mock_video2x.side_effect = lambda _config, _source, output: Path(output).write_bytes(b"4k")

        with self.assertRaisesRegex(RuntimeError, "prefetch failed"):
            process_upscale_job({}, job)

        mock_publish.assert_called_once()
        manifest = next((Path(job["output_dir"])).rglob("upscale_manifest.json"))
        state = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertTrue(state["episodes"]["1"]["delivery"]["video_uploaded"])
        self.assertFalse((download_dir / "episode_001").exists())

    @patch("core.upscale.validate_upscale_source", return_value={"duration": 120, "video": {"width": 1920}})
    @patch("core.upscale.validate_upscale_environment")
    @patch("core.upscale.run_video2x", side_effect=RuntimeError("render failed"))
    @patch("core.upscale._download_upscale_source")
    @patch("core.upscale.prepare_torrent_episode_downloads")
    def test_render_failure_preserves_current_and_prefetched_sources(
        self,
        mock_prepare,
        mock_download,
        _mock_video2x,
        _mock_environment,
        _mock_source,
    ):
        job = self.make_job()
        download_dir = Path(job["source"]["download_dir"])
        mock_prepare.return_value = (download_dir / "release.torrent", self._selected(2))
        next_ready = threading.Event()

        def fake_download(torrent, root, selected, **kwargs):
            source = self._fake_source_download(torrent, root, selected)
            if int(selected["episode"]) == 2:
                next_ready.set()
            return source

        mock_download.side_effect = fake_download

        with self.assertRaisesRegex(RuntimeError, "render failed"):
            process_upscale_job({}, job)

        self.assertTrue(next_ready.is_set())
        self.assertTrue((download_dir / "episode_001").exists())
        self.assertTrue((download_dir / "episode_002").exists())


if __name__ == "__main__":
    unittest.main()
