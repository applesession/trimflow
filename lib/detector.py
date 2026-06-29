import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from statistics import median

from lib.constants import DEFAULT_TIMING_DETECTION


SEASON_CLUSTER_TOLERANCE_SECONDS = 10.0
HIGH_CONFIDENCE_BOUNDARY_TOLERANCE = 5.0


def normalize_timing_detection_config(job):
    merged = dict(DEFAULT_TIMING_DETECTION)
    merged.update(job.get("timing_detection", {}))
    merged["enabled"] = bool(merged.get("enabled", False))
    merged["search_head_seconds"] = int(merged["search_head_seconds"])
    merged["search_tail_seconds"] = int(merged["search_tail_seconds"])
    merged["min_support_episodes"] = int(merged["min_support_episodes"])
    merged["frame_step_seconds"] = float(merged["frame_step_seconds"])
    merged["min_segment_seconds"] = float(merged["min_segment_seconds"])
    merged["max_segment_seconds"] = float(merged["max_segment_seconds"])
    merged["feature_sample_rate"] = int(merged["feature_sample_rate"])
    hop_length = merged.get("feature_hop_length")
    merged["feature_hop_length"] = (
        None if hop_length in (None, "", 0, "0") else int(hop_length)
    )
    merged["consensus_min_similarity"] = float(merged["consensus_min_similarity"])
    merged["pair_match_min_seconds"] = float(merged["pair_match_min_seconds"])
    merged["cache_enabled"] = bool(merged.get("cache_enabled", True))
    cache_dir = merged.get("cache_dir")
    merged["cache_dir"] = None if cache_dir in (None, "") else str(cache_dir)
    merged["detector_version"] = str(merged["detector_version"])
    merged["auto_cut_min_confidence"] = str(merged["auto_cut_min_confidence"]).lower()

    if merged["feature_hop_length"] is None:
        merged["feature_hop_length"] = max(
            256,
            int(round(merged["feature_sample_rate"] * merged["frame_step_seconds"])),
        )

    return merged


def _optional_dependency_available(module_name):
    return importlib.util.find_spec(module_name) is not None


def get_detector_support_status():
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        return {"supported": False, "reason": "ffmpeg_not_available"}

    missing_modules = [
        module_name
        for module_name in ["numpy", "librosa"]
        if not _optional_dependency_available(module_name)
    ]
    if missing_modules:
        return {
            "supported": False,
            "reason": f"detector_dependencies_missing:{','.join(missing_modules)}",
        }

    return {"supported": True, "reason": None}


def _load_numeric_dependencies():
    import numpy as np
    import librosa

    return np, librosa


def build_detector_cache_key(episode_infos, config, detector_inputs=None):
    relevant_config = {
        "mode": config["mode"],
        "search_head_seconds": config["search_head_seconds"],
        "search_tail_seconds": config["search_tail_seconds"],
        "min_support_episodes": config["min_support_episodes"],
        "frame_step_seconds": config["frame_step_seconds"],
        "min_segment_seconds": config["min_segment_seconds"],
        "max_segment_seconds": config["max_segment_seconds"],
        "feature_sample_rate": config["feature_sample_rate"],
        "feature_hop_length": config["feature_hop_length"],
        "consensus_min_similarity": config["consensus_min_similarity"],
        "pair_match_min_seconds": config["pair_match_min_seconds"],
        "detector_version": config["detector_version"],
    }
    payload = {
        "config": relevant_config,
        "episodes": [
            {
                "episode": info["episode"],
                "path": str(info["path"]),
                "duration": round(float(info["duration"]), 3),
            }
            for info in sorted(episode_infos, key=lambda item: item["episode"])
        ],
        "references": {},
    }
    references_payload = {}
    for provider_name in ["aniskip_by_episode", "anilibria_by_episode"]:
        provider_values = (detector_inputs or {}).get(provider_name, {})
        references_payload[provider_name] = {
            str(episode): [
                {
                    "type": segment["type"],
                    "start": round(float(segment["start"]), 3),
                    "end": round(float(segment["end"]), 3),
                    "source": segment["source"],
                }
                for segment in sorted(result.get("segments", []), key=lambda item: (item["type"], item["start"]))
            ]
            for episode, result in sorted(provider_values.items())
        }
    payload["references"] = references_payload
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


