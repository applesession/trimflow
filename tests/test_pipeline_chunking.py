import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.pipeline import process_job, split_episode_infos_into_chunks


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

    @patch("lib.pipeline.cleanup_job_artifacts")
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
    @patch("lib.pipeline.reset_temp_dir")
    def test_process_job_uses_chunk_outputs_for_final_concat(
        self,
        mock_reset_temp_dir,
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
        mock_cleanup,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        temp_dir = tmp_dir / "temp" / "Chunk_Test"
        temp_dir.mkdir(parents=True, exist_ok=True)
        mock_reset_temp_dir.return_value = temp_dir
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
                temp_dir / "chunk_001" / "concat_output.mkv",
                temp_dir / "chunk_002" / "concat_output.mkv",
            ],
        )
        self.assertEqual(mock_render_concat.call_count, 3)
        mock_render_final.assert_called_once()
        manifest_episodes = mock_build_compact_manifest.call_args.kwargs["manifest_episodes"]
        self.assertEqual([item["episode"] for item in manifest_episodes], [1, 2, 3])
        processing_metadata = mock_build_compact_manifest.call_args.kwargs["processing_metadata"]
        self.assertEqual(processing_metadata["chunk_size_episodes"], 2)
        self.assertEqual(processing_metadata["chunks_count"], 2)


if __name__ == "__main__":
    unittest.main()
