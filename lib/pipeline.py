import json
import shutil
from pathlib import Path

from lib.aniskip import (
    build_quality_summary,
    get_aniskip_segments,
    print_skip_log,
    summarize_skips,
)
from lib.anilibria import get_anilibria_segments
from lib.detector import (
    build_detector_context,
    get_detector_type_result,
    normalize_timing_detection_config,
)
from lib.discovery import filter_episode_files, find_episode_files
from lib.helpers import (
    build_compilation_display_name,
    build_single_episode_display_name,
    build_timestamps_description,
    build_vk_comment_text,
    build_vk_wall_post_text,
    create_concat_file,
    ensure_non_empty_slug,
    get_display_title,
    parse_episodes_range,
    sanitize_filename,
    seconds_to_timestamp,
    run,
)
from lib.media import (
    cap_subsegment_durations,
    get_preferred_audio_stream,
    snap_remove_segments_to_keyframes,
    build_hybrid_subsegments,
    build_keep_segments,
    ffprobe_duration,
    get_keyframes,
    render_concat,
    render_final,
    render_segment,
)
from lib.runtime import update_runtime_status
from lib.storage import upload_file_to_s3
from lib.validation import reset_temp_dir
from lib.vk import publish_video_to_vk


def download_magnet(magnet: str, download_dir: Path):
    download_dir.mkdir(parents=True, exist_ok=True)

    run([
        "aria2c",
        "--dir", download_dir,
        "--seed-time=0",
        "--summary-interval=30",
        "--max-connection-per-server=16",
        "--split=16",
        "--continue=true",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        magnet,
    ])


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
        "timing_sources_summary": {
            "anilibria_available": any(result["segments"] for result in prefetched_anilibria_results.values()),
            "aniskip_available": any(result["segments"] for result in prefetched_aniskip_results.values()),
            "detector_available": detector_context["available"],
        },
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
        "pixel_format": encoding.get("segment_pixel_format", default_pixel_format),
        "cut_mode": encoding.get("segment_cut_mode", "precise"),
        "boundary_reencode_seconds": float(encoding.get("boundary_reencode_seconds", 3.0)),
        "max_render_seconds": float(encoding.get("segment_max_render_seconds", 150)),
    }


def normalize_processing_config(job):
    processing = {
        "chunk_size_episodes": 12,
    }
    processing.update(job.get("processing", {}))
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


def collect_episode_files(source, title_slug):
    if source["type"] == "magnet":
        download_dir = Path(source.get("download_dir", f"./downloads/{title_slug}"))
        download_magnet(source["magnet"], download_dir)
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
        "vk_comment_banner_path": "./assets/banner.png",
        "vk_comment_template": "",
    }
    delivery.update(job.get("delivery", {}))
    delivery["s3_enabled"] = bool(delivery.get("s3_enabled", True))
    delivery["s3_upload_video"] = bool(delivery.get("s3_upload_video", False))
    delivery["s3_upload_timestamps"] = bool(delivery.get("s3_upload_timestamps", False))
    delivery["s3_upload_manifest"] = bool(delivery.get("s3_upload_manifest", True))
    delivery["vk_enabled"] = bool(delivery.get("vk_enabled", True))
    delivery["vk_wall_post_enabled"] = bool(delivery.get("vk_wall_post_enabled", True))
    delivery["vk_comment_enabled"] = bool(delivery.get("vk_comment_enabled", True))
    delivery["vk_privacy_view"] = int(delivery.get("vk_privacy_view", 0))
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
        "post_id": result.get("post_id"),
        "comment_id": result.get("comment_id"),
        "comment_attachment": result.get("comment_attachment"),
        "errors_by_stage": result.get("errors_by_stage", {}),
    }


