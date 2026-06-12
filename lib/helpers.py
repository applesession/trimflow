import re
import subprocess
from pathlib import Path


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


def sanitize_filename(value: str) -> str:
    cleaned = str(value).replace("/", "-")
    cleaned = re.sub(r'[<>:"\\|?*]', "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        raise RuntimeError(f"Value '{value}' produced an empty filename")
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


def run(cmd):
    print("\n[RUN]", " ".join(map(str, cmd)))
    subprocess.run(list(map(str, cmd)), check=True)


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
