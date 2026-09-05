import json
import hashlib
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
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
    DETECTOR_RESULT_VERSION,
    build_detector_context,
    confidence_meets_threshold,
    get_detector_type_result,
    normalize_timing_detection_config,
)
from core.discovery import filter_episode_files, find_episode_files, find_external_audio_files
from core.torrent import (
    discover_torrent_episode_numbers,
    download_selected_episodes,
    download_selected_episodes_from_sources,
)
from shared.helpers import (
    JobCancelled,
    build_job_workspace_name,
    build_compilation_display_name,
    build_multi_season_display_name,
    build_single_episode_display_name,
    build_timestamps_description,
    build_vk_comment_text,
    build_vk_wall_post_text,
    create_concat_file,
    ensure_non_empty_slug,
    format_episodes_label,
    format_navigation_label,
    get_automatic_navigation_label,
    get_display_title,
    parse_episodes_range,
    raise_if_cancelled,
    sanitize_filename,
    seconds_to_timestamp,
)
from shared.constants import DEFAULT_TIMING_DETECTION, TEMP_ROOT
from core.media import (
    analyze_audio_recovery,
    analyze_external_audio_recovery,
    detect_audio_streams,
    get_preferred_audio_stream,
    build_keep_segments,
    ffprobe_duration,
    ffprobe_episode_timeline,
    ffprobe_media_signature,
    render_concat,
    render_episode,
    render_final,
    select_audio_stream_by_language,
    select_external_audio,
    validate_episode_render,
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
        "support_episode_count",
        "consensus_score",
        "reference_interval",
        "analysis_audio",
        "full_reference_similarity",
        "reference_core_similarity",
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

    compact = {
        "episode": manifest_episode["episode"],
        "source_file": Path(manifest_episode["source_file"]).name,
        "original_duration": _round_or_none(original_duration),
        "expected_cleaned_duration": _round_or_none(
            manifest_episode.get("expected_cleaned_duration", cleaned_duration)
        ),
        "cleaned_duration": _round_or_none(cleaned_duration),
        "removed_duration": _round_or_none(max(0.0, original_duration - cleaned_duration)),
        "segment_cut_mode": manifest_episode.get("segment_cut_mode"),
        "keyframe_aligned": manifest_episode.get("keyframe_aligned", False),
        "timing_info": _compact_timing_info(manifest_episode.get("timing_info", {}), skip_types),
        "skip_summary": manifest_episode.get("skip_summary", {}),
    }
    if manifest_episode.get("audio_recovery"):
        compact["audio_recovery"] = manifest_episode["audio_recovery"]
    if manifest_episode.get("audio"):
        compact["audio"] = manifest_episode["audio"]
    if manifest_episode.get("analysis_audio"):
        compact["analysis_audio"] = manifest_episode["analysis_audio"]
    if manifest_episode.get("support_banner") is not None:
        compact["support_banner"] = manifest_episode["support_banner"]
    return compact


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
    missing_source_episodes=None,
):
    missing_source_episodes = sorted(int(value) for value in (missing_source_episodes or []))
    manifest = {
        "render_pipeline_version": RENDER_PIPELINE_VERSION,
        "title": job["title"],
        "title_ru": job.get("title_ru"),
        "mal_id": job.get("mal_id"),
        "season": season,
        "episodes_range": episodes_range,
        "episodes_count": len(episode_files),
        "source": job["source"]["type"],
        "source_summary": {
            "selected_episode_count": len(episode_files),
            "missing_episode_count": len(missing_source_episodes),
            "missing_episodes": missing_source_episodes,
            "excluded_file_count": len(excluded_files),
            "external_audio_episode_count": sum(
                1 for episode in manifest_episodes
                if (episode.get("audio") or {}).get("source") == "external"
            ),
        },
        "timing_detection": {
            "enabled": timing_detection["enabled"],
            "available": detector_context["available"],
            "reason": detector_context["reason"],
            "algorithm_version": DETECTOR_RESULT_VERSION,
            "analysis_audio_language": timing_detection.get(
                "analysis_audio_language",
                DEFAULT_TIMING_DETECTION["analysis_audio_language"],
            ),
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


def normalize_processing_config(job):
    return dict(job.get("processing") or {})


AUTO_AUDIO_TAIL_RECOVERY_SECONDS = 3.0


def build_audio_recovery_info(
    enabled,
    path,
    audio_stream_index,
    video_path=None,
    timeline=None,
):
    if audio_stream_index is None:
        return {
            "enabled": bool(enabled),
            "applied": False,
            "automatic": False,
            "reasons": [],
        }
    if video_path is not None:
        recovery = analyze_external_audio_recovery(
            video_path,
            path,
            audio_stream_index=audio_stream_index,
            enforce_limits=enabled,
        )
    else:
        recovery = analyze_audio_recovery(
            path,
            audio_stream_index=audio_stream_index,
            timeline=timeline,
            enforce_limits=enabled,
        )
    automatic = (
        not enabled
        and recovery.get("reasons") == ["early_end"]
        and float(recovery.get("source_audio_end_shortfall", 0.0))
        <= AUTO_AUDIO_TAIL_RECOVERY_SECONDS
    )
    return {
        **recovery,
        "enabled": bool(enabled),
        "applied": bool(recovery.get("applied")) if enabled else automatic,
        "automatic": automatic,
    }


def validate_expected_episode_duration(validation, expected_duration, path):
    video_timeline = (validation.get("timeline") or {}).get("video") or {}
    actual_duration = float(video_timeline.get("duration", validation["duration"]))
    expected_duration = float(expected_duration)
    delta = actual_duration - expected_duration
    difference = abs(delta)
    if difference > 0.25:
        raise RuntimeError(
            f"Episode checkpoint duration mismatch {difference:.3f}s "
            f"(expected={expected_duration:.3f}s, actual={actual_duration:.3f}s, "
            f"delta={delta:+.3f}s): {path}"
        )


RENDER_PIPELINE_VERSION = 3

DEFAULT_SUPPORT_BANNER = {
    "enabled": True,
    "path": "./assets/support_banner.png",
    "interval_episodes": 6,
    "duration_seconds": 6.0,
    "slide_seconds": 0.5,
    "width_px": 596,
    "bottom_margin_px": 40,
}


def normalize_support_banner_config(job, privacy_view=None):
    raw = job.get("support_banner")
    config = dict(DEFAULT_SUPPORT_BANNER)
    if raw is None:
        config["enabled"] = False
    elif not isinstance(raw, dict):
        raise RuntimeError("support_banner must be a JSON object")
    else:
        config.update(raw)

    config["enabled"] = bool(config.get("enabled", True))
    numeric_fields = {
        "interval_episodes": int,
        "duration_seconds": float,
        "slide_seconds": float,
        "width_px": int,
        "bottom_margin_px": int,
    }
    for field, converter in numeric_fields.items():
        try:
            config[field] = converter(config[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"support_banner.{field} must be numeric") from exc
        if field == "bottom_margin_px":
            if config[field] < 0:
                raise RuntimeError("support_banner.bottom_margin_px must be >= 0")
        elif config[field] <= 0:
            raise RuntimeError(f"support_banner.{field} must be > 0")

    if privacy_view is None:
        try:
            privacy_view = int((job.get("delivery") or {}).get("vk_privacy_view", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("delivery.vk_privacy_view must be an integer") from exc
    config["path"] = str(config.get("path") or "")
    config["privacy_view"] = int(privacy_view)
    config["active"] = config["enabled"] and config["privacy_view"] != 5
    config["episode_ordinal_offset"] = int(
        (job.get("processing") or {}).get("_support_banner_episode_offset", 0)
    )
    if config["episode_ordinal_offset"] < 0:
        raise RuntimeError("support banner episode offset must be >= 0")
    return config


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


def build_support_banner_render_signature(job, support_banner=None):
    config = support_banner or normalize_support_banner_config(job)
    if not config["active"]:
        return {"enabled": False}
    return {
        "enabled": True,
        "asset": _file_identity(config["path"]),
        "interval_episodes": config["interval_episodes"],
        "duration_seconds": config["duration_seconds"],
        "slide_seconds": config["slide_seconds"],
        "width_px": config["width_px"],
        "bottom_margin_px": config["bottom_margin_px"],
        "episode_ordinal_offset": config["episode_ordinal_offset"],
    }


def validate_support_banner_asset(support_banner):
    if support_banner["active"] and not Path(support_banner["path"]).is_file():
        raise RuntimeError(
            f"Support banner file not found: {support_banner['path']}"
        )


def build_support_banner_episode_spec(
    support_banner,
    cleaned_duration,
    *,
    episode_ordinal=1,
    single_episode=False,
):
    shown = bool(
        support_banner["active"]
        and (
            single_episode
            or int(episode_ordinal) % support_banner["interval_episodes"] == 0
        )
    )
    cleaned_duration = max(0.0, float(cleaned_duration))
    if not shown or cleaned_duration <= 0:
        return {"shown": False}

    duration = min(support_banner["duration_seconds"], cleaned_duration)
    slide = min(support_banner["slide_seconds"], duration / 2.0)
    start = max(0.0, (cleaned_duration - duration) / 2.0)
    return {
        "shown": True,
        "path": support_banner["path"],
        "start": round(start, 6),
        "duration": round(duration, 6),
        "slide_seconds": round(slide, 6),
        "width_px": support_banner["width_px"],
        "bottom_margin_px": support_banner["bottom_margin_px"],
    }


def _source_fingerprint(path):
    encoded = json.dumps(
        _file_identity(path),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path, payload):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _effective_episode_encoding(encoding):
    encoding = encoding or {}
    return {
        key: encoding[key]
        for key in (
            "video_codec",
            "preset",
            "cq",
            "pixel_format",
            "audio_codec",
            "audio_bitrate",
            "audio_sample_rate",
            "audio_channels",
        )
        if key in encoding
    }


def build_episode_fingerprint(
    job,
    episode_infos,
    *,
    watermark_path,
    timing_detection,
    preferred_language,
):
    support_banner = normalize_support_banner_config(job)
    payload = {
        "version": RENDER_PIPELINE_VERSION,
        "title": job.get("title"),
        "season": str(job.get("season", "")).lstrip("0"),
        "episodes_range": job.get("episodes_range"),
        "source": job.get("source"),
        "skip_types": job.get("skip_types", ["op", "ed"]),
        "timing_detection": timing_detection,
        **({
            "timing_detection_algorithm_version": DETECTOR_RESULT_VERSION,
        } if timing_detection.get("enabled") else {}),
        "timing_providers": job.get("timing_providers") or {},
        "encoding": _effective_episode_encoding(job.get("encoding")),
        "preferred_audio_language": preferred_language,
        "watermark": _file_identity(watermark_path),
        "support_banner": build_support_banner_render_signature(job, support_banner),
        "episodes": [
            {
                "episode": item["episode"],
                "duration": round(float(item["duration"]), 3),
                "frame_rate": item.get("frame_rate"),
                "width": item.get("width"),
                "height": item.get("height"),
                "file": _file_identity(item["path"]),
                **({
                    "external_audio": {
                        "file": _file_identity(item["external_audio"]["path"]),
                        "audio_index": item["external_audio"]["audio_index"],
                        "stream_index": item["external_audio"]["stream_index"],
                    }
                } if item.get("external_audio") else {}),
                **({"analysis_audio": item["analysis_audio"]} if item.get("analysis_audio") else {}),
            }
            for item in episode_infos
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def initialize_episode_checkpoints(temp_dir, fingerprint):
    checkpoint_path = temp_dir / "checkpoint.json"
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        checkpoint = None

    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("render_pipeline_version") != RENDER_PIPELINE_VERSION
        or checkpoint.get("fingerprint") != fingerprint
    ):
        temp_dir.mkdir(parents=True, exist_ok=True)
        for item in temp_dir.iterdir():
            if item.name == "timing_detection_cache":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        checkpoint = {
            "render_pipeline_version": RENDER_PIPELINE_VERSION,
            "fingerprint": fingerprint,
            "render_context": None,
        }
        _write_json_atomic(checkpoint_path, checkpoint)
    return checkpoint


def load_episode_checkpoint(temp_dir, episode_info, audio_recovery_enabled=False):
    episode_number = episode_info["episode"]
    episode_dir = temp_dir / f"episode_{episode_number:03d}"
    checkpoint_path = episode_dir / "checkpoint.json"
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("render_pipeline_version") != RENDER_PIPELINE_VERSION:
            return None
        if checkpoint.get("episode") != episode_number:
            return None
        if checkpoint.get("source_fingerprint") != _source_fingerprint(episode_info["path"]):
            return None
        if checkpoint.get("output_file") != "rendered.mkv":
            return None
        output = episode_dir / "rendered.mkv"
        if not output.is_file() or output.stat().st_size != checkpoint.get("size"):
            return None
        validation = validate_episode_render(output)
    except Exception:
        return None

    if validation["media_signature"] != checkpoint.get("media_signature"):
        return None
    if abs(validation["duration"] - float(checkpoint.get("duration", 0.0))) > 0.05:
        return None
    manifest_episode = checkpoint.get("manifest_episode")
    if not isinstance(manifest_episode, dict) or manifest_episode.get("episode") != episode_number:
        return None
    audio_recovery = manifest_episode.get("audio_recovery") or {}
    if (
        audio_recovery.get("applied")
        and not audio_recovery.get("automatic")
        and not audio_recovery_enabled
    ):
        return None
    try:
        validate_expected_episode_duration(
            validation,
            manifest_episode.get("expected_cleaned_duration", checkpoint.get("duration", 0.0)),
            output,
        )
    except (TypeError, ValueError, RuntimeError):
        return None
    return {
        **checkpoint,
        "output": output,
        "duration": validation["duration"],
        "media_signature": validation["media_signature"],
        "timeline": validation["timeline"],
    }


def save_episode_checkpoint(
    episode_dir,
    episode_info,
    rendered_work,
    manifest_episode,
):
    validation = validate_episode_render(rendered_work)
    validate_expected_episode_duration(
        validation,
        manifest_episode["expected_cleaned_duration"],
        rendered_work,
    )
    rendered = episode_dir / "rendered.mkv"
    rendered.unlink(missing_ok=True)
    rendered_work.replace(rendered)
    manifest_episode["cleaned_duration"] = validation["duration"]
    checkpoint = {
        "render_pipeline_version": RENDER_PIPELINE_VERSION,
        "episode": episode_info["episode"],
        "source_fingerprint": _source_fingerprint(episode_info["path"]),
        "output_file": rendered.name,
        "size": rendered.stat().st_size,
        "duration": validation["duration"],
        "media_signature": validation["media_signature"],
        "timeline": validation["timeline"],
        "manifest_episode": manifest_episode,
    }
    _write_json_atomic(episode_dir / "checkpoint.json", checkpoint)
    return {
        **checkpoint,
        "output": rendered,
    }


def build_timestamps_from_episodes(manifest_episodes):
    cumulative_time = 0.0
    timestamps = []
    for episode in manifest_episodes:
        timestamps.append(f"{seconds_to_timestamp(cumulative_time)} - {episode['episode']} серия")
        cumulative_time += float(episode.get("cleaned_duration", 0.0))
    return timestamps


def build_multi_season_timestamps(manifest_episodes):
    cumulative_time = 0.0
    timestamps = []
    for episode in manifest_episodes:
        timestamps.append(
            f"{seconds_to_timestamp(cumulative_time)} - "
            f"{episode['season']} сезон, {episode['episode']} серия"
        )
        cumulative_time += float(episode.get("cleaned_duration", 0.0))
    return timestamps


def renumber_season_part_episodes(
    manifest_episodes,
    season,
    episode_offset,
    source_episode_start=1,
):
    return [
        {
            **episode,
            "season": int(season),
            "source_episode": int(episode["episode"]),
            "episode": (
                int(episode_offset)
                + int(episode["episode"])
                - int(source_episode_start)
                + 1
            ),
        }
        for episode in manifest_episodes
    ]


def build_timing_sources_summary(prefetched_anilibria_results, prefetched_aniskip_results, detector_context):
    return {
        "anilibria_available": any(result["segments"] for result in prefetched_anilibria_results.values()),
        "aniskip_available": any(result["segments"] for result in prefetched_aniskip_results.values()),
        "detector_available": detector_context["available"],
    }


def build_episode_infos(
    episode_files,
    external_audio_files=None,
    preferred_language="rus",
    analysis_audio_language="jpn",
):
    external_by_episode = {}
    for episode_number, path in external_audio_files or []:
        external_by_episode.setdefault(episode_number, []).append(path)
    episode_infos = []
    for episode_number, path in episode_files:
        signature = ffprobe_media_signature(path)
        video = (signature or {}).get("video") or {}
        container_duration = ffprobe_duration(path)
        source_timeline = ffprobe_episode_timeline(path)
        video_timeline = source_timeline.get("video")
        if video_timeline is None:
            raise RuntimeError(f"Episode has no video packets: {path}")
        duration = float(video_timeline["duration"])
        external_audio = select_external_audio(
            external_by_episode.get(episode_number, []),
            container_duration,
            preferred_language,
        )
        embedded_audio_streams = detect_audio_streams(path)
        analysis_stream = select_audio_stream_by_language(
            embedded_audio_streams,
            analysis_audio_language,
            fallback=False,
        )
        if analysis_stream is not None:
            analysis_audio = {
                "path": str(path),
                "audio_index": analysis_stream["audio_index"],
                "stream_index": analysis_stream["stream_index"],
                "language": analysis_stream.get("language") or analysis_audio_language,
                "source": "embedded",
            }
        elif external_audio is not None:
            analysis_audio = {
                "path": external_audio["path"],
                "audio_index": external_audio["audio_index"],
                "stream_index": external_audio["stream_index"],
                "language": external_audio.get("language") or preferred_language,
                "source": "external",
            }
        else:
            fallback_stream = select_audio_stream_by_language(
                embedded_audio_streams,
                preferred_language,
            )
            analysis_audio = (
                {
                    "path": str(path),
                    "audio_index": fallback_stream["audio_index"],
                    "stream_index": fallback_stream["stream_index"],
                    "language": fallback_stream.get("language") or preferred_language,
                    "source": "embedded",
                }
                if fallback_stream is not None
                else None
            )
        episode_infos.append({
            "episode": episode_number,
            "path": str(path),
            "duration": duration,
            "container_duration": container_duration,
            "source_timeline": source_timeline,
            "frame_rate": video.get("r_frame_rate"),
            "width": video.get("width"),
            "height": video.get("height"),
            "external_audio": external_audio,
            "analysis_audio": analysis_audio,
        })
    return episode_infos


def select_compilation_frame_rate(episode_infos):
    counts = {}
    for episode_info in episode_infos:
        try:
            frame_rate = Fraction(str(episode_info.get("frame_rate")))
        except (ValueError, ZeroDivisionError):
            continue
        if frame_rate <= 0:
            continue
        counts[frame_rate] = counts.get(frame_rate, 0) + 1
    if not counts:
        raise RuntimeError("Unable to determine compilation frame rate")
    selected = max(counts, key=counts.get)
    return f"{selected.numerator}/{selected.denominator}"


def select_compilation_frame_size(episode_infos):
    sizes = []
    for episode_info in episode_infos:
        try:
            width = int(episode_info.get("width"))
            height = int(episode_info.get("height"))
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            sizes.append((width, height))
    if not sizes:
        raise RuntimeError("Unable to determine compilation frame size")
    return max(sizes, key=lambda size: size[0] * size[1])


def describe_media_signature_groups(episode_infos, episode_signatures):
    groups = {}
    for episode_info, signature in zip(episode_infos, episode_signatures):
        encoded = json.dumps(
            signature,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        groups.setdefault(encoded, []).append(int(episode_info["episode"]))
    return "; ".join(
        f"episodes={','.join(f'{episode:03d}' for episode in episodes)} signature={signature}"
        for signature, episodes in groups.items()
    )


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
        source_parts = source.get("parts") or []
        if source_parts:
            download_selected_episodes_from_sources(
                source_parts,
                download_dir,
                allowed_episodes,
                timeout=download_timeout,
                allow_missing_episodes=bool(
                    (processing or {}).get("allow_missing_episodes", False)
                ),
            )
        else:
            download_selected_episodes(
                source["magnet"],
                download_dir,
                allowed_episodes,
                path_filter=(processing or {}).get("source_path_contains"),
                timeout=download_timeout,
                allow_missing_episodes=bool(
                    (processing or {}).get("allow_missing_episodes", False)
                ),
            )
        detected_episode_files, ignored_files = find_episode_files(download_dir)
        external_audio_files = find_external_audio_files(download_dir, allowed_episodes)
        return download_dir, detected_episode_files, ignored_files, external_audio_files

    if source["type"] == "local":
        input_dir = Path(source.get("input_dir", "./input"))
        detected_episode_files, ignored_files = find_episode_files(input_dir)
        external_audio_files = find_external_audio_files(input_dir, allowed_episodes)
        return None, detected_episode_files, ignored_files, external_audio_files

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
    analysis_audio=None,
    full_reference_similarity=None,
    reference_core_similarity=None,
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
        "analysis_audio": analysis_audio,
        "full_reference_similarity": full_reference_similarity,
        "reference_core_similarity": reference_core_similarity,
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

        detector_auto_cut_threshold = (detector_context.get("config") or {}).get(
            "auto_cut_min_confidence",
            "high",
        )
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
            and confidence_meets_threshold(
                detector_result["confidence"],
                detector_auto_cut_threshold,
            )
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
            analysis_audio=detector_result.get("analysis_audio"),
            full_reference_similarity=detector_result.get("full_reference_similarity"),
            reference_core_similarity=detector_result.get("reference_core_similarity"),
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
                "analysis_audio": per_type[skip_type]["analysis_audio"],
                "full_reference_similarity": per_type[skip_type]["full_reference_similarity"],
                "reference_core_similarity": per_type[skip_type]["reference_core_similarity"],
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


def build_episode_render_plan(
    episode_info,
    skip_types,
    detector_context,
    anilibria_result,
    aniskip_result,
    preferred_language="rus",
    audio_recovery_enabled=False,
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
    print_skip_log(
        detected_ep,
        skip_summary,
        skip_types,
        review_required=timing_info["review_required"],
    )

    keep_segments = [
        (start, end)
        for start, end in build_keep_segments(duration, remove_segments)
        if end > start
    ]
    expected_duration = sum(end - start for start, end in keep_segments)
    if expected_duration <= 0:
        raise RuntimeError(f"Episode {detected_ep} has no video left after OP/ED cuts")
    external_audio = episode_info.get("external_audio")
    audio_path = Path(external_audio["path"]) if external_audio else ep_file
    audio_streams = detect_audio_streams(ep_file) if not external_audio else [external_audio]
    audio_stream_index = (
        external_audio["audio_index"] if external_audio
        else get_preferred_audio_stream(ep_file, preferred_language) if audio_streams
        else None
    )
    audio_recovery = build_audio_recovery_info(
        audio_recovery_enabled,
        audio_path,
        audio_stream_index,
        video_path=ep_file if external_audio else None,
        timeline=(
            episode_info.get("source_timeline")
            if not external_audio and audio_stream_index == 0 else None
        ),
    )
    audio_manifest = {
        "source": "external" if external_audio else "embedded" if audio_streams else "none",
        "stream_index": (
            external_audio["stream_index"] if external_audio
            else audio_streams[audio_stream_index]["stream_index"] if audio_stream_index is not None
            else None
        ),
    }
    if external_audio:
        audio_manifest.update({
            "external_file": Path(external_audio["path"]).name,
            "start_time": _round_or_none(external_audio["start_time"]),
            "duration_delta": _round_or_none(external_audio["duration_delta"]),
        })
    manifest_episode = {
        "episode": detected_ep,
        "source_file": str(ep_file),
        "original_duration": duration,
        "expected_cleaned_duration": expected_duration,
        "cleaned_duration": expected_duration,
        "segment_cut_mode": "normalized_episode",
        "keyframe_aligned": False,
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
        "kept_segments": [
            {"start": start, "end": end, "cut_mode": "normalized_filter"}
            for start, end in keep_segments
        ],
        "audio_recovery": audio_recovery,
        "audio": audio_manifest,
        "analysis_audio": episode_info.get("analysis_audio"),
    }
    return {
        "keep_segments": keep_segments,
        "expected_duration": expected_duration,
        "audio_stream_index": audio_stream_index,
        "external_audio_path": external_audio["path"] if external_audio else None,
        "audio_recovery": audio_recovery,
        "manifest_episode": manifest_episode,
    }


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
    if processing_mode == "multi_season":
        season_range = str((job.get("processing") or {}).get("season_range") or "").strip()
        if not season_range:
            raise RuntimeError("multi_season mode requires season_range")
        pretty_base_name = build_multi_season_display_name(job, season_range)
        job_output_dir = Path(output_root) / ensure_non_empty_slug(job["title"])
        file_base_name = sanitize_filename(pretty_base_name)
        return {
            "job_output_dir": job_output_dir,
            "pretty_base_name": pretty_base_name,
            "output_video": job_output_dir / f"{file_base_name}.mkv",
            "output_txt": job_output_dir / f"{file_base_name}.txt",
            "output_manifest": job_output_dir / f"{file_base_name}_manifest.json",
        }
    if processing_mode == "single_episode":
        episodes = sorted(parse_episodes_range(episodes_range))
        if len(episodes) != 1:
            raise RuntimeError("single_episode mode requires exactly one requested episode")
        pretty_base_name = build_single_episode_display_name(job, season, episodes[0])
    else:
        pretty_base_name = build_compilation_display_name(job, season, episodes_range)

    job_output_dir = Path(output_root) / ensure_non_empty_slug(job["title"])
    file_base_name = sanitize_filename(pretty_base_name)
    automatic_label = get_automatic_navigation_label(job)
    display_automatic_label = format_navigation_label(automatic_label)
    if processing_mode == "single_episode":
        automatic_details = " ".join(
            part for part in [f"{int(episodes[0])} Серия", display_automatic_label] if part
        )
        automatic_name = f"{get_display_title(job)} - {automatic_details}"
        previous_automatic_details = " ".join(
            part for part in [automatic_label, f"{int(episodes[0])} Серия"] if part
        )
        previous_automatic_name = f"{get_display_title(job)} - {previous_automatic_details}"
        legacy_name = (
            f"{get_display_title(job)} - {int(job['season'])} Сезон "
            f"{int(episodes[0])} Серия"
        )
    else:
        automatic_details = " ".join(
            part
            for part in [
                format_episodes_label(episodes_range),
                display_automatic_label,
                "[Без OP/ED]",
            ]
            if part
        )
        automatic_name = f"{get_display_title(job)} - {automatic_details}"
        previous_automatic_details = " ".join(
            part for part in [
                automatic_label,
                format_episodes_label(episodes_range),
                "[Без OP/ED]",
            ]
            if part
        )
        previous_automatic_name = f"{get_display_title(job)} - {previous_automatic_details}"
        legacy_name = (
            f"{get_display_title(job)} - {int(job['season'])} Сезон "
            f"{format_episodes_label(episodes_range)} [Без OP/ED]"
        )
    for existing_base_name in dict.fromkeys([
        sanitize_filename(automatic_name),
        sanitize_filename(previous_automatic_name),
        sanitize_filename(legacy_name),
    ]):
        if existing_base_name == file_base_name:
            continue
        existing_paths = [
            job_output_dir / f"{existing_base_name}.mkv",
            job_output_dir / f"{existing_base_name}.txt",
            job_output_dir / f"{existing_base_name}_manifest.json",
        ]
        if any(path.exists() for path in existing_paths):
            file_base_name = existing_base_name
            break
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
    processing_mode = str(
        job.get("processing_mode", "compilation") or "compilation"
    ).strip().lower()
    if (
        processing_mode in {"compilation", "multi_season"}
        and manifest.get("render_pipeline_version") != RENDER_PIPELINE_VERSION
    ):
        return None
    if (
        bool((job.get("timing_detection") or {}).get("enabled", False))
        and (manifest.get("timing_detection") or {}).get("algorithm_version")
        != DETECTOR_RESULT_VERSION
    ):
        return None
    if manifest.get("title") != job.get("title"):
        return None
    if str(manifest.get("season", "")).lstrip("0") != str(job.get("season", "")).lstrip("0"):
        return None
    if manifest.get("episodes_range") != job.get("episodes_range"):
        return None
    if manifest.get("output_video") != output_video.name or manifest.get("output_timestamps") != output_txt.name:
        return None
    expected_support_banner = build_support_banner_render_signature(job)
    actual_support_banner = manifest.get("support_banner")
    if expected_support_banner["enabled"]:
        if actual_support_banner != expected_support_banner:
            return None
    elif isinstance(actual_support_banner, dict) and actual_support_banner.get("enabled"):
        return None
    audio_recovery_enabled = bool(
        (job.get("processing") or {}).get("audio_recovery_enabled", False)
    )
    if not audio_recovery_enabled and any(
        (episode.get("audio_recovery") or {}).get("applied")
        for episode in manifest.get("episodes", [])
        if isinstance(episode, dict)
    ):
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
    output_files=None,
    *,
    render_completed=False,
    job_completed=False,
    preserve_temp_on_failure=False,
    cancellation_requested=False,
):
    if cancellation_requested:
        return
    cleanup = cleanup or {}

    if render_completed and cleanup.get("downloads", True) and download_dir:
        print(f"[CLEANUP] Removing downloads: {download_dir}")
        shutil.rmtree(download_dir, ignore_errors=True)

    if cleanup.get("temp", True) and temp_dir and (render_completed or not preserve_temp_on_failure):
        print(f"[CLEANUP] Removing temp: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)

    if job_completed and cleanup.get("output", False) and job_output_dir:
        print(f"[CLEANUP] Removing output: {job_output_dir}")
        if output_files:
            for path in output_files:
                Path(path).unlink(missing_ok=True)
            try:
                Path(job_output_dir).rmdir()
            except OSError:
                pass
        else:
            shutil.rmtree(job_output_dir, ignore_errors=True)


def cleanup_cancelled_job_artifacts(job):
    workspace_name = build_job_workspace_name(job)
    source = job.get("source") or {}
    if source.get("type") == "magnet":
        download_dir = Path(source.get("download_dir") or f"./downloads/{workspace_name}")
        shutil.rmtree(download_dir, ignore_errors=True)
    shutil.rmtree(TEMP_ROOT / workspace_name, ignore_errors=True)
    if job.get("episodes_range"):
        artifacts = build_output_artifacts(job, Path(job.get("output_dir") or "./output"))
        for key in ("output_video", "output_txt", "output_manifest"):
            artifacts[key].unlink(missing_ok=True)
        try:
            artifacts["job_output_dir"].rmdir()
        except OSError:
            pass
    else:
        shutil.rmtree(Path(job.get("output_dir") or "./output") / workspace_name, ignore_errors=True)


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


def process_multi_season_job(job, runtime_status_path=None):
    processing = normalize_processing_config(job)
    timing_detection = normalize_timing_detection_config(job)
    source_parts = (job.get("source") or {}).get("parts") or []
    season_range = str(processing.get("season_range") or "").strip()
    if not season_range or not source_parts:
        raise RuntimeError("multi_season mode requires season_range and season sources")

    artifacts = build_output_artifacts(job, Path(job["output_dir"]))
    artifacts["job_output_dir"].mkdir(parents=True, exist_ok=True)
    workspace_name = build_job_workspace_name(job)
    temp_dir = prepare_temp_dir(workspace_name)
    staging_output = temp_dir / "seasons"
    download_dir = Path(
        (job.get("source") or {}).get("download_dir")
        or f"./downloads/{workspace_name}"
    )
    cleanup = job.get("cleanup") or {"downloads": True, "temp": True}
    delivery = build_delivery_config(job)
    support_banner = normalize_support_banner_config(
        job,
        privacy_view=delivery["vk_privacy_view"],
    )
    validate_support_banner_asset(support_banner)
    download_cfg = job.get("download") or {}
    download_timeout = int(download_cfg.get("timeout_minutes_maximum", 1440)) * 60
    render_completed = False
    job_completed = False
    cancellation_requested = False

    try:
        checkpoint = load_render_checkpoint(job, artifacts)
        if checkpoint:
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
                total_episodes=int(checkpoint["manifest"].get("episodes_count", 0)),
                runtime_status_path=runtime_status_path,
            )
            job_completed = is_job_completed(result)
            return result

        discovered = []
        set_runtime_stage(runtime_status_path, "torrent_metadata")
        part_counts = {}
        for part in source_parts:
            season = int(part["season"])
            part_counts[season] = part_counts.get(season, 0) + 1
            part_index = part_counts[season]
            part_dir = download_dir / f"season_{season:02d}" / f"part_{part_index:02d}"
            episodes = discover_torrent_episode_numbers(
                part["magnet"],
                part_dir,
                path_filter=part.get("path_filter"),
                timeout=download_timeout,
                allow_missing_episodes=bool(processing.get("allow_missing_episodes", False)),
            )
            print(
                f"[SEASON] {season}: found {len(episodes)} episodes, "
                f"part {part_index}, range {episodes[0]}-{episodes[-1]}"
            )
            discovered.append((season, part_index, part, part_dir, episodes))

        set_runtime_stage(runtime_status_path, "download")
        for _season, _part_index, part, part_dir, episodes in discovered:
            download_selected_episodes(
                part["magnet"],
                part_dir,
                set(episodes),
                path_filter=part.get("path_filter"),
                timeout=download_timeout,
            )

        season_inputs = []
        all_episode_infos = []
        preferred_language = str(job.get("preferred_audio_language", "rus")).strip().lower() or "rus"
        for season, part_index, _part, part_dir, episodes in discovered:
            detected, ignored = find_episode_files(part_dir)
            selected, excluded = filter_episode_files(detected, set(episodes))
            external_audio = find_external_audio_files(part_dir, set(episodes))
            log_episode_selection(selected, ignored + excluded)
            infos = build_episode_infos(
                selected,
                external_audio,
                preferred_language,
                timing_detection["analysis_audio_language"],
            )
            all_episode_infos.extend(infos)
            season_inputs.append((season, part_index, part_dir, episodes))

        target_frame_rate = select_compilation_frame_rate(all_episode_infos)
        target_frame_width, target_frame_height = select_compilation_frame_size(all_episode_infos)
        season_outputs = []
        manifest_episodes = []
        timing_summaries = []
        set_runtime_stage(runtime_status_path, "season_render", total_episodes=len(all_episode_infos))
        episode_offsets = {}
        support_banner_episode_offset = 0
        for season, part_index, part_dir, episodes in season_inputs:
            subjob = deepcopy(job)
            subjob["season"] = season
            subjob["episodes_range"] = (
                f"{episodes[0]:03d}"
                if len(episodes) == 1
                else f"{episodes[0]:03d}-{episodes[-1]:03d}"
            )
            subjob["processing_mode"] = "compilation"
            subjob["source"] = {"type": "local", "input_dir": str(part_dir)}
            subjob["output_dir"] = str(
                staging_output / f"season_{season:02d}_part_{part_index:02d}"
            )
            subjob["processing"] = {
                key: value
                for key, value in processing.items()
                if key not in {"season_range", "target_frame_rate", "target_frame_width", "target_frame_height"}
            }
            subjob["processing"].update({
                "target_frame_rate": target_frame_rate,
                "target_frame_width": target_frame_width,
                "target_frame_height": target_frame_height,
                "_support_banner_episode_offset": support_banner_episode_offset,
            })
            subjob["delivery"] = {
                "s3_enabled": False,
                "vk_enabled": False,
                "vk_privacy_view": delivery["vk_privacy_view"],
            }
            subjob["cleanup"] = {"downloads": False, "temp": True, "output": False}
            result = process_job(subjob, runtime_status_path=runtime_status_path)
            season_output = Path(result["output_video"])
            season_manifest = json.loads(Path(result["output_manifest"]).read_text(encoding="utf-8"))
            season_outputs.append(season_output)
            timing_summaries.append(season_manifest.get("timing_sources_summary") or {})
            episode_offset = episode_offsets.get(season, 0)
            manifest_episodes.extend(renumber_season_part_episodes(
                season_manifest.get("episodes", []),
                season,
                episode_offset,
                source_episode_start=episodes[0],
            ))
            episode_offsets[season] = episode_offset + episodes[-1] - episodes[0] + 1
            support_banner_episode_offset += len(episodes)

        signatures = [ffprobe_media_signature(path) for path in season_outputs]
        if any(signature != signatures[0] for signature in signatures[1:]):
            raise RuntimeError("Rendered seasons have incompatible media signatures")
        concat_file = temp_dir / "seasons.txt"
        durations = [ffprobe_duration(path) for path in season_outputs]
        create_concat_file(season_outputs, concat_file, durations=durations)
        partial_output = artifacts["output_video"].with_name(
            artifacts["output_video"].stem + ".partial" + artifacts["output_video"].suffix
        )
        partial_output.unlink(missing_ok=True)
        set_runtime_stage(runtime_status_path, "concat", total_episodes=len(manifest_episodes))
        try:
            render_concat(concat_file, partial_output, allow_reencode=False)
            final_duration = ffprobe_duration(partial_output)
            if final_duration <= 0 or abs(final_duration - sum(durations)) > 1.0:
                raise RuntimeError("Final multi-season concat duration mismatch")
            if ffprobe_media_signature(partial_output) != signatures[0]:
                raise RuntimeError("Final multi-season concat has incompatible media signature")
            partial_output.replace(artifacts["output_video"])
        except Exception:
            partial_output.unlink(missing_ok=True)
            raise

        timestamps = build_multi_season_timestamps(manifest_episodes)
        quality_summary = build_quality_summary(manifest_episodes, job.get("skip_types", ["op", "ed"]))
        delivery_summary = {
            "s3": build_s3_summary(delivery["s3_enabled"], uploaded=False),
            "vk": build_vk_summary(delivery["vk_enabled"], uploaded=False),
        }
        manifest = {
            "render_pipeline_version": RENDER_PIPELINE_VERSION,
            "title": job["title"],
            "title_ru": job.get("title_ru"),
            "season": str(job["season"]).zfill(2),
            "season_range": season_range,
            "episodes_range": job["episodes_range"],
            "episodes_count": len(manifest_episodes),
            "source": "magnet",
            "display_title": get_display_title(job),
            "output_display_name": artifacts["pretty_base_name"],
            "output_video": artifacts["output_video"].name,
            "output_timestamps": artifacts["output_txt"].name,
            "delivery_summary": delivery_summary,
            "quality_summary": quality_summary,
            "timing_detection": {
                "enabled": timing_detection["enabled"],
                "algorithm_version": DETECTOR_RESULT_VERSION,
                "analysis_audio_language": timing_detection.get(
                    "analysis_audio_language",
                    DEFAULT_TIMING_DETECTION["analysis_audio_language"],
                ),
            },
            "timing_sources_summary": {
                key: any(summary.get(key) for summary in timing_summaries)
                for key in ("anilibria_available", "aniskip_available", "detector_available")
            },
            "episodes": manifest_episodes,
            "timestamps": timestamps,
            "processing": {"mode": "multi_season", "season_range": season_range},
            "support_banner": build_support_banner_render_signature(job, support_banner),
            "render_complete": True,
        }
        write_outputs(artifacts["output_txt"], artifacts["output_manifest"], timestamps, manifest)
        render_completed = True
        result = deliver_rendered_output(
            job,
            delivery,
            output_video=artifacts["output_video"],
            output_txt=artifacts["output_txt"],
            output_manifest=artifacts["output_manifest"],
            manifest=manifest,
            timestamps=timestamps,
            pretty_base_name=artifacts["pretty_base_name"],
            temp_dir=temp_dir,
            total_episodes=len(manifest_episodes),
            runtime_status_path=runtime_status_path,
        )
        job_completed = is_job_completed(result)
        set_runtime_stage(runtime_status_path, "job_done", total_episodes=len(manifest_episodes))
        return result
    except JobCancelled:
        cancellation_requested = True
        raise
    finally:
        cleanup_job_artifacts(
            cleanup,
            download_dir=download_dir,
            temp_dir=temp_dir,
            job_output_dir=artifacts["job_output_dir"],
            output_files=[artifacts["output_video"], artifacts["output_txt"], artifacts["output_manifest"]],
            render_completed=render_completed,
            job_completed=job_completed,
            preserve_temp_on_failure=True,
            cancellation_requested=cancellation_requested,
        )


def process_job(job, runtime_status_path=None):
    title = job["title"]
    mal_id = job.get("mal_id")
    season = str(job["season"]).zfill(2)
    episodes_range = job["episodes_range"]
    processing_mode = str(job.get("processing_mode", "compilation") or "compilation").strip().lower()
    if processing_mode == "multi_season":
        return process_multi_season_job(job, runtime_status_path=runtime_status_path)
    source = job["source"]
    output_root = Path(job["output_dir"])
    watermark_path = Path(job["watermark_path"])
    skip_types = job.get("skip_types", ["op", "ed"])
    encoding = dict(job.get("encoding") or {})
    cleanup = job.get("cleanup") or {"downloads": True, "temp": True}
    processing = normalize_processing_config(job)
    audio_recovery_enabled = bool(processing.get("audio_recovery_enabled", False))
    timing_detection = normalize_timing_detection_config(job)
    delivery = build_delivery_config(job)
    support_banner = normalize_support_banner_config(
        job,
        privacy_view=delivery["vk_privacy_view"],
    )
    validate_support_banner_asset(support_banner)
    timing_providers = job.get("timing_providers") or {}
    anilibria_enabled = timing_providers.get("anilibria_enabled", True)
    aniskip_enabled = timing_providers.get("aniskip_enabled", False)
    preferred_language = str(job.get("preferred_audio_language", "rus")).strip().lower() or "rus"

    title_slug = ensure_non_empty_slug(title)
    workspace_name = build_job_workspace_name(job)
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
        reset_temp_dir(workspace_name)
        if processing_mode == "single_episode"
        else prepare_temp_dir(workspace_name)
    )
    download_dir = None
    render_completed = False
    job_completed = False
    cancellation_requested = False

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
        download_dir, detected_episode_files, ignored_files, external_audio_files = collect_episode_files(
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
        selected_episode_numbers = {episode for episode, _path in episode_files}
        missing_source_episodes = sorted(allowed_episodes - selected_episode_numbers)
        if missing_source_episodes and processing.get("allow_missing_episodes"):
            print(f"[SOURCE WARNING] Missing episodes allowed: {missing_source_episodes}")

        if processing_mode == "single_episode":
            if len(episode_files) != 1:
                raise RuntimeError("single_episode mode requires exactly one selected episode")

            episode_number, episode_path = episode_files[0]
            episode_info = build_episode_infos(
                episode_files,
                external_audio_files,
                preferred_language,
                timing_detection["analysis_audio_language"],
            )[0]
            external_audio = episode_info.get("external_audio")
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
            embedded_audio_streams = detect_audio_streams(episode_path) if not external_audio else []
            episode_audio_index = (
                external_audio["audio_index"] if external_audio
                else get_preferred_audio_stream(Path(episode_path), preferred_language)
                if embedded_audio_streams else None
            )
            expected_duration = episode_info["duration"]
            audio_path = Path(external_audio["path"]) if external_audio else Path(episode_path)
            audio_recovery = build_audio_recovery_info(
                audio_recovery_enabled,
                audio_path,
                episode_audio_index,
                video_path=Path(episode_path) if external_audio else None,
                timeline=(
                    episode_info.get("source_timeline")
                    if not external_audio and episode_audio_index == 0 else None
                ),
            )
            support_banner_episode = build_support_banner_episode_spec(
                support_banner,
                expected_duration,
                single_episode=True,
            )
            render_final(
                concat_output=Path(episode_path),
                watermark_path=watermark_path,
                output_video=output_video,
                encoding={**encoding, "audio_codec": "aac"},
                audio_stream_index=episode_audio_index,
                audio_recovery=audio_recovery["applied"],
                external_audio_path=external_audio["path"] if external_audio else None,
                target_duration=expected_duration,
                support_banner=support_banner_episode,
            )
            validation = validate_episode_render(output_video)
            validate_expected_episode_duration(validation, expected_duration, output_video)
            manifest_episode = manifest["episodes"][0]
            manifest_episode["original_duration"] = expected_duration
            manifest_episode["expected_cleaned_duration"] = expected_duration
            manifest_episode["cleaned_duration"] = validation["duration"]
            manifest_episode["audio_recovery"] = audio_recovery
            manifest_episode["audio"] = {
                "source": "external" if external_audio else "embedded" if embedded_audio_streams else "none",
                "stream_index": (
                    external_audio["stream_index"] if external_audio
                    else embedded_audio_streams[episode_audio_index]["stream_index"]
                    if episode_audio_index is not None else None
                ),
                **({
                    "external_file": Path(external_audio["path"]).name,
                    "start_time": _round_or_none(external_audio["start_time"]),
                    "duration_delta": _round_or_none(external_audio["duration_delta"]),
                } if external_audio else {}),
            }
            manifest_episode["support_banner"] = {
                key: value
                for key, value in support_banner_episode.items()
                if key != "path"
            }
            manifest["source_summary"]["external_audio_episode_count"] = int(bool(external_audio))
            manifest["quality_summary"] = {
                "episodes_count": 1,
                "episodes_audio_recovery": (
                    [episode_number] if audio_recovery["applied"] else []
                ),
            }
            manifest["support_banner"] = build_support_banner_render_signature(
                job,
                support_banner,
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

        episode_infos = build_episode_infos(
            episode_files,
            external_audio_files,
            preferred_language,
            timing_detection["analysis_audio_language"],
        )
        frame_rate = processing.get("target_frame_rate") or select_compilation_frame_rate(episode_infos)
        if processing.get("target_frame_width") and processing.get("target_frame_height"):
            frame_width = int(processing["target_frame_width"])
            frame_height = int(processing["target_frame_height"])
        else:
            frame_width, frame_height = select_compilation_frame_size(episode_infos)
        encoding["frame_rate"] = frame_rate
        encoding["frame_width"] = frame_width
        encoding["frame_height"] = frame_height
        print(f"[EPISODE FPS] target={frame_rate}")
        print(f"[EPISODE FRAME] target={frame_width}x{frame_height}")
        fingerprint = build_episode_fingerprint(
            job,
            episode_infos,
            watermark_path=watermark_path,
            timing_detection=timing_detection,
            preferred_language=preferred_language,
        )
        episode_checkpoint = initialize_episode_checkpoints(temp_dir, fingerprint)
        set_runtime_stage(
            runtime_status_path,
            "episode_scan",
            total_episodes=len(episode_infos),
            total_chunks=None,
        )
        loaded_episodes = [
            load_episode_checkpoint(
                temp_dir,
                episode_info,
                audio_recovery_enabled=audio_recovery_enabled,
            )
            for episode_info in episode_infos
        ]
        render_context = episode_checkpoint.get("render_context")
        render_context_valid = (
            isinstance(render_context, dict)
            and isinstance(render_context.get("detector"), dict)
            and isinstance(render_context.get("timing_sources_summary"), dict)
        )
        needs_processing_context = any(
            episode is None for episode in loaded_episodes
        ) or not render_context_valid
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
            episode_checkpoint["render_context"] = render_context
            _write_json_atomic(temp_dir / "checkpoint.json", episode_checkpoint)

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

        episode_outputs = []
        episode_durations = []
        episode_signatures = []
        manifest_episodes = []

        for episode_index, episode_info in enumerate(episode_infos):
            episode_number = episode_info["episode"]
            episode_result = loaded_episodes[episode_index]
            if episode_result:
                print(f"[EPISODE CHECKPOINT] Reusing episode {episode_number}")
            else:
                episode_dir = temp_dir / f"episode_{episode_number:03d}"
                shutil.rmtree(episode_dir, ignore_errors=True)
                episode_dir.mkdir(parents=True, exist_ok=True)
                rendered_work = episode_dir / "rendered.work.mkv"
                try:
                    set_runtime_stage(
                        runtime_status_path,
                        "render_episode",
                        current_chunk_index=None,
                        total_chunks=None,
                        current_chunk_episode_range=None,
                        current_episode=episode_number,
                        total_episodes=len(episode_infos),
                        current_episode_file=Path(episode_info["path"]).name,
                    )
                    render_plan = build_episode_render_plan(
                        episode_info,
                        skip_types=skip_types,
                        detector_context=detector_context,
                        anilibria_result=prefetched_anilibria_results[episode_number],
                        aniskip_result=prefetched_aniskip_results[episode_number],
                        preferred_language=preferred_language,
                        audio_recovery_enabled=audio_recovery_enabled,
                    )
                    cleaned_duration = sum(
                        float(end) - float(start)
                        for start, end in render_plan["keep_segments"]
                    )
                    support_banner_episode = build_support_banner_episode_spec(
                        support_banner,
                        cleaned_duration,
                        episode_ordinal=(
                            support_banner["episode_ordinal_offset"]
                            + episode_index
                            + 1
                        ),
                    )
                    render_plan["manifest_episode"]["support_banner"] = {
                        key: value
                        for key, value in support_banner_episode.items()
                        if key != "path"
                    }
                    render_episode(
                        episode_info["path"],
                        rendered_work,
                        render_plan["keep_segments"],
                        watermark_path,
                        {**encoding, "audio_codec": "aac"},
                        audio_stream_index=render_plan["audio_stream_index"],
                        audio_recovery=bool(
                            (render_plan.get("audio_recovery") or {}).get("applied")
                        ),
                        external_audio_path=render_plan.get("external_audio_path"),
                        support_banner=support_banner_episode,
                    )
                    episode_result = save_episode_checkpoint(
                        episode_dir,
                        episode_info,
                        rendered_work,
                        render_plan["manifest_episode"],
                    )
                except Exception:
                    rendered_work.unlink(missing_ok=True)
                    raise

            manifest_episode = episode_result["manifest_episode"]
            expected_duration = float(
                manifest_episode.get("expected_cleaned_duration", episode_result["duration"])
            )
            actual_duration = float(episode_result["duration"])
            print(
                f"[EPISODE TIMELINE] episode={episode_number} "
                f"expected={expected_duration:.3f}s actual={actual_duration:.3f}s "
                f"drift={actual_duration - expected_duration:+.3f}s"
            )
            episode_outputs.append(episode_result["output"])
            episode_durations.append(actual_duration)
            episode_signatures.append(episode_result["media_signature"])
            manifest_episodes.append(manifest_episode)

        if any(signature != episode_signatures[0] for signature in episode_signatures[1:]):
            details = describe_media_signature_groups(episode_infos, episode_signatures)
            raise RuntimeError(
                f"Rendered episodes have incompatible media signatures: {details}"
            )

        set_runtime_stage(
            runtime_status_path,
            "concat",
            total_episodes=len(episode_infos),
            current_chunk_index=None,
            total_chunks=None,
            current_chunk_episode_range=None,
        )
        create_concat_file(episode_outputs, concat_file, durations=episode_durations)
        partial_output = output_video.with_name(output_video.stem + ".partial" + output_video.suffix)
        partial_output.unlink(missing_ok=True)
        try:
            render_concat(concat_file, partial_output, allow_reencode=False)
            final_duration = ffprobe_duration(partial_output)
            expected_final_duration = sum(episode_durations)
            final_drift = final_duration - expected_final_duration
            print(
                f"[FINAL TIMELINE] expected={expected_final_duration:.3f}s "
                f"actual={final_duration:.3f}s drift={final_drift:+.3f}s"
            )
            if final_duration <= 0:
                raise RuntimeError("Final concat has zero duration")
            if abs(final_drift) > 1.0:
                raise RuntimeError(
                    f"Final concat duration mismatch: {final_duration:.3f}s vs "
                    f"{expected_final_duration:.3f}s"
                )
            if ffprobe_media_signature(partial_output) != episode_signatures[0]:
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
        if missing_source_episodes:
            quality_summary["missing_source_episodes"] = missing_source_episodes
            quality_summary["episodes_with_warnings"] = sorted(set(
                (quality_summary.get("episodes_with_warnings") or [])
                + missing_source_episodes
            ))
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
                "episode_checkpoints": True,
            },
            timing_sources_summary=timing_sources_summary,
            missing_source_episodes=missing_source_episodes,
        )
        manifest["timestamps"] = timestamps
        manifest["support_banner"] = build_support_banner_render_signature(
            job,
            support_banner,
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
    except JobCancelled:
        cancellation_requested = True
        raise
    finally:
        cleanup_job_artifacts(
            cleanup,
            download_dir=download_dir,
            temp_dir=temp_dir,
            job_output_dir=job_output_dir,
            output_files=[
                artifacts["output_video"],
                artifacts["output_txt"],
                artifacts["output_manifest"],
            ],
            render_completed=render_completed,
            job_completed=job_completed,
            preserve_temp_on_failure=processing_mode != "single_episode",
            cancellation_requested=cancellation_requested,
        )
