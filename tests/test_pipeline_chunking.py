import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.pipeline import (
    build_delivery_config,
    build_chunk_fingerprint,
    build_output_artifacts,
    deliver_rendered_output,
    initialize_chunk_checkpoint,
    load_chunk_checkpoint,
    load_render_checkpoint,
    process_episode,
    process_job,
    save_chunk_checkpoint,
    split_episode_infos_into_chunks,
)


class PipelineChunkingTests(unittest.TestCase):
    def make_workspace_temp_dir(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(dir=root))
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def make_job(self, tmp_dir, *, chunk_size=12, episodes_range="001-003"):
        watermark_path = tmp_dir / "watermark.png"
        watermark_path.write_bytes(b"png")
        return {
            "title": "Chunk Test",
            "title_ru": "Тест чанков",
            "season": 1,
            "episodes_range": episodes_range,
            "source": {"type": "local", "input_dir": str(tmp_dir / "input")},
            "output_dir": str(tmp_dir / "output"),
            "watermark_path": str(watermark_path),
            "skip_types": ["op", "ed"],
            "cleanup": {"downloads": False, "temp": False, "output": False},
            "processing": {"chunk_size_episodes": chunk_size},
            "timing_detection": {"enabled": False},
            "timing_providers": {"anilibria_enabled": False, "aniskip_enabled": False},
            "delivery": {"s3_enabled": False, "vk_enabled": False},
            "encoding": {
                "video_codec": "libx264",
                "preset": "fast",
                "cq": 23,
                "segment_video_codec": "libx264",
                "segment_preset": "fast",
                "segment_cq": 18,
                "segment_cut_mode": "copy",
                "audio_codec": "aac",
            },
        }

    def test_split_episode_infos_into_chunks_preserves_order(self):
        episode_infos = [
            {"episode": 1},
            {"episode": 2},
            {"episode": 3},
            {"episode": 4},
            {"episode": 5},
        ]

        chunks = split_episode_infos_into_chunks(episode_infos, 2)

        self.assertEqual([[item["episode"] for item in chunk] for chunk in chunks], [[1, 2], [3, 4], [5]])

    def test_chunk_fingerprint_change_clears_stale_temp(self):
        temp_dir = self.make_workspace_temp_dir() / "temp" / "title"
        temp_dir.mkdir(parents=True)
        initialize_chunk_checkpoint(temp_dir, "first")
        marker = temp_dir / "chunk_001" / "rendered.mkv"
        marker.parent.mkdir()
        marker.write_bytes(b"video")

        initialize_chunk_checkpoint(temp_dir, "first")
        self.assertTrue(marker.exists())

        initialize_chunk_checkpoint(temp_dir, "second")
        self.assertFalse(marker.exists())

    def test_chunk_fingerprint_tracks_render_inputs(self):
        tmp_dir = self.make_workspace_temp_dir()
        job = self.make_job(tmp_dir, chunk_size=1, episodes_range="001")
        source_file = tmp_dir / "episode.mkv"
        source_file.write_bytes(b"source")
        episode_infos = [{"episode": 1, "path": str(source_file), "duration": 10.0}]

        def fingerprint():
            return build_chunk_fingerprint(
                job,
                episode_infos,
                watermark_path=job["watermark_path"],
                processing=job["processing"],
                timing_detection=job["timing_detection"],
                segment_encoding=job["encoding"],
                preferred_language="rus",
            )

        original = fingerprint()
        job["encoding"]["cq"] = 24
        self.assertNotEqual(original, fingerprint())
        job["encoding"]["cq"] = 23
        source_file.write_bytes(b"changed source")
        self.assertNotEqual(original, fingerprint())

    @patch("lib.pipeline.ffprobe_media_signature", return_value={"video": {"codec_name": "h264"}, "audio": None})
    @patch("lib.pipeline.ffprobe_duration", return_value=10.0)
    def test_chunk_checkpoint_requires_unchanged_complete_media(self, mock_duration, mock_signature):
        temp_dir = self.make_workspace_temp_dir()
        work_dir = temp_dir / "chunk_001.work"
        chunk_dir = temp_dir / "chunk_001"
        work_dir.mkdir()
        output = work_dir / "rendered.mkv"
        output.write_bytes(b"video")
        manifest_episodes = [{"episode": 1, "cleaned_duration": 10.0}]
        save_chunk_checkpoint(work_dir, 1, [1], manifest_episodes, output)
        work_dir.replace(chunk_dir)

        self.assertIsNotNone(load_chunk_checkpoint(temp_dir, 1, [1]))
        (chunk_dir / "rendered.mkv").write_bytes(b"corrupt")
        self.assertIsNone(load_chunk_checkpoint(temp_dir, 1, [1]))
        self.assertIsNone(load_chunk_checkpoint(temp_dir, 1, [2]))

    @patch("lib.pipeline.ffprobe_duration", return_value=10.0)
    def test_load_render_checkpoint_requires_matching_complete_manifest(self, mock_duration):
        tmp_dir = self.make_workspace_temp_dir()
        job = self.make_job(tmp_dir)
        artifacts = build_output_artifacts(job, job["output_dir"])
        artifacts["job_output_dir"].mkdir(parents=True)
        artifacts["output_video"].write_bytes(b"video")
        artifacts["output_txt"].write_text("00:00:00 - 1 серия\n", encoding="utf-8")
        artifacts["output_manifest"].write_text(json.dumps({
            "render_complete": True,
            "title": job["title"],
            "season": "01",
            "episodes_range": job["episodes_range"],
            "output_video": artifacts["output_video"].name,
            "output_timestamps": artifacts["output_txt"].name,
        }), encoding="utf-8")

        self.assertIsNotNone(load_render_checkpoint(job, artifacts))

        legacy_manifest = json.loads(artifacts["output_manifest"].read_text(encoding="utf-8"))
        legacy_manifest.pop("render_complete")
        artifacts["output_manifest"].write_text(json.dumps(legacy_manifest), encoding="utf-8")
        self.assertIsNone(load_render_checkpoint(job, artifacts))

        legacy_manifest["render_complete"] = True
        artifacts["output_manifest"].write_text(json.dumps(legacy_manifest), encoding="utf-8")
        job["episodes_range"] = "001-004"
        self.assertIsNone(load_render_checkpoint(job, artifacts))

    @patch("lib.pipeline.get_preferred_audio_stream", return_value=0)
    @patch("lib.pipeline.render_final", side_effect=RuntimeError("ffmpeg failed"))
    @patch("lib.pipeline.filter_episode_files")
    @patch("lib.pipeline.collect_episode_files")
    @patch("lib.pipeline.reset_temp_dir")
    def test_render_failure_preserves_downloads_without_checkpoint(
        self,
        mock_reset_temp_dir,
        mock_collect_episode_files,
        mock_filter_episode_files,
        mock_render_final,
        mock_audio_stream,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        job = self.make_job(tmp_dir, episodes_range="010")
        job["processing_mode"] = "single_episode"
        job["cleanup"] = {"downloads": True, "temp": True, "output": True}
        download_dir = tmp_dir / "downloads" / "Chunk_Test"
        temp_dir = tmp_dir / "temp" / "Chunk_Test"
        download_dir.mkdir(parents=True)
        temp_dir.mkdir(parents=True)
        episode_files = [(10, Path("/tmp/ep10.mkv"))]
        mock_reset_temp_dir.return_value = temp_dir
        mock_collect_episode_files.return_value = (download_dir, episode_files, [])
        mock_filter_episode_files.return_value = (episode_files, [])

        with self.assertRaisesRegex(RuntimeError, "ffmpeg failed"):
            process_job(job)

        artifacts = build_output_artifacts(job, job["output_dir"])
        self.assertTrue(download_dir.exists())
        self.assertFalse(temp_dir.exists())
        self.assertFalse(artifacts["output_manifest"].exists())

    @patch("lib.pipeline.render_final")
    @patch("lib.pipeline.collect_episode_files")
    @patch("lib.pipeline.deliver_to_vk")
    @patch("lib.pipeline.ffprobe_duration", return_value=10.0)
    @patch("lib.pipeline.prepare_temp_dir")
    def test_retry_uses_render_checkpoint_and_only_retries_delivery(
        self,
        mock_prepare_temp_dir,
        mock_duration,
        mock_deliver_to_vk,
        mock_collect_episode_files,
        mock_render_final,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        mock_prepare_temp_dir.return_value = tmp_dir / "temp"
        job = self.make_job(tmp_dir)
        job["delivery"] = {"s3_enabled": False, "vk_enabled": True, "vk_preview_enabled": False}
        job["cleanup"]["output"] = True
        artifacts = build_output_artifacts(job, job["output_dir"])
        artifacts["job_output_dir"].mkdir(parents=True)
        artifacts["output_video"].write_bytes(b"video")
        timestamps = ["00:00:00 - 1 серия"]
        manifest = {
            "render_complete": True,
            "title": job["title"],
            "season": "01",
            "episodes_range": job["episodes_range"],
            "episodes_count": 3,
            "output_video": artifacts["output_video"].name,
            "output_timestamps": artifacts["output_txt"].name,
            "quality_summary": {},
        }
        mock_deliver_to_vk.side_effect = RuntimeError("vk down")
        first = deliver_rendered_output(
            job,
            build_delivery_config(job),
            output_video=artifacts["output_video"],
            output_txt=artifacts["output_txt"],
            output_manifest=artifacts["output_manifest"],
            manifest=manifest,
            timestamps=timestamps,
            pretty_base_name=artifacts["pretty_base_name"],
            temp_dir=tmp_dir / "temp",
            total_episodes=3,
        )
        self.assertFalse(first["delivery_summary"]["vk"]["video_uploaded"])
        self.assertTrue(artifacts["output_video"].exists())

        mock_deliver_to_vk.side_effect = None
        mock_deliver_to_vk.return_value = {"video_uploaded": True, "post_created": True}
        result = process_job(job)

        self.assertTrue(result["delivery_summary"]["vk"]["video_uploaded"])
        mock_collect_episode_files.assert_not_called()
        mock_render_final.assert_not_called()
        self.assertFalse(artifacts["output_video"].exists())

    @patch("lib.pipeline.cleanup_job_artifacts")
    @patch("lib.pipeline.write_outputs")
    @patch("lib.pipeline.render_final")
    @patch("lib.pipeline.process_episode")
    @patch("lib.pipeline.build_detector_context")
    @patch("lib.pipeline.build_episode_infos")
    @patch("lib.pipeline.filter_episode_files")
    @patch("lib.pipeline.collect_episode_files")
    @patch("lib.pipeline.reset_temp_dir")
    def test_process_job_single_episode_mode_skips_detector_and_segment_pipeline(
        self,
        mock_reset_temp_dir,
        mock_collect_episode_files,
        mock_filter_episode_files,
        mock_build_episode_infos,
        mock_build_detector_context,
        mock_process_episode,
        mock_render_final,
        mock_write_outputs,
        mock_cleanup,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        temp_dir = tmp_dir / "temp" / "Chunk_Test"
        temp_dir.mkdir(parents=True, exist_ok=True)
        mock_reset_temp_dir.return_value = temp_dir
        job = self.make_job(tmp_dir, episodes_range="010")
        job["processing_mode"] = "single_episode"

        episode_files = [
            (10, Path("/tmp/ep10.mkv")),
        ]
        mock_collect_episode_files.return_value = (None, episode_files, [])
        mock_filter_episode_files.return_value = (episode_files, [])

        result = process_job(job)

        self.assertTrue(result["output_video"].endswith(".mkv"))
        self.assertEqual(result["quality_summary"], {})
        mock_build_episode_infos.assert_not_called()
        mock_build_detector_context.assert_not_called()
        mock_process_episode.assert_not_called()
        mock_render_final.assert_called_once()
        self.assertEqual(mock_render_final.call_args.kwargs["concat_output"], Path("/tmp/ep10.mkv"))
        self.assertEqual(mock_render_final.call_args.kwargs["encoding"]["audio_codec"], "aac")
        self.assertIn("10 Серия", result["output_display_name"])

    @patch("lib.pipeline.cleanup_job_artifacts")
    @patch("lib.pipeline.ffprobe_media_signature", return_value={"video": {"codec_name": "h264"}, "audio": None})
    @patch("lib.pipeline.ffprobe_duration", return_value=10.0)
    @patch("lib.pipeline.write_outputs")
    @patch("lib.pipeline.build_compact_manifest")
    @patch("lib.pipeline.build_quality_summary")
    @patch("lib.pipeline.render_final")
    @patch("lib.pipeline.render_concat")
    @patch("lib.pipeline.create_concat_file")
    @patch("lib.pipeline.process_episode")
    @patch("lib.pipeline.build_detector_context")
    @patch("lib.pipeline.build_episode_infos")
    @patch("lib.pipeline.filter_episode_files")
    @patch("lib.pipeline.collect_episode_files")
    @patch("lib.pipeline.prepare_temp_dir")
    def test_process_job_uses_chunk_outputs_for_final_concat(
        self,
        mock_prepare_temp_dir,
        mock_collect_episode_files,
        mock_filter_episode_files,
        mock_build_episode_infos,
        mock_build_detector_context,
        mock_process_episode,
        mock_create_concat_file,
        mock_render_concat,
        mock_render_final,
        mock_build_quality_summary,
        mock_build_compact_manifest,
        mock_write_outputs,
        mock_duration,
        mock_signature,
        mock_cleanup,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        temp_dir = tmp_dir / "temp" / "Chunk_Test"
        temp_dir.mkdir(parents=True, exist_ok=True)
        mock_prepare_temp_dir.return_value = temp_dir
        job = self.make_job(tmp_dir, chunk_size=2)

        episode_files = [
            (1, Path("/tmp/ep1.mkv")),
            (2, Path("/tmp/ep2.mkv")),
            (3, Path("/tmp/ep3.mkv")),
        ]
        episode_infos = [
            {"episode": 1, "path": "/tmp/ep1.mkv", "duration": 100.0},
            {"episode": 2, "path": "/tmp/ep2.mkv", "duration": 100.0},
            {"episode": 3, "path": "/tmp/ep3.mkv", "duration": 100.0},
        ]

        mock_collect_episode_files.return_value = (None, episode_files, [])
        mock_filter_episode_files.return_value = (episode_files, [])
        mock_build_episode_infos.return_value = episode_infos
        mock_build_detector_context.return_value = {"enabled": False, "available": False, "reason": "disabled"}

        def process_episode_side_effect(
            episode_info,
            skip_types,
            episode_temp_dir,
            cumulative_time,
            detector_context,
            segment_encoding,
            anilibria_result,
            aniskip_result,
            preferred_language="rus",
        ):
            segment_output = episode_temp_dir / f"ep{episode_info['episode']:03d}_seg000_000.mkv"
            manifest_episode = {
                "episode": episode_info["episode"],
                "source_file": episode_info["path"],
                "original_duration": 100.0,
                "cleaned_duration": 90.0,
                "segment_cut_mode": "copy",
                "timing_info": {
                    "strategy": "manual_review",
                    "confidence": "none",
                    "review_required": False,
                    "per_type": {
                        "op": {
                            "source": "not_found",
                            "confidence": "none",
                            "interval": None,
                            "review_required": False,
                            "removed": False,
                            "reason": "not_found",
                            "consensus_score": None,
                            "support_episode_count": 0,
                            "reference_interval": None,
                            "cache_hit": False,
                            "match_strategy": "not_found",
                            "reference_episode": None,
                            "reference_source": "none",
                            "reference_similarity": None,
                        },
                        "ed": {
                            "source": "not_found",
                            "confidence": "none",
                            "interval": None,
                            "review_required": False,
                            "removed": False,
                            "reason": "not_found",
                            "consensus_score": None,
                            "support_episode_count": 0,
                            "reference_interval": None,
                            "cache_hit": False,
                            "match_strategy": "not_found",
                            "reference_episode": None,
                            "reference_source": "none",
                            "reference_similarity": None,
                        },
                    },
                    "used_fallback": False,
                    "request_error": None,
                    "detector_error": None,
                    "reference_episodes": {},
                },
                "skip_summary": {},
            }
            timestamp_line = f"00:00:{episode_info['episode']:02d} - {episode_info['episode']} серия"
            return cumulative_time + 90.0, [segment_output], manifest_episode, timestamp_line

        mock_process_episode.side_effect = process_episode_side_effect
        mock_render_final.side_effect = lambda **kwargs: Path(kwargs["output_video"]).write_bytes(b"video")
        mock_render_concat.side_effect = lambda concat_file, concat_output, **kwargs: Path(concat_output).write_bytes(b"concat")
        mock_build_quality_summary.return_value = {"episodes_count": 3}
        mock_build_compact_manifest.return_value = {"episodes": [], "delivery_summary": {}}

        result = process_job(job)

        self.assertTrue(result["output_video"].endswith(".mkv"))
        self.assertEqual(mock_create_concat_file.call_count, 3)
        first_chunk_segments = mock_create_concat_file.call_args_list[0].args[0]
        second_chunk_segments = mock_create_concat_file.call_args_list[1].args[0]
        final_concat_inputs = mock_create_concat_file.call_args_list[2].args[0]
        self.assertEqual(len(first_chunk_segments), 2)
        self.assertEqual(len(second_chunk_segments), 1)
        self.assertEqual(
            final_concat_inputs,
            [
                temp_dir / "chunk_001" / "rendered.mkv",
                temp_dir / "chunk_002" / "rendered.mkv",
            ],
        )
        self.assertEqual(mock_render_concat.call_count, 3)
        self.assertEqual(mock_render_final.call_count, 2)
        self.assertFalse(mock_render_concat.call_args.kwargs["allow_reencode"])
        manifest_episodes = mock_build_compact_manifest.call_args.kwargs["manifest_episodes"]
        self.assertEqual([item["episode"] for item in manifest_episodes], [1, 2, 3])
        processing_metadata = mock_build_compact_manifest.call_args.kwargs["processing_metadata"]
        self.assertEqual(processing_metadata["chunk_size_episodes"], 2)
        self.assertEqual(processing_metadata["chunks_count"], 2)
        self.assertTrue(processing_metadata["resumable_final_chunks"])

    @patch("lib.pipeline.ffprobe_media_signature", return_value={"video": {"codec_name": "h264"}, "audio": None})
    @patch("lib.pipeline.ffprobe_duration", return_value=10.0)
    @patch("lib.pipeline.build_compact_manifest")
    @patch("lib.pipeline.build_quality_summary", return_value={})
    @patch("lib.pipeline.render_final")
    @patch("lib.pipeline.render_concat")
    @patch("lib.pipeline.process_episode")
    @patch("lib.pipeline.build_detector_context", return_value={"enabled": False, "available": False, "reason": "disabled"})
    @patch("lib.pipeline.build_episode_infos")
    @patch("lib.pipeline.filter_episode_files")
    @patch("lib.pipeline.collect_episode_files")
    @patch("lib.pipeline.prepare_temp_dir")
    def test_retries_resume_chunks_and_final_concat(
        self,
        mock_prepare_temp_dir,
        mock_collect_episode_files,
        mock_filter_episode_files,
        mock_build_episode_infos,
        mock_detector,
        mock_process_episode,
        mock_render_concat,
        mock_render_final,
        mock_quality,
        mock_manifest,
        mock_duration,
        mock_signature,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        temp_dir = tmp_dir / "temp" / "Chunk_Test"
        temp_dir.mkdir(parents=True)
        mock_prepare_temp_dir.return_value = temp_dir
        job = self.make_job(tmp_dir, chunk_size=1, episodes_range="001-002")
        episode_files = [(1, Path("/tmp/ep1.mkv")), (2, Path("/tmp/ep2.mkv"))]
        episode_infos = [
            {"episode": 1, "path": "/tmp/ep1.mkv", "duration": 10.0},
            {"episode": 2, "path": "/tmp/ep2.mkv", "duration": 10.0},
        ]
        mock_collect_episode_files.return_value = (None, episode_files, [])
        mock_filter_episode_files.return_value = (episode_files, [])
        mock_build_episode_infos.return_value = episode_infos

        def process_episode_side_effect(episode_info, *args, **kwargs):
            cumulative_time = args[2]
            episode = episode_info["episode"]
            return cumulative_time + 10.0, [Path(args[1]) / f"ep{episode}.mkv"], {
                "episode": episode,
                "cleaned_duration": 10.0,
            }, f"00:00:00 - {episode} серия"

        mock_process_episode.side_effect = process_episode_side_effect
        render_calls = 0

        def render_final_side_effect(**kwargs):
            nonlocal render_calls
            render_calls += 1
            if render_calls == 2:
                raise RuntimeError("render failed")
            Path(kwargs["output_video"]).write_bytes(b"chunk")

        mock_render_final.side_effect = render_final_side_effect

        concat_calls = 0

        def render_concat_side_effect(concat_file, concat_output, **kwargs):
            nonlocal concat_calls
            concat_calls += 1
            if concat_calls == 4:
                raise RuntimeError("concat failed")
            Path(concat_output).write_bytes(b"concat")

        mock_render_concat.side_effect = render_concat_side_effect
        mock_manifest.side_effect = lambda **kwargs: {
            "delivery_summary": kwargs["delivery_summary"],
            "quality_summary": kwargs["quality_summary"],
        }

        with self.assertRaisesRegex(RuntimeError, "render failed"):
            process_job(job)

        self.assertTrue((temp_dir / "chunk_001" / "rendered.mkv").exists())
        self.assertFalse((temp_dir / "chunk_002").exists())
        self.assertFalse((temp_dir / "chunk_002.work").exists())

        with self.assertRaisesRegex(RuntimeError, "concat failed"):
            process_job(job)

        self.assertTrue((temp_dir / "chunk_002" / "rendered.mkv").exists())

        result = process_job(job)

        self.assertTrue(result["output_video"].endswith(".mkv"))
        self.assertEqual(mock_process_episode.call_count, 3)
        self.assertEqual(mock_render_final.call_count, 3)
        self.assertEqual(mock_detector.call_count, 2)
        self.assertEqual(mock_render_concat.call_count, 5)

    @patch("lib.pipeline.render_segment")
    @patch("lib.pipeline.print_skip_log")
    @patch("lib.pipeline.summarize_skips")
    def test_process_episode_caps_long_compilation_subsegments(
        self,
        mock_summarize_skips,
        mock_print_skip_log,
        mock_render_segment,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        episode_info = {
            "episode": 1,
            "path": "/tmp/ep1.mkv",
            "duration": 549.0,
        }
        detector_context = {"enabled": False, "reason": "disabled"}
        segment_encoding = {
            "cut_mode": "copy",
            "boundary_reencode_seconds": 3.0,
            "max_render_seconds": 150.0,
        }
        anilibria_result = {"segments": [], "request_error": None, "request_urls": []}
        aniskip_result = {
            "segments": [],
            "used_fallback": False,
            "request_error": None,
            "requested_episode_length": 549.0,
            "fallback_from_episode_length": None,
            "request_urls": [],
        }
        mock_summarize_skips.return_value = {"warnings": []}

        cumulative_time, segment_outputs, manifest_episode, timestamp_line = process_episode(
            episode_info,
            ["op", "ed"],
            tmp_dir,
            0.0,
            detector_context,
            segment_encoding,
            anilibria_result,
            aniskip_result,
        )

        self.assertEqual(cumulative_time, 549.0)
        self.assertEqual(len(segment_outputs), 1)
        self.assertEqual(mock_render_segment.call_count, 1)
        render_ranges = [
            (call.args[2], call.args[3])
            for call in mock_render_segment.call_args_list
        ]
        self.assertEqual(render_ranges, [(0.0, 549.0)])
        self.assertEqual(manifest_episode["kept_segments"], [
            {"start": 0.0, "end": 549.0, "cut_mode": "copy"},
        ])
        self.assertEqual(timestamp_line, "00:00:00 - 1 серия")


if __name__ == "__main__":
    unittest.main()
