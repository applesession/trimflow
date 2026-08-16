import json
import re
import subprocess
from pathlib import Path

from shared.helpers import run


def detect_audio_streams(video_path):
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index:stream_tags=language,title,handler_name:stream_disposition=default",
            "-of", "json",
            str(video_path),
        ]
        result = subprocess.check_output(cmd, encoding="utf-8", errors="replace")
        data = json.loads(result)
        streams = []
        for audio_index, stream in enumerate(data.get("streams", [])):
            index = stream.get("index", 0)
            tags = stream.get("tags", {}) or {}
            disposition = stream.get("disposition", {}) or {}
            streams.append({
                "audio_index": audio_index,
                "stream_index": index,
                "language": tags.get("language"),
                "title": tags.get("title"),
                "handler_name": tags.get("handler_name"),
                "is_default": bool(disposition.get("default", 0)),
            })
        return streams
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, json.JSONDecodeError):
        return []


def _build_language_variants(preferred_language):
    normalized = str(preferred_language or "").strip().lower()
    alias_map = {
        "rus": {"rus", "ru", "russian", "russkiy", "рус", "русский", "дубляж", "озвучка"},
        "ru": {"rus", "ru", "russian", "russkiy", "рус", "русский", "дубляж", "озвучка"},
        "russian": {"rus", "ru", "russian", "russkiy", "рус", "русский", "дубляж", "озвучка"},
        "jpn": {"jpn", "jp", "japanese", "nihongo", "япон", "японский"},
        "jp": {"jpn", "jp", "japanese", "nihongo", "япон", "японский"},
        "japanese": {"jpn", "jp", "japanese", "nihongo", "япон", "японский"},
    }
    if normalized in alias_map:
        return alias_map[normalized]
    return {normalized} if normalized else set()


def get_preferred_audio_stream(video_path, preferred_language="rus"):
    streams = detect_audio_streams(video_path)
    if not streams:
        return 0

    preferred_variants = _build_language_variants(preferred_language)
    best_match = None

    for stream in streams:
        score = 0
        language = str(stream.get("language") or "").strip().lower()
        title = str(stream.get("title") or "").strip().lower()
        handler_name = str(stream.get("handler_name") or "").strip().lower()
        searchable_text = " ".join(part for part in [language, title, handler_name] if part)

        if language in preferred_variants:
            score += 100
        elif any(variant and variant in searchable_text for variant in preferred_variants):
            score += 60

        if stream.get("is_default"):
            score += 5

        candidate = (score, -stream["audio_index"], stream["audio_index"])
        if best_match is None or candidate > best_match:
            best_match = candidate

    if best_match and best_match[0] > 0:
        return best_match[2]
    return streams[0]["audio_index"]


def _external_language_score(path, stream, preferred_language):
    variants = _build_language_variants(preferred_language)
    language = str(stream.get("language") or "").strip().casefold()
    if language in variants:
        return 100
    searchable = " ".join([
        str(path),
        language,
        str(stream.get("title") or ""),
        str(stream.get("handler_name") or ""),
    ]).casefold()
    tokens = set(re.findall(r"[\w]+", searchable, flags=re.UNICODE))
    return 60 if variants.intersection(tokens) else 0


def ffprobe_audio_timeline(path, audio_stream_index=0):
    try:
        result = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-select_streams", f"a:{audio_stream_index}",
            "-show_entries", "stream=start_time,duration:format=duration",
            "-of", "json",
            str(path),
        ], encoding="utf-8", errors="replace")
        data = json.loads(result)
        stream = (data.get("streams") or [{}])[0]
        duration = stream.get("duration") or (data.get("format") or {}).get("duration")
        if duration is None:
            raise RuntimeError("duration is missing")
        return {
            "start_time": float(stream.get("start_time") or 0.0),
            "duration": float(duration),
        }
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to probe external audio timeline: {path}") from exc


