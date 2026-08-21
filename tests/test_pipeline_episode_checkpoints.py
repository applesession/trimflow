import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.detector import DETECTOR_RESULT_VERSION
from lib.pipeline import (
    AUTO_AUDIO_TAIL_RECOVERY_SECONDS,
    RENDER_PIPELINE_VERSION,
    build_audio_recovery_info,
    build_delivery_config,
    build_episode_infos,
    build_episode_fingerprint,
    build_output_artifacts,
    build_multi_season_timestamps,
    build_timestamps_from_episodes,
    describe_media_signature_groups,
    deliver_rendered_output,
    renumber_season_part_episodes,
    initialize_episode_checkpoints,
    load_episode_checkpoint,
    load_render_checkpoint,
    process_job,
    save_episode_checkpoint,
    select_compilation_frame_rate,
    select_compilation_frame_size,
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

    def test_safe_tail_recovery_is_automatic_but_other_anomalies_are_not(self):
        safe_tail = build_audio_recovery_info(
            False,
            "episode.mkv",
            0,
            timeline={
                "video": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.01},
                "audio": {"start": 0.0, "duration": 8.0, "max_packet_gap": 0.01},
            },
        )
        self.assertTrue(safe_tail["applied"])
        self.assertTrue(safe_tail["automatic"])
        self.assertFalse(safe_tail["enabled"])
        self.assertEqual(AUTO_AUDIO_TAIL_RECOVERY_SECONDS, 3.0)

        long_tail = build_audio_recovery_info(
            False,
            "episode.mkv",
            0,
            timeline={
                "video": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.01},
                "audio": {"start": 0.0, "duration": 6.9, "max_packet_gap": 0.01},
            },
        )
        self.assertFalse(long_tail["applied"])
        self.assertFalse(long_tail["automatic"])

        internal_gap = build_audio_recovery_info(
            False,
            "episode.mkv",
            0,
            timeline={
                "video": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.01},
                "audio": {
                    "start": 0.0,
                    "duration": 10.0,
                    "max_packet_gap": 0.6,
                    "total_packet_gap": 0.6,
                },
            },
        )
        self.assertFalse(internal_gap["applied"])
        self.assertFalse(internal_gap["automatic"])

        manual_gap = build_audio_recovery_info(
            True,
            "episode.mkv",
            0,
            timeline={
                "video": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.01},
                "audio": {
                    "start": 0.0,
                    "duration": 10.0,
                    "max_packet_gap": 0.6,
                    "total_packet_gap": 0.6,
                },
            },
        )
        self.assertTrue(manual_gap["applied"])
        self.assertTrue(manual_gap["enabled"])
        self.assertFalse(manual_gap["automatic"])

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
        episode_infos = [{
            "episode": 1,
            "path": str(source),
            "duration": 10.0,
            "frame_rate": "30/1",
            "width": 1920,
            "height": 1080,
        }]

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

        job["encoding"]["cq"] = 23
        episode_infos[0]["frame_rate"] = "24000/1001"
        self.assertNotEqual(original, fingerprint())

        episode_infos[0]["frame_rate"] = "30/1"
        episode_infos[0]["width"] = 1440
        self.assertNotEqual(original, fingerprint())

        episode_infos[0]["width"] = 1920
        external_audio = tmp_dir / "episode.mka"
        external_audio.write_bytes(b"audio")
        episode_infos[0]["external_audio"] = {
            "path": str(external_audio),
            "audio_index": 0,
            "stream_index": 0,
        }
        with_external = fingerprint()
        self.assertNotEqual(original, with_external)
        external_audio.write_bytes(b"changed audio")
        self.assertNotEqual(with_external, fingerprint())

    def test_episode_fingerprint_tracks_detector_algorithm_version(self):
        tmp_dir = self.make_workspace_temp_dir()
        source = tmp_dir / "episode.mkv"
        source.write_bytes(b"source")
        job = self.make_job(tmp_dir, "001")
        episode_infos = [{
            "episode": 1,
            "path": str(source),
            "duration": 10.0,
            "frame_rate": "30/1",
            "width": 1920,
            "height": 1080,
        }]

        current = build_episode_fingerprint(
            job,
            episode_infos,
            watermark_path=job["watermark_path"],
            timing_detection=job["timing_detection"],
            preferred_language="rus",
        )
        with patch("lib.pipeline.DETECTOR_RESULT_VERSION", "future_version"):
            disabled_future = build_episode_fingerprint(
                job,
                episode_infos,
                watermark_path=job["watermark_path"],
                timing_detection=job["timing_detection"],
                preferred_language="rus",
            )
        self.assertEqual(current, disabled_future)

        job["timing_detection"]["enabled"] = True
        current = build_episode_fingerprint(
            job,
            episode_infos,
            watermark_path=job["watermark_path"],
            timing_detection=job["timing_detection"],
            preferred_language="rus",
        )
        with patch("lib.pipeline.DETECTOR_RESULT_VERSION", "future_version"):
            future = build_episode_fingerprint(
                job,
                episode_infos,
                watermark_path=job["watermark_path"],
                timing_detection=job["timing_detection"],
                preferred_language="rus",
            )

        self.assertNotEqual(current, future)

    @patch("lib.pipeline.ffprobe_episode_timeline")
    @patch("lib.pipeline.ffprobe_duration", return_value=10.0)
    @patch("lib.pipeline.ffprobe_media_signature")
    def test_episode_scan_uses_video_timeline_duration(
        self,
        mock_signature,
        mock_duration,
        mock_timeline,
    ):
        mock_signature.return_value = {
            "video": {
                "r_frame_rate": "24000/1001",
                "width": 1440,
                "height": 1080,
            },
            "audio": None,
        }
        mock_timeline.return_value = {
            "video": {"start": 0.0, "duration": 9.5, "max_packet_gap": 0.0},
            "audio": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.0},
        }

        infos = build_episode_infos([(1, Path("episode.mkv"))])

        self.assertEqual(infos[0]["duration"], 9.5)
        self.assertEqual(infos[0]["container_duration"], 10.0)
        self.assertEqual(infos[0]["frame_rate"], "24000/1001")
        self.assertEqual((infos[0]["width"], infos[0]["height"]), (1440, 1080))

    def test_compilation_frame_rate_uses_majority_and_first_on_tie(self):
        self.assertEqual(select_compilation_frame_rate([
            {"frame_rate": "30/1"},
            {"frame_rate": "24000/1001"},
            {"frame_rate": "30/1"},
        ]), "30/1")
        self.assertEqual(select_compilation_frame_rate([
            {"frame_rate": "24000/1001"},
            {"frame_rate": "30/1"},
        ]), "24000/1001")
        self.assertEqual(select_compilation_frame_rate([
            {"frame_rate": "24000/1001"},
            {"frame_rate": "24000/1001"},
        ]), "24000/1001")

    def test_compilation_frame_rate_requires_detected_video_rate(self):
        with self.assertRaisesRegex(RuntimeError, "Unable to determine compilation frame rate"):
            select_compilation_frame_rate([{"frame_rate": None}, {"frame_rate": "0/0"}])

    def test_compilation_frame_size_uses_largest_area_and_first_on_tie(self):
        self.assertEqual(select_compilation_frame_size([
            {"width": 1440, "height": 1080},
            {"width": 1920, "height": 1080},
        ]), (1920, 1080))
        self.assertEqual(select_compilation_frame_size([
            {"width": 1920, "height": 1080},
            {"width": 1080, "height": 1920},
        ]), (1920, 1080))

    def test_compilation_frame_size_requires_detected_dimensions(self):
        with self.assertRaisesRegex(RuntimeError, "Unable to determine compilation frame size"):
            select_compilation_frame_size([{"width": None, "height": None}])

    def test_media_signature_groups_include_affected_episodes(self):
        details = describe_media_signature_groups(
            [{"episode": 1}, {"episode": 2}, {"episode": 3}],
            [
                {"video": {"width": 1920, "r_frame_rate": "30/1"}},
                {"video": {"width": 1280, "r_frame_rate": "30/1"}},
                {"video": {"width": 1920, "r_frame_rate": "30/1"}},
            ],
        )

        self.assertIn("episodes=001,003", details)
        self.assertIn('"width":1920', details)
        self.assertIn("episodes=002", details)
        self.assertIn('"width":1280', details)

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
        feature_cache = temp_dir / "timing_detection_cache" / "features" / "cached.npz"
        feature_cache.parent.mkdir(parents=True)
        feature_cache.write_bytes(b"features")

        checkpoint = initialize_episode_checkpoints(temp_dir, "same")

        self.assertEqual(checkpoint["render_pipeline_version"], RENDER_PIPELINE_VERSION)
        self.assertFalse(stale.exists())
        self.assertTrue(feature_cache.exists())

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

    def test_old_detector_output_checkpoint_is_invalid(self):
        tmp_dir = self.make_workspace_temp_dir()
        job = self.make_job(tmp_dir)
        job["timing_detection"]["enabled"] = True
        artifacts = build_output_artifacts(job, job["output_dir"])
        artifacts["job_output_dir"].mkdir(parents=True)
        artifacts["output_video"].write_bytes(b"video")
        artifacts["output_txt"].write_text("00:00:00 - 1 серия\n", encoding="utf-8")
        manifest = {
            "render_complete": True,
            "render_pipeline_version": RENDER_PIPELINE_VERSION,
            "title": job["title"],
            "season": "01",
            "episodes_range": job["episodes_range"],
            "output_video": artifacts["output_video"].name,
            "output_timestamps": artifacts["output_txt"].name,
            "timing_detection": {"enabled": True},
        }
        artifacts["output_manifest"].write_text(json.dumps(manifest), encoding="utf-8")

        with patch("lib.pipeline.ffprobe_duration", return_value=10.0):
            self.assertIsNone(load_render_checkpoint(job, artifacts))
            manifest["timing_detection"]["algorithm_version"] = DETECTOR_RESULT_VERSION
            artifacts["output_manifest"].write_text(json.dumps(manifest), encoding="utf-8")
            self.assertIsNotNone(load_render_checkpoint(job, artifacts))

    def test_navigation_label_change_reuses_existing_output_paths(self):
        tmp_dir = self.make_workspace_temp_dir()
        job = self.make_job(tmp_dir)
        original = build_output_artifacts(job, job["output_dir"])
        original["job_output_dir"].mkdir(parents=True)
        original["output_video"].write_bytes(b"video")
        job["processing"]["naming"] = {
            "navigation_label": "Перерождение",
            "source": "manual",
        }

        renamed = build_output_artifacts(job, job["output_dir"])

        self.assertEqual(renamed["output_video"], original["output_video"])
        self.assertIn("Перерождение", renamed["pretty_base_name"])

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

    @patch("lib.pipeline.validate_episode_render", return_value=VALIDATION)
    def test_recovered_checkpoint_requires_enabled_mode(self, _mock_validate):
        tmp_dir = self.make_workspace_temp_dir()
        source = tmp_dir / "source.mkv"
        source.write_bytes(b"source")
        episode_info = {"episode": 1, "path": str(source), "duration": 10.0}
        episode_dir = tmp_dir / "episode_001"
        episode_dir.mkdir()
        work = episode_dir / "rendered.work.mkv"
        work.write_bytes(b"video")
        save_episode_checkpoint(episode_dir, episode_info, work, {
            "episode": 1,
            "source_file": str(source),
            "expected_cleaned_duration": 10.0,
            "cleaned_duration": 10.0,
            "audio_recovery": {"enabled": True, "applied": True},
        })

        self.assertIsNone(load_episode_checkpoint(tmp_dir, episode_info))
        self.assertIsNotNone(
            load_episode_checkpoint(tmp_dir, episode_info, audio_recovery_enabled=True)
        )

    @patch("lib.pipeline.validate_episode_render", return_value=VALIDATION)
    def test_automatic_tail_checkpoint_is_reusable_without_audiofix(self, _mock_validate):
        tmp_dir = self.make_workspace_temp_dir()
        source = tmp_dir / "source.mkv"
        source.write_bytes(b"source")
        episode_info = {"episode": 1, "path": str(source), "duration": 10.0}
        episode_dir = tmp_dir / "episode_001"
        episode_dir.mkdir()
        work = episode_dir / "rendered.work.mkv"
        work.write_bytes(b"video")
        save_episode_checkpoint(episode_dir, episode_info, work, {
            "episode": 1,
            "source_file": str(source),
            "expected_cleaned_duration": 10.0,
            "cleaned_duration": 10.0,
            "audio_recovery": {
                "enabled": False,
                "applied": True,
                "automatic": True,
                "reasons": ["early_end"],
            },
        })

        self.assertIsNotNone(load_episode_checkpoint(tmp_dir, episode_info))

    @patch("lib.pipeline.validate_episode_render")
    def test_episode_checkpoint_rejects_expected_duration_drift(self, mock_validate):
        validation = dict(VALIDATION)
        validation["duration"] = 9.6
        mock_validate.return_value = validation
        tmp_dir = self.make_workspace_temp_dir()
        source = tmp_dir / "source.mkv"
        source.write_bytes(b"source")
        episode_info = {"episode": 1, "path": str(source), "duration": 10.0}
        episode_dir = tmp_dir / "episode_001"
        episode_dir.mkdir()
        work = episode_dir / "rendered.work.mkv"
        work.write_bytes(b"video")

        with self.assertRaisesRegex(RuntimeError, "duration mismatch 0.400s"):
            save_episode_checkpoint(episode_dir, episode_info, work, {
                "episode": 1,
                "source_file": str(source),
                "expected_cleaned_duration": 10.0,
                "cleaned_duration": 10.0,
            })

        self.assertTrue(work.exists())

    def test_one_hundred_actual_durations_build_exact_cumulative_timeline(self):
        episodes = [
            {"episode": number, "cleaned_duration": 1331.375}
            for number in range(1, 101)
        ]

        timestamps = build_timestamps_from_episodes(episodes)

        self.assertEqual(timestamps[0], "00:00:00 - 1 серия")
        self.assertEqual(timestamps[-1], "36:36:46 - 100 серия")

    def test_multi_season_artifacts_and_timestamps_use_season_labels(self):
        tmp_dir = self.make_workspace_temp_dir()
        job = self.make_job(tmp_dir)
        job.update({
            "processing_mode": "multi_season",
            "processing": {"season_range": "1-3"},
        })

        artifacts = build_output_artifacts(job, job["output_dir"])
        timestamps = build_multi_season_timestamps([
            {"season": 1, "episode": 1, "cleaned_duration": 10},
            {"season": 2, "episode": 1, "cleaned_duration": 10},
        ])

        self.assertEqual(
            artifacts["pretty_base_name"],
            "Тест серий - 1-3 Сезон ВСЕ СЕРИИ [Без OP/ED]",
        )
        self.assertEqual(timestamps, [
            "00:00:00 - 1 сезон, 1 серия",
            "00:00:10 - 2 сезон, 1 серия",
        ])

    def test_later_season_part_restarts_source_numbering(self):
        episodes = renumber_season_part_episodes(
            [{"episode": 1}, {"episode": 2}],
            season=5,
            episode_offset=12,
        )

        self.assertEqual(
            [(episode["season"], episode["source_episode"], episode["episode"]) for episode in episodes],
            [(5, 1, 13), (5, 2, 14)],
        )

    def test_later_season_part_keeps_continuous_source_numbering(self):
        episodes = renumber_season_part_episodes(
            [{"episode": 14}, {"episode": 15}],
            season=5,
            episode_offset=13,
            source_episode_start=14,
        )

        self.assertEqual(
            [(episode["season"], episode["source_episode"], episode["episode"]) for episode in episodes],
            [(5, 14, 14), (5, 15, 15)],
        )

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
            patch("lib.pipeline.collect_episode_files", return_value=(None, episode_files, [], [])),
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
            {"episode": 1, "path": str(tmp_dir / "ep1.mkv"), "duration": 10.0, "frame_rate": "30/1", "width": 1440, "height": 1080},
            {"episode": 2, "path": str(tmp_dir / "ep2.mkv"), "duration": 10.0, "frame_rate": "24000/1001", "width": 1920, "height": 1080},
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
        self.assertTrue(all(
            call.args[4]["frame_rate"] == "30/1"
            and call.args[4]["frame_width"] == 1920
            and call.args[4]["frame_height"] == 1080
            for call in render_mock.call_args_list
        ))
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
            {"episode": 1, "path": str(tmp_dir / "ep1.mkv"), "duration": 10.0, "frame_rate": "30/1", "width": 1920, "height": 1080},
            {"episode": 2, "path": str(tmp_dir / "ep2.mkv"), "duration": 10.0, "frame_rate": "30/1", "width": 1920, "height": 1080},
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
