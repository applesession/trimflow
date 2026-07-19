import re
import os
import signal
import subprocess
import time
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


def get_display_title(payload) -> str:
    if not isinstance(payload, dict):
        return "Без названия"
    for key in ["title_ru", "title"]:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Без названия"


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


def build_compilation_display_name(job, season, episodes_range, suffix="[Без OP/ED]") -> str:
    display_title = get_display_title(job)
    season_number = int(str(season))
    episodes_label = format_episodes_label(episodes_range)
    return f"{display_title} - {season_number} Сезон {episodes_label} {suffix}".strip()


def build_single_episode_display_name(job, season, episode_number) -> str:
    display_title = get_display_title(job)
    season_number = int(str(season))
    return f"{display_title} - {season_number} Сезон {int(episode_number)} Серия".strip()


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
    process = subprocess.Popen(str_cmd, start_new_session=os.name == "posix")
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
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, str_cmd)
    except JobCancelled:
        _stop_process(process)
        raise
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffmpeg exited with code {exc.returncode}: {' '.join(str_cmd)}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _stop_process(process)
        raise RuntimeError(
            f"Command timed out after {timeout}s: {' '.join(str_cmd)}"
        ) from exc
    except BaseException:
        _stop_process(process)
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


def create_concat_file(segment_files, path: Path):
    with open(path, "w", encoding="utf-8") as file:
        for segment in segment_files:
            resolved = str(segment.resolve()).replace("'", r"'\''")
            file.write(f"file '{resolved}'\n")