def select_external_audio(candidates, video_duration, preferred_language="rus"):
    streams = []
    for path in candidates:
        path = Path(path)
        detected = detect_audio_streams(path)
        if not detected:
            raise RuntimeError(f"External audio file has no audio streams: {path}")
        for stream in detected:
            streams.append({
                **stream,
                "path": path,
                "language_score": _external_language_score(path, stream, preferred_language),
            })

    if not streams:
        return None
    if len(streams) == 1:
        selected = streams[0]
    else:
        best_score = max(stream["language_score"] for stream in streams)
        matches = [stream for stream in streams if stream["language_score"] == best_score]
        if best_score <= 0 or len(matches) != 1:
            details = "; ".join(
                f"{stream['path']}#a:{stream['audio_index']}" for stream in streams
            )
            raise RuntimeError(
                f"Ambiguous external audio for preferred language {preferred_language!r}: {details}"
            )
        selected = matches[0]

    timeline = ffprobe_audio_timeline(selected["path"], selected["audio_index"])
    start_offset = timeline["start_time"]
    duration_delta = timeline["duration"] - float(video_duration)
    if abs(start_offset) > 0.1 or abs(duration_delta) > 1.0:
        raise RuntimeError(
            f"External audio is not synchronized with video: {selected['path']} "
            f"(start={start_offset:+.3f}s, duration_delta={duration_delta:+.3f}s)"
        )
    return {
        "path": str(selected["path"]),
        "audio_index": selected["audio_index"],
        "stream_index": selected["stream_index"],
        "language": selected.get("language"),
        "start_time": start_offset,
        "duration": timeline["duration"],
        "duration_delta": duration_delta,
    }


def ffprobe_duration(path):
    result = subprocess.check_output([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ])
    return float(result.decode().strip())


def ffprobe_media_signature(path):
    try:
        result = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,"
            "time_base,sample_fmt,sample_rate,channels,channel_layout",
            "-of", "json",
            str(path),
        ], encoding="utf-8", errors="replace")
        streams = json.loads(result).get("streams", [])
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        return None
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    return {
        "video": {
            key: video.get(key)
            for key in [
                "codec_name",
                "width",
                "height",
                "pix_fmt",
                "r_frame_rate",
                "time_base",
            ]
        },
        "audio": {
            key: audio.get(key)
            for key in [
                "codec_name",
                "sample_fmt",
                "sample_rate",
                "channels",
                "channel_layout",
                "time_base",
            ]
        } if audio else None,
    }


def ffprobe_episode_timeline(path, audio_stream_index=0):
    result = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=index,codec_type:packet=stream_index,pts_time,duration_time",
        "-of", "json",
        str(path),
    ], encoding="utf-8", errors="replace")
    data = json.loads(result)
    stream_types = {
        int(stream["index"]): stream.get("codec_type")
        for stream in data.get("streams", [])
        if stream.get("codec_type") in {"video", "audio"}
    }
    packet_times = {stream_index: [] for stream_index in stream_types}
    for packet in data.get("packets", []):
        stream_index = int(packet.get("stream_index", -1))
        if stream_index not in packet_times or packet.get("pts_time") is None:
            continue
        start = float(packet["pts_time"])
        duration = max(0.0, float(packet.get("duration_time") or 0.0))
        packet_times[stream_index].append((start, start + duration))

    timeline = {}
    for stream_type in ("video", "audio"):
        matching_streams = [
            index for index, kind in stream_types.items() if kind == stream_type
        ]
        selected_position = audio_stream_index if stream_type == "audio" else 0
        stream_index = (
            matching_streams[selected_position]
            if 0 <= selected_position < len(matching_streams)
            else None
        )
        if stream_index is None:
            timeline[stream_type] = None
            continue
        packets = sorted(packet_times[stream_index])
        if not packets:
            raise RuntimeError(f"Episode checkpoint has no {stream_type} packets: {path}")
        positive_gaps = [
            max(0.0, current[0] - previous[1])
            for previous, current in zip(packets, packets[1:])
        ]
        max_gap = max(positive_gaps, default=0.0)
        timeline[stream_type] = {
            "start": packets[0][0],
            "duration": max(end for _, end in packets) - packets[0][0],
            "max_packet_gap": max_gap,
            "total_packet_gap": sum(gap for gap in positive_gaps if gap > 0.1),
        }
    return timeline


