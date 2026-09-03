import re
from pathlib import Path

from shared.constants import SUPPORTED_EXTERNAL_AUDIO_EXTENSIONS, SUPPORTED_VIDEO_EXTENSIONS


def extract_episode_number(filename: str):
    patterns = [
        r"^(\d{1,4})\.",
        r"^(\d{1,4})\s*[-–—]\s*",
        r"\[(\d{1,4})[\s_]+of[\s_]+(?:\d{1,4}|[Xx]{1,4})\]",
        r"\[(\d{1,4})\]",
        r"[Ss]\d{1,2}[Ee](\d{1,4})",
        r"[Ee]pisode[\s._-]*(\d{1,4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            return int(match.group(1))

    hyphenated_match = re.search(
        r"\s[-–—]\s*(\d{1,4})(\.\d+)?(?=\s|[._-])",
        filename,
    )
    if hyphenated_match:
        if hyphenated_match.group(2):
            return None
        return int(hyphenated_match.group(1))

    generic_matches = re.findall(r"(?<=[\s._-])(\d{1,4})(?=[\s._-])", filename)
    if generic_matches:
        return int(generic_matches[-1])

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


def find_external_audio_files(source_dir: Path, allowed_episodes=None):
    allowed = None if allowed_episodes is None else set(allowed_episodes)
    detected_files = []
    for path in source_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTERNAL_AUDIO_EXTENSIONS:
            continue
        episode_number = extract_episode_number(path.name)
        if episode_number is not None and (allowed is None or episode_number in allowed):
            detected_files.append((episode_number, path))
    return sorted(detected_files, key=lambda item: (item[0], str(item[1]).casefold()))


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
