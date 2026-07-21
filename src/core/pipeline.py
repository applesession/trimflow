import json
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import requests
from api.aniskip import (
    build_quality_summary,
    get_aniskip_segments,
    print_skip_log,
    summarize_skips,
)
from api.anilibria import extract_release_poster_url, get_anilibria_segments, get_release_details
from core.detector import (
    build_detector_context,
    get_detector_type_result,
    normalize_timing_detection_config,
)
from core.discovery import filter_episode_files, find_episode_files
from core.torrent import download_selected_episodes
from shared.helpers import (
    build_compilation_display_name,
    build_single_episode_display_name,
    build_timestamps_description,
    build_vk_comment_text,
    build_vk_wall_post_text,
    create_concat_file,
    ensure_non_empty_slug,
    get_display_title,
    parse_episodes_range,
    raise_if_cancelled,
    sanitize_filename,
    seconds_to_timestamp,
)
from shared.constants import TEMP_ROOT
from core.media import (
    cap_subsegment_durations,
    get_preferred_audio_stream,
    snap_remove_segments_to_keyframes,
    build_hybrid_subsegments,
    build_keep_segments,
    ffprobe_duration,
    ffprobe_media_signature,
    get_keyframes,
    render_concat,
    render_final,
    render_segment,
)
from shared.runtime import update_runtime_status
from api.storage import upload_file_to_s3
from api.wavespeed import run_edit_prediction
from shared.validation import prepare_temp_dir, reset_temp_dir
from api.vk import publish_private_video_link_to_vk, publish_video_to_vk


def build_source_summary(selected_episodes, excluded_files):
    return {
        "selected_episode_count": len(selected_episodes),
        "excluded_file_count": len(excluded_files),
        "excluded_files": excluded_files,
    }


def _round_or_none(value, digits=3):
    if value is None:
        return None
    return round(float(value), digits)


def _compact_type_info(type_info):
    compact = {
        "source": type_info.get("source", "not_found"),
        "confidence": type_info.get("confidence", "none"),
        "interval": type_info.get("interval"),
        "removed": bool(type_info.get("removed", False)),
        "review_required": bool(type_info.get("review_required", False)),
    }

    optional_fields = [
        "reason",
        "match_strategy",
        "reference_source",
        "reference_episode",
        "reference_similarity",
    ]
    for field in optional_fields:
        value = type_info.get(field)
        if value not in [None, "", "none", "not_found"]:
            compact[field] = value

    return compact


def _compact_timing_info(timing_info, skip_types):
    compact = {
        "strategy": timing_info.get("strategy"),
        "confidence": timing_info.get("confidence"),
        "review_required": bool(timing_info.get("review_required", False)),
        "per_type": {
            skip_type: _compact_type_info(timing_info.get("per_type", {}).get(skip_type, {}))
            for skip_type in skip_types
        },
    }

    if timing_info.get("used_fallback"):
        compact["used_fallback"] = True
    if timing_info.get("request_error"):
        compact["request_error"] = timing_info["request_error"]
    if timing_info.get("detector_error"):
        compact["detector_error"] = timing_info["detector_error"]

    reference_episodes = timing_info.get("reference_episodes") or {}
    if any(reference_episodes.values()):
        compact["reference_episodes"] = reference_episodes

    return compact


def compact_manifest_episode(manifest_episode, skip_types):
    original_duration = float(manifest_episode.get("original_duration", 0.0))
    cleaned_duration = float(manifest_episode.get("cleaned_duration", 0.0))

    return {
        "episode": manifest_episode["episode"],
        "source_file": Path(manifest_episode["source_file"]).name,
        "original_duration": _round_or_none(original_duration),
        "cleaned_duration": _round_or_none(cleaned_duration),
        "removed_duration": _round_or_none(max(0.0, original_duration - cleaned_duration)),
        "segment_cut_mode": manifest_episode.get("segment_cut_mode"),
        "keyframe_aligned": manifest_episode.get("keyframe_aligned", False),
        "timing_info": _compact_timing_info(manifest_episode.get("timing_info", {}), skip_types),
        "skip_summary": manifest_episode.get("skip_summary", {}),
    }


def build_compact_manifest(
    *,
    job,
    season,
    episodes_range,
    episode_files,
    excluded_files,
    detector_context,
    timing_detection,
    prefetched_anilibria_results,
    prefetched_aniskip_results,
    pretty_base_name,
    output_video,
    output_txt,
    delivery_summary,
    quality_summary,
    manifest_episodes,
    processing_metadata=None,
    timing_sources_summary=None,
):
    manifest = {
        "title": job["title"],
        "title_ru": job.get("title_ru"),
        "mal_id": job.get("mal_id"),
        "season": season,
        "episodes_range": episodes_range,
        "episodes_count": len(episode_files),
        "source": job["source"]["type"],
        "source_summary": {
            "selected_episode_count": len(episode_files),
            "excluded_file_count": len(excluded_files),
        },
        "timing_detection": {
            "enabled": timing_detection["enabled"],
            "available": detector_context["available"],
            "reason": detector_context["reason"],
        },
        "timing_sources_summary": timing_sources_summary or build_timing_sources_summary(
            prefetched_anilibria_results,
            prefetched_aniskip_results,
            detector_context,
        ),
        "display_title": get_display_title(job),
        "output_display_name": pretty_base_name,
        "output_video": output_video.name,
        "output_timestamps": output_txt.name,
        "delivery_summary": delivery_summary,
        "quality_summary": quality_summary,
        "episodes": [
            compact_manifest_episode(episode, job.get("skip_types", ["op", "ed"]))
            for episode in manifest_episodes
        ],
    }
    if processing_metadata:
        manifest["processing"] = processing_metadata
    return manifest


def build_segment_encoding(encoding):
    segment_video_codec = encoding.get("segment_video_codec", encoding.get("video_codec", "libx264"))
    default_pixel_format = "yuv420p" if "nvenc" in segment_video_codec else None

    return {
        "video_codec": segment_video_codec,
        "preset": encoding.get("segment_preset", encoding.get("preset", "medium")),
        "cq": encoding.get("segment_cq", 18 if "nvenc" in segment_video_codec else 15),
        "audio_codec": encoding.get("segment_audio_codec", encoding.get("audio_codec", "aac")),
        "audio_bitrate": encoding.get("segment_audio_bitrate", "192k"),
        "audio_sample_rate": int(encoding.get("segment_audio_sample_rate", 48000)),
        "audio_channels": int(encoding.get("segment_audio_channels", 2)),
        "pixel_format": encoding.get("segment_pixel_format", default_pixel_format),
        "cut_mode": encoding.get("segment_cut_mode", "precise"),
        "boundary_reencode_seconds": float(encoding.get("boundary_reencode_seconds", 3.0)),
        "max_render_seconds": float(encoding.get("segment_max_render_seconds", 150)),
    }


def normalize_processing_config(job):
    processing = {
        "chunk_size_episodes": 12,
    }
    processing.update(job.get("processing") or {})
    try:
        chunk_size = int(processing.get("chunk_size_episodes", 12))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("processing.chunk_size_episodes must be an integer") from exc
    if chunk_size < 1:
        raise RuntimeError("processing.chunk_size_episodes must be at least 1")
    processing["chunk_size_episodes"] = chunk_size
    return processing


def split_episode_infos_into_chunks(episode_infos, chunk_size):
    if chunk_size < 1:
        raise RuntimeError("chunk_size must be at least 1")
    return [
        episode_infos[index:index + chunk_size]
        for index in range(0, len(episode_infos), chunk_size)
    ]


CHUNK_CHECKPOINT_VERSION = 1