def analyze_audio_recovery(path, audio_stream_index=0, timeline=None):
    timeline = timeline or ffprobe_episode_timeline(
        path,
        audio_stream_index=audio_stream_index,
    )
    return _analyze_audio_recovery_timeline(timeline, path)


def analyze_external_audio_recovery(video_path, audio_path, audio_stream_index=0):
    video_timeline = ffprobe_episode_timeline(video_path).get("video")
    audio_timeline = ffprobe_episode_timeline(
        audio_path,
        audio_stream_index=audio_stream_index,
    ).get("audio")
    return _analyze_audio_recovery_timeline(
        {"video": video_timeline, "audio": audio_timeline},
        audio_path,
    )


def _analyze_audio_recovery_timeline(timeline, path):
    video = timeline.get("video")
    audio = timeline.get("audio")
    if video is None or audio is None:
        return {
            "enabled": True,
            "applied": False,
            "reasons": [],
            "source_audio_max_packet_gap": 0.0,
            "source_audio_total_packet_gap": 0.0,
            "source_audio_start_delay": 0.0,
            "source_audio_end_shortfall": 0.0,
            "inserted_silence_seconds": 0.0,
        }

    video_end = video["start"] + video["duration"]
    audio_end = audio["start"] + audio["duration"]
    max_gap = max(0.0, float(audio.get("max_packet_gap", 0.0)))
    total_gap = max(0.0, float(audio.get("total_packet_gap", max_gap)))
    start_delay = max(0.0, float(audio["start"]) - float(video["start"]))
    end_shortfall = max(0.0, video_end - audio_end)

    reasons = []
    if max_gap > 0.5:
        reasons.append("packet_gap")
    if start_delay > 0.1:
        reasons.append("late_start")
    if end_shortfall > 0.25:
        reasons.append("early_end")

    inserted_silence = total_gap + start_delay + end_shortfall
    if max_gap > 2.0:
        raise RuntimeError(
            f"Audio recovery packet gap {max_gap:.3f}s exceeds 2.000s: {path}"
        )
    if end_shortfall > 15.0:
        raise RuntimeError(
            f"Audio recovery end shortfall {end_shortfall:.3f}s exceeds 15.000s: {path}"
        )
    if inserted_silence > 15.0:
        raise RuntimeError(
            f"Audio recovery total silence {inserted_silence:.3f}s exceeds 15.000s: {path}"
        )

    return {
        "enabled": True,
        "applied": bool(reasons),
        "reasons": reasons,
        "source_audio_max_packet_gap": round(max_gap, 3),
        "source_audio_total_packet_gap": round(total_gap, 3),
        "source_audio_start_delay": round(start_delay, 3),
        "source_audio_end_shortfall": round(end_shortfall, 3),
        "inserted_silence_seconds": round(inserted_silence, 3),
    }


def validate_episode_render(path):
    duration = ffprobe_duration(path)
    signature = ffprobe_media_signature(path)
    timeline = ffprobe_episode_timeline(path)
    if duration <= 0 or not signature or timeline["video"] is None:
        raise RuntimeError(f"Episode checkpoint failed media validation: {path}")

    for stream_type in ("video", "audio"):
        stream = timeline[stream_type]
        if stream is None:
            continue
        if abs(stream["start"]) > 0.1:
            raise RuntimeError(
                f"Episode checkpoint {stream_type} starts at {stream['start']:.3f}s: {path}"
            )
        if stream["max_packet_gap"] > 0.5:
            raise RuntimeError(
                f"Episode checkpoint {stream_type} packet gap "
                f"{stream['max_packet_gap']:.3f}s: {path}"
            )

    if timeline["audio"] is not None:
        difference = abs(timeline["video"]["duration"] - timeline["audio"]["duration"])
        if difference > 0.25:
            raise RuntimeError(f"Episode checkpoint A/V duration mismatch {difference:.3f}s: {path}")

    return {
        "duration": duration,
        "media_signature": signature,
        "timeline": timeline,
    }


