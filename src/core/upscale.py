import hashlib
import json
import os
import shutil
import subprocess
from collections import Counter
from fractions import Fraction
from pathlib import Path

from api.vk import publish_video_to_vk
from core.discovery import filter_episode_files, find_episode_files
from core.torrent import download_selected_episodes
from shared.helpers import (
    ensure_non_empty_slug,
    get_display_title,
    parse_episodes_range,
    raise_if_cancelled,
    run,
    sanitize_filename,
)
from shared.runtime import update_runtime_status


DEFAULT_VIDEO2X_PATH = "~/tools/video2x/6.4.0/Video2X-x86_64.AppImage"


def _update_status(status_path, **changes):
    raise_if_cancelled()
    if status_path:
        update_runtime_status(status_path, **changes)


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _source_fingerprint(path):
    path = Path(path)
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _job_fingerprint(job):
    source = job.get("source") or {}
    payload = {
        "title": job.get("title"),
        "season": int(job.get("season", 1)),
        "episodes_range": job.get("episodes_range"),
        "magnet": source.get("magnet"),
        "processor": "realesrgan",
        "model": "realesr-animevideov3",
        "scale": 2,
        "codec": "h264_nvenc",
        "preset": "fast",
        "cq": 23,
        "source_path_contains": (job.get("processing") or {}).get("source_path_contains"),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _probe(path):
    output = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
        "-of", "json",
        str(path),
    ], encoding="utf-8", errors="replace")
    data = json.loads(output)
    streams = data.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError(f"Video stream not found: {path}")
    return {
        "duration": float((data.get("format") or {}).get("duration") or 0),
        "video": video,
        "audio_codecs": [stream.get("codec_name") for stream in streams if stream.get("codec_type") == "audio"],
        "subtitle_codecs": [stream.get("codec_name") for stream in streams if stream.get("codec_type") == "subtitle"],
    }


def validate_upscale_source(source_path):
    source = _probe(source_path)
    if (source["video"].get("width"), source["video"].get("height")) != (1920, 1080):
        raise RuntimeError(f"4K upscale requires 1920x1080 source: {source_path}")
    if source["duration"] <= 0:
        raise RuntimeError(f"Source duration is invalid: {source_path}")
    return source


def validate_upscale_output(source_path, output_path):
    source = validate_upscale_source(source_path)
    output = _probe(output_path)
    if (output["video"].get("width"), output["video"].get("height")) != (3840, 2160):
        raise RuntimeError(f"Video2X output is not 3840x2160: {output_path}")
    if output["video"].get("codec_name") != "h264":
        raise RuntimeError("Video2X output codec is not H.264")
    if output["video"].get("avg_frame_rate") != source["video"].get("avg_frame_rate"):
        raise RuntimeError("Video2X changed frame rate")
    if output["audio_codecs"] != source["audio_codecs"] or output["subtitle_codecs"] != source["subtitle_codecs"]:
        raise RuntimeError("Video2X did not preserve audio/subtitle streams")
    try:
        frame_rate = Fraction(source["video"].get("avg_frame_rate") or "0/1")
        tolerance = (1 / float(frame_rate)) + 0.05 if frame_rate else 0.1
    except (ValueError, ZeroDivisionError):
        tolerance = 0.1
    if output["duration"] <= 0 or abs(output["duration"] - source["duration"]) > tolerance:
        raise RuntimeError("Video2X output duration does not match source")
    return output


def build_video2x_command(config, source_path, output_path):
    upscale = config.get("upscale") or {}
    executable = Path(upscale.get("video2x_path", DEFAULT_VIDEO2X_PATH)).expanduser()
    if not executable.is_file():
        raise RuntimeError(f"Video2X not found: {executable}")
    command = [str(executable)]
    if upscale.get("appimage_extract_and_run", True):
        command.append("--appimage-extract-and-run")
    command.extend([
        "-i", str(source_path),
        "-o", str(output_path),
        "-p", "realesrgan",
        "-s", "2",
        "--realesrgan-model", "realesr-animevideov3",
        "-c", "h264_nvenc",
        "--pix-fmt", "yuv420p",
        "-e", "preset=fast",
        "-e", "cq=23",
    ])
    return command


def validate_upscale_environment(config):
    missing_env = [
        name for name in ("VK_ACCESS_TOKEN", "VK_API_VERSION", "VK_PUBLIC_GROUP_ID", "VK_DONUT_LEVEL_ID")
        if not os.getenv(name)
    ]
    if missing_env:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing_env))
    missing_tools = [name for name in ("aria2c", "ffprobe") if shutil.which(name) is None]
    if missing_tools:
        raise RuntimeError("Missing required tools: " + ", ".join(missing_tools))
    build_video2x_command(config, "input.mkv", "output.mkv")


def run_video2x(config, source_path, output_path):
    run(build_video2x_command(config, source_path, output_path))


def cleanup_cancelled_upscale_job(job):
    title_slug = ensure_non_empty_slug(job["title"])
    source = job.get("source") or {}
    shutil.rmtree(Path(source.get("download_dir") or f"./upscale_downloads/{title_slug}"), ignore_errors=True)
    shutil.rmtree(Path(job.get("output_dir") or "./upscale_output") / title_slug, ignore_errors=True)


