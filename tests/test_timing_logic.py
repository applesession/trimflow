import unittest

from lib.aniskip import build_quality_summary, summarize_skips
from lib.detector import build_detector_cache_key, normalize_timing_detection_config
from lib.media import build_hybrid_subsegments
from lib.pipeline import build_type_info


class TimingLogicTests(unittest.TestCase):
    def test_normalize_timing_detection_config_uses_defaults(self):
        config = normalize_timing_detection_config({})

        self.assertFalse(config["enabled"])
        self.assertEqual(config["search_head_seconds"], 300)
        self.assertEqual(config["search_tail_seconds"], 210)
        self.assertEqual(config["feature_sample_rate"], 16000)
        self.assertTrue(config["cache_enabled"])
        self.assertEqual(config["auto_cut_min_confidence"], "high")

    def test_normalize_timing_detection_config_applies_overrides(self):
        config = normalize_timing_detection_config({
            "timing_detection": {
                "enabled": True,
                "min_support_episodes": "5",
                "frame_step_seconds": "0.5",
                "feature_sample_rate": "22050",
                "consensus_min_similarity": "0.81",
            }
        })

        self.assertTrue(config["enabled"])
        self.assertEqual(config["min_support_episodes"], 5)
        self.assertEqual(config["frame_step_seconds"], 0.5)
        self.assertEqual(config["feature_sample_rate"], 22050)
        self.assertEqual(config["consensus_min_similarity"], 0.81)

    def test_build_detector_cache_key_is_stable(self):
        config = normalize_timing_detection_config({})
        episode_infos = [
            {"episode": 1, "path": "a.mkv", "duration": 1400.0},
            {"episode": 2, "path": "b.mkv", "duration": 1401.0},
        ]

        key_a = build_detector_cache_key(episode_infos, config)
        key_b = build_detector_cache_key(list(reversed(episode_infos)), config)

        self.assertEqual(key_a, key_b)

    def test_build_detector_cache_key_changes_on_config_change(self):
        base_config = normalize_timing_detection_config({})
        changed_config = normalize_timing_detection_config({
            "timing_detection": {
                "consensus_min_similarity": 0.9,
            }
        })
        episode_infos = [{"episode": 1, "path": "a.mkv", "duration": 1400.0}]

        self.assertNotEqual(
            build_detector_cache_key(episode_infos, base_config),
            build_detector_cache_key(episode_infos, changed_config),
        )

    def test_build_detector_cache_key_changes_on_reference_inputs(self):
        config = normalize_timing_detection_config({})
        episode_infos = [{"episode": 1, "path": "a.mkv", "duration": 1400.0}]
        no_reference = {"aniskip_by_episode": {1: {"segments": []}}}
        with_reference = {
            "aniskip_by_episode": {
                1: {
                    "segments": [
                        {
                            "type": "op",
                            "start": 10.0,
                            "end": 100.0,
                            "source": "aniskip_exact",
                        }
                    ]
                }
            }
        }

        self.assertNotEqual(
            build_detector_cache_key(episode_infos, config, no_reference),
            build_detector_cache_key(episode_infos, config, with_reference),
        )

    def test_build_detector_cache_key_changes_on_anilibria_reference_inputs(self):
        config = normalize_timing_detection_config({})
        episode_infos = [{"episode": 1, "path": "a.mkv", "duration": 1400.0}]
        no_reference = {"anilibria_by_episode": {1: {"segments": []}}}
        with_reference = {
            "anilibria_by_episode": {
                1: {
                    "segments": [
                        {
                            "type": "op",
                            "start": 10.0,
                            "end": 100.0,
                            "source": "anilibria_exact",
                        }
                    ]
                }
            }
        }

        self.assertNotEqual(
            build_detector_cache_key(episode_infos, config, no_reference),
            build_detector_cache_key(episode_infos, config, with_reference),
        )

    def test_build_type_info_keeps_reference_metadata(self):
        info = build_type_info(
            source="audio_fingerprint",
            confidence="high",
            match_strategy="aniskip_reference",
            reference_episode=3,
            reference_source="aniskip_exact",
            reference_similarity=0.97,
        )

        self.assertEqual(info["match_strategy"], "aniskip_reference")
        self.assertEqual(info["reference_episode"], 3)
        self.assertEqual(info["reference_source"], "aniskip_exact")
        self.assertEqual(info["reference_similarity"], 0.97)

    def test_build_type_info_supports_anilibria_reference_metadata(self):
        info = build_type_info(
            source="audio_fingerprint",
            confidence="high",
            match_strategy="anilibria_reference",
            reference_episode=2,
            reference_source="anilibria_exact",
            reference_similarity=0.99,
        )

        self.assertEqual(info["match_strategy"], "anilibria_reference")
        self.assertEqual(info["reference_episode"], 2)
        self.assertEqual(info["reference_source"], "anilibria_exact")
        self.assertEqual(info["reference_similarity"], 0.99)

    def test_summarize_skips_marks_manual_review(self):
        remove_segments = [{
            "type": "op",
            "start": 10.0,
            "end": 100.0,
            "source": "audio_fingerprint",
            "confidence": "high",
        }]
        per_type = {
            "op": {
                "source": "audio_fingerprint",
                "confidence": "high",
                "review_required": False,
                "removed": True,
            },
            "ed": {
                "source": "audio_fingerprint",
                "confidence": "medium",
                "review_required": True,
                "removed": False,
            },
        }

        summary = summarize_skips(remove_segments, ["op", "ed"], per_type)

        self.assertTrue(summary["op"])
        self.assertFalse(summary["ed"])
        self.assertEqual(summary["op_source"], "audio_fingerprint")
        self.assertEqual(summary["ed_confidence"], "medium")
        self.assertIn("ED requires manual review (medium)", summary["warnings"])

    def test_build_quality_summary_counts_strategies(self):
        manifest_episodes = [
            {
                "episode": 1,
                "skip_summary": {"op": True, "ed": True, "warnings": []},
                "timing_info": {
                    "strategy": "anilibria_only",
                    "per_type": {
                        "op": {"source": "anilibria_exact", "confidence": "high"},
                        "ed": {"source": "anilibria_exact", "confidence": "high"},
                    },
                },
            },
            {
                "episode": 2,
                "skip_summary": {"op": True, "ed": False, "warnings": ["ED not found"]},
                "timing_info": {
                    "strategy": "manual_review",
                    "per_type": {
                        "op": {"source": "audio_fingerprint", "confidence": "high"},
                        "ed": {"source": "not_found", "confidence": "none"},
                    },
                },
            },
        ]

        summary = build_quality_summary(manifest_episodes, ["op", "ed"])

        self.assertEqual(summary["episodes_anilibria_only"], 1)
        self.assertEqual(summary["episodes_manual_review"], 1)
        self.assertEqual(summary["episodes_with_warnings"], [2])
        self.assertEqual(summary["episodes_detector_completed_op_only"], 1)
        self.assertEqual(summary["episodes_detector_high"], 1)

    def test_build_hybrid_subsegments_keeps_copy_when_no_adjacent_remove(self):
        result = build_hybrid_subsegments((100.0, 200.0), [], 3.0)

        self.assertEqual(result, [{
            "start": 100.0,
            "end": 200.0,
            "cut_mode": "copy",
        }])

    def test_build_hybrid_subsegments_splits_between_two_remove_boundaries(self):
        remove_segments = [
            {"start": 0.0, "end": 100.0},
            {"start": 200.0, "end": 260.0},
        ]

        result = build_hybrid_subsegments((100.0, 200.0), remove_segments, 3.0)

        self.assertEqual(result, [
            {"start": 100.0, "end": 103.0, "cut_mode": "precise"},
            {"start": 103.0, "end": 197.0, "cut_mode": "copy"},
            {"start": 197.0, "end": 200.0, "cut_mode": "precise"},
        ])

    def test_build_hybrid_subsegments_uses_precise_for_short_segment(self):
        remove_segments = [
            {"start": 0.0, "end": 100.0},
            {"start": 104.0, "end": 160.0},
        ]

        result = build_hybrid_subsegments((100.0, 104.0), remove_segments, 3.0)

        self.assertEqual(result, [{
            "start": 100.0,
            "end": 104.0,
            "cut_mode": "precise",
        }])


if __name__ == "__main__":
    unittest.main()
