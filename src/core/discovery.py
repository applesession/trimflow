import re
from pathlib import Path

from shared.constants import SUPPORTED_VIDEO_EXTENSIONS


def extract_episode_number(filename: str):
    patterns = [
        r"^(\d{1,3})\.",
        r"\[(\d{1,3})\s+of\s+\d{1,3}\]",
        r"\[(\d{1,3})\]",
        r"[Ss]\d{1,2}[Ee](\d{1,3})",
        r"[\s._-](\d{1,3})[\s._-]",
        r"[Ee]pisode[\s._-]*(\d{1,3})",
    ]

    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            return int(match.group(1))

    return None


def find_episode_files(source_dir: Path):
    detected_files = []
    ignored_files = []

    for path in source_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
            episode_number = extract_episode_number(path.name)
            if episode_number is not None:
                detected_files.append((episode_number, path))
            else:
                ignored_files.append({
                    "path": str(path),
                    "reason": "episode_number_not_detected",
                })

    detected_files.sort(key=lambda item: item[0])

    if not detected_files:
        raise RuntimeError(f"No episode files found in {source_dir}")

    return detected_files, ignored_files


def filter_episode_files(episode_files, allowed_episodes):
    filtered = []
    excluded = []
    detected_episode_numbers = []

    for episode_number, path in episode_files:
        detected_episode_numbers.append(int(episode_number))
        if episode_number in allowed_episodes:
            filtered.append((episode_number, path))
        else:
            excluded.append({
                "episode": episode_number,
                "path": str(path),
                "reason": "out_of_range",
            })

    if not filtered:
        requested = sorted(int(episode_number) for episode_number in allowed_episodes)
        raise RuntimeError(
            "No episodes remained after applying episodes_range; "
            f"requested={requested}; found={detected_episode_numbers}"
        )

    return filtered, excluded
