import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.pipeline import (
    RENDER_PIPELINE_VERSION,
    build_delivery_config,
    build_episode_fingerprint,
    build_output_artifacts,
    build_timestamps_from_episodes,
    deliver_rendered_output,
    initialize_episode_checkpoints,
    load_episode_checkpoint,
    load_render_checkpoint,
    process_job,
    save_episode_checkpoint,
)


SIGNATURE = {
    "video": {
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
    },
    "audio": {
        "codec_name": "aac",
        "sample_rate": "48000",
        "channels": 2,
        "channel_layout": "stereo",
    },
}
VALIDATION = {
    "duration": 10.0,
    "media_signature": SIGNATURE,
    "timeline": {
        "video": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.04},
        "audio": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.03},
    },
}


class PipelineEpisodeCheckpointTests(unittest.TestCase):
    def make_workspace_temp_dir(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(dir=root))
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def make_job(self, tmp_dir, episodes_range="001-002"):
        watermark_path = tmp_dir / "watermark.png"
        watermark_path.write_bytes(b"png")
        return {
            "title": "Episode Test",
            "title_ru": "Тест серий",
            "season": 1,
            "episodes_range": episodes_range,
            "source": {"type": "local", "input_dir": str(tmp_dir / "input")},
            "output_dir": str(tmp_dir / "output"),
            "watermark_path": str(watermark_path),
            "skip_types": ["op", "ed"],
            "cleanup": {"downloads": False, "temp": False, "output": False},
            "processing": {
                "chunk_size_episodes": "legacy value is ignored",
            },
            "timing_detection": {"enabled": False},
            "timing_providers": {
                "anilibria_enabled": False,
                "aniskip_enabled": False,
            },
            "delivery": {"s3_enabled": False, "vk_enabled": False},
            "encoding": {
                "video_codec": "libx264",
                "preset": "fast",
                "cq": 23,
                "segment_cut_mode": "copy",
                "segment_max_render_seconds": 1,
                "audio_codec": "aac",
            },
        }

    def test_episode_fingerprint_tracks_effective_inputs_not_legacy_chunk_settings(self):
        tmp_dir = self.make_workspace_temp_dir()
        source = tmp_dir / "episode.mkv"
        source.write_bytes(b"source")
        job = self.make_job(tmp_dir, "001")
        episode_infos = [{"episode": 1, "path": str(source), "duration": 10.0}]

        def fingerprint():
            return build_episode_fingerprint(
                job,
                episode_infos,
                watermark_path=job["watermark_path"],
                timing_detection=job["timing_detection"],
                preferred_language="rus",
            )

        original = fingerprint()
        job["processing"]["chunk_size_episodes"] = 99
        job["encoding"]["segment_cut_mode"] = "hybrid"
        self.assertEqual(original, fingerprint())
        job["encoding"]["cq"] = 24
        self.assertNotEqual(original, fingerprint())

    def test_pipeline_v1_root_and_output_checkpoints_are_invalid(self):
        tmp_dir = self.make_workspace_temp_dir()
        temp_dir = tmp_dir / "temp"
        temp_dir.mkdir()
        (temp_dir / "checkpoint.json").write_text(
            json.dumps({"version": 1, "fingerprint": "same"}),
            encoding="utf-8",
        )
        stale = temp_dir / "chunk_001" / "rendered.mkv"
        stale.parent.mkdir()
        stale.write_bytes(b"old")

        checkpoint = initialize_episode_checkpoints(temp_dir, "same")

        self.assertEqual(checkpoint["render_pipeline_version"], RENDER_PIPELINE_VERSION)
        self.assertFalse(stale.exists())

        job = self.make_job(tmp_dir)
        artifacts = build_output_artifacts(job, job["output_dir"])
        artifacts["job_output_dir"].mkdir(parents=True)
        artifacts["output_video"].write_bytes(b"video")
        artifacts["output_txt"].write_text("00:00:00 - 1 серия\n", encoding="utf-8")
        manifest = {
            "render_complete": True,
            "title": job["title"],
            "season": "01",
            "episodes_range": job["episodes_range"],
            "output_video": artifacts["output_video"].name,
            "output_timestamps": artifacts["output_txt"].name,
        }
        artifacts["output_manifest"].write_text(json.dumps(manifest), encoding="utf-8")
        with patch("lib.pipeline.ffprobe_duration", return_value=10.0):
            self.assertIsNone(load_render_checkpoint(job, artifacts))
            manifest["render_pipeline_version"] = RENDER_PIPELINE_VERSION
            artifacts["output_manifest"].write_text(json.dumps(manifest), encoding="utf-8")
            self.assertIsNotNone(load_render_checkpoint(job, artifacts))

    @patch("lib.pipeline.validate_episode_render", return_value=VALIDATION)
    def test_episode_checkpoint_rejects_changed_or_corrupt_media(self, mock_validate):
        tmp_dir = self.make_workspace_temp_dir()
        episode_info = {
            "episode": 1,
            "path": str(tmp_dir / "source.mkv"),
            "duration": 10.0,
        }
        Path(episode_info["path"]).write_bytes(b"source")
        episode_dir = tmp_dir / "episode_001"
        episode_dir.mkdir()
        work = episode_dir / "rendered.work.mkv"
        work.write_bytes(b"video")
        manifest_episode = {
            "episode": 1,
            "source_file": episode_info["path"],
            "expected_cleaned_duration": 10.0,
            "cleaned_duration": 10.0,
        }
        save_episode_checkpoint(episode_dir, episode_info, work, manifest_episode)

        self.assertIsNotNone(load_episode_checkpoint(tmp_dir, episode_info))
        (episode_dir / "rendered.mkv").write_bytes(b"corrupt")
        self.assertIsNone(load_episode_checkpoint(tmp_dir, episode_info))

    def test_one_hundred_actual_durations_build_exact_cumulative_timeline(self):
        episodes = [
            {"episode": number, "cleaned_duration": 1331.375}
            for number in range(1, 101)
        ]

        timestamps = build_timestamps_from_episodes(episodes)

        self.assertEqual(timestamps[0], "00:00:00 - 1 серия")
        self.assertEqual(timestamps[-1], "36:36:46 - 100 серия")

    def _mock_render_plan(self, episode_info, **kwargs):
        episode = episode_info["episode"]
        return {
            "keep_segments": [(0.0, 10.0)],
            "expected_duration": 10.0,
            "audio_stream_index": 0,
            "manifest_episode": {
                "episode": episode,
                "source_file": episode_info["path"],
                "original_duration": 10.0,
                "expected_cleaned_duration": 10.0,
                "cleaned_duration": 10.0,
                "timing_info": {},
                "skip_summary": {},
                "kept_segments": [{"start": 0.0, "end": 10.0}],
                "removed_segments": [],
            },
        }

    def _process_patches(self, temp_dir, episode_infos):
        episode_files = [
            (item["episode"], Path(item["path"]))
            for item in episode_infos
        ]
        return [
            patch("lib.pipeline.prepare_temp_dir", return_value=temp_dir),
            patch("lib.pipeline.collect_episode_files", return_value=(None, episode_files, [])),
            patch("lib.pipeline.filter_episode_files", return_value=(episode_files, [])),
            patch("lib.pipeline.build_episode_infos", return_value=episode_infos),
            patch(
                "lib.pipeline.build_detector_context",
                return_value={"enabled": False, "available": False, "reason": "disabled"},
            ),
            patch("lib.pipeline.build_episode_render_plan", side_effect=self._mock_render_plan),
            patch("lib.pipeline.validate_episode_render", return_value=VALIDATION),
            patch("lib.pipeline.ffprobe_media_signature", return_value=SIGNATURE),
            patch("lib.pipeline.ffprobe_duration", return_value=20.0),
            patch("lib.pipeline.build_quality_summary", return_value={}),
        ]

    def test_retry_renders_only_missing_episode_and_final_concat_uses_durations(self):
        tmp_dir = self.make_workspace_temp_dir()
        temp_dir = tmp_dir / "temp" / "Episode_Test"
        temp_dir.mkdir(parents=True)
        job = self.make_job(tmp_dir)
        episode_infos = [
            {"episode": 1, "path": str(tmp_dir / "ep1.mkv"), "duration": 10.0},
            {"episode": 2, "path": str(tmp_dir / "ep2.mkv"), "duration": 10.0},
        ]
        for item in episode_infos:
            Path(item["path"]).write_bytes(b"source")

        patches = self._process_patches(temp_dir, episode_infos)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        render_patcher = patch("lib.pipeline.render_episode")
        concat_patcher = patch("lib.pipeline.render_concat")
        render_mock = render_patcher.start()
        concat_mock = concat_patcher.start()
        self.addCleanup(render_patcher.stop)
        self.addCleanup(concat_patcher.stop)

        render_calls = 0

        def render_side_effect(ep_file, output, *args, **kwargs):
            nonlocal render_calls
            render_calls += 1
            if render_calls == 2:
                raise RuntimeError("episode 2 failed")
            Path(output).write_bytes(b"episode")

        render_mock.side_effect = render_side_effect
        concat_mock.side_effect = lambda concat_file, output, **kwargs: Path(output).write_bytes(b"season")

        with self.assertRaisesRegex(RuntimeError, "episode 2 failed"):
            process_job(job)
        self.assertTrue((temp_dir / "episode_001" / "rendered.mkv").exists())
        self.assertFalse((temp_dir / "episode_002" / "rendered.work.mkv").exists())

        result = process_job(job)

        self.assertTrue(result["output_video"].endswith(".mkv"))
        self.assertEqual(render_mock.call_count, 3)
        concat_mock.assert_called_once()
        concat_text = (temp_dir / "concat.txt").read_text(encoding="utf-8")
        self.assertTrue(concat_text.startswith("ffconcat version 1.0"))
        self.assertEqual(concat_text.count("duration 10.000000"), 2)
        self.assertFalse(concat_mock.call_args.kwargs["allow_reencode"])

    def test_final_duration_mismatch_blocks_publication_and_keeps_episodes(self):
        tmp_dir = self.make_workspace_temp_dir()
        temp_dir = tmp_dir / "temp" / "Episode_Test"
        temp_dir.mkdir(parents=True)
        job = self.make_job(tmp_dir)
        job["delivery"] = {"s3_enabled": False, "vk_enabled": True}
        episode_infos = [
            {"episode": 1, "path": str(tmp_dir / "ep1.mkv"), "duration": 10.0},
            {"episode": 2, "path": str(tmp_dir / "ep2.mkv"), "duration": 10.0},
        ]
        for item in episode_infos:
            Path(item["path"]).write_bytes(b"source")

        patches = self._process_patches(temp_dir, episode_infos)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        render_patcher = patch("lib.pipeline.render_episode")
        render_mock = render_patcher.start()
        self.addCleanup(render_patcher.stop)
        render_mock.side_effect = lambda ep_file, output, *args, **kwargs: Path(output).write_bytes(b"ep")
        concat_patcher = patch("lib.pipeline.render_concat")
        concat_mock = concat_patcher.start()
        self.addCleanup(concat_patcher.stop)
        concat_mock.side_effect = lambda concat_file, output, **kwargs: Path(output).write_bytes(b"season")

        with patch("lib.pipeline.ffprobe_duration", return_value=25.0), patch(
            "lib.pipeline.deliver_to_vk"
        ) as deliver_mock:
            with self.assertRaisesRegex(RuntimeError, "duration mismatch"):
                process_job(job)

        deliver_mock.assert_not_called()
        self.assertTrue((temp_dir / "episode_001" / "rendered.mkv").exists())
        self.assertTrue((temp_dir / "episode_002" / "rendered.mkv").exists())

    @patch("lib.pipeline.render_final")
    @patch("lib.pipeline.collect_episode_files")
    @patch("lib.pipeline.deliver_to_vk")
    @patch("lib.pipeline.ffprobe_duration", return_value=10.0)
    @patch("lib.pipeline.prepare_temp_dir")
    def test_vk_retry_uses_v2_output_without_render(
        self,
        mock_prepare,
        mock_duration,
        mock_deliver,
        mock_collect,
        mock_render_final,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        mock_prepare.return_value = tmp_dir / "temp"
        job = self.make_job(tmp_dir)
        job["delivery"] = {
            "s3_enabled": False,
            "vk_enabled": True,
            "vk_preview_enabled": False,
        }
        artifacts = build_output_artifacts(job, job["output_dir"])
        artifacts["job_output_dir"].mkdir(parents=True)
        artifacts["output_video"].write_bytes(b"video")
        manifest = {
            "render_pipeline_version": RENDER_PIPELINE_VERSION,
            "render_complete": True,
            "title": job["title"],
            "season": "01",
            "episodes_range": job["episodes_range"],
            "episodes_count": 2,
            "output_video": artifacts["output_video"].name,
            "output_timestamps": artifacts["output_txt"].name,
            "quality_summary": {},
        }
        mock_deliver.side_effect = RuntimeError("vk down")
        deliver_rendered_output(
            job,
            build_delivery_config(job),
            output_video=artifacts["output_video"],
            output_txt=artifacts["output_txt"],
            output_manifest=artifacts["output_manifest"],
            manifest=manifest,
            timestamps=["00:00:00 - 1 серия"],
            pretty_base_name=artifacts["pretty_base_name"],
            temp_dir=tmp_dir / "temp",
            total_episodes=2,
        )

        mock_deliver.side_effect = None
        mock_deliver.return_value = {"video_uploaded": True}
        result = process_job(job)

        self.assertTrue(result["delivery_summary"]["vk"]["video_uploaded"])
        mock_collect.assert_not_called()
        mock_render_final.assert_not_called()


if __name__ == "__main__":
    unittest.main()