def _build_cache_paths(temp_dir: Path, config, cache_key):
    cache_root = (
        Path(config["cache_dir"])
        if config.get("cache_dir")
        else temp_dir / "timing_detection_cache"
    )
    features_dir = cache_root / "features"
    results_dir = cache_root / "results"
    return {
        "root": cache_root,
        "features": features_dir,
        "results": results_dir,
        "result_file": results_dir / f"{cache_key}.json",
    }


def _extract_audio_samples(path: Path, start: float, duration: float, sample_rate: int):
    np, _ = _load_numeric_dependencies()
    print(
        "[DETECTOR] ffmpeg extract start:"
        f" file={path.name}"
        f" start={start:.3f}"
        f" duration={duration:.3f}"
        f" sample_rate={sample_rate}"
    )
    cmd = [
        "ffmpeg",
        "-v", "error",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(path),
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, check=True)
    print(
        "[DETECTOR] ffmpeg extract done:"
        f" file={path.name}"
        f" bytes={len(result.stdout)}"
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def _normalize_feature_rows(np, matrix):
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


def _compute_feature_matrix(samples, config):
    np, librosa = _load_numeric_dependencies()

    if samples.size == 0:
        return np.zeros((0, 20), dtype=np.float32)

    sample_rate = config["feature_sample_rate"]
    hop_length = config["feature_hop_length"]
    n_fft = max(2048, hop_length * 4)

    chroma = librosa.feature.chroma_stft(
        y=samples,
        sr=sample_rate,
        hop_length=hop_length,
        n_fft=n_fft,
    )
    mfcc = librosa.feature.mfcc(
        y=samples,
        sr=sample_rate,
        hop_length=hop_length,
        n_mfcc=8,
        n_fft=n_fft,
    )
    spectral_centroid = librosa.feature.spectral_centroid(
        y=samples,
        sr=sample_rate,
        hop_length=hop_length,
        n_fft=n_fft,
    )
    spectral_rolloff = librosa.feature.spectral_rolloff(
        y=samples,
        sr=sample_rate,
        hop_length=hop_length,
        n_fft=n_fft,
    )

    stacked = np.vstack([chroma, mfcc, spectral_centroid, spectral_rolloff]).T
    stacked = np.nan_to_num(stacked, nan=0.0, posinf=0.0, neginf=0.0)
    return _normalize_feature_rows(np, stacked.astype(np.float32))


def _build_zone_track(episode_info, zone_type, search_seconds, config, cache_paths):
    np, _ = _load_numeric_dependencies()
    episode_path = Path(episode_info["path"])
    duration = float(episode_info["duration"])
    zone_duration = min(duration, float(search_seconds))

    if zone_type == "op":
        zone_start = 0.0
    else:
        zone_start = max(0.0, duration - zone_duration)

    feature_cache_key = hashlib.sha1(
        json.dumps(
            {
                "path": str(episode_path.resolve()),
                "episode": episode_info["episode"],
                "zone_type": zone_type,
                "zone_start": round(zone_start, 3),
                "zone_duration": round(zone_duration, 3),
                "sample_rate": config["feature_sample_rate"],
                "hop_length": config["feature_hop_length"],
                "version": config["detector_version"],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    feature_cache_path = cache_paths["features"] / f"{feature_cache_key}.npz"
    cache_hit = False

    if config["cache_enabled"] and feature_cache_path.exists():
        print(
            "[DETECTOR] feature cache hit:"
            f" episode={episode_info['episode']:03d}"
            f" zone={zone_type}"
            f" start={zone_start:.3f}"
            f" duration={zone_duration:.3f}"
        )
        cached = np.load(feature_cache_path)
        features = cached["features"]
        cache_hit = True
    else:
        print(
            "[DETECTOR] feature extraction start:"
            f" episode={episode_info['episode']:03d}"
            f" zone={zone_type}"
            f" start={zone_start:.3f}"
            f" duration={zone_duration:.3f}"
            f" file={episode_path.name}"
        )
        samples = _extract_audio_samples(
            episode_path,
            start=zone_start,
            duration=zone_duration,
            sample_rate=config["feature_sample_rate"],
        )
        features = _compute_feature_matrix(samples, config)
        print(
            "[DETECTOR] feature extraction done:"
            f" episode={episode_info['episode']:03d}"
            f" zone={zone_type}"
            f" frames={features.shape[0]}"
        )
        if config["cache_enabled"]:
            cache_paths["features"].mkdir(parents=True, exist_ok=True)
            np.savez_compressed(feature_cache_path, features=features)

    return {
        "episode": episode_info["episode"],
        "path": episode_info["path"],
        "zone_type": zone_type,
        "zone_start": zone_start,
        "zone_duration": zone_duration,
        "features": features,
        "cache_hit": cache_hit,
    }


def _interval_to_frame_bounds(track, interval, frame_step):
    start = max(track["zone_start"], float(interval["start"]))
    end = min(track["zone_start"] + track["zone_duration"], float(interval["end"]))
    if end <= start:
        return None
    start_frame = max(0, int(round((start - track["zone_start"]) / frame_step)))
    end_frame = min(track["features"].shape[0], int(round((end - track["zone_start"]) / frame_step)))
    if end_frame <= start_frame:
        return None
    return start_frame, end_frame


def _iter_diagonal_runs(diagonal_values, diagonal_indexes, min_similarity):
    run_start = None

    for position, similarity in enumerate(diagonal_values):
        if similarity >= min_similarity:
            if run_start is None:
                run_start = position
        elif run_start is not None:
            yield diagonal_indexes[run_start], diagonal_indexes[position - 1], diagonal_values[run_start:position]
            run_start = None

    if run_start is not None:
        yield diagonal_indexes[run_start], diagonal_indexes[-1], diagonal_values[run_start:]


def _find_pairwise_candidate(track_a, track_b, config):
    np, _ = _load_numeric_dependencies()
    features_a = track_a["features"]
    features_b = track_b["features"]

    if features_a.size == 0 or features_b.size == 0:
        return None

    min_frames = max(1, int(round(config["pair_match_min_seconds"] / config["frame_step_seconds"])))
    max_frames = max(min_frames, int(round(config["max_segment_seconds"] / config["frame_step_seconds"])))
    similarity_matrix = np.matmul(features_a, features_b.T)

    best = None

    for offset in range(-(features_b.shape[0] - 1), features_a.shape[0]):
        diagonal = np.diagonal(similarity_matrix, offset=offset)
        if diagonal.size < min_frames:
            continue

        if offset >= 0:
            indexes = [(offset + idx, idx) for idx in range(diagonal.size)]
        else:
            indexes = [(idx, idx - offset) for idx in range(diagonal.size)]

        for start_pair, end_pair, values in _iter_diagonal_runs(
            diagonal,
            indexes,
            config["consensus_min_similarity"],
        ):
            length = len(values)
            if length < min_frames:
                continue

            if length > max_frames:
                values = values[:max_frames]
                length = len(values)
                end_pair = (
                    start_pair[0] + length - 1,
                    start_pair[1] + length - 1,
                )

            score = float(np.mean(values))
            if best is None or score > best["score"]:
                best = {
                    "episode_a": track_a["episode"],
                    "episode_b": track_b["episode"],
                    "start_frame_a": start_pair[0],
                    "start_frame_b": start_pair[1],
                    "end_frame_a": end_pair[0],
                    "end_frame_b": end_pair[1],
                    "length_frames": length,
                    "score": round(score, 4),
                }

    return best


def _cluster_interval_votes(votes, min_support_episodes):
    if len(votes) < min_support_episodes:
        return None

    best_cluster = None

    for vote in votes:
        cluster = [
            candidate
            for candidate in votes
            if abs(candidate["start"] - vote["start"]) <= SEASON_CLUSTER_TOLERANCE_SECONDS
            and abs(candidate["end"] - vote["end"]) <= SEASON_CLUSTER_TOLERANCE_SECONDS
        ]
        if len(cluster) < min_support_episodes:
            continue

        support = len(cluster)
        avg_score = sum(item["score"] for item in cluster) / support
        start = median([item["start"] for item in cluster])
        end = median([item["end"] for item in cluster])
        summary = {
            "cluster": cluster,
            "support": support,
            "avg_score": round(avg_score, 4),
            "start": round(start, 3),
            "end": round(end, 3),
        }

        if best_cluster is None or (support, avg_score) > (best_cluster["support"], best_cluster["avg_score"]):
            best_cluster = summary

    return best_cluster


def _build_episode_candidates(pair_candidates, zone_tracks, config):
    votes_by_episode = {track["episode"]: [] for track in zone_tracks}
    frame_step = config["frame_step_seconds"]

    for candidate in pair_candidates:
        track_a = next(track for track in zone_tracks if track["episode"] == candidate["episode_a"])
        track_b = next(track for track in zone_tracks if track["episode"] == candidate["episode_b"])

        duration = candidate["length_frames"] * frame_step
        votes_by_episode[candidate["episode_a"]].append({
            "start": round(track_a["zone_start"] + candidate["start_frame_a"] * frame_step, 3),
            "end": round(track_a["zone_start"] + candidate["start_frame_a"] * frame_step + duration, 3),
            "score": candidate["score"],
            "peer_episode": candidate["episode_b"],
        })
        votes_by_episode[candidate["episode_b"]].append({
            "start": round(track_b["zone_start"] + candidate["start_frame_b"] * frame_step, 3),
            "end": round(track_b["zone_start"] + candidate["start_frame_b"] * frame_step + duration, 3),
            "score": candidate["score"],
            "peer_episode": candidate["episode_a"],
        })

    episode_candidates = {}
    for episode, votes in votes_by_episode.items():
        cluster = _cluster_interval_votes(votes, max(1, config["min_support_episodes"] - 1))
        if not cluster:
            continue
        support_episode_count = len({item["peer_episode"] for item in cluster["cluster"]}) + 1
        episode_candidates[episode] = {
            "episode": episode,
            "start": cluster["start"],
            "end": cluster["end"],
            "score": cluster["avg_score"],
            "support_episode_count": support_episode_count,
            "votes": cluster["cluster"],
        }

    return episode_candidates


def _derive_confidence(season_cluster, total_episodes, config):
    if season_cluster is None:
        return "none"

    majority_count = total_episodes // 2 + 1
    starts = [candidate["start"] for candidate in season_cluster["cluster"]]
    ends = [candidate["end"] for candidate in season_cluster["cluster"]]
    max_start_delta = max(abs(start - season_cluster["start"]) for start in starts)
    max_end_delta = max(abs(end - season_cluster["end"]) for end in ends)

    if (
        season_cluster["support"] >= majority_count
        and season_cluster["avg_score"] >= config["consensus_min_similarity"]
        and max_start_delta <= HIGH_CONFIDENCE_BOUNDARY_TOLERANCE
        and max_end_delta <= HIGH_CONFIDENCE_BOUNDARY_TOLERANCE
    ):
        return "high"

    if season_cluster["support"] >= config["min_support_episodes"]:
        return "medium"

    return "low"


def _build_zone_results(zone_tracks, config, zone_type):
    pair_candidates = []
    for index, track_a in enumerate(zone_tracks):
        for track_b in zone_tracks[index + 1:]:
            candidate = _find_pairwise_candidate(track_a, track_b, config)
            if candidate is not None:
                pair_candidates.append(candidate)

    if not pair_candidates:
        return {
            "results": {},
            "reference_episodes": [],
            "reference_interval": None,
            "consensus_score": None,
            "confidence": "none",
            "error": "no_pairwise_matches",
        }

    episode_candidates = _build_episode_candidates(pair_candidates, zone_tracks, config)
    season_cluster = _cluster_interval_votes(
        list(episode_candidates.values()),
        config["min_support_episodes"],
    )

    if not season_cluster:
        return {
            "results": {
                episode: {
                    "found": False,
                    "source": "not_found",
                    "confidence": "none",
                    "start": None,
                    "end": None,
                    "review_required": True,
                    "reason": "insufficient_consensus",
                    "support_episode_count": 0,
                    "consensus_score": None,
                    "reference_interval": None,
                    "cache_hit": False,
                    "match_strategy": "not_found",
                    "reference_episode": None,
                    "reference_source": "none",
                    "reference_similarity": None,
                }
                for episode in [track["episode"] for track in zone_tracks]
            },
            "reference_episodes": [],
            "reference_interval": None,
            "consensus_score": None,
            "confidence": "none",
            "error": "insufficient_consensus",
        }

    confidence = _derive_confidence(season_cluster, len(zone_tracks), config)
    reference_interval = {
        "start": season_cluster["start"],
        "end": season_cluster["end"],
    }
    reference_episodes = [candidate["episode"] for candidate in season_cluster["cluster"]]
    season_support_set = set(reference_episodes)
    results = {}

    for track in zone_tracks:
        episode = track["episode"]
        candidate = episode_candidates.get(episode)
        cache_hit = track.get("cache_hit", False)

        if candidate is None:
            results[episode] = {
                "found": False,
                "source": "not_found",
                "confidence": "none",
                "start": None,
                "end": None,
                "review_required": True,
                "reason": "insufficient_consensus",
                "support_episode_count": 0,
                "consensus_score": None,
                "reference_interval": reference_interval,
                "cache_hit": cache_hit,
                "match_strategy": "not_found",
                "reference_episode": None,
                "reference_source": "none",
                "reference_similarity": None,
            }
            continue

        in_consensus = episode in season_support_set
        results[episode] = {
            "found": in_consensus,
            "source": "audio_fingerprint",
            "confidence": confidence if in_consensus else "medium",
            "start": candidate["start"],
            "end": candidate["end"],
            "review_required": not in_consensus or confidence != "high",
            "reason": None if in_consensus and confidence == "high" else "insufficient_consensus",
            "support_episode_count": candidate["support_episode_count"],
            "consensus_score": candidate["score"],
            "reference_interval": reference_interval,
            "cache_hit": cache_hit,
            "match_strategy": "season_consensus",
            "reference_episode": None,
            "reference_source": "none",
            "reference_similarity": candidate["score"],
        }

    return {
        "results": results,
        "reference_episodes": reference_episodes,
        "reference_interval": reference_interval,
        "consensus_score": season_cluster["avg_score"],
        "confidence": confidence,
        "error": None if confidence == "high" else "insufficient_consensus",
    }


def _select_reference_segment(zone_type, aniskip_by_episode, anilibria_by_episode=None):
    priority = {"anilibria_exact": 0, "aniskip_exact": 1, "aniskip_lengthless": 2}
    candidates = []
    for provider_payload in [anilibria_by_episode or {}, aniskip_by_episode]:
        for episode, result in sorted(provider_payload.items()):
            for segment in result.get("segments", []):
                if segment["type"] != zone_type:
                    continue
                candidates.append({
                    "episode": episode,
                    "source": segment["source"],
                    "start": segment["start"],
                    "end": segment["end"],
                    "priority": priority.get(segment["source"], 99),
                })

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item["priority"], item["episode"], item["start"]))
    return candidates[0]


def _match_reference_to_track(reference_track, reference_interval, target_track, config):
    np, _ = _load_numeric_dependencies()
    frame_step = config["frame_step_seconds"]
    bounds = _interval_to_frame_bounds(reference_track, reference_interval, frame_step)
    if bounds is None:
        return None

    ref_start_frame, ref_end_frame = bounds
    reference_features = reference_track["features"][ref_start_frame:ref_end_frame]
    if reference_features.size == 0:
        return None

    reference_frames = reference_features.shape[0]
    min_frames = max(1, int(round(config["pair_match_min_seconds"] / frame_step)))
    if reference_frames < min_frames:
        return None

    target_features = target_track["features"]
    if target_features.shape[0] < reference_frames:
        return None

    best = None
    for start_frame in range(0, target_features.shape[0] - reference_frames + 1):
        window = target_features[start_frame:start_frame + reference_frames]
        similarities = np.sum(reference_features * window, axis=1)
        score = float(np.mean(similarities))
        if best is None or score > best["score"]:
            best = {
                "start_frame": start_frame,
                "end_frame": start_frame + reference_frames,
                "score": round(score, 4),
            }

    return best


def _build_reference_results(zone_tracks, config, zone_type, aniskip_by_episode, anilibria_by_episode):
    reference_segment = _select_reference_segment(zone_type, aniskip_by_episode, anilibria_by_episode)
    if reference_segment is None:
        return {
            "results": {},
            "reference_episodes": [],
            "reference_interval": None,
            "consensus_score": None,
            "confidence": "none",
            "error": "reference_match_not_found",
        }

    track_by_episode = {track["episode"]: track for track in zone_tracks}
    reference_track = track_by_episode.get(reference_segment["episode"])
    if reference_track is None:
        return {
            "results": {},
            "reference_episodes": [],
            "reference_interval": None,
            "consensus_score": None,
            "confidence": "none",
            "error": "reference_match_not_found",
        }

    reference_interval = {
        "start": round(float(reference_segment["start"]), 3),
        "end": round(float(reference_segment["end"]), 3),
    }
    results = {}
    best_scores = []
    reference_length = reference_interval["end"] - reference_interval["start"]
    high_threshold = max(config["consensus_min_similarity"], 0.8)
    medium_threshold = max(config["consensus_min_similarity"] - 0.08, 0.6)

    for track in zone_tracks:
        if track["episode"] == reference_segment["episode"]:
            results[track["episode"]] = {
                "found": True,
                "source": "audio_fingerprint",
                "confidence": "high",
                "start": reference_interval["start"],
                "end": reference_interval["end"],
                "review_required": False,
                "reason": "reference_match_used",
                "support_episode_count": 1,
                "consensus_score": 1.0,
                "reference_interval": reference_interval,
                "cache_hit": track.get("cache_hit", False),
                "match_strategy": (
                    "anilibria_reference"
                    if reference_segment["source"] == "anilibria_exact"
                    else "aniskip_reference"
                ),
                "reference_episode": reference_segment["episode"],
                "reference_source": reference_segment["source"],
                "reference_similarity": 1.0,
            }
            best_scores.append(1.0)
            continue

        match = _match_reference_to_track(reference_track, reference_interval, track, config)
        if match is None:
            results[track["episode"]] = {
                "found": False,
                "source": "not_found",
                "confidence": "none",
                "start": None,
                "end": None,
                "review_required": True,
                "reason": "reference_match_not_found",
                "support_episode_count": 0,
                "consensus_score": None,
                "reference_interval": reference_interval,
                "cache_hit": track.get("cache_hit", False),
                "match_strategy": "not_found",
                "reference_episode": reference_segment["episode"],
                "reference_source": reference_segment["source"],
                "reference_similarity": None,
            }
            continue

        start = round(track["zone_start"] + match["start_frame"] * config["frame_step_seconds"], 3)
        end = round(start + reference_length, 3)
        similarity = match["score"]
        duration_delta = abs((end - start) - reference_length)
        confidence = "low"
        review_required = True
        found = False
        reason = "reference_match_not_found"

        if similarity >= high_threshold and duration_delta <= 5.0:
            confidence = "high"
            review_required = False
            found = True
            reason = "reference_match_used"
        elif similarity >= medium_threshold:
            confidence = "medium"
            reason = "reference_match_used"

        results[track["episode"]] = {
            "found": found,
            "source": "audio_fingerprint" if confidence != "low" else "not_found",
            "confidence": confidence if confidence != "low" else "none",
            "start": start if confidence != "low" else None,
            "end": end if confidence != "low" else None,
            "review_required": review_required,
            "reason": reason,
            "support_episode_count": 2 if confidence != "low" else 0,
            "consensus_score": similarity if confidence != "low" else None,
            "reference_interval": reference_interval,
            "cache_hit": track.get("cache_hit", False),
                "match_strategy": (
                    "anilibria_reference"
                    if confidence != "low" and reference_segment["source"] == "anilibria_exact"
                    else "aniskip_reference"
                    if confidence != "low"
                    else "not_found"
                ),
                "reference_episode": reference_segment["episode"],
                "reference_source": reference_segment["source"],
                "reference_similarity": similarity if confidence != "low" else None,
        }
        if confidence != "low":
            best_scores.append(similarity)

    confidence = "none"
    if any(result.get("confidence") == "high" for result in results.values()):
        confidence = "high"
    elif any(result.get("confidence") == "medium" for result in results.values()):
        confidence = "medium"

    return {
        "results": results,
        "reference_episodes": [reference_segment["episode"]],
        "reference_interval": reference_interval,
        "consensus_score": round(sum(best_scores) / len(best_scores), 4) if best_scores else None,
        "confidence": confidence,
        "error": None if confidence in {"high", "medium"} else "reference_match_not_found",
    }


def _merge_zone_results(preferred_result, fallback_result):
    merged_results = {}
    for episode in set(preferred_result["results"]) | set(fallback_result["results"]):
        preferred_episode = preferred_result["results"].get(episode)
        fallback_episode = fallback_result["results"].get(episode)

        if preferred_episode is None:
            merged_results[episode] = fallback_episode
            continue
        if fallback_episode is None:
            merged_results[episode] = preferred_episode
            continue

        if (
            preferred_episode.get("confidence") == "high"
            and not preferred_episode.get("review_required", True)
        ):
            merged_results[episode] = preferred_episode
            continue

        fallback_is_better = (
            fallback_episode.get("confidence") == "high"
            and not fallback_episode.get("review_required", True)
        )
        if fallback_is_better:
            merged_results[episode] = fallback_episode
            continue

        confidence_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
        preferred_rank = confidence_rank.get(preferred_episode.get("confidence", "none"), 0)
        fallback_rank = confidence_rank.get(fallback_episode.get("confidence", "none"), 0)
        if fallback_rank > preferred_rank:
            merged_results[episode] = fallback_episode
        else:
            merged_results[episode] = preferred_episode

    merged = dict(preferred_result)
    merged["results"] = merged_results
    if fallback_result["confidence"] == "high":
        merged["confidence"] = "high"
        merged["reference_episodes"] = fallback_result["reference_episodes"]
        merged["reference_interval"] = fallback_result["reference_interval"]
        merged["consensus_score"] = fallback_result["consensus_score"]
        merged["error"] = None
    return merged


def _serialize_context_for_cache(context):
    payload = {
        "available": context["available"],
        "reason": context["reason"],
        "results": context["results"],
        "reference_episodes": context["reference_episodes"],
        "reference_intervals": context.get("reference_intervals", {}),
        "consensus_scores": context.get("consensus_scores", {}),
        "zone_confidences": context.get("zone_confidences", {}),
        "input_reference_episodes": context.get("input_reference_episodes", {}),
    }
    return payload


def _load_context_from_cache(context, cache_paths):
    result_file = cache_paths["result_file"]
    if not result_file.exists():
        return None

    payload = json.loads(result_file.read_text(encoding="utf-8"))
    context["available"] = payload.get("available", False)
    context["reason"] = "cache_hit"
    cached_results = payload.get("results", context["results"])
    context["results"] = {
        zone_type: {int(episode): result for episode, result in zone_results.items()}
        for zone_type, zone_results in cached_results.items()
    }
    context["reference_episodes"] = payload.get("reference_episodes", context["reference_episodes"])
    context["reference_intervals"] = payload.get("reference_intervals", {"op": None, "ed": None})
    context["consensus_scores"] = payload.get("consensus_scores", {"op": None, "ed": None})
    context["zone_confidences"] = payload.get("zone_confidences", {"op": "none", "ed": "none"})
    context["input_reference_episodes"] = payload.get("input_reference_episodes", {"op": [], "ed": []})

    for zone_results in context["results"].values():
        for episode_result in zone_results.values():
            if episode_result.get("source") == "audio_fingerprint":
                episode_result["cache_hit"] = True

    return context


def build_detector_context(episode_infos, config, temp_dir: Path, detector_inputs=None):
    cache_key = build_detector_cache_key(episode_infos, config, detector_inputs)
    cache_paths = _build_cache_paths(temp_dir, config, cache_key)
    aniskip_by_episode = (detector_inputs or {}).get("aniskip_by_episode", {})
    anilibria_by_episode = (detector_inputs or {}).get("anilibria_by_episode", {})
    context = {
        "enabled": config["enabled"],
        "available": False,
        "reason": None,
        "config": config,
        "results": {skip_type: {} for skip_type in ["op", "ed"]},
        "reference_episodes": {skip_type: [] for skip_type in ["op", "ed"]},
        "reference_intervals": {skip_type: None for skip_type in ["op", "ed"]},
        "consensus_scores": {skip_type: None for skip_type in ["op", "ed"]},
        "zone_confidences": {skip_type: "none" for skip_type in ["op", "ed"]},
        "input_reference_episodes": {skip_type: [] for skip_type in ["op", "ed"]},
        "analysis_dir": str((temp_dir / "timing_detection").resolve()),
        "cache_key": cache_key,
        "cache_root": str(cache_paths["root"].resolve()),
    }

    if not config["enabled"]:
        context["reason"] = "timing_detection_disabled"
        return context

    if len(episode_infos) < config["min_support_episodes"]:
        context["reason"] = "not_enough_episodes_for_detector"
        return context

    support = get_detector_support_status()
    if not support["supported"]:
        context["reason"] = support["reason"]
        return context

    if config["cache_enabled"]:
        cache_paths["root"].mkdir(parents=True, exist_ok=True)
        cache_paths["results"].mkdir(parents=True, exist_ok=True)
        loaded = _load_context_from_cache(context, cache_paths)
        if loaded is not None:
            return loaded

    try:
        analysis_dir = Path(context["analysis_dir"])
        analysis_dir.mkdir(parents=True, exist_ok=True)
        print(
            "[DETECTOR] context start:"
            f" episodes={len(episode_infos)}"
            f" head={config['search_head_seconds']}s"
            f" tail={config['search_tail_seconds']}s"
            f" cache_enabled={config['cache_enabled']}"
        )

        zone_tracks = {"op": [], "ed": []}
        for episode_info in episode_infos:
            print(
                "[DETECTOR] analyze episode:"
                f" episode={episode_info['episode']:03d}"
                f" path={Path(episode_info['path']).name}"
                f" duration={float(episode_info['duration']):.3f}"
            )
            zone_tracks["op"].append(
                _build_zone_track(
                    episode_info,
                    zone_type="op",
                    search_seconds=config["search_head_seconds"],
                    config=config,
                    cache_paths=cache_paths,
                )
            )
            zone_tracks["ed"].append(
                _build_zone_track(
                    episode_info,
                    zone_type="ed",
                    search_seconds=config["search_tail_seconds"],
                    config=config,
                    cache_paths=cache_paths,
                )
            )

        for zone_type in ["op", "ed"]:
            print(f"[DETECTOR] zone analysis start: zone={zone_type}")
            zone_result = _build_zone_results(zone_tracks[zone_type], config, zone_type)
            should_try_reference = zone_result["confidence"] == "none" or (
                zone_type == "ed"
                and any(
                    result.get("review_required", True)
                    or result.get("confidence") != "high"
                    for result in zone_result["results"].values()
                )
            )
            if should_try_reference:
                reference_result = _build_reference_results(
                    zone_tracks[zone_type],
                    config,
                    zone_type,
                    aniskip_by_episode,
                    anilibria_by_episode,
                )
                if reference_result["confidence"] != "none":
                    if zone_result["confidence"] == "none":
                        zone_result = reference_result
                    else:
                        zone_result = _merge_zone_results(zone_result, reference_result)
            print(
                "[DETECTOR] zone analysis done:"
                f" zone={zone_type}"
                f" confidence={zone_result['confidence']}"
                f" reference_episodes={zone_result['reference_episodes']}"
            )

            context["results"][zone_type] = zone_result["results"]
            context["reference_episodes"][zone_type] = zone_result["reference_episodes"]
            context["reference_intervals"][zone_type] = zone_result["reference_interval"]
            context["consensus_scores"][zone_type] = zone_result["consensus_score"]
            context["zone_confidences"][zone_type] = zone_result["confidence"]
            context["input_reference_episodes"][zone_type] = [
                episode
                for episode, result in sorted({**aniskip_by_episode, **anilibria_by_episode}.items())
                if any(segment["type"] == zone_type for segment in result.get("segments", []))
            ]

            if zone_result["error"] and context["reason"] is None:
                context["reason"] = f"{zone_type}_{zone_result['error']}"

        context["available"] = True
        if any(
            result.get("match_strategy") in {"aniskip_reference", "anilibria_reference"}
            for zone_results in context["results"].values()
            for result in zone_results.values()
        ):
            context["reason"] = "reference_match_used"
        elif all(confidence == "high" for confidence in context["zone_confidences"].values() if confidence != "none"):
            context["reason"] = "season_consensus_only"

        debug_payload = {
            "cache_key": cache_key,
            "reason": context["reason"],
            "reference_episodes": context["reference_episodes"],
            "reference_intervals": context["reference_intervals"],
            "consensus_scores": context["consensus_scores"],
            "zone_confidences": context["zone_confidences"],
            "input_reference_episodes": context["input_reference_episodes"],
        }
        (analysis_dir / "detector_summary.json").write_text(
            json.dumps(debug_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if config["cache_enabled"]:
            cache_paths["result_file"].write_text(
                json.dumps(_serialize_context_for_cache(context), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        print(
            "[DETECTOR] context done:"
            f" reason={context['reason']}"
            f" op_confidence={context['zone_confidences']['op']}"
            f" ed_confidence={context['zone_confidences']['ed']}"
        )

        return context
    except subprocess.CalledProcessError as exc:
        context["reason"] = f"feature_extraction_failed: {exc}"
        return context
    except Exception as exc:
        context["reason"] = f"detector_error: {exc}"
        return context


def get_detector_type_result(detector_context, episode_number, skip_type):
    return detector_context.get("results", {}).get(skip_type, {}).get(episode_number)