def _build_episode_title(job, episode):
    return (
        f"{get_display_title(job)} - {int(job.get('season', 1))} Сезон "
        f"{int(episode)} Серия [4K]"
    )


def _load_manifest(job, manifest_path):
    fingerprint = _job_fingerprint(job)
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("fingerprint") != fingerprint:
        manifest = {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "title": job.get("title"),
            "season": int(job.get("season", 1)),
            "episodes_range": job.get("episodes_range"),
            "episodes": {},
        }
        _atomic_write_json(manifest_path, manifest)
    manifest.setdefault("episodes", {})
    return manifest


def _valid_render_checkpoint(source_path, output_path, episode_state):
    if not episode_state.get("render_complete") or episode_state.get("source") != _source_fingerprint(source_path):
        return False
    if not Path(output_path).is_file():
        return False
    try:
        validate_upscale_output(source_path, output_path)
    except (RuntimeError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    return True


def process_upscale_job(config, job, runtime_status_path=None, on_episode_success=None):
    if job.get("processing_mode") != "upscale_4k":
        raise RuntimeError("Not an upscale_4k job")
    source = job.get("source") or {}
    if source.get("type") != "magnet" or not source.get("magnet"):
        raise RuntimeError("4K job requires a magnet source")
    validate_upscale_environment(config)

    title_slug = ensure_non_empty_slug(job["title"])
    download_dir = Path(source.get("download_dir") or f"./upscale_downloads/{title_slug}")
    output_root = Path(job.get("output_dir") or "./upscale_output")
    output_dir = output_root / title_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "upscale_manifest.json"
    manifest = _load_manifest(job, manifest_path)
    requested_episodes = parse_episodes_range(job["episodes_range"])

    _update_status(runtime_status_path, current_stage="upscale_download", current_job={"stage": "upscale_download"})
    download_selected_episodes(
        source["magnet"],
        download_dir,
        requested_episodes,
        path_filter=(job.get("processing") or {}).get("source_path_contains"),
    )
    detected, _ = find_episode_files(download_dir)
    episode_files, _ = filter_episode_files(detected, requested_episodes)
    found_episodes = [episode for episode, _ in episode_files]
    duplicates = sorted(episode for episode, count in Counter(found_episodes).items() if count > 1)
    missing = sorted(requested_episodes - set(found_episodes))
    if duplicates:
        raise RuntimeError(f"Multiple source files found for episodes: {duplicates}")
    if missing:
        raise RuntimeError(f"Source episodes not found: {missing}")
    total = len(episode_files)

    for episode, source_path in episode_files:
        episode_key = str(int(episode))
        episode_state = manifest["episodes"].get(episode_key) or {}
        if (episode_state.get("delivery") or {}).get("video_uploaded"):
            continue

        pretty_name = _build_episode_title(job, episode)
        output_path = output_dir / f"{sanitize_filename(pretty_name)}.mkv"
        work_path = output_path.with_suffix(".work.mkv")
        _update_status(
            runtime_status_path,
            current_stage="upscale_render",
            current_job={
                "stage": "upscale_render",
                "current_episode": int(episode),
                "total_episodes": total,
                "current_episode_file": Path(source_path).name,
            },
        )

        validate_upscale_source(source_path)

        if not _valid_render_checkpoint(source_path, output_path, episode_state):
            work_path.unlink(missing_ok=True)
            run_video2x(config, source_path, work_path)
            media = validate_upscale_output(source_path, work_path)
            os.replace(work_path, output_path)
            episode_state = {
                "episode": int(episode),
                "source": _source_fingerprint(source_path),
                "output": str(output_path),
                "render_complete": True,
                "media": media,
                "delivery": {},
            }
            manifest["episodes"][episode_key] = episode_state
            _atomic_write_json(manifest_path, manifest)

        _update_status(
            runtime_status_path,
            current_stage="upscale_delivery_vk",
            current_job={"stage": "upscale_delivery_vk", "current_episode": int(episode)},
        )
        vk_result = publish_video_to_vk(
            output_path,
            pretty_name,
            pretty_name,
            wall_post_text=pretty_name,
            privacy_view=5,
        )
        episode_state["delivery"] = vk_result
        manifest["episodes"][episode_key] = episode_state
        _atomic_write_json(manifest_path, manifest)
        if not vk_result.get("video_uploaded"):
            raise RuntimeError(vk_result.get("error") or "VK video upload failed")
        if (job.get("cleanup") or {}).get("output", True):
            output_path.unlink(missing_ok=True)
        if on_episode_success:
            on_episode_success(job, int(episode), vk_result)

    completed = all(
        (manifest["episodes"].get(str(int(episode))) or {}).get("delivery", {}).get("video_uploaded")
        for episode, _ in episode_files
    )
    if completed and (job.get("cleanup") or {}).get("downloads", True):
        shutil.rmtree(download_dir, ignore_errors=True)
    return {
        "completed": completed,
        "output_display_name": get_display_title(job),
        "output_video": None,
        "output_manifest": str(manifest_path),
        "delivery_summary": {"vk": {"enabled": True, "uploaded": completed, "video_uploaded": completed}},
    }