def cleanup_job_artifacts(cleanup, download_dir=None, temp_dir=None, job_output_dir=None, success=False):
    cleanup = cleanup or {}

    if cleanup.get("downloads", True) and download_dir:
        print(f"[CLEANUP] Removing downloads: {download_dir}")
        shutil.rmtree(download_dir, ignore_errors=True)

    if cleanup.get("temp", True) and temp_dir:
        print(f"[CLEANUP] Removing temp: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)

    if success and cleanup.get("output", False) and job_output_dir:
        print(f"[CLEANUP] Removing output: {job_output_dir}")
        shutil.rmtree(job_output_dir, ignore_errors=True)


def set_runtime_stage(runtime_status_path, stage, **current_job_updates):
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
    temp_dir,
    cumulative_time,
    detector_context,
    segment_encoding,
    prefetched_anilibria_results,
    prefetched_aniskip_results,
    runtime_status_path=None,
    total_episodes=None,
    preferred_language="rus",
):
    chunk_dir = temp_dir / f"chunk_{chunk_index:03d}"
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
    encoding = job.get("encoding", {})
    cleanup = job.get("cleanup", {"downloads": True, "temp": True})
    processing = normalize_processing_config(job)
    timing_detection = normalize_timing_detection_config(job)
    segment_encoding = build_segment_encoding(encoding)
    delivery = build_delivery_config(job)
    timing_providers = job.get("timing_providers", {})
    anilibria_enabled = timing_providers.get("anilibria_enabled", True)
    aniskip_enabled = timing_providers.get("aniskip_enabled", False)
    preferred_language = str(job.get("preferred_audio_language", "rus")).strip().lower() or "rus"

    title_slug = ensure_non_empty_slug(title)
    allowed_episodes = parse_episodes_range(episodes_range)

    job_output_dir = output_root / title_slug
    job_output_dir.mkdir(parents=True, exist_ok=True)

    temp_dir = reset_temp_dir(title_slug)
    download_dir = None
    success = False

    try:
        set_runtime_stage(runtime_status_path, "validation")
        set_runtime_stage(runtime_status_path, "download")
        download_dir, detected_episode_files, ignored_files = collect_episode_files(
            source,
            title_slug,
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
            pretty_base_name = build_single_episode_display_name(job, season, episode_number)
            file_base_name = sanitize_filename(pretty_base_name)
            output_video = job_output_dir / f"{file_base_name}.mkv"
            output_txt = job_output_dir / f"{file_base_name}.txt"
            output_manifest = job_output_dir / f"{file_base_name}_manifest.json"
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

            write_outputs(output_txt, output_manifest, timestamps, manifest)

            s3_prefix = f"animonster/{title_slug}/S{season}/"
            s3_uploaded_files = {}
            s3_manifest_pending = False
            if delivery["s3_enabled"]:
                set_runtime_stage(runtime_status_path, "delivery_s3", total_episodes=1)
                print(f"[DELIVERY] S3 start: {pretty_base_name}")
                try:
                    if delivery.get("s3_upload_video", False):
                        upload_file_to_s3(output_video, s3_prefix + output_video.name)
                        s3_uploaded_files["video"] = s3_prefix + output_video.name
                    if delivery.get("s3_upload_timestamps", False):
                        upload_file_to_s3(output_txt, s3_prefix + output_txt.name)
                        s3_uploaded_files["timestamps"] = s3_prefix + output_txt.name
                    s3_manifest_pending = delivery.get("s3_upload_manifest", True)
                    delivery_summary["s3"] = build_s3_summary(
                        enabled=True,
                        uploaded=bool(s3_uploaded_files) or s3_manifest_pending,
                        uploaded_files=s3_uploaded_files,
                    )
                    print(f"[DELIVERY] S3 ok: {pretty_base_name}")
                except Exception as exc:
                    s3_error = repr(exc)
                    print(f"[DELIVERY] S3 failed: {s3_error}")
                    delivery_summary["s3"] = build_s3_summary(
                        enabled=True,
                        uploaded=False,
                        error=s3_error,
                        uploaded_files=s3_uploaded_files,
                    )

            if delivery["vk_enabled"]:
                set_runtime_stage(runtime_status_path, "delivery_vk", total_episodes=1)
                print(f"[DELIVERY] VK video start: {pretty_base_name}")
                try:
                    wall_post_text = (
                        build_vk_wall_post_text(job, pretty_base_name)
                        if delivery.get("vk_wall_post_enabled", True)
                        else None
                    )
                    comment_text = (
                        build_vk_comment_text(delivery.get("vk_comment_template", ""))
                        if delivery.get("vk_comment_enabled", True)
                        else None
                    )
                    if wall_post_text:
                        print(f"[DELIVERY] VK post start: {pretty_base_name}")
                    if comment_text:
                        print(f"[DELIVERY] VK comment start: {pretty_base_name}")
                    vk_result = publish_video_to_vk(
                        output_video,
                        pretty_base_name,
                        timestamps_description,
                        wall_post_text=wall_post_text,
                        comment_text=comment_text,
                        comment_banner_path=delivery.get("vk_comment_banner_path"),
                        privacy_view=delivery.get("vk_privacy_view", 0),
                    )
                    delivery_summary["vk"] = build_vk_summary(
                        enabled=True,
                        uploaded=True,
                        result=vk_result,
                    )
                    print(f"[DELIVERY] VK video ok: {pretty_base_name}")
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

            manifest["delivery_summary"] = delivery_summary
            write_outputs(output_txt, output_manifest, timestamps, manifest)

            if delivery["s3_enabled"] and s3_manifest_pending:
                try:
                    upload_file_to_s3(output_manifest, s3_prefix + output_manifest.name)
                    s3_uploaded_files["manifest"] = s3_prefix + output_manifest.name
                    delivery_summary["s3"] = build_s3_summary(
                        enabled=True,
                        uploaded=True,
                        uploaded_files=s3_uploaded_files,
                    )
                    manifest["delivery_summary"] = delivery_summary
                    write_outputs(output_txt, output_manifest, timestamps, manifest)
                    print(f"[DELIVERY] S3 manifest ok: {pretty_base_name}")
                except Exception as exc:
                    s3_error = repr(exc)
                    print(f"[DELIVERY] S3 failed on manifest: {s3_error}")
                    delivery_summary["s3"] = build_s3_summary(
                        enabled=True,
                        uploaded=False,
                        error=s3_error,
                        uploaded_files=s3_uploaded_files,
                    )
                    manifest["delivery_summary"] = delivery_summary
                    write_outputs(output_txt, output_manifest, timestamps, manifest)

            set_runtime_stage(runtime_status_path, "job_done", total_episodes=1)
            success = True
            print(f"\n=== JOB DONE: {title} ===")
            print(output_video)
            print(output_txt)
            print(output_manifest)
            return {
                "output_video": str(output_video),
                "output_timestamps": str(output_txt),
                "output_manifest": str(output_manifest),
                "delivery_summary": delivery_summary,
                "quality_summary": {},
                "output_display_name": pretty_base_name,
                "timestamps_description": timestamps_description,
            }

        episode_infos = build_episode_infos(episode_files)
        episode_chunks = split_episode_infos_into_chunks(
            episode_infos,
            processing["chunk_size_episodes"],
        )
        set_runtime_stage(
            runtime_status_path,
            "episode_scan",
            total_episodes=len(episode_infos),
            total_chunks=len(episode_chunks),
        )
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

        if detector_context["enabled"]:
            status_text = "ready" if detector_context["available"] else f"disabled: {detector_context['reason']}"
            print(f"\n[DETECTOR] {status_text}")

        pretty_base_name = build_compilation_display_name(job, season, episodes_range)
        file_base_name = sanitize_filename(pretty_base_name)
        output_video = job_output_dir / f"{file_base_name}.mkv"
        output_txt = job_output_dir / f"{file_base_name}.txt"
        output_manifest = job_output_dir / f"{file_base_name}_manifest.json"
        concat_file = temp_dir / "concat.txt"
        concat_output = temp_dir / "concat_output.mkv"

        timestamps = []
        cumulative_time = 0.0
        chunk_outputs = []
        manifest_episodes = []

        for chunk_index, chunk_episode_infos in enumerate(episode_chunks, start=1):
            chunk_result = process_episode_chunk(
                chunk_episode_infos,
                chunk_index=chunk_index,
                total_chunks=len(episode_chunks),
                skip_types=skip_types,
                temp_dir=temp_dir,
                cumulative_time=cumulative_time,
                detector_context=detector_context,
                segment_encoding=segment_encoding,
                prefetched_anilibria_results=prefetched_anilibria_results,
                prefetched_aniskip_results=prefetched_aniskip_results,
                runtime_status_path=runtime_status_path,
                total_episodes=len(episode_infos),
                preferred_language=preferred_language,
            )
            cumulative_time = chunk_result["cumulative_time"]
            chunk_outputs.append(chunk_result["chunk_output"])
            manifest_episodes.extend(chunk_result["manifest_episodes"])
            timestamps.extend(chunk_result["timestamps"])

        set_runtime_stage(
            runtime_status_path,
            "concat",
            total_episodes=len(episode_infos),
            current_chunk_index=None,
            current_chunk_episode_range=None,
        )
        create_concat_file(chunk_outputs, concat_file)
        render_concat(concat_file, concat_output)
        set_runtime_stage(
            runtime_status_path,
            "final_render",
            total_episodes=len(episode_infos),
            current_chunk_index=None,
            current_chunk_episode_range=None,
        )
        render_final(
            concat_output=concat_output,
            watermark_path=watermark_path,
            output_video=output_video,
            encoding=encoding,
        )

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
            },
        )

        print("\n[QUALITY SUMMARY]")
        print(json.dumps(quality_summary, indent=2, ensure_ascii=False))

        # Persist the local manifest before remote delivery so failed uploads
        # still leave behind a trace of what stage the job reached.
        write_outputs(output_txt, output_manifest, timestamps, manifest)

        s3_prefix = f"animonster/{title_slug}/S{season}/"
        s3_uploaded_files = {}
        s3_manifest_pending = False
        if delivery["s3_enabled"]:
            set_runtime_stage(runtime_status_path, "delivery_s3", total_episodes=len(episode_infos))
            print(f"[DELIVERY] S3 start: {pretty_base_name}")
            s3_error = None
            try:
                if delivery.get("s3_upload_video", False):
                    upload_file_to_s3(output_video, s3_prefix + output_video.name)
                    s3_uploaded_files["video"] = s3_prefix + output_video.name
                if delivery.get("s3_upload_timestamps", False):
                    upload_file_to_s3(output_txt, s3_prefix + output_txt.name)
                    s3_uploaded_files["timestamps"] = s3_prefix + output_txt.name
                s3_manifest_pending = delivery.get("s3_upload_manifest", True)
                delivery_summary["s3"] = build_s3_summary(
                    enabled=True,
                    uploaded=bool(s3_uploaded_files) or s3_manifest_pending,
                    uploaded_files=s3_uploaded_files,
                )
                print(f"[DELIVERY] S3 ok: {pretty_base_name}")
            except Exception as exc:
                s3_error = repr(exc)
                print(f"[DELIVERY] S3 failed: {s3_error}")
                delivery_summary["s3"] = build_s3_summary(
                    enabled=True,
                    uploaded=False,
                    error=s3_error,
                    uploaded_files=s3_uploaded_files,
                )

        if delivery["vk_enabled"]:
            set_runtime_stage(runtime_status_path, "delivery_vk", total_episodes=len(episode_infos))
            print(f"[DELIVERY] VK video start: {pretty_base_name}")
            try:
                wall_post_text = (
                    build_vk_wall_post_text(job, pretty_base_name)
                    if delivery.get("vk_wall_post_enabled", True)
                    else None
                )
                comment_text = (
                    build_vk_comment_text(delivery.get("vk_comment_template", ""))
                    if delivery.get("vk_comment_enabled", True)
                    else None
                )
                if wall_post_text:
                    print(f"[DELIVERY] VK post start: {pretty_base_name}")
                if comment_text:
                    print(f"[DELIVERY] VK comment start: {pretty_base_name}")
                vk_result = publish_video_to_vk(
                    output_video,
                    pretty_base_name,
                    timestamps_description,
                    wall_post_text=wall_post_text,
                    comment_text=comment_text,
                    comment_banner_path=delivery.get("vk_comment_banner_path"),
                    privacy_view=delivery.get("vk_privacy_view", 0),
                )
                delivery_summary["vk"] = build_vk_summary(
                    enabled=True,
                    uploaded=True,
                    result=vk_result,
                )
                print(f"[DELIVERY] VK video ok: {pretty_base_name}")
                if wall_post_text:
                    if vk_result.get("post_created"):
                        print(f"[DELIVERY] VK post ok: {pretty_base_name}")
                    else:
                        print(f"[DELIVERY] VK post failed: {vk_result.get('errors_by_stage', {}).get('wall_post')}")
                if comment_text and vk_result.get("post_created"):
                    if vk_result.get("comment_created"):
                        print(f"[DELIVERY] VK comment ok: {pretty_base_name}")
                    else:
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

        manifest["delivery_summary"] = delivery_summary
        write_outputs(output_txt, output_manifest, timestamps, manifest)

        if delivery["s3_enabled"] and s3_manifest_pending:
            try:
                upload_file_to_s3(output_manifest, s3_prefix + output_manifest.name)
                s3_uploaded_files["manifest"] = s3_prefix + output_manifest.name
                delivery_summary["s3"] = build_s3_summary(
                    enabled=True,
                    uploaded=True,
                    uploaded_files=s3_uploaded_files,
                )
                manifest["delivery_summary"] = delivery_summary
                write_outputs(output_txt, output_manifest, timestamps, manifest)
                print(f"[DELIVERY] S3 manifest ok: {pretty_base_name}")
            except Exception as exc:
                s3_error = repr(exc)
                print(f"[DELIVERY] S3 failed on manifest: {s3_error}")
                delivery_summary["s3"] = build_s3_summary(
                    enabled=True,
                    uploaded=False,
                    error=s3_error,
                    uploaded_files=s3_uploaded_files,
                )
                manifest["delivery_summary"] = delivery_summary
                write_outputs(output_txt, output_manifest, timestamps, manifest)

        set_runtime_stage(runtime_status_path, "job_done", total_episodes=len(episode_infos))
        success = True
        print(f"\n=== JOB DONE: {title} ===")
        print(output_video)
        print(output_txt)
        print(output_manifest)
        return {
            "output_video": str(output_video),
            "output_timestamps": str(output_txt),
            "output_manifest": str(output_manifest),
            "delivery_summary": delivery_summary,
            "quality_summary": quality_summary,
            "output_display_name": pretty_base_name,
            "timestamps_description": timestamps_description,
        }
    finally:
        cleanup_job_artifacts(
            cleanup,
            download_dir=download_dir,
            temp_dir=temp_dir,
            job_output_dir=job_output_dir,
            success=success,
        )
