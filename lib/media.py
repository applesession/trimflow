import subprocess

from lib.helpers import run


def ffprobe_duration(path):
    result = subprocess.check_output([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ])
    return float(result.decode().strip())


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


def align_subsegments_to_keyframes(subsegments, keyframes):
    """Align copy-mode subsegment boundaries to keyframes to prevent overlap.

    With -c copy, -ss before -i seeks to the nearest keyframe *before* the
    requested time, causing consecutive copy subsegments to overlap by up to
    the keyframe interval.

    This only touches subsegments with cut_mode="copy". Precise subsegments
    (re-encoded) pass through unchanged.

    Internal copy-to-copy boundaries are aligned so adjacent subsegments meet
    at the *same* keyframe. Edges that touch a precise segment are preserved:
    - first copy keeps its original start (interface with left precise)
    - last copy keeps its original end   (interface with right precise)

    This works for both pure-copy mode (all subsegments are "copy") and hybrid
    mode (mix of "copy" and "precise").
    """
    if not keyframes or not subsegments:
        return subsegments

    keyframes = sorted(set(keyframes))

    def kf_before(time):
        result = None
        for kf in keyframes:
            if kf <= time + 0.001:
                result = kf
            else:
                break
        return result

    aligned = []

    for i, sub in enumerate(subsegments):
        if sub["cut_mode"] != "copy":
            aligned.append(sub)
            continue

        start = sub["start"]
        end = sub["end"]

        prev_is_copy = aligned and aligned[-1]["cut_mode"] == "copy"
        if prev_is_copy:
            kf_start = kf_before(start)
            if kf_start is None:
                kf_start = start
            prev_end = aligned[-1]["end"]
            kf_start = max(kf_start, prev_end)
        else:
            kf_start = start

        next_is_copy = i + 1 < len(subsegments) and subsegments[i + 1]["cut_mode"] == "copy"
        kf_end = end
        if next_is_copy:
            aligned_end = kf_before(end)
            if aligned_end is not None:
                kf_end = aligned_end

        if kf_end > kf_start:
            aligned.append({
                "start": kf_start,
                "end": kf_end,
                "cut_mode": sub["cut_mode"],
            })

    return aligned


def render_segment_copy(ep_file, segment_output, start, end):
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", ep_file,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-map", "0:s?",
        "-c", "copy",
        "-c:s", "copy",
        segment_output,
    ]

    run(cmd)


def render_segment_precise(ep_file, segment_output, start, end, segment_encoding=None):
    duration = max(0.0, end - start)
    if duration <= 0:
        raise RuntimeError(f"Invalid precise segment duration for {ep_file}: {start} -> {end}")

    encoding = segment_encoding or {}
    video_codec = encoding.get("video_codec", "libx264")
    preset = encoding.get("preset", "medium")
    cq = str(encoding.get("cq", 18 if "nvenc" in video_codec else 15))
    audio_codec = encoding.get("audio_codec", "aac")
    audio_bitrate = encoding.get("audio_bitrate", "192k")
    pixel_format = encoding.get("pixel_format")

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", ep_file,
        "-map", "0:v:0",
        "-map", "0:a:0?",
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
        "-c:s", "copy",
        segment_output,
    ]

    run(cmd)


def render_segment(ep_file, segment_output, start, end, segment_encoding=None):
    duration = max(0.0, end - start)
    if duration <= 0:
        raise RuntimeError(f"Invalid segment duration for {ep_file}: {start} -> {end}")

    cut_mode = (segment_encoding or {}).get("cut_mode", "hybrid")
    if cut_mode == "precise":
        render_segment_precise(ep_file, segment_output, start, end, segment_encoding=segment_encoding)
        return

    render_segment_copy(ep_file, segment_output, start, end)


def render_concat(concat_file, concat_output):
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
    except subprocess.CalledProcessError:
        pass

    safe_path = [
        "ffmpeg",
        "-y",
        "-fflags", "+genpts",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-map", "0:v?",
        "-map", "0:a:0?",
        "-map", "0:s?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "18",
        "-c:a", "aac",
        "-c:s", "copy",
        concat_output,
    ]
    run(safe_path)


def render_final(concat_output, watermark_path, output_video, encoding):
    video_codec = encoding.get("video_codec", "h264_nvenc")
    preset = encoding.get("preset", "fast")
    cq = str(encoding.get("cq", 23))
    audio_codec = encoding.get("audio_codec", "aac")

    overlay_filter = (
        "[0:v]format=yuv420p[base];"
        "[1:v]scale=160:-1,format=rgba[wm];"
        "[base][wm]overlay=W-w-20:20,format=yuv420p[v]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", concat_output,
        "-i", watermark_path,
        "-filter_complex", overlay_filter,
        "-map", "[v]",
        "-map", "0:a:0?",
        "-map", "0:s?",
        "-c:v", video_codec,
    ]

    if "nvenc" in video_codec:
        cmd += ["-preset", preset, "-cq", cq]
    elif video_codec in ["libx264", "libx265"]:
        cmd += ["-preset", preset, "-crf", cq]

    cmd += [
        "-c:a", audio_codec,
        "-c:s", "copy",
        output_video,
    ]

    run(cmd)