def merge_remove_segments(remove_segments):
    if not remove_segments:
        return []

    segments = sorted(
        [(segment["start"], segment["end"]) for segment in remove_segments],
        key=lambda item: item[0],
    )

    merged = [segments[0]]

    for start, end in segments[1:]:
        last_start, last_end = merged[-1]

        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def build_keep_segments(duration, remove_segments):
    merged_remove = merge_remove_segments(remove_segments)

    if not merged_remove:
        return [(0.0, duration)]

    keep = []
    current = 0.0

    for start, end in merged_remove:
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))

        if start > current:
            keep.append((current, start))

        current = max(current, end)

    if current < duration:
        keep.append((current, duration))

    return keep


def build_hybrid_subsegments(keep_segment, remove_segments, boundary_window):
    keep_start, keep_end = keep_segment
    keep_duration = keep_end - keep_start
    if keep_duration <= 0:
        return []

    left_adjacent = any(abs(segment["end"] - keep_start) < 0.001 for segment in remove_segments)
    right_adjacent = any(abs(segment["start"] - keep_end) < 0.001 for segment in remove_segments)

    if not left_adjacent and not right_adjacent:
        return [{
            "start": keep_start,
            "end": keep_end,
            "cut_mode": "copy",
        }]

    if keep_duration <= boundary_window * 2:
        return [{
            "start": keep_start,
            "end": keep_end,
            "cut_mode": "precise",
        }]

    segments = []
    current_start = keep_start

    if left_adjacent:
        left_end = min(keep_end, keep_start + boundary_window)
        segments.append({
            "start": keep_start,
            "end": left_end,
            "cut_mode": "precise",
        })
        current_start = left_end

    middle_end = keep_end
    if right_adjacent:
        middle_end = max(current_start, keep_end - boundary_window)

    if middle_end > current_start:
        segments.append({
            "start": current_start,
            "end": middle_end,
            "cut_mode": "copy",
        })

    if right_adjacent:
        right_start = max(keep_start, keep_end - boundary_window)
        if right_start < keep_end:
            if segments and segments[-1]["cut_mode"] == "copy" and segments[-1]["end"] > right_start:
                segments[-1]["end"] = right_start
            segments.append({
                "start": right_start,
                "end": keep_end,
                "cut_mode": "precise",
            })

    return [segment for segment in segments if segment["end"] - segment["start"] > 0]


def cap_subsegment_durations(subsegments, max_duration_seconds):
    if max_duration_seconds is None:
        return list(subsegments)

    max_duration_seconds = float(max_duration_seconds)
    if max_duration_seconds <= 0:
        raise RuntimeError("segment_max_render_seconds must be greater than 0")

    capped = []
    for subsegment in subsegments:
        start = float(subsegment["start"])
        end = float(subsegment["end"])
        cut_mode = subsegment["cut_mode"]

        if end <= start:
            continue

        current_start = start
        while current_start < end:
            current_end = min(end, current_start + max_duration_seconds)
            capped.append({
                "start": current_start,
                "end": current_end,
                "cut_mode": cut_mode,
            })
            current_start = current_end

    return capped


