import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from lib.aniskip import build_quality_summary, summarize_skips
from lib.detector import (
    DETECTOR_RESULT_VERSION,
    _build_cache_paths,
    _build_zone_results,
    _compute_feature_matrix,
    _derive_local_confidence,
    _find_pairwise_candidate,
    _load_context_from_cache,
    _needs_reference_fallback,
    _select_reference_segment,
    build_detector_cache_key,
    normalize_timing_detection_config,
)
from lib.media import build_hybrid_subsegments, cap_subsegment_durations
from lib.pipeline import build_type_info, merge_timing_sources


class TimingLogicTests(unittest.TestCase):
    @staticmethod
    def _zone_tracks(count):
        return [
            {"episode": episode, "zone_start": 0.0, "cache_hit": False}
            for episode in range(1, count + 1)
        ]

    @staticmethod
    def _pair_matcher(groups, *, score=0.9):
        groups_by_episode = {
            episode: (group_index, start_frame)
            for group_index, (episodes, start_frame) in enumerate(groups)
            for episode in episodes
        }

        def match(track_a, track_b, _config):
            group_a = groups_by_episode.get(track_a["episode"])
            group_b = groups_by_episode.get(track_b["episode"])
            if group_a is None or group_b is None or group_a[0] != group_b[0]:
                return None
            return {
                "episode_a": track_a["episode"],
                "episode_b": track_b["episode"],
                "start_frame_a": group_a[1],
                "start_frame_b": group_b[1],
                "end_frame_a": group_a[1] + 359,
                "end_frame_b": group_b[1] + 359,
                "length_frames": 360,
                "score": score,
            }

        return match

    def test_normalize_timing_detection_config_uses_defaults(self):
        config = normalize_timing_detection_config({})

        self.assertFalse(config["enabled"])
        self.assertEqual(config["search_head_seconds"], 300)
        self.assertEqual(config["search_tail_seconds"], 210)
        self.assertEqual(config["feature_sample_rate"], 16000)
        self.assertTrue(config["cache_enabled"])
        self.assertEqual(config["auto_cut_min_confidence"], "high")
        self.assertEqual(config["high_confidence_boundary_tolerance_seconds"], 2.0)

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

    def test_normalize_timing_detection_config_rejects_unsafe_values(self):
        invalid_configs = [
            {"min_support_episodes": 1},
            {"min_segment_seconds": 0},
            {"min_segment_seconds": 90, "max_segment_seconds": 45},
            {"consensus_min_similarity": 1.1},
            {"auto_cut_min_confidence": "low"},
        ]

        for timing_detection in invalid_configs:
            with self.subTest(timing_detection=timing_detection):
                with self.assertRaises(ValueError):
                    normalize_timing_detection_config({"timing_detection": timing_detection})

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

    def test_build_detector_cache_key_tracks_auto_cut_threshold(self):
        base_config = normalize_timing_detection_config({})
        disabled_config = normalize_timing_detection_config({
            "timing_detection": {"auto_cut_min_confidence": "disabled"}
        })
        episode_infos = [{"episode": 1, "path": "a.mkv", "duration": 1400.0}]

        self.assertNotEqual(
            build_detector_cache_key(episode_infos, base_config),
            build_detector_cache_key(episode_infos, disabled_config),
        )

    def test_build_detector_cache_key_tracks_result_algorithm_version(self):
        config = normalize_timing_detection_config({})
        episode_infos = [{"episode": 1, "path": "a.mkv", "duration": 1400.0}]

        current = build_detector_cache_key(episode_infos, config)
        with patch("lib.detector.DETECTOR_RESULT_VERSION", "future_version"):
            future = build_detector_cache_key(episode_infos, config)

        self.assertNotEqual(current, future)

    def test_old_detector_result_cache_is_not_loaded(self):
        config = normalize_timing_detection_config({})
        context = {
            "results": {"op": {}, "ed": {}},
            "reference_episodes": {"op": [], "ed": []},
        }
        with TemporaryDirectory() as tmp:
            paths = _build_cache_paths(Path(tmp), config, "old")
            paths["results"].mkdir(parents=True)
            paths["result_file"].write_text(
                '{"algorithm_version": "legacy", "available": true}',
                encoding="utf-8",
            )

            self.assertIsNone(_load_context_from_cache(context, paths))

    def test_local_consensus_is_independent_of_total_episode_count(self):
        config = normalize_timing_detection_config({})
        group = [(range(1, 21), 40)]
        matcher = self._pair_matcher(group)

        with patch("lib.detector._find_pairwise_candidate", side_effect=matcher):
            short = _build_zone_results(self._zone_tracks(20), config, "op")
            long = _build_zone_results(self._zone_tracks(130), config, "op")

        self.assertTrue(all(short["results"][episode]["confidence"] == "high" for episode in range(1, 21)))
        self.assertEqual(
            [short["results"][episode] for episode in range(1, 21)],
            [long["results"][episode] for episode in range(1, 21)],
        )

    def test_local_consensus_supports_multiple_opening_groups(self):
        config = normalize_timing_detection_config({})
        groups = [
            (range(1, 21), 40),
            (range(21, 41), 80),
            (range(41, 61), 120),
        ]

        with patch(
            "lib.detector._find_pairwise_candidate",
            side_effect=self._pair_matcher(groups),
        ):
            result = _build_zone_results(self._zone_tracks(130), config, "op")

        self.assertTrue(all(result["results"][episode]["confidence"] == "high" for episode in range(1, 61)))
        self.assertTrue(all(result["results"][episode]["match_strategy"] == "local_consensus" for episode in range(1, 61)))
        self.assertTrue(all(result["results"][episode]["source"] == "not_found" for episode in range(61, 131)))

    def test_local_consensus_drives_final_removed_episode_count(self):
        config = normalize_timing_detection_config({})
        matcher = self._pair_matcher([(range(1, 21), 40)])
        with patch("lib.detector._find_pairwise_candidate", side_effect=matcher):
            result = _build_zone_results(self._zone_tracks(130), config, "op")

        detector = {
            "enabled": True,
            "reason": None,
            "reference_episodes": {"op": result["reference_episodes"]},
            "results": {"op": result["results"], "ed": {}},
        }
        empty_provider = {"segments": [], "request_error": None}
        empty_aniskip = {**empty_provider, "used_fallback": False}
        manifest_episodes = []
        for episode in range(1, 131):
            per_type, segments, _, _ = merge_timing_sources(
                ["op"], empty_provider, empty_aniskip, detector, episode
            )
            manifest_episodes.append({
                "episode": episode,
                "skip_summary": summarize_skips(segments, ["op"], per_type),
                "timing_info": {
                    "strategy": "detector_only",
                    "per_type": {"op": per_type["op"]},
                },
            })

        summary = build_quality_summary(manifest_episodes, ["op"])

        self.assertEqual(summary["episodes_with_op_removed"], 20)

    def test_local_consensus_rejects_small_or_unstable_groups(self):
        config = normalize_timing_detection_config({})
        too_small = {
            "support_episode_count": 2,
            "score": 0.99,
            "start": 10.0,
            "end": 100.0,
            "votes": [{"start": 10.0, "end": 100.0}],
        }
        unstable = {
            "support_episode_count": 3,
            "score": 0.99,
            "start": 20.0,
            "end": 110.0,
            "votes": [
                {"start": 10.0, "end": 100.0},
                {"start": 20.0, "end": 110.0},
            ],
        }
        low_similarity = {
            "support_episode_count": 3,
            "score": 0.7,
            "start": 10.0,
            "end": 100.0,
            "votes": [
                {"start": 10.0, "end": 100.0},
                {"start": 10.0, "end": 100.0},
            ],
        }

        self.assertEqual(_derive_local_confidence(too_small, config), "low")
        self.assertEqual(_derive_local_confidence(unstable, config), "medium")
        self.assertEqual(_derive_local_confidence(low_similarity, config), "medium")

    def test_pairwise_match_rejects_preview_shorter_than_min_segment(self):
        config = normalize_timing_detection_config({
            "timing_detection": {
                "frame_step_seconds": 0.25,
                "min_segment_seconds": 45,
                "pair_match_min_seconds": 20,
            }
        })
        short_features = np.ones((160, 1), dtype=np.float32)
        long_features = np.ones((180, 1), dtype=np.float32)

        short = _find_pairwise_candidate(
            {"episode": 1, "features": short_features},
            {"episode": 2, "features": short_features},
            config,
        )
        long = _find_pairwise_candidate(
            {"episode": 1, "features": long_features},
            {"episode": 2, "features": long_features},
            config,
        )

        self.assertIsNone(short)
        self.assertIsNotNone(long)
        self.assertGreaterEqual(
            long["length_frames"] * config["frame_step_seconds"],
            config["min_segment_seconds"],
        )

    def test_feature_families_are_balanced_before_similarity(self):
        class FakeFeatures:
            @staticmethod
            def chroma_stft(**_kwargs):
                return np.ones((12, 4), dtype=np.float32)

            @staticmethod
            def mfcc(**_kwargs):
                values = np.ones((9, 4), dtype=np.float32)
                values[0] = 1000.0
                return values

            @staticmethod
            def spectral_centroid(**_kwargs):
                return np.full((1, 4), 5000.0, dtype=np.float32)

            @staticmethod
            def spectral_rolloff(**_kwargs):
                return np.full((1, 4), 7500.0, dtype=np.float32)

        class FakeLibrosa:
            feature = FakeFeatures()

        config = normalize_timing_detection_config({})
        samples = np.ones(16000, dtype=np.float32)
        with patch("lib.detector._load_numeric_dependencies", return_value=(np, FakeLibrosa())):
            features = _compute_feature_matrix(samples, config)

        chroma_energy = np.mean(np.sum(features[:, :12] ** 2, axis=1))
        mfcc_energy = np.mean(np.sum(features[:, 12:20] ** 2, axis=1))
        spectral_energy = np.mean(np.sum(features[:, 20:22] ** 2, axis=1))
        self.assertGreater(chroma_energy, 0.4)
        self.assertGreater(mfcc_energy, 0.4)
        self.assertLess(spectral_energy, 0.1)

    def test_short_provider_interval_is_not_used_as_detector_reference(self):
        config = normalize_timing_detection_config({})
        short_reference = {
            1: {
                "segments": [{
                    "type": "ed",
                    "start": 1370.0,
                    "end": 1400.0,
                    "source": "anilibria_exact",
                }]
            }
        }

        selected = _select_reference_segment("ed", {}, short_reference, config)

        self.assertIsNone(selected)

    def test_auto_cut_min_confidence_medium_accepts_medium_consensus(self):
        config = normalize_timing_detection_config({
            "timing_detection": {"auto_cut_min_confidence": "medium"}
        })
        matcher = self._pair_matcher([(range(1, 4), 40)], score=0.7)
        with patch("lib.detector._find_pairwise_candidate", side_effect=matcher):
            result = _build_zone_results(self._zone_tracks(3), config, "op")

        self.assertEqual(result["results"][1]["confidence"], "medium")
        self.assertFalse(result["results"][1]["review_required"])

        detector = {
            "enabled": True,
            "reason": None,
            "config": config,
            "reference_episodes": {"op": result["reference_episodes"]},
            "results": {"op": result["results"], "ed": {}},
        }
        empty_provider = {"segments": [], "request_error": None}
        empty_aniskip = {**empty_provider, "used_fallback": False}
        per_type, segments, _, _ = merge_timing_sources(
            ["op"], empty_provider, empty_aniskip, detector, 1
        )

        self.assertTrue(per_type["op"]["removed"])
        self.assertEqual(len(segments), 1)

    def test_auto_cut_disabled_never_accepts_detector_result(self):
        config = normalize_timing_detection_config({
            "timing_detection": {"auto_cut_min_confidence": "disabled"}
        })
        matcher = self._pair_matcher([(range(1, 4), 40)], score=0.99)
        with patch("lib.detector._find_pairwise_candidate", side_effect=matcher):
            result = _build_zone_results(self._zone_tracks(3), config, "op")

        self.assertEqual(result["results"][1]["confidence"], "high")
        self.assertTrue(result["results"][1]["review_required"])
        self.assertFalse(result["results"][1]["found"])

    def test_reference_fallback_is_identical_for_op_and_ed_results(self):
        partial_result = {
            "confidence": "high",
            "results": {
                1: {"confidence": "high", "review_required": False},
                2: {"confidence": "medium", "review_required": True},
            },
        }

        self.assertTrue(_needs_reference_fallback(partial_result))

    def test_exact_provider_timing_keeps_priority_over_detector(self):
        provider_result = {
            "segments": [{
                "type": "op",
                "start": 10.0,
                "end": 100.0,
                "source": "anilibria_exact",
                "confidence": "high",
            }],
            "request_error": None,
        }
        empty_aniskip = {"segments": [], "request_error": None, "used_fallback": False}
        detector = {
            "enabled": True,
            "reason": None,
            "reference_episodes": {},
            "results": {
                "op": {1: {
                    "source": "audio_fingerprint",
                    "confidence": "high",
                    "start": 20.0,
                    "end": 110.0,
                    "review_required": False,
                }},
                "ed": {},
            },
        }

        per_type, segments, _, _ = merge_timing_sources(
            ["op"], provider_result, empty_aniskip, detector, 1
        )

        self.assertEqual(per_type["op"]["source"], "anilibria_exact")
        self.assertEqual(segments[0]["start"], 10.0)

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
                "audio_recovery": {"enabled": True, "applied": True},
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
        self.assertEqual(summary["episodes_audio_recovery"], [2])
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

    def test_cap_subsegment_durations_keeps_short_segment(self):
        result = cap_subsegment_durations([{
            "start": 100.0,
            "end": 200.0,
            "cut_mode": "copy",
        }], 150.0)

        self.assertEqual(result, [{
            "start": 100.0,
            "end": 200.0,
            "cut_mode": "copy",
        }])

    def test_cap_subsegment_durations_splits_long_segment_and_preserves_cut_mode(self):
        result = cap_subsegment_durations([{
            "start": 0.0,
            "end": 549.0,
            "cut_mode": "precise",
        }], 150.0)

        self.assertEqual(result, [
            {"start": 0.0, "end": 150.0, "cut_mode": "precise"},
            {"start": 150.0, "end": 300.0, "cut_mode": "precise"},
            {"start": 300.0, "end": 450.0, "cut_mode": "precise"},
            {"start": 450.0, "end": 549.0, "cut_mode": "precise"},
        ])

    def test_cap_subsegment_durations_applies_after_hybrid_splitting(self):
        hybrid_segments = build_hybrid_subsegments(
            (100.0, 400.0),
            [{"start": 0.0, "end": 100.0}],
            3.0,
        )

        result = cap_subsegment_durations(hybrid_segments, 150.0)

        self.assertEqual(result, [
            {"start": 100.0, "end": 103.0, "cut_mode": "precise"},
            {"start": 103.0, "end": 253.0, "cut_mode": "copy"},
            {"start": 253.0, "end": 400.0, "cut_mode": "copy"},
        ])


if __name__ == "__main__":
    unittest.main()