def _file_identity(path):
    path = Path(path)
    try:
        stat = path.stat()
        return {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    except OSError:
        return {"path": str(path.resolve()), "size": None, "mtime_ns": None}


def build_chunk_fingerprint(
    job,
    episode_infos,
    *,
    watermark_path,
    processing,
    timing_detection,
    segment_encoding,
    preferred_language,
):
    payload = {
        "version": CHUNK_CHECKPOINT_VERSION,
        "title": job.get("title"),
        "season": str(job.get("season", "")).lstrip("0"),
        "episodes_range": job.get("episodes_range"),
        "source": job.get("source"),
        "skip_types": job.get("skip_types", ["op", "ed"]),
        "processing": processing,
        "timing_detection": timing_detection,
        "timing_providers": job.get("timing_providers") or {},
        "segment_encoding": segment_encoding,
        "encoding": job.get("encoding") or {},
        "preferred_audio_language": preferred_language,
        "watermark": _file_identity(watermark_path),
        "episodes": [
            {
                "episode": item["episode"],
                "duration": round(float(item["duration"]), 3),
                "file": _file_identity(item["path"]),
            }
            for item in episode_infos
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path, payload):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def initialize_chunk_checkpoint(temp_dir, fingerprint):
    checkpoint_path = temp_dir / "checkpoint.json"
    checkpoint = None
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass

    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("version") != CHUNK_CHECKPOINT_VERSION
        or checkpoint.get("fingerprint") != fingerprint
    ):
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = {
            "version": CHUNK_CHECKPOINT_VERSION,
            "fingerprint": fingerprint,
            "render_context": None,
        }
        _write_json_atomic(checkpoint_path, checkpoint)
    return checkpoint


def load_chunk_checkpoint(temp_dir, chunk_index, episode_numbers):
    chunk_dir = temp_dir / f"chunk_{chunk_index:03d}"
    checkpoint_path = chunk_dir / "checkpoint.json"
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("output_file") != "rendered.mkv":
            return None
        output = chunk_dir / checkpoint["output_file"]
        duration = ffprobe_duration(output)
        signature = ffprobe_media_signature(output)
    except Exception:
        return None

    if checkpoint.get("version") != CHUNK_CHECKPOINT_VERSION:
        return None
    if checkpoint.get("chunk_index") != chunk_index:
        return None
    if checkpoint.get("episodes") != list(episode_numbers):
        return None
    if not output.is_file() or output.stat().st_size != checkpoint.get("size"):
        return None
    if duration <= 0 or not signature or signature != checkpoint.get("media_signature"):
        return None
    manifest_episodes = checkpoint.get("manifest_episodes")
    if not isinstance(manifest_episodes, list):
        return None
    if [item.get("episode") for item in manifest_episodes] != list(episode_numbers):
        return None

    return {
        **checkpoint,
        "chunk_output": output,
        "duration": duration,
        "media_signature": signature,
    }


def save_chunk_checkpoint(work_dir, chunk_index, episode_numbers, manifest_episodes, output):
    duration = ffprobe_duration(output)
    signature = ffprobe_media_signature(output)
    if duration <= 0 or not signature:
        raise RuntimeError(f"Rendered chunk {chunk_index} failed ffprobe validation")
    checkpoint = {
        "version": CHUNK_CHECKPOINT_VERSION,
        "chunk_index": chunk_index,
        "episodes": list(episode_numbers),
        "output_file": output.name,
        "size": output.stat().st_size,
        "duration": duration,
        "media_signature": signature,
        "manifest_episodes": manifest_episodes,
    }
    _write_json_atomic(work_dir / "checkpoint.json", checkpoint)
    return checkpoint


def build_timestamps_from_episodes(manifest_episodes):
    cumulative_time = 0.0
    timestamps = []
    for episode in manifest_episodes:
        timestamps.append(f"{seconds_to_timestamp(cumulative_time)} - {episode['episode']} серия")
        cumulative_time += float(episode.get("cleaned_duration", 0.0))
    return timestamps


def build_timing_sources_summary(prefetched_anilibria_results, prefetched_aniskip_results, detector_context):
    return {
        "anilibria_available": any(result["segments"] for result in prefetched_anilibria_results.values()),
        "aniskip_available": any(result["segments"] for result in prefetched_aniskip_results.values()),
        "detector_available": detector_context["available"],
    }


def build_chunk_episode_range(chunk_episode_infos):
    episodes = [episode_info["episode"] for episode_info in chunk_episode_infos]
    if not episodes:
        return None
    return f"{min(episodes):03d}-{max(episodes):03d}"


def build_episode_infos(episode_files):
    episode_infos = []
    for episode_number, path in episode_files:
        episode_infos.append({
            "episode": episode_number,
            "path": str(path),
            "duration": ffprobe_duration(path),
        })
    return episode_infos


def build_prefetched_aniskip_results(episode_infos, mal_id, skip_types):
    prefetched = {}
    for episode_info in episode_infos:
        prefetched[episode_info["episode"]] = get_aniskip_segments(
            mal_id=mal_id,
            episode_number=episode_info["episode"],
            episode_length=episode_info["duration"],
            skip_types=skip_types,
        )
    return prefetched


def build_prefetched_anilibria_results(episode_infos, title, season, source):
    prefetched = {}
    aliases = [title]
    for episode_info in episode_infos:
        prefetched[episode_info["episode"]] = get_anilibria_segments(
            title=title,
            season=season,
            episode_number=episode_info["episode"],
            source=source,
            aliases=aliases,
        )
    return prefetched


def build_prefetched_empty_provider_results(episode_infos, provider_name, reason):
    return {
        episode_info["episode"]: {
            "segments": [],
            "request_error": reason,
            "request_urls": [],
            "provider": provider_name,
        }
        for episode_info in episode_infos
    }


def build_prefetched_empty_aniskip_results(episode_infos, reason):
    return {
        episode_info["episode"]: {
            "segments": [],
            "per_type_sources": {},
            "used_fallback": False,
            "request_error": reason,
            "requested_episode_length": float(episode_info["duration"]),
            "fallback_from_episode_length": None,
            "request_urls": [],
            "provider": "aniskip",
        }
        for episode_info in episode_infos
    }


def collect_episode_files(source, title_slug, allowed_episodes, processing=None, download_timeout=None):
    if source["type"] == "magnet":
        download_dir = Path(source.get("download_dir", f"./downloads/{title_slug}"))
        download_selected_episodes(
            source["magnet"],
            download_dir,
            allowed_episodes,
            path_filter=(processing or {}).get("source_path_contains"),
            timeout=download_timeout,
        )
        detected_episode_files, ignored_files = find_episode_files(download_dir)
        return download_dir, detected_episode_files, ignored_files

    if source["type"] == "local":
        input_dir = Path(source.get("input_dir", "./input"))
        detected_episode_files, ignored_files = find_episode_files(input_dir)
        return None, detected_episode_files, ignored_files

    raise RuntimeError(f"Unknown source type: {source['type']}")


def log_episode_selection(episode_files, excluded_files):
    print("\n[EPISODES]")
    for episode_number, path in episode_files:
        print(f"{episode_number:03d} -> {path}")

    if excluded_files:
        print("\n[EXCLUDED FILES]")
        for item in excluded_files:
            episode_text = ""
            if "episode" in item:
                episode_text = f"EP{item['episode']:03d} | "
            print(f"{episode_text}{item['reason']} -> {item['path']}")


def build_type_info(
    source="not_found",
    confidence="none",
    interval=None,
    review_required=True,
    removed=False,
    reason=None,
    consensus_score=None,
    support_episode_count=0,
    reference_interval=None,
    cache_hit=False,
    match_strategy="not_found",
    reference_episode=None,
    reference_source="none",
    reference_similarity=None,
):
    return {
        "source": source,
        "confidence": confidence,
        "interval": interval,
        "review_required": review_required,
        "removed": removed,
        "reason": reason,
        "consensus_score": consensus_score,
        "support_episode_count": support_episode_count,
        "reference_interval": reference_interval,
        "cache_hit": cache_hit,
        "match_strategy": match_strategy,
        "reference_episode": reference_episode,
        "reference_source": reference_source,
        "reference_similarity": reference_similarity,
    }


def merge_timing_sources(skip_types, anilibria_result, aniskip_result, detector_context, episode_number):
    per_type = {
        skip_type: build_type_info(reason="not_found")
        for skip_type in skip_types
    }
    remove_segments = []

    for provider_name, provider_result in [("anilibria", anilibria_result), ("aniskip", aniskip_result)]:
        for segment in provider_result["segments"]:
            skip_type = segment["type"]
            if skip_type not in per_type or per_type[skip_type]["removed"]:
                continue
            interval = {
                "start": segment["start"],
                "end": segment["end"],
            }
            per_type[skip_type] = build_type_info(
                source=segment["source"],
                confidence=segment["confidence"],
                interval=interval,
                review_required=False,
                removed=True,
                reason=None,
                match_strategy=provider_name,
                reference_source=segment["source"],
            )
            remove_segments.append(segment)

    detector_reason = None
    reference_episodes = {}

    if detector_context["enabled"]:
        detector_reason = detector_context["reason"]
        reference_episodes = detector_context.get("reference_episodes", {})

    for skip_type in skip_types:
        if per_type[skip_type]["removed"]:
            continue

        detector_result = get_detector_type_result(detector_context, episode_number, skip_type)
        if detector_result is None:
            continue

        per_type[skip_type] = build_type_info(
            source=detector_result["source"],
            confidence=detector_result["confidence"],
            interval=(
                None
                if detector_result["start"] is None or detector_result["end"] is None
                else {
                    "start": detector_result["start"],
                    "end": detector_result["end"],
                }
            ),
            review_required=detector_result["review_required"],
            removed=detector_result["source"] == "audio_fingerprint"
            and detector_result["confidence"] == "high"
            and not detector_result["review_required"],
            reason=detector_result.get("reason") or detector_reason,
            consensus_score=detector_result.get("consensus_score"),
            support_episode_count=detector_result.get("support_episode_count", 0),
            reference_interval=detector_result.get("reference_interval"),
            cache_hit=detector_result.get("cache_hit", False),
            match_strategy=detector_result.get("match_strategy", "not_found"),
            reference_episode=detector_result.get("reference_episode"),
            reference_source=detector_result.get("reference_source", "none"),
            reference_similarity=detector_result.get("reference_similarity"),
        )

        if per_type[skip_type]["removed"]:
            remove_segments.append({
                "type": skip_type,
                "start": detector_result["start"],
                "end": detector_result["end"],
                "source": "audio_fingerprint",
                "confidence": detector_result["confidence"],
            })

    for skip_type in skip_types:
        if per_type[skip_type]["source"] == "not_found" and detector_context["enabled"]:
            per_type[skip_type]["reason"] = detector_reason or "detector_not_found"

    return per_type, remove_segments, reference_episodes, detector_reason


def build_timing_info(skip_types, per_type, anilibria_result, aniskip_result, detector_reason, reference_episodes):
    review_required = any(info["review_required"] for info in per_type.values())
    used_detector = any(info["source"] == "audio_fingerprint" for info in per_type.values())
    used_aniskip = any(info["source"].startswith("aniskip") for info in per_type.values())
    used_anilibria = any(info["source"].startswith("anilibria") for info in per_type.values())

    if review_required:
        strategy = "manual_review"
    elif used_anilibria and used_detector:
        strategy = "anilibria_with_detector"
    elif used_aniskip and used_detector:
        strategy = "aniskip_with_detector"
    elif used_detector:
        strategy = "detector_only"
    elif used_anilibria:
        strategy = "anilibria_only"
    else:
        strategy = "aniskip_only"

    confidence_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    overall_confidence = "none"
    if per_type:
        overall_confidence = min(
            per_type.items(),
            key=lambda item: confidence_rank.get(item[1]["confidence"], 0),
        )[1]["confidence"]

    return {
        "strategy": strategy,
        "per_type": {
            skip_type: {
                "source": per_type[skip_type]["source"],
                "confidence": per_type[skip_type]["confidence"],
                "interval": per_type[skip_type]["interval"],
                "review_required": per_type[skip_type]["review_required"],
                "removed": per_type[skip_type]["removed"],
                "reason": per_type[skip_type]["reason"],
                "consensus_score": per_type[skip_type]["consensus_score"],
                "support_episode_count": per_type[skip_type]["support_episode_count"],
                "reference_interval": per_type[skip_type]["reference_interval"],
                "cache_hit": per_type[skip_type]["cache_hit"],
                "match_strategy": per_type[skip_type]["match_strategy"],
                "reference_episode": per_type[skip_type]["reference_episode"],
                "reference_source": per_type[skip_type]["reference_source"],
                "reference_similarity": per_type[skip_type]["reference_similarity"],
            }
            for skip_type in skip_types
        },
        "used_fallback": aniskip_result["used_fallback"] or used_detector,
        "request_error": "; ".join(
            [
                error
                for error in [anilibria_result["request_error"], aniskip_result["request_error"]]
                if error
            ]
        ) or None,
        "detector_error": detector_reason,
        "confidence": overall_confidence,
        "reference_episodes": reference_episodes,
        "review_required": review_required,
    }


def process_episode(
    episode_info,
    skip_types,
    temp_dir,
    cumulative_time,
    detector_context,
    segment_encoding,
    anilibria_result,
    aniskip_result,
    preferred_language="rus",
):
    detected_ep = episode_info["episode"]
    ep_file = Path(episode_info["path"])
    duration = episode_info["duration"]
    print(f"\n=== Processing Episode {detected_ep}: {ep_file.name} ===")

    per_type, remove_segments, reference_episodes, detector_reason = merge_timing_sources(
        skip_types,
        anilibria_result,
        aniskip_result,
        detector_context,
        detected_ep,
    )
    timing_info = build_timing_info(
        skip_types,
        per_type,
        anilibria_result,
        aniskip_result,
        detector_reason,
        reference_episodes,
    )

    skip_summary = summarize_skips(
        remove_segments,
        skip_types,
        per_type,
        request_error=aniskip_result["request_error"],
    )
    audio_stream_index = get_preferred_audio_stream(ep_file, preferred_language)
    print_skip_log(
        detected_ep,
        skip_summary,
        skip_types,
        review_required=timing_info["review_required"],
    )

    segment_cut_mode = segment_encoding.get("cut_mode", "hybrid")
    boundary_window = segment_encoding.get("boundary_reencode_seconds", 3.0)
    max_render_seconds = segment_encoding.get("max_render_seconds", 150.0)

    ep_keyframes = None
    keyframe_aligned = False
    if segment_cut_mode in ("copy", "hybrid"):
        ep_keyframes = get_keyframes(ep_file)

        if ep_keyframes:
            remove_segments = snap_remove_segments_to_keyframes(remove_segments, ep_keyframes)
            keyframe_aligned = True

    keep_segments = build_keep_segments(duration, remove_segments)
    cleaned_duration = 0.0
    segment_outputs = []
    kept_segments_manifest = []

    for seg_index, (start, end) in enumerate(keep_segments):
        if end <= start:
            continue

        if segment_cut_mode == "hybrid":
            subsegments = build_hybrid_subsegments(
                (start, end),
                remove_segments,
                boundary_window,
            )
        else:
            subsegments = [{
                "start": start,
                "end": end,
                "cut_mode": segment_cut_mode,
            }]

        if segment_cut_mode != "copy":
            subsegments = cap_subsegment_durations(subsegments, max_render_seconds)

        for sub_index, subsegment in enumerate(subsegments):
            sub_start = subsegment["start"]
            sub_end = subsegment["end"]
            segment_output = temp_dir / f"ep{detected_ep:03d}_seg{seg_index:03d}_{sub_index:03d}.mkv"
            sub_encoding = {**segment_encoding, "cut_mode": subsegment["cut_mode"]}
            render_segment(ep_file, segment_output, sub_start, sub_end, segment_encoding=sub_encoding, audio_stream_index=audio_stream_index)

            seg_duration = sub_end - sub_start
            cleaned_duration += seg_duration
            cumulative_time += seg_duration
            segment_outputs.append(segment_output)
            kept_segments_manifest.append({
                "start": sub_start,
                "end": sub_end,
                "cut_mode": subsegment["cut_mode"],
            })

    manifest_episode = {
        "episode": detected_ep,
        "source_file": str(ep_file),
        "original_duration": duration,
        "cleaned_duration": cleaned_duration,
        "segment_cut_mode": segment_cut_mode,
        "keyframe_aligned": keyframe_aligned,
        "boundary_reencode_seconds": boundary_window,
        "timing_info": {
            **timing_info,
            "requested_episode_length": aniskip_result["requested_episode_length"],
            "fallback_from_episode_length": aniskip_result.get("fallback_from_episode_length"),
            "request_urls": {
                "anilibria": anilibria_result["request_urls"],
                "aniskip": aniskip_result["request_urls"],
            },
        },
        "skip_summary": skip_summary,
        "removed_segments": remove_segments,
        "kept_segments": kept_segments_manifest,
    }

    timestamp_line = f"{seconds_to_timestamp(cumulative_time - cleaned_duration)} - {detected_ep} серия"
    return cumulative_time, segment_outputs, manifest_episode, timestamp_line


def write_outputs(output_txt, output_manifest, timestamps, manifest):
    with open(output_txt, "w", encoding="utf-8") as file:
        file.write("\n".join(timestamps) + "\n")

    with open(output_manifest, "w", encoding="utf-8") as file:
        json.dump(manifest, file, indent=2, ensure_ascii=False)


def build_single_episode_manifest(
    *,
    job,
    season,
    episode_number,
    source_file,
    pretty_base_name,
    output_video,
    output_txt,
    delivery_summary,
):
    return {
        "title": job["title"],
        "title_ru": job.get("title_ru"),
        "mal_id": job.get("mal_id"),
        "season": season,
        "episodes_range": f"{int(episode_number):03d}",
        "episodes_count": 1,
        "source": job["source"]["type"],
        "source_summary": {
            "selected_episode_count": 1,
            "excluded_file_count": 0,
        },
        "timing_detection": {
            "enabled": False,
            "available": False,
            "reason": "single_episode_mode",
        },
        "timing_sources_summary": {
            "anilibria_available": False,
            "aniskip_available": False,
            "detector_available": False,
        },
        "display_title": get_display_title(job),
        "output_display_name": pretty_base_name,
        "output_video": output_video.name,
        "output_timestamps": output_txt.name,
        "delivery_summary": delivery_summary,
        "quality_summary": {},
        "episodes": [{
            "episode": int(episode_number),
            "source_file": Path(source_file).name,
            "original_duration": None,
            "cleaned_duration": None,
            "removed_duration": 0.0,
            "segment_cut_mode": "single_episode",
            "timing_info": {
                "strategy": "single_episode_mode",
                "confidence": "none",
                "review_required": False,
                "per_type": {},
            },
            "skip_summary": {
                "warnings": [],
            },
        }],
        "processing": {
            "mode": "single_episode",
        },
    }


def build_delivery_config(job):
    delivery = {
        "s3_enabled": True,
        "s3_upload_video": False,
        "s3_upload_timestamps": False,
        "s3_upload_manifest": True,
        "vk_enabled": True,
        "vk_wall_post_enabled": True,
        "vk_comment_enabled": True,
        "vk_privacy_view": 0,
        "vk_preview_enabled": True,
        "vk_preview_mode": "video_thumb",
        "vk_preview_provider": "wavespeed",
        "vk_preview_model": "google/nano-banana-2/edit-fast",
        "vk_preview_timeout_seconds": 180,
        "vk_preview_prompt_template": (
            "Transform the provided anime poster into a clickable VK video thumbnail. "
            "Keep the anime identity and main character recognizable. Use a bold, stylish, "
            "highly readable title treatment integrated into the artwork. Clean composition, "
            "high contrast, professional social-media look, 16:9, 1280x720, no extra logos, "
            "no unrelated text, no watermarks. Main text: \"{title_text}\". Secondary text: "
            "\"{episode_text}\"."
        ),
        "vk_preview_output_aspect_ratio": "16:9",
        "vk_preview_target_size": "1280x720",
        "vk_preview_temp_dir": None,
        "vk_comment_banner_path": "./assets/banner.png",
        "vk_comment_template": "",
    }
    delivery.update(job.get("delivery") or {})
    delivery["s3_enabled"] = bool(delivery.get("s3_enabled", True))
    delivery["s3_upload_video"] = bool(delivery.get("s3_upload_video", False))
    delivery["s3_upload_timestamps"] = bool(delivery.get("s3_upload_timestamps", False))
    delivery["s3_upload_manifest"] = bool(delivery.get("s3_upload_manifest", True))
    delivery["vk_enabled"] = bool(delivery.get("vk_enabled", True))
    delivery["vk_wall_post_enabled"] = bool(delivery.get("vk_wall_post_enabled", True))
    delivery["vk_comment_enabled"] = bool(delivery.get("vk_comment_enabled", True))
    delivery["vk_privacy_view"] = int(delivery.get("vk_privacy_view", 0))
    delivery["vk_preview_enabled"] = bool(delivery.get("vk_preview_enabled", True))
    delivery["vk_preview_mode"] = str(delivery.get("vk_preview_mode", "video_thumb") or "video_thumb").strip().lower()
    delivery["vk_preview_timeout_seconds"] = int(delivery.get("vk_preview_timeout_seconds", 180))
    return delivery


def build_s3_summary(enabled, uploaded, error=None, uploaded_files=None):
    return {
        "enabled": enabled,
        "uploaded": uploaded,
        "error": error,
        "uploaded_files": uploaded_files or {},
    }


def build_vk_summary(enabled, uploaded=False, error=None, result=None):
    result = result or {}
    return {
        "enabled": enabled,
        "uploaded": uploaded,
        "video_uploaded": result.get("video_uploaded", uploaded),
        "post_created": result.get("post_created", False),
        "comment_created": result.get("comment_created", False),
        "error": error or result.get("error"),
        "video_title": result.get("video_title"),
        "video_description": result.get("video_description"),
        "video_id": result.get("video_id"),
        "owner_id": result.get("owner_id"),
        "video_url": result.get("video_url"),
        "video_group_id": result.get("video_group_id"),
        "wall_group_id": result.get("wall_group_id"),
        "post_id": result.get("post_id"),
        "comment_id": result.get("comment_id"),
        "comment_attachment": result.get("comment_attachment"),
        "post_preview_attachment": result.get("post_preview_attachment"),
        "preview_attempted": result.get("preview_attempted", False),
        "preview_generated": result.get("preview_generated", False),
        "preview_attached": result.get("preview_attached", False),
        "preview_error": result.get("preview_error"),
        "errors_by_stage": result.get("errors_by_stage", {}),
    }


def is_job_completed(job_result):
    delivery_summary = job_result.get("delivery_summary", {})
    vk_summary = delivery_summary.get("vk", {})
    s3_summary = delivery_summary.get("s3", {})

    if vk_summary.get("enabled"):
        return bool(vk_summary.get("video_uploaded"))
    if s3_summary.get("enabled"):
        return bool(s3_summary.get("uploaded"))
    return bool(job_result.get("output_video"))


def is_private_vk_delivery(delivery):
    return int(delivery.get("vk_privacy_view", 0)) == 5


def get_vk_preview_mode(delivery):
    if not delivery.get("vk_preview_enabled", True):
        return "disabled"
    mode = str(delivery.get("vk_preview_mode", "video_thumb") or "video_thumb").strip().lower()
    if mode != "video_thumb":
        return "video_thumb"
    return mode


def _resolve_preview_temp_dir(temp_dir, delivery, pretty_base_name):
    configured_dir = delivery.get("vk_preview_temp_dir")
    if configured_dir:
        preview_dir = Path(configured_dir)
        if not preview_dir.is_absolute():
            preview_dir = Path.cwd() / preview_dir
        preview_dir = preview_dir / sanitize_filename(pretty_base_name)
    else:
        preview_dir = temp_dir / "vk_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir


def _build_vk_preview_episode_text(job):
    episodes = sorted(parse_episodes_range(job.get("episodes_range", "")))
    season = int(job.get("season", 1))
    if not episodes:
        return f"Season {season}"
    if len(episodes) == 1:
        return f"Season {season} • Episode {episodes[0]}"
    return f"Season {season} • Episodes {episodes[0]}-{episodes[-1]}"


def build_vk_preview_prompt(job, delivery):
    title_text = str(get_display_title(job) or job.get("title") or "Anime").strip()
    episode_text = _build_vk_preview_episode_text(job)
    template = str(delivery.get("vk_preview_prompt_template") or "").strip()
    if not template:
        raise RuntimeError("vk_preview_prompt_template is empty")
    return template.format(
        title_text=title_text,
        episode_text=episode_text,
        title=title_text,
        season=job.get("season", 1),
        episodes_range=job.get("episodes_range", ""),
        processing_mode=str(job.get("processing_mode", "compilation") or "compilation").strip().lower(),
        aspect_ratio=delivery.get("vk_preview_output_aspect_ratio", "16:9"),
        target_size=delivery.get("vk_preview_target_size", "1280x720"),
    )


def get_job_poster_url(job):
    automation = job.get("automation") or {}
    return str(automation.get("poster_url") or "").strip() or None


def refresh_job_poster_url(job):
    automation = job.get("automation") or {}
    release_id = automation.get("release_id")
    if not release_id:
        return None
    release_details = get_release_details(release_id)
    poster_url = extract_release_poster_url(release_details.get("release") or {})
    if poster_url:
        automation["poster_url"] = poster_url
        automation["poster_fetched_at"] = datetime.now(timezone.utc).isoformat()
        job["automation"] = automation
    return poster_url


def download_preview_image(output_url, preview_dir, pretty_base_name):
    response = requests.get(output_url, timeout=120)
    response.raise_for_status()
    suffix = Path(output_url.split("?", 1)[0]).suffix or ".jpg"
    preview_path = preview_dir / f"{sanitize_filename(pretty_base_name)}_vk_preview{suffix}"
    preview_path.write_bytes(response.content)
    return preview_path


def generate_vk_video_thumb_preview(job, delivery, pretty_base_name, temp_dir, runtime_status_path=None):
    preview_status = {
        "preview_attempted": False,
        "preview_generated": False,
        "preview_attached": False,
        "preview_error": None,
    }
    preview_mode = get_vk_preview_mode(delivery)
    if preview_mode == "disabled":
        return None, preview_status
    if preview_mode != "video_thumb":
        return None, preview_status

    preview_status["preview_attempted"] = True
    try:
        poster_url = get_job_poster_url(job) or refresh_job_poster_url(job)
        if not poster_url:
            raise RuntimeError("poster_url_missing")

        if runtime_status_path:
            set_runtime_stage(runtime_status_path, "preview_generate")
        print(f"[DELIVERY] VK preview start: {pretty_base_name}")
        prompt = build_vk_preview_prompt(job, delivery)
        model = str(delivery.get("vk_preview_model", "google/nano-banana-2/edit-fast")).strip()
        provider = str(delivery.get("vk_preview_provider", "wavespeed")).strip().lower()
        if provider != "wavespeed":
            raise RuntimeError(f"unsupported_vk_preview_provider:{provider}")

        preview_result = run_edit_prediction(
            model,
            {
                "prompt": prompt,
                "images": [poster_url],
                "aspect_ratio": delivery.get("vk_preview_output_aspect_ratio", "16:9"),
                "resolution": "2k",
                "output_format": "jpeg",
                "enable_web_search": False,
            },
            timeout_seconds=delivery.get("vk_preview_timeout_seconds", 180),
        )
        preview_dir = _resolve_preview_temp_dir(temp_dir, delivery, pretty_base_name)
        preview_path = download_preview_image(preview_result["output_url"], preview_dir, pretty_base_name)
        preview_status["preview_generated"] = True
        print(f"[DELIVERY] VK preview ok: {pretty_base_name}")
        return preview_path, preview_status
    except Exception as exc:
        preview_status["preview_error"] = repr(exc)
        print(f"[DELIVERY] VK preview failed: {preview_status['preview_error']}")
        return None, preview_status


def deliver_to_vk(job, delivery, output_video, pretty_base_name, timestamps_description, temp_dir, runtime_status_path=None):
    wall_post_text = (
        build_vk_wall_post_text(job, pretty_base_name)
        if delivery.get("vk_wall_post_enabled", True)
        else None
    )
    comment_text = (
        build_vk_comment_text(delivery.get("vk_comment_template", ""))
        if delivery.get("vk_comment_enabled", True) and not is_private_vk_delivery(delivery)
        else None
    )

    if wall_post_text:
        print(f"[DELIVERY] VK post start: {pretty_base_name}")
    if comment_text:
        print(f"[DELIVERY] VK comment start: {pretty_base_name}")

    video_thumb_preview_path, preview_status = generate_vk_video_thumb_preview(
        job,
        delivery,
        pretty_base_name,
        temp_dir,
        runtime_status_path=runtime_status_path,
    )

    if is_private_vk_delivery(delivery):
        result = publish_private_video_link_to_vk(
            output_video,
            pretty_base_name,
            timestamps_description,
            wall_post_text=wall_post_text,
            video_thumb_path=video_thumb_preview_path,
            video_thumb_size=delivery.get("vk_preview_target_size"),
        )
    else:
        result = publish_video_to_vk(
            output_video,
            pretty_base_name,
            timestamps_description,
            wall_post_text=wall_post_text,
            comment_text=comment_text,
            comment_banner_path=delivery.get("vk_comment_banner_path"),
            privacy_view=delivery.get("vk_privacy_view", 0),
            video_thumb_path=video_thumb_preview_path,
            video_thumb_size=delivery.get("vk_preview_target_size"),
        )

    result["preview_attempted"] = preview_status["preview_attempted"]
    result["preview_generated"] = preview_status["preview_generated"]
    result["preview_attached"] = bool(result.get("preview_attached", False))
    if video_thumb_preview_path:
        result["preview_local_path"] = str(video_thumb_preview_path)
    if preview_status["preview_error"] and not result.get("preview_error"):
        result["preview_error"] = preview_status["preview_error"]
    return result


def log_vk_delivery_result(pretty_base_name, delivery, vk_result):
    if not isinstance(vk_result, dict):
        print(f"[DELIVERY] VK debug unexpected_result_type: {type(vk_result).__name__}")
        return

    debug_payload = {
        "title": pretty_base_name,
        "private_mode": is_private_vk_delivery(delivery),
        "video_uploaded": vk_result.get("video_uploaded"),
        "post_created": vk_result.get("post_created"),
        "comment_created": vk_result.get("comment_created"),
        "video_group_id": vk_result.get("video_group_id"),
        "wall_group_id": vk_result.get("wall_group_id"),
        "video_url": vk_result.get("video_url"),
        "post_id": vk_result.get("post_id"),
        "preview_attempted": vk_result.get("preview_attempted"),
        "preview_generated": vk_result.get("preview_generated"),
        "preview_attached": vk_result.get("preview_attached"),
        "preview_error": vk_result.get("preview_error"),
        "error": vk_result.get("error"),
        "errors_by_stage": vk_result.get("errors_by_stage", {}),
    }
    if is_private_vk_delivery(delivery):
        debug_payload["post_mode"] = vk_result.get("post_mode")
        debug_payload["post_message"] = vk_result.get("post_message")

    print("[DELIVERY] VK debug " + json.dumps(debug_payload, ensure_ascii=False))


def deliver_rendered_output(
    job,
    delivery,
    *,
    output_video,
    output_txt,
    output_manifest,
    manifest,
    timestamps,
    pretty_base_name,
    temp_dir,
    total_episodes,
    runtime_status_path=None,
):
    timestamps_description = build_timestamps_description(timestamps)
    previous_summary = manifest.get("delivery_summary") or {}
    delivery_summary = {
        "s3": previous_summary.get("s3") or build_s3_summary(delivery["s3_enabled"], uploaded=False),
        "vk": previous_summary.get("vk") or build_vk_summary(delivery["vk_enabled"], uploaded=False),
    }
    delivery_summary["s3"]["enabled"] = delivery["s3_enabled"]
    delivery_summary["vk"]["enabled"] = delivery["vk_enabled"]

    def persist():
        manifest["delivery_summary"] = delivery_summary
        write_outputs(output_txt, output_manifest, timestamps, manifest)

    s3_prefix = f"animonster/{ensure_non_empty_slug(job['title'])}/S{str(job['season']).zfill(2)}/"
    s3_uploaded_files = dict(delivery_summary["s3"].get("uploaded_files") or {})
    s3_manifest_pending = False
    if delivery["s3_enabled"] and not delivery_summary["s3"].get("uploaded"):
        set_runtime_stage(runtime_status_path, "delivery_s3", total_episodes=total_episodes)
        print(f"[DELIVERY] S3 start: {pretty_base_name}")
        try:
            if delivery.get("s3_upload_video", False) and "video" not in s3_uploaded_files:
                upload_file_to_s3(output_video, s3_prefix + output_video.name)
                s3_uploaded_files["video"] = s3_prefix + output_video.name
            if delivery.get("s3_upload_timestamps", False) and "timestamps" not in s3_uploaded_files:
                upload_file_to_s3(output_txt, s3_prefix + output_txt.name)
                s3_uploaded_files["timestamps"] = s3_prefix + output_txt.name
            s3_manifest_pending = delivery.get("s3_upload_manifest", True) and "manifest" not in s3_uploaded_files
            delivery_summary["s3"] = build_s3_summary(
                enabled=True,
                uploaded=not s3_manifest_pending,
                uploaded_files=s3_uploaded_files,
            )
            print(f"[DELIVERY] S3 ok: {pretty_base_name}")
        except Exception as exc:
            print(f"[DELIVERY] S3 failed: {repr(exc)}")
            delivery_summary["s3"] = build_s3_summary(
                enabled=True,
                uploaded=False,
                error=repr(exc),
                uploaded_files=s3_uploaded_files,
            )
        persist()

    if delivery["vk_enabled"] and not delivery_summary["vk"].get("video_uploaded"):
        set_runtime_stage(runtime_status_path, "delivery_vk", total_episodes=total_episodes)
        print(f"[DELIVERY] VK video start: {pretty_base_name}")
        try:
            vk_result = deliver_to_vk(
                job,
                delivery,
                output_video,
                pretty_base_name,
                timestamps_description,
                temp_dir,
                runtime_status_path=runtime_status_path,
            )
            delivery_summary["vk"] = build_vk_summary(enabled=True, uploaded=True, result=vk_result)
            print(f"[DELIVERY] VK video ok: {pretty_base_name}")
            log_vk_delivery_result(pretty_base_name, delivery, vk_result)
            if delivery.get("vk_wall_post_enabled", True):
                if vk_result.get("post_created"):
                    print(f"[DELIVERY] VK post ok: {pretty_base_name}")
                elif vk_result.get("errors_by_stage", {}).get("wall_post"):
                    print(f"[DELIVERY] VK post failed: {vk_result['errors_by_stage']['wall_post']}")
            if delivery.get("vk_comment_enabled", True) and vk_result.get("post_created"):
                if vk_result.get("comment_created"):
                    print(f"[DELIVERY] VK comment ok: {pretty_base_name}")
                elif not is_private_vk_delivery(delivery):
                    comment_error = (
                        vk_result.get("errors_by_stage", {}).get("wall_comment")
                        or vk_result.get("errors_by_stage", {}).get("comment_photo")
                    )
                    print(f"[DELIVERY] VK comment failed: {comment_error}")
        except Exception as exc:
            print(f"[DELIVERY] VK video failed: {repr(exc)}")
            delivery_summary["vk"] = build_vk_summary(
                enabled=True,
                uploaded=False,
                error=repr(exc),
                result={
                    "video_uploaded": False,
                    "post_created": False,
                    "comment_created": False,
                    "video_title": pretty_base_name,
                    "video_description": timestamps_description,
                    "errors_by_stage": {"video_upload": repr(exc)},
                },
            )
        persist()

    if delivery["s3_enabled"] and s3_manifest_pending:
        try:
            persist()
            upload_file_to_s3(output_manifest, s3_prefix + output_manifest.name)
            s3_uploaded_files["manifest"] = s3_prefix + output_manifest.name
            delivery_summary["s3"] = build_s3_summary(
                enabled=True,
                uploaded=True,
                uploaded_files=s3_uploaded_files,
            )
            print(f"[DELIVERY] S3 manifest ok: {pretty_base_name}")
        except Exception as exc:
            print(f"[DELIVERY] S3 failed on manifest: {repr(exc)}")
            delivery_summary["s3"] = build_s3_summary(
                enabled=True,
                uploaded=bool(s3_uploaded_files),
                error=repr(exc),
                uploaded_files=s3_uploaded_files,
            )
        persist()

    result = {
        "output_video": str(output_video),
        "output_timestamps": str(output_txt),
        "output_manifest": str(output_manifest),
        "delivery_summary": delivery_summary,
        "quality_summary": manifest.get("quality_summary", {}),
        "output_display_name": pretty_base_name,
        "timestamps_description": timestamps_description,
    }
    return result


def build_output_artifacts(job, output_root):
    season = str(job["season"]).zfill(2)
    episodes_range = job["episodes_range"]
    processing_mode = str(job.get("processing_mode", "compilation") or "compilation").strip().lower()
    if processing_mode == "single_episode":
        episodes = sorted(parse_episodes_range(episodes_range))
        if len(episodes) != 1:
            raise RuntimeError("single_episode mode requires exactly one requested episode")
        pretty_base_name = build_single_episode_display_name(job, season, episodes[0])
    else:
        pretty_base_name = build_compilation_display_name(job, season, episodes_range)

    job_output_dir = Path(output_root) / ensure_non_empty_slug(job["title"])
    file_base_name = sanitize_filename(pretty_base_name)
    return {
        "job_output_dir": job_output_dir,
        "pretty_base_name": pretty_base_name,
        "output_video": job_output_dir / f"{file_base_name}.mkv",
        "output_txt": job_output_dir / f"{file_base_name}.txt",
        "output_manifest": job_output_dir / f"{file_base_name}_manifest.json",
    }


def load_render_checkpoint(job, artifacts):
    output_video = artifacts["output_video"]
    output_txt = artifacts["output_txt"]
    output_manifest = artifacts["output_manifest"]
    if not output_video.is_file() or not output_txt.is_file() or not output_manifest.is_file():
        return None

    try:
        manifest = json.loads(output_manifest.read_text(encoding="utf-8"))
        timestamps = output_txt.read_text(encoding="utf-8").splitlines()
        duration = ffprobe_duration(output_video)
    except Exception:
        return None

    if not isinstance(manifest, dict) or not manifest.get("render_complete") or duration <= 0:
        return None
    if manifest.get("title") != job.get("title"):
        return None
    if str(manifest.get("season", "")).lstrip("0") != str(job.get("season", "")).lstrip("0"):
        return None
    if manifest.get("episodes_range") != job.get("episodes_range"):
        return None
    if manifest.get("output_video") != output_video.name or manifest.get("output_timestamps") != output_txt.name:
        return None

    return {
        "manifest": manifest,
        "timestamps": timestamps,
    }


def cleanup_job_artifacts(
    cleanup,
    download_dir=None,
    temp_dir=None,
    job_output_dir=None,
    *,
    render_completed=False,
    job_completed=False,
    preserve_temp_on_failure=False,
):
    cleanup = cleanup or {}

    if render_completed and cleanup.get("downloads", True) and download_dir:
        print(f"[CLEANUP] Removing downloads: {download_dir}")
        shutil.rmtree(download_dir, ignore_errors=True)

    if cleanup.get("temp", True) and temp_dir and (render_completed or not preserve_temp_on_failure):
        print(f"[CLEANUP] Removing temp: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)

    if job_completed and cleanup.get("output", False) and job_output_dir:
        print(f"[CLEANUP] Removing output: {job_output_dir}")
        shutil.rmtree(job_output_dir, ignore_errors=True)


def cleanup_cancelled_job_artifacts(job):
    title_slug = ensure_non_empty_slug(job["title"])
    source = job.get("source") or {}
    if source.get("type") == "magnet":
        download_dir = Path(source.get("download_dir") or f"./downloads/{title_slug}")
        shutil.rmtree(download_dir, ignore_errors=True)
    shutil.rmtree(TEMP_ROOT / title_slug, ignore_errors=True)
    shutil.rmtree(Path(job.get("output_dir") or "./output") / title_slug, ignore_errors=True)


def set_runtime_stage(runtime_status_path, stage, **current_job_updates):
    raise_if_cancelled()
    if not runtime_status_path:
        return

    payload = {
        "current_stage": stage,
    }
    if current_job_updates:
        payload["current_job"] = {"stage": stage, **current_job_updates}
    update_runtime_status(runtime_status_path, **payload)


def process_episode_chunk(
    chunk_episode_infos,
    *,
    chunk_index,
    total_chunks,
    skip_types,
    chunk_dir,
    cumulative_time,
    detector_context,
    segment_encoding,
    prefetched_anilibria_results,
    prefetched_aniskip_results,
    runtime_status_path=None,
    total_episodes=None,
    preferred_language="rus",
):
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_segments = []
    chunk_manifest_episodes = []
    chunk_timestamps = []
    chunk_episode_range = build_chunk_episode_range(chunk_episode_infos)

    for episode_info in chunk_episode_infos:
        set_runtime_stage(
            runtime_status_path,
            "render_segments",
            current_chunk_index=chunk_index,
            total_chunks=total_chunks,
            current_chunk_episode_range=chunk_episode_range,
            current_episode=episode_info["episode"],
            total_episodes=total_episodes,
            current_episode_file=Path(episode_info["path"]).name,
        )
        cumulative_time, segment_outputs, manifest_episode, timestamp_line = process_episode(
            episode_info,
            skip_types,
            chunk_dir,
            cumulative_time,
            detector_context,
            segment_encoding,
            prefetched_anilibria_results[episode_info["episode"]],
            prefetched_aniskip_results[episode_info["episode"]],
            preferred_language=preferred_language,
        )
        chunk_segments.extend(segment_outputs)
        chunk_manifest_episodes.append(manifest_episode)
        chunk_timestamps.append(timestamp_line)

    chunk_concat_file = chunk_dir / "concat.txt"
    chunk_concat_output = chunk_dir / "concat_output.mkv"
    set_runtime_stage(
        runtime_status_path,
        "concat",
        current_chunk_index=chunk_index,
        total_chunks=total_chunks,
        current_chunk_episode_range=chunk_episode_range,
        total_episodes=total_episodes,
        current_episode=None,
        current_episode_file=None,
    )
    create_concat_file(chunk_segments, chunk_concat_file)
    render_concat(chunk_concat_file, chunk_concat_output)
    return {
        "cumulative_time": cumulative_time,
        "chunk_output": chunk_concat_output,
        "manifest_episodes": chunk_manifest_episodes,
        "timestamps": chunk_timestamps,
    }


def process_job(job, runtime_status_path=None):
    title = job["title"]
    mal_id = job.get("mal_id")
    season = str(job["season"]).zfill(2)
    episodes_range = job["episodes_range"]
    processing_mode = str(job.get("processing_mode", "compilation") or "compilation").strip().lower()
    source = job["source"]
    output_root = Path(job["output_dir"])
    watermark_path = Path(job["watermark_path"])
    skip_types = job.get("skip_types", ["op", "ed"])
    encoding = job.get("encoding") or {}
    cleanup = job.get("cleanup") or {"downloads": True, "temp": True}
    processing = normalize_processing_config(job)
    timing_detection = normalize_timing_detection_config(job)
    segment_encoding = build_segment_encoding(encoding)
    delivery = build_delivery_config(job)
    timing_providers = job.get("timing_providers") or {}
    anilibria_enabled = timing_providers.get("anilibria_enabled", True)
    aniskip_enabled = timing_providers.get("aniskip_enabled", False)
    preferred_language = str(job.get("preferred_audio_language", "rus")).strip().lower() or "rus"

    title_slug = ensure_non_empty_slug(title)
    allowed_episodes = parse_episodes_range(episodes_range)

    download_cfg = job.get("download") or {}
    episode_count = len(allowed_episodes)
    download_timeout = max(
        int(download_cfg.get("timeout_minutes_minimum", 30)) * 60,
        min(
            episode_count * int(download_cfg.get("timeout_minutes_per_episode", 20)) * 60,
            int(download_cfg.get("timeout_minutes_maximum", 1440)) * 60,
        ),
    )
    print(f"\n[DOWNLOAD TIMEOUT] {episode_count} episodes -> {download_timeout // 60} minutes")

    artifacts = build_output_artifacts(job, output_root)
    job_output_dir = artifacts["job_output_dir"]
    job_output_dir.mkdir(parents=True, exist_ok=True)

    temp_dir = (
        reset_temp_dir(title_slug)
        if processing_mode == "single_episode"
        else prepare_temp_dir(title_slug)
    )
    download_dir = None
    render_completed = False
    job_completed = False

    try:
        checkpoint = load_render_checkpoint(job, artifacts)
        if checkpoint:
            print(f"[CHECKPOINT] Reusing rendered output: {artifacts['output_video']}")
            set_runtime_stage(runtime_status_path, "delivery_resume")
            render_completed = True
            result = deliver_rendered_output(
                job,
                delivery,
                output_video=artifacts["output_video"],
                output_txt=artifacts["output_txt"],
                output_manifest=artifacts["output_manifest"],
                manifest=checkpoint["manifest"],
                timestamps=checkpoint["timestamps"],
                pretty_base_name=artifacts["pretty_base_name"],
                temp_dir=temp_dir,
                total_episodes=int(checkpoint["manifest"].get("episodes_count", episode_count)),
                runtime_status_path=runtime_status_path,
            )
            job_completed = is_job_completed(result)
            return result

        set_runtime_stage(runtime_status_path, "validation")
        set_runtime_stage(runtime_status_path, "download")
        download_dir, detected_episode_files, ignored_files = collect_episode_files(
            source,
            title_slug,
            allowed_episodes,
            processing=processing,
            download_timeout=download_timeout,
        )

        set_runtime_stage(runtime_status_path, "episode_scan")
        episode_files, excluded_out_of_range = filter_episode_files(
            detected_episode_files,
            allowed_episodes,
        )
        excluded_files = ignored_files + excluded_out_of_range
        log_episode_selection(episode_files, excluded_files)

        if processing_mode == "single_episode":
            if len(episode_files) != 1:
                raise RuntimeError("single_episode mode requires exactly one selected episode")

            episode_number, episode_path = episode_files[0]
            pretty_base_name = artifacts["pretty_base_name"]
            output_video = artifacts["output_video"]
            output_txt = artifacts["output_txt"]
            output_manifest = artifacts["output_manifest"]
            timestamps = [f"00:00:00 - {episode_number} серия"]
            timestamps_description = build_timestamps_description(timestamps)
            delivery_summary = {
                "s3": build_s3_summary(delivery["s3_enabled"], uploaded=False),
                "vk": build_vk_summary(delivery["vk_enabled"], uploaded=False),
            }
            manifest = build_single_episode_manifest(
                job=job,
                season=season,
                episode_number=episode_number,
                source_file=episode_path,
                pretty_base_name=pretty_base_name,
                output_video=output_video,
                output_txt=output_txt,
                delivery_summary=delivery_summary,
            )

            set_runtime_stage(
                runtime_status_path,
                "episode_scan",
                total_episodes=1,
                total_chunks=None,
                current_episode=episode_number,
                current_episode_file=Path(episode_path).name,
            )
            set_runtime_stage(
                runtime_status_path,
                "final_render",
                total_episodes=1,
                current_episode=episode_number,
                current_episode_file=Path(episode_path).name,
                current_chunk_index=None,
                total_chunks=None,
                current_chunk_episode_range=None,
            )
            episode_audio_index = get_preferred_audio_stream(Path(episode_path), preferred_language)
            render_final(
                concat_output=Path(episode_path),
                watermark_path=watermark_path,
                output_video=output_video,
                encoding={**encoding, "audio_codec": "aac"},
                audio_stream_index=episode_audio_index,
            )
            manifest["render_complete"] = True
            write_outputs(output_txt, output_manifest, timestamps, manifest)
            render_completed = True
            result = deliver_rendered_output(
                job,
                delivery,
                output_video=output_video,
                output_txt=output_txt,
                output_manifest=output_manifest,
                manifest=manifest,
                timestamps=timestamps,
                pretty_base_name=pretty_base_name,
                temp_dir=temp_dir,
                total_episodes=1,
                runtime_status_path=runtime_status_path,
            )
            job_completed = is_job_completed(result)

            set_runtime_stage(runtime_status_path, "job_done", total_episodes=1)
            print(f"\n=== JOB DONE: {title} ===")
            print(output_video)
            print(output_txt)
            print(output_manifest)
            return result

        episode_infos = build_episode_infos(episode_files)
        episode_chunks = split_episode_infos_into_chunks(episode_infos, processing["chunk_size_episodes"])
        fingerprint = build_chunk_fingerprint(
            job,
            episode_infos,
            watermark_path=watermark_path,
            processing=processing,
            timing_detection=timing_detection,
            segment_encoding=segment_encoding,
            preferred_language=preferred_language,
        )
        chunk_checkpoint = initialize_chunk_checkpoint(temp_dir, fingerprint)
        set_runtime_stage(
            runtime_status_path,
            "episode_scan",
            total_episodes=len(episode_infos),
            total_chunks=len(episode_chunks),
        )
        loaded_chunks = [
            load_chunk_checkpoint(
                temp_dir,
                index,
                [item["episode"] for item in episode_chunk],
            )
            for index, episode_chunk in enumerate(episode_chunks, start=1)
        ]
        render_context = chunk_checkpoint.get("render_context")
        render_context_valid = (
            isinstance(render_context, dict)
            and isinstance(render_context.get("detector"), dict)
            and isinstance(render_context.get("timing_sources_summary"), dict)
        )
        needs_processing_context = any(chunk is None for chunk in loaded_chunks) or not render_context_valid
        prefetched_aniskip_results = {}
        prefetched_anilibria_results = {}

        if needs_processing_context:
            if aniskip_enabled and mal_id:
                prefetched_aniskip_results = build_prefetched_aniskip_results(episode_infos, mal_id, skip_types)
            elif aniskip_enabled and not mal_id:
                prefetched_aniskip_results = build_prefetched_empty_aniskip_results(
                    episode_infos,
                    "AniSkip provider skipped: missing mal_id",
                )
            else:
                prefetched_aniskip_results = build_prefetched_empty_aniskip_results(
                    episode_infos,
                    "AniSkip provider disabled by config",
                )
            if anilibria_enabled:
                prefetched_anilibria_results = build_prefetched_anilibria_results(episode_infos, title, season, source)
            else:
                prefetched_anilibria_results = build_prefetched_empty_provider_results(
                    episode_infos,
                    "anilibria",
                    "AniLibria provider disabled by config",
                )
            set_runtime_stage(runtime_status_path, "detector", total_episodes=len(episode_infos))
            detector_inputs = {
                "aniskip_by_episode": prefetched_aniskip_results,
                "anilibria_by_episode": prefetched_anilibria_results,
            }
            detector_context = build_detector_context(episode_infos, timing_detection, temp_dir, detector_inputs)
            timing_sources_summary = build_timing_sources_summary(
                prefetched_anilibria_results,
                prefetched_aniskip_results,
                detector_context,
            )
            render_context = {
                "detector": {
                    key: detector_context.get(key)
                    for key in ["enabled", "available", "reason"]
                },
                "timing_sources_summary": timing_sources_summary,
            }
            chunk_checkpoint["render_context"] = render_context
            _write_json_atomic(temp_dir / "checkpoint.json", chunk_checkpoint)

            if detector_context["enabled"]:
                status_text = "ready" if detector_context["available"] else f"disabled: {detector_context['reason']}"
                print(f"\n[DETECTOR] {status_text}")
        else:
            detector_context = render_context["detector"]
            timing_sources_summary = render_context["timing_sources_summary"]

        pretty_base_name = artifacts["pretty_base_name"]
        output_video = artifacts["output_video"]
        output_txt = artifacts["output_txt"]
        output_manifest = artifacts["output_manifest"]
        concat_file = temp_dir / "concat.txt"

        cumulative_time = 0.0
        chunk_outputs = []
        chunk_signatures = []
        manifest_episodes = []

        for chunk_index, chunk_episode_infos in enumerate(episode_chunks, start=1):
            episode_numbers = [item["episode"] for item in chunk_episode_infos]
            chunk_result = loaded_chunks[chunk_index - 1]
            if chunk_result:
                print(f"[CHUNK CHECKPOINT] Reusing chunk {chunk_index}/{len(episode_chunks)}")
            else:
                chunk_dir = temp_dir / f"chunk_{chunk_index:03d}"
                work_dir = temp_dir / f"chunk_{chunk_index:03d}.work"
                shutil.rmtree(chunk_dir, ignore_errors=True)
                shutil.rmtree(work_dir, ignore_errors=True)
                work_dir.mkdir(parents=True, exist_ok=True)
                try:
                    processed = process_episode_chunk(
                        chunk_episode_infos,
                        chunk_index=chunk_index,
                        total_chunks=len(episode_chunks),
                        skip_types=skip_types,
                        chunk_dir=work_dir,
                        cumulative_time=cumulative_time,
                        detector_context=detector_context,
                        segment_encoding=segment_encoding,
                        prefetched_anilibria_results=prefetched_anilibria_results,
                        prefetched_aniskip_results=prefetched_aniskip_results,
                        runtime_status_path=runtime_status_path,
                        total_episodes=len(episode_infos),
                        preferred_language=preferred_language,
                    )
                    rendered_chunk = work_dir / "rendered.mkv"
                    set_runtime_stage(
                        runtime_status_path,
                        "final_render",
                        total_episodes=len(episode_infos),
                        current_chunk_index=chunk_index,
                        total_chunks=len(episode_chunks),
                        current_chunk_episode_range=build_chunk_episode_range(chunk_episode_infos),
                    )
                    render_final(
                        concat_output=processed["chunk_output"],
                        watermark_path=watermark_path,
                        output_video=rendered_chunk,
                        encoding={**encoding, "audio_codec": "copy"},
                    )
                    save_chunk_checkpoint(
                        work_dir,
                        chunk_index,
                        episode_numbers,
                        processed["manifest_episodes"],
                        rendered_chunk,
                    )
                    for item in work_dir.iterdir():
                        if item.name in {"rendered.mkv", "checkpoint.json"}:
                            continue
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                    work_dir.replace(chunk_dir)
                    chunk_result = load_chunk_checkpoint(temp_dir, chunk_index, episode_numbers)
                    if not chunk_result:
                        raise RuntimeError(f"Chunk {chunk_index} checkpoint validation failed")
                except Exception:
                    shutil.rmtree(work_dir, ignore_errors=True)
                    raise

            chunk_outputs.append(chunk_result["chunk_output"])
            chunk_signatures.append(chunk_result["media_signature"])
            manifest_episodes.extend(chunk_result["manifest_episodes"])
            cumulative_time += sum(
                float(item.get("cleaned_duration", 0.0))
                for item in chunk_result["manifest_episodes"]
            )

        if any(signature != chunk_signatures[0] for signature in chunk_signatures[1:]):
            raise RuntimeError("Rendered chunks have incompatible media signatures")

        set_runtime_stage(
            runtime_status_path,
            "concat",
            total_episodes=len(episode_infos),
            current_chunk_index=None,
            current_chunk_episode_range=None,
        )
        create_concat_file(chunk_outputs, concat_file)
        partial_output = output_video.with_name(output_video.stem + ".partial" + output_video.suffix)
        partial_output.unlink(missing_ok=True)
        try:
            render_concat(concat_file, partial_output, allow_reencode=False)
            if ffprobe_duration(partial_output) <= 0:
                raise RuntimeError("Final concat has zero duration")
            if ffprobe_media_signature(partial_output) != chunk_signatures[0]:
                raise RuntimeError("Final concat has incompatible media signature")
            partial_output.replace(output_video)
        except Exception:
            partial_output.unlink(missing_ok=True)
            raise

        timestamps = build_timestamps_from_episodes(manifest_episodes)

        timestamps_description = build_timestamps_description(timestamps)
        with open(output_txt, "w", encoding="utf-8") as file:
            file.write(timestamps_description + ("\n" if timestamps_description else ""))

        delivery_summary = {
            "s3": build_s3_summary(delivery["s3_enabled"], uploaded=False),
            "vk": build_vk_summary(delivery["vk_enabled"], uploaded=False),
        }

        quality_summary = build_quality_summary(manifest_episodes, skip_types)
        manifest = build_compact_manifest(
            job=job,
            season=season,
            episodes_range=episodes_range,
            episode_files=episode_files,
            excluded_files=excluded_files,
            detector_context=detector_context,
            timing_detection=timing_detection,
            prefetched_anilibria_results=prefetched_anilibria_results,
            prefetched_aniskip_results=prefetched_aniskip_results,
            pretty_base_name=pretty_base_name,
            output_video=output_video,
            output_txt=output_txt,
            delivery_summary=delivery_summary,
            quality_summary=quality_summary,
            manifest_episodes=manifest_episodes,
            processing_metadata={
                "chunk_size_episodes": processing["chunk_size_episodes"],
                "chunks_count": len(episode_chunks),
                "resumable_final_chunks": True,
            },
            timing_sources_summary=timing_sources_summary,
        )

        print("\n[QUALITY SUMMARY]")
        print(json.dumps(quality_summary, indent=2, ensure_ascii=False))

        manifest["render_complete"] = True
        write_outputs(output_txt, output_manifest, timestamps, manifest)
        render_completed = True
        result = deliver_rendered_output(
            job,
            delivery,
            output_video=output_video,
            output_txt=output_txt,
            output_manifest=output_manifest,
            manifest=manifest,
            timestamps=timestamps,
            pretty_base_name=pretty_base_name,
            temp_dir=temp_dir,
            total_episodes=len(episode_infos),
            runtime_status_path=runtime_status_path,
        )
        job_completed = is_job_completed(result)

        set_runtime_stage(runtime_status_path, "job_done", total_episodes=len(episode_infos))
        print(f"\n=== JOB DONE: {title} ===")
        print(output_video)
        print(output_txt)
        print(output_manifest)
        return result
    finally:
        cleanup_job_artifacts(
            cleanup,
            download_dir=download_dir,
            temp_dir=temp_dir,
            job_output_dir=job_output_dir,
            render_completed=render_completed,
            job_completed=job_completed,
            preserve_temp_on_failure=processing_mode != "single_episode",
        )