def get_keyframes(video_path):
    """Get sorted list of keyframe PTS timestamps from a video file.

    Uses ffprobe to detect all keyframes (I-frames). Empty list on error.
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time",
            "-of", "csv=p=0",
            "-skip_frame", "nokey",
            str(video_path),
        ]
        result = subprocess.check_output(cmd, encoding="utf-8", errors="replace").strip()
        if not result:
            return []
        times = []
        for line in result.split("\n"):
            line = line.strip()
            if line:
                try:
                    times.append(float(line))
                except ValueError:
                    pass
        return times
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []


def snap_remove_segments_to_keyframes(remove_segments, keyframes):
    """Snap remove segment boundaries to nearest keyframes.

    For each remove segment [s, e]:
    - start rounds DOWN to nearest keyframe at or before s
    - end rounds UP   to nearest keyframe at or after e

    This ensures keep_segments between removes start and end on keyframes,
    eliminating duplicates in -c copy mode at the cost of ~1 keyframe
    interval of imprecision at each OP/ED boundary.
    """
    if not keyframes or not remove_segments:
        return list(remove_segments)

    keyframes = sorted(set(keyframes))

    def kf_before(time):
        result = keyframes[0]
        for kf in keyframes:
            if kf <= time + 0.001:
                result = kf
            else:
                break
        return result

    def kf_after(time):
        for kf in keyframes:
            if kf >= time - 0.001:
                return kf
        return keyframes[-1]

    snapped = []
    for seg in remove_segments:
        snapped_start = kf_before(seg["start"])
        snapped_end = kf_after(seg["end"])
        if snapped_end > snapped_start:
            snapped.append({
                "start": snapped_start,
                "end": snapped_end,
            })

    return snapped


def render_segment_copy(ep_file, segment_output, start, end, segment_encoding=None, audio_stream_index=0):
    encoding = segment_encoding or {}
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", ep_file,
        "-map", "0:v:0",
        "-map", f"0:a:{audio_stream_index}?",
        "-map", "0:s?",
        "-c:v", "copy",
        "-c:a", encoding.get("audio_codec", "aac"),
        "-b:a", str(encoding.get("audio_bitrate", "192k")),
        "-ar", str(encoding.get("audio_sample_rate", 48000)),
        "-ac", str(encoding.get("audio_channels", 2)),
        "-c:s", "copy",
        segment_output,
    ]

    run(cmd)


def _build_segment_precise_cmd(ep_file, segment_output, start, end, segment_encoding=None, audio_stream_index=0):
    encoding = segment_encoding or {}
    video_codec = encoding.get("video_codec", "libx264")
    preset = encoding.get("preset", "medium")
    cq = str(encoding.get("cq", 18 if "nvenc" in video_codec else 15))
    audio_codec = encoding.get("audio_codec", "aac")
    audio_bitrate = encoding.get("audio_bitrate", "192k")
    audio_sample_rate = str(encoding.get("audio_sample_rate", 48000))
    audio_channels = str(encoding.get("audio_channels", 2))
    pixel_format = encoding.get("pixel_format")

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", ep_file,
        "-map", "0:v:0",
        "-map", f"0:a:{audio_stream_index}?",
        "-map", "0:s?",
        "-c:v", video_codec,
    ]

    if "nvenc" in video_codec:
        cmd += ["-preset", preset, "-cq", cq]
    elif video_codec in ["libx264", "libx265"]:
        cmd += ["-preset", preset, "-crf", cq]

    if pixel_format:
        cmd += ["-pix_fmt", pixel_format]

    cmd += [
        "-c:a", audio_codec,
        "-b:a", audio_bitrate,
        "-ar", audio_sample_rate,
        "-ac", audio_channels,
        "-c:s", "copy",
        segment_output,
    ]

    return cmd


def render_segment_precise(ep_file, segment_output, start, end, segment_encoding=None, audio_stream_index=0):
    duration = max(0.0, end - start)
    if duration <= 0:
        raise RuntimeError(f"Invalid precise segment duration for {ep_file}: {start} -> {end}")

    encoding = segment_encoding or {}
    cmd = _build_segment_precise_cmd(ep_file, segment_output, start, end, encoding, audio_stream_index)
    video_codec = encoding.get("video_codec", "libx264")

    try:
        run(cmd)
        return
    except RuntimeError as exc:
        is_nvenc = "nvenc" in str(video_codec).lower()
        called_process = exc.__cause__
        exit_code = called_process.returncode if isinstance(called_process, subprocess.CalledProcessError) else None
        if is_nvenc and exit_code in NVENC_FALLBACK_CODES:
            pass
        else:
            raise

    fallback_codec = get_nvenc_fallback_codec(video_codec)
    print(f"[SEGMENT_PRECISE] NVENC failed (code {exit_code}), falling back to {fallback_codec}")
    fallback_encoding = dict(encoding)
    fallback_encoding["video_codec"] = fallback_codec
    fallback_encoding["preset"] = "ultrafast"
    fallback_cmd = _build_segment_precise_cmd(
        ep_file, segment_output, start, end,
        fallback_encoding, audio_stream_index,
    )
    run(fallback_cmd)


def render_segment(ep_file, segment_output, start, end, segment_encoding=None, audio_stream_index=0):
    duration = max(0.0, end - start)
    if duration <= 0:
        raise RuntimeError(f"Invalid segment duration for {ep_file}: {start} -> {end}")

    cut_mode = (segment_encoding or {}).get("cut_mode", "hybrid")
    if cut_mode == "precise":
        render_segment_precise(ep_file, segment_output, start, end, segment_encoding=segment_encoding, audio_stream_index=audio_stream_index)
        return

    render_segment_copy(
        ep_file,
        segment_output,
        start,
        end,
        segment_encoding=segment_encoding,
        audio_stream_index=audio_stream_index,
    )


def render_concat(concat_file, concat_output, audio_stream_index=0, allow_reencode=True):
    fast_path = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-map", "0",
        "-c", "copy",
        concat_output,
    ]

    try:
        run(fast_path)
        return
    except Exception as exc:
        if not allow_reencode:
            raise
        print(f"[CONCAT] fast path failed ({exc}), falling back to re-encode")

    safe_path = [
        "ffmpeg",
        "-y",
        "-fflags", "+genpts",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-map", "0:v?",
        "-map", f"0:a:{audio_stream_index}?",
        "-map", "0:s?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "18",
        "-c:a", "aac",
        "-c:s", "copy",
        concat_output,
    ]
    run(safe_path)


def _probe_video_streams(path):
    try:
        result = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type,duration",
            "-of", "csv=p=0",
            str(path),
        ], encoding="utf-8", errors="replace").strip()
        return bool(result)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


NVENC_FALLBACK_CODES = {1, 7, 220}
AUDIO_PAD_DURATION_SECONDS = 15


def get_nvenc_fallback_codec(video_codec):
    normalized = str(video_codec or "").lower()
    if "hevc" in normalized or "h265" in normalized:
        return "libx265"
    return "libx264"


def _build_episode_render_cmd(
    ep_file,
    output,
    keep_segments,
    watermark_path,
    encoding,
    audio_stream_index,
    audio_recovery=False,
    external_audio_path=None,
):
    if not keep_segments:
        raise RuntimeError(f"Episode has no ranges to render: {ep_file}")

    video_codec = encoding.get("video_codec", "h264_nvenc")
    preset = encoding.get("preset", "fast")
    cq = str(encoding.get("cq", 23))
    audio_codec = encoding.get("audio_codec", "aac")
    if audio_codec == "copy":
        audio_codec = "aac"

    audio_input_index = 2 if external_audio_path else 0
    audio_selector = f"[{audio_input_index}:a:{audio_stream_index}]"
    filters = []
    segment_count = len(keep_segments)
    if segment_count > 1:
        filters.append(
            f"[0:v:0]split={segment_count}"
            + "".join(f"[vsrc{index}]" for index in range(segment_count))
        )
        if audio_stream_index is not None:
            filters.append(
                f"{audio_selector}asplit={segment_count}"
                + "".join(f"[asrc{index}]" for index in range(segment_count))
            )

    for index, (start, end) in enumerate(keep_segments):
        video_input = f"[vsrc{index}]" if segment_count > 1 else "[0:v:0]"
        filters.append(
            f"{video_input}trim=start={start:.6f}:end={end:.6f},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        if audio_stream_index is not None:
            audio_input = f"[asrc{index}]" if segment_count > 1 else audio_selector
            filters.append(
                f"{audio_input}atrim=start={start:.6f}:end={end:.6f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )

    if segment_count > 1:
        inputs = "".join(
            f"[v{index}]" + (f"[a{index}]" if audio_stream_index is not None else "")
            for index in range(segment_count)
        )
        filters.append(
            f"{inputs}concat=n={segment_count}:v=1:a={1 if audio_stream_index is not None else 0}"
            + ("[vcat][acat]" if audio_stream_index is not None else "[vcat]")
        )
    else:
        filters.append("[v0]null[vcat]")
        if audio_stream_index is not None:
            filters.append("[a0]anull[acat]")

    audio_output = "acat"
    if audio_stream_index is not None and audio_recovery:
        filters.append(
            f"[acat]aresample=async=1:first_pts=0,"
            f"apad=pad_dur={AUDIO_PAD_DURATION_SECONDS}[arecovered]"
        )
        audio_output = "arecovered"
    elif audio_stream_index is not None and external_audio_path:
        filters.append(
            f"[acat]apad=pad_dur={AUDIO_PAD_DURATION_SECONDS}[aexternal]"
        )
        audio_output = "aexternal"

    frame_rate = encoding.get("frame_rate")
    frame_width = encoding.get("frame_width")
    frame_height = encoding.get("frame_height")
    frame_size_filter = (
        f"scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease,"
        f"pad={frame_width}:{frame_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        if frame_width and frame_height else ""
    )
    filters.extend([
        f"[vcat]{f'fps=fps={frame_rate},' if frame_rate else ''}"
        f"{frame_size_filter}format=yuv420p[base]",
        "[1:v]scale=160:-1,format=rgba[wm]",
        "[base][wm]overlay=W-w-20:20,format=yuv420p[vout]",
    ])
    cmd = [
        "ffmpeg", "-y",
        "-i", str(ep_file),
        "-i", str(watermark_path),
    ]
    if external_audio_path:
        cmd += ["-i", str(external_audio_path)]
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
    ]
    if audio_stream_index is not None:
        cmd += ["-map", f"[{audio_output}]"]
    cmd += ["-c:v", video_codec]
    if "nvenc" in video_codec:
        cmd += ["-preset", preset, "-cq", cq]
    elif video_codec in {"libx264", "libx265"}:
        cmd += ["-preset", preset, "-crf", cq]
    pixel_format = encoding.get("pixel_format")
    if pixel_format:
        cmd += ["-pix_fmt", str(pixel_format)]
    if audio_stream_index is not None:
        cmd += [
            "-c:a", audio_codec,
            "-b:a", str(encoding.get("audio_bitrate", "192k")),
            "-ar", str(encoding.get("audio_sample_rate", 48000)),
            "-ac", str(encoding.get("audio_channels", 2)),
            "-shortest",
        ]
    cmd.append(str(output))
    return cmd


def render_episode(
    ep_file,
    output,
    keep_segments,
    watermark_path,
    encoding,
    audio_stream_index=None,
    audio_recovery=False,
    external_audio_path=None,
):
    cmd = _build_episode_render_cmd(
        ep_file,
        output,
        keep_segments,
        watermark_path,
        encoding,
        audio_stream_index,
        audio_recovery,
        external_audio_path,
    )
    video_codec = encoding.get("video_codec", "h264_nvenc")
    try:
        run(cmd)
        return
    except RuntimeError as exc:
        called_process = exc.__cause__
        exit_code = called_process.returncode if isinstance(
            called_process, subprocess.CalledProcessError
        ) else None
        if "nvenc" not in str(video_codec).lower() or exit_code not in NVENC_FALLBACK_CODES:
            raise

    fallback_codec = get_nvenc_fallback_codec(video_codec)
    print(f"[EPISODE RENDER] NVENC failed (code {exit_code}), falling back to {fallback_codec}")
    fallback_encoding = {**encoding, "video_codec": fallback_codec, "preset": "fast"}
    run(_build_episode_render_cmd(
        ep_file,
        output,
        keep_segments,
        watermark_path,
        fallback_encoding,
        audio_stream_index,
        audio_recovery,
        external_audio_path,
    ))


def _build_final_cmd(
    concat_output,
    watermark_path,
    output_video,
    encoding,
    audio_stream_index,
    audio_recovery=False,
    external_audio_path=None,
):
    video_codec = encoding.get("video_codec", "h264_nvenc")
    preset = encoding.get("preset", "fast")
    cq = str(encoding.get("cq", 23))
    audio_codec = encoding.get("audio_codec", "aac")

    filters = [
        "[0:v]format=yuv420p[base];"
        "[1:v]scale=160:-1,format=rgba[wm];"
        "[base][wm]overlay=W-w-20:20,format=yuv420p[v]"
    ]
    audio_input_index = 2 if external_audio_path else 0
    audio_selector = f"{audio_input_index}:a:{audio_stream_index}"
    audio_output = audio_selector
    if audio_stream_index is not None and audio_recovery:
        filters.append(
            f"[{audio_selector}]aresample=async=1:first_pts=0,"
            f"apad=pad_dur={AUDIO_PAD_DURATION_SECONDS}[arecovered]"
        )
        audio_output = "[arecovered]"
    elif audio_stream_index is not None and external_audio_path:
        filters.append(
            f"[{audio_selector}]asetpts=PTS-STARTPTS,"
            f"apad=pad_dur={AUDIO_PAD_DURATION_SECONDS}[aexternal]"
        )
        audio_output = "[aexternal]"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", concat_output,
        "-i", watermark_path,
    ]
    if external_audio_path:
        cmd += ["-i", external_audio_path]
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[v]",
    ]
    if audio_stream_index is not None:
        cmd += ["-map", audio_output if audio_output.startswith("[") else f"{audio_output}?"]
    cmd += [
        "-map", "0:s?",
        "-c:v", video_codec,
    ]

    if "nvenc" in video_codec:
        cmd += ["-preset", preset, "-cq", cq]
    elif video_codec in ["libx264", "libx265"]:
        cmd += ["-preset", preset, "-crf", cq]

    if audio_stream_index is not None:
        cmd += ["-c:a", audio_codec]
    cmd += ["-c:s", "copy"]
    if audio_recovery or external_audio_path:
        cmd.append("-shortest")
    cmd.append(output_video)

    return cmd


def render_final(
    concat_output,
    watermark_path,
    output_video,
    encoding,
    audio_stream_index=0,
    audio_recovery=False,
    external_audio_path=None,
):
    if not _probe_video_streams(concat_output):
        raise RuntimeError(
            f"concat_output has no video stream, file may be corrupt: {concat_output}"
        )

    cmd = _build_final_cmd(
        concat_output,
        watermark_path,
        output_video,
        encoding,
        audio_stream_index,
        audio_recovery,
        external_audio_path,
    )
    video_codec = encoding.get("video_codec", "h264_nvenc")

    try:
        run(cmd)
        return
    except RuntimeError as exc:
        is_nvenc = "nvenc" in str(video_codec).lower()
        called_process = exc.__cause__
        exit_code = called_process.returncode if isinstance(called_process, subprocess.CalledProcessError) else None
        if is_nvenc and exit_code in NVENC_FALLBACK_CODES:
            pass
        else:
            raise

    fallback_codec = get_nvenc_fallback_codec(video_codec)
    print(f"[FINAL_RENDER] NVENC failed (code {exit_code}), falling back to {fallback_codec}")
    fallback_encoding = dict(encoding)
    fallback_encoding["video_codec"] = fallback_codec
    fallback_encoding["preset"] = "fast"
    fallback_cmd = _build_final_cmd(
        concat_output, watermark_path, output_video,
        fallback_encoding, audio_stream_index, audio_recovery, external_audio_path,
    )
    run(fallback_cmd)
