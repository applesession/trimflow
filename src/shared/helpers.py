import re
import os
import signal
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path


class JobCancelled(RuntimeError):
    pass


_cancel_check = None
_last_cancel_check = 0.0


@contextmanager
def cancellation_scope(cancel_check):
    global _cancel_check, _last_cancel_check
    previous = _cancel_check
    previous_check_time = _last_cancel_check
    _cancel_check = cancel_check
    _last_cancel_check = 0.0
    try:
        yield
    finally:
        _cancel_check = previous
        _last_cancel_check = previous_check_time


def raise_if_cancelled():
    global _last_cancel_check
    if not _cancel_check:
        return
    now = time.monotonic()
    if now - _last_cancel_check < 0.5:
        return
    _last_cancel_check = now
    if _cancel_check():
        raise JobCancelled("Job removed from queue")


def _stop_process(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            process.kill()
        process.wait()


def slugify(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", "_", value)
    return value


def ensure_non_empty_slug(title: str) -> str:
    slug = slugify(title)
    if not slug:
        raise RuntimeError(f"Title '{title}' produced an empty slug")
    return slug


def build_job_workspace_name(job) -> str:
    title_slug = ensure_non_empty_slug(job["title"])
    automation = job.get("automation") or {}
    episodes_range = str(job.get("episodes_range", "")).strip()
    if automation.get("release_id") is not None or not episodes_range:
        return title_slug

    season = str(job.get("season", 1)).strip().zfill(2)
    processing_mode = str(job.get("processing_mode", "compilation") or "compilation").strip().lower()
    range_slug = slugify(episodes_range.replace(",", "_"))
    return f"{title_slug}__S{season}__{range_slug}__{processing_mode}"


def get_source_signature(source) -> str:
    source = source or {}
    source_type = str(source.get("type", "")).strip().lower()
    if source_type == "magnet":
        parts = source.get("parts") or []
        if parts:
            return "||".join(
                f"{str(part.get('magnet', '')).strip()}|{str(part.get('path_filter') or '').strip().casefold()}"
                for part in parts
            )
        return str(source.get("magnet", "")).strip()
    if source_type == "local":
        return str(source.get("input_dir", "")).strip()
    return ""


def get_display_title(payload) -> str:
    if not isinstance(payload, dict):
        return "Без названия"
    for key in ["title_ru", "title"]:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Без названия"


def _confirmed_title_season(payload):
    title_ru = str((payload or {}).get("title_ru") or "").strip()
    title = str((payload or {}).get("title") or "").strip()
    ru_match = re.search(r"(?:^|\s)(\d+)\s*$", title_ru)
    en_match = re.search(
        r"\b(?:(\d+)(?:st|nd|rd|th)\s+season|season\s+(\d+))\b",
        title,
        flags=re.IGNORECASE,
    )
    if not ru_match or not en_match:
        return None
    russian_number = int(ru_match.group(1))
    english_number = int(en_match.group(1) or en_match.group(2))
    return russian_number if russian_number == english_number else None


def get_automatic_navigation_label(payload):
    if not isinstance(payload, dict):
        return None

    automation = payload.get("automation") or {}
    provider_season = automation.get("season_number")
    try:
        if provider_season is not None and int(provider_season) > 0:
            return f"Сезон {int(provider_season)}"
    except (TypeError, ValueError):
        pass

    confirmed_season = _confirmed_title_season(payload)
    if confirmed_season is not None:
        return f"Сезон {confirmed_season}"

    release_type = str(automation.get("release_type") or "").strip().upper()
    type_labels = {
        "MOVIE": "Фильм",
        "OVA": "OVA",
        "ONA": "ONA",
        "SPECIAL": "Спешл",
    }
    if release_type in type_labels:
        return type_labels[release_type]

    if automation.get("release_id") is not None:
        return None

    try:
        return f"Сезон {int(payload.get('season', 1))}"
    except (TypeError, ValueError):
        return None


def get_navigation_label(payload):
    if isinstance(payload, dict):
        direct_label = payload.get("navigation_label")
        if isinstance(direct_label, str) and direct_label.strip():
            return direct_label.strip()
        naming = (payload.get("processing") or {}).get("naming") or {}
        explicit_label = naming.get("navigation_label")
        if isinstance(explicit_label, str) and explicit_label.strip():
            return explicit_label.strip()
    return get_automatic_navigation_label(payload)


def set_navigation_label(payload, label, source="manual"):
    processing = dict(payload.get("processing") or {})
    naming = dict(processing.get("naming") or {})
    naming.update({
        "navigation_label": str(label).strip(),
        "source": str(source).strip() or "manual",
    })
    processing["naming"] = naming
    payload["processing"] = processing
    return payload


def clear_navigation_label(payload):
    processing = dict(payload.get("processing") or {})
    naming = dict(processing.get("naming") or {})
    naming.pop("navigation_label", None)
    naming.pop("source", None)
    if naming:
        processing["naming"] = naming
    else:
        processing.pop("naming", None)
    if processing:
        payload["processing"] = processing
    else:
        payload.pop("processing", None)
    return payload


def _normalize_episode_part(part: str) -> str:
    if "-" in part:
        start_raw, end_raw = [item.strip() for item in part.split("-", 1)]
        return f"{int(start_raw)}-{int(end_raw)}"
    return str(int(part.strip()))


def format_episodes_label(episodes_range: str) -> str:
    normalized_parts = []
    for raw_part in str(episodes_range).split(","):
        part = raw_part.strip()
        if not part:
            continue
        normalized_parts.append(_normalize_episode_part(part))

    joined = ",".join(normalized_parts)
    return f"{joined} Серия"


def format_navigation_label(label):
    season_match = re.fullmatch(r"Сезон\s+(\d+)", label or "", flags=re.IGNORECASE)
    return f"{season_match.group(1)} Сезон" if season_match else label


def build_compilation_display_name(job, season, episodes_range, suffix="[Без OP/ED]") -> str:
    display_title = get_display_title(job)
    episodes_label = format_episodes_label(episodes_range)
    navigation_label = format_navigation_label(get_navigation_label(job))
    details = " ".join(
        part for part in [episodes_label, navigation_label, suffix] if part
    )
    return f"{display_title} - {details}"


def build_single_episode_display_name(job, season, episode_number) -> str:
    display_title = get_display_title(job)
    navigation_label = format_navigation_label(get_navigation_label(job))
    details = " ".join(
        part for part in [f"{int(episode_number)} Серия", navigation_label] if part
    )
    return f"{display_title} - {details}"


def sanitize_filename(value: str, max_bytes: int = 200) -> str:
    cleaned = str(value).replace("/", "-")
    cleaned = re.sub(r'[<>:"\\|?*]', "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        raise RuntimeError(f"Value '{value}' produced an empty filename")

    encoded = cleaned.encode("utf-8")
    if len(encoded) > max_bytes:
        cleaned = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()

    if not cleaned:
        raise RuntimeError(f"Value '{value}' produced an empty filename after truncation")
    return cleaned


def build_timestamps_description(timestamps) -> str:
    values = [str(item).strip() for item in (timestamps or []) if str(item).strip()]
    return "\n".join(values)


def build_vk_wall_post_text(job, pretty_base_name: str) -> str:
    display_title = get_display_title(job)
    if str(pretty_base_name).startswith(display_title):
        return pretty_base_name
    return f"{display_title}\n\n{pretty_base_name}"


def build_vk_comment_text(template: str) -> str:
    return str(template or "").strip()


def run(cmd, timeout=None):
    print("\n[RUN]", " ".join(map(str, cmd)))
    str_cmd = [str(arg) for arg in cmd]
    output_tail = deque(maxlen=20)
    process = subprocess.Popen(
        str_cmd,
        start_new_session=os.name == "posix",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def forward_output():
        try:
            for line in process.stdout:
                stripped = line.rstrip("\r\n")
                if stripped:
                    output_tail.append(stripped[-1000:])
                print(line, end="", flush=True)
        finally:
            process.stdout.close()

    output_thread = threading.Thread(target=forward_output, daemon=True)
    output_thread.start()
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        while True:
            raise_if_cancelled()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(str_cmd, timeout)
            try:
                process.wait(timeout=min(0.5, remaining) if remaining is not None else 0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        output_thread.join(timeout=5)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, str_cmd)
    except JobCancelled:
        _stop_process(process)
        output_thread.join(timeout=5)
        raise
    except subprocess.CalledProcessError as exc:
        diagnostic = "\n".join(output_tail)
        diagnostic_suffix = f"\nffmpeg output tail:\n{diagnostic}" if diagnostic else ""
        raise RuntimeError(
            f"ffmpeg exited with code {exc.returncode}: {' '.join(str_cmd)}"
            f"{diagnostic_suffix}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _stop_process(process)
        output_thread.join(timeout=5)
        raise RuntimeError(
            f"Command timed out after {timeout}s: {' '.join(str_cmd)}"
        ) from exc
    except BaseException:
        _stop_process(process)
        output_thread.join(timeout=5)
        raise


def parse_episodes_range(episodes_range: str):
    if not isinstance(episodes_range, str) or not episodes_range.strip():
        raise RuntimeError("episodes_range must be a non-empty string")

    allowed = set()

    for raw_part in episodes_range.split(","):
        part = raw_part.strip()
        if not part:
            continue

        if "-" in part:
            start_raw, end_raw = [item.strip() for item in part.split("-", 1)]
            if not start_raw.isdigit() or not end_raw.isdigit():
                raise RuntimeError(f"Invalid range segment: {part}")

            start = int(start_raw)
            end = int(end_raw)
            if start > end:
                raise RuntimeError(f"Invalid range segment: {part}")

            allowed.update(range(start, end + 1))
        else:
            if not part.isdigit():
                raise RuntimeError(f"Invalid episode number: {part}")
            allowed.add(int(part))

    if not allowed:
        raise RuntimeError("episodes_range did not contain any episode numbers")

    return allowed


def seconds_to_timestamp(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def create_concat_file(segment_files, path: Path, durations=None):
    if durations is not None and len(segment_files) != len(durations):
        raise ValueError("segment_files and durations must have equal length")

    with open(path, "w", encoding="utf-8") as file:
        if durations is not None:
            file.write("ffconcat version 1.0\n")
        for index, segment in enumerate(segment_files):
            resolved = str(segment.resolve()).replace("'", r"'\''")
            file.write(f"file '{resolved}'\n")
            if durations is not None:
                file.write(f"duration {float(durations[index]):.6f}\n")
