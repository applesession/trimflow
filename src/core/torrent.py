import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from core.discovery import extract_episode_number
from shared.constants import SUPPORTED_EXTERNAL_AUDIO_EXTENSIONS, SUPPORTED_VIDEO_EXTENSIONS
from shared.helpers import run


SOURCE_MARKER_NAME = ".torrent_source.json"
BONUS_DIRECTORY_NAMES = {"bonus", "bonuses", "extra", "extras", "special", "specials"}


class TorrentSelectionError(RuntimeError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


def _atomic_write_json(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _source_fingerprint(magnet, path_filter):
    payload = json.dumps(
        {"magnet": str(magnet), "path_filter": str(path_filter or "").casefold()},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _prepare_download_dir(download_dir, magnet, path_filter):
    download_dir = Path(download_dir)
    marker_path = download_dir / SOURCE_MARKER_NAME
    fingerprint = _source_fingerprint(magnet, path_filter)
    marker = None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass

    if download_dir.exists() and (not isinstance(marker, dict) or marker.get("fingerprint") != fingerprint):
        shutil.rmtree(download_dir, ignore_errors=True)
        marker = None

    download_dir.mkdir(parents=True, exist_ok=True)
    if not marker:
        marker = {"schema_version": 1, "fingerprint": fingerprint, "torrent_file": None}
        _atomic_write_json(marker_path, marker)
    return download_dir, marker_path, marker


def _aria_common_options(download_dir):
    return [
        "--dir", str(download_dir),
        "--seed-time=0",
        "--summary-interval=30",
        "--max-connection-per-server=16",
        "--split=16",
        "--continue=true",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
    ]


def _ensure_torrent_metadata(magnet, download_dir, marker_path, marker, timeout=None):
    recorded = marker.get("torrent_file")
    if recorded:
        torrent_path = download_dir / recorded
        if torrent_path.is_file():
            return torrent_path

    torrents = list(download_dir.glob("*.torrent"))
    if not torrents:
        run([
            "aria2c",
            *_aria_common_options(download_dir),
            "--bt-metadata-only=true",
            "--bt-save-metadata=true",
            str(magnet),
        ], timeout=timeout)
        torrents = list(download_dir.glob("*.torrent"))

    if len(torrents) != 1:
        raise RuntimeError(f"Expected one torrent metadata file, found {len(torrents)} in {download_dir}")

    torrent_path = torrents[0]
    marker["torrent_file"] = torrent_path.name
    _atomic_write_json(marker_path, marker)
    return torrent_path


def list_torrent_files(torrent_path):
    output = subprocess.check_output(
        ["aria2c", "--show-files", str(torrent_path)],
        encoding="utf-8",
        errors="replace",
    )
    files = []
    for line in output.splitlines():
        match = re.match(r"^\s*(\d+)\|(.*\S)\s*$", line)
        if match:
            files.append({"index": int(match.group(1)), "path": match.group(2).strip()})
    if not files:
        raise RuntimeError(f"aria2 did not list files for {torrent_path}")
    return files


def prepare_torrent_metadata(magnet, download_dir, path_filter=None, timeout=None):
    download_dir, marker_path, marker = _prepare_download_dir(download_dir, magnet, path_filter)
    torrent_path = _ensure_torrent_metadata(
        magnet,
        download_dir,
        marker_path,
        marker,
        timeout=timeout,
    )
    return torrent_path, list_torrent_files(torrent_path)


def _looks_1080p(path):
    return re.search(r"(?<!\d)(?:1080p?|1920[x×]1080)(?!\d)", str(path), flags=re.IGNORECASE) is not None


def _is_episode_path(path):
    return "episodes" in (part.casefold() for part in PurePosixPath(str(path).replace("\\", "/")).parts[:-1])


def _is_bonus_path(path):
    directories = PurePosixPath(str(path).replace("\\", "/")).parts[:-1]
    return any(part.casefold() in BONUS_DIRECTORY_NAMES for part in directories)


def select_torrent_episode_files(
    torrent_files,
    allowed_episodes,
    path_filter=None,
    allow_missing_episodes=False,
):
    requested = sorted(int(episode) for episode in allowed_episodes)
    filter_text = str(path_filter or "").strip().casefold()
    candidates = {episode: [] for episode in requested}

    for item in torrent_files:
        relative_path = str(item["path"])
        if Path(relative_path).suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        if filter_text and filter_text not in relative_path.casefold():
            continue
        episode = extract_episode_number(Path(relative_path).name)
        if episode in candidates:
            candidates[episode].append({"index": int(item["index"]), "path": relative_path})

    missing = [episode for episode, items in candidates.items() if not items]
    if missing and not allow_missing_episodes:
        suffix = f" after path filter {path_filter!r}" if filter_text else ""
        raise RuntimeError(f"Torrent files not found for episodes: {missing}{suffix}")

    if missing:
        print(f"[TORRENT MISSING] allowed missing episodes: {missing}")

    selected = []
    for episode in requested:
        items = candidates[episode]
        if not items:
            continue
        if len(items) > 1:
            episode_paths = [item for item in items if _is_episode_path(item["path"])]
            if episode_paths:
                items = episode_paths
            regular_paths = [item for item in items if not _is_bonus_path(item["path"])]
            if regular_paths:
                items = regular_paths
            preferred = [item for item in items if _looks_1080p(item["path"])]
            if len(preferred) == 1:
                items = preferred
            elif len(items) > 1:
                details = json.dumps({"episode": episode, "candidates": items}, ensure_ascii=False)
                print("[TORRENT CANDIDATES] " + details)
                preview = "; ".join(f"[{item['index']}] {item['path']}" for item in items[:3])
                extra = f"; and {len(items) - 3} more" if len(items) > 3 else ""
                raise TorrentSelectionError(
                    f"Multiple torrent files found for episode {episode}: {preview}{extra}. "
                    "Use source path filter",
                    details=details,
                )
        selected.append({"episode": episode, **items[0]})
    if not selected:
        raise RuntimeError("Torrent contains none of the requested episodes")
    return selected


def discover_torrent_episode_numbers(magnet, download_dir, path_filter=None, timeout=None):
    _, torrent_files = prepare_torrent_metadata(
        magnet,
        download_dir,
        path_filter=path_filter,
        timeout=timeout,
    )
    filter_text = str(path_filter or "").strip().casefold()
    episodes = set()
    for item in torrent_files:
        relative_path = str(item["path"])
        if Path(relative_path).suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        if filter_text and filter_text not in relative_path.casefold():
            continue
        episode = extract_episode_number(Path(relative_path).name)
        if episode is not None and episode > 0:
            episodes.add(episode)
    if not episodes:
        raise RuntimeError("Torrent contains no detectable episode files")

    expected = set(range(min(episodes), max(episodes) + 1))
    missing = sorted(expected - episodes)
    if missing:
        raise RuntimeError(f"Torrent season has missing episodes: {missing}")
    select_torrent_episode_files(torrent_files, expected, path_filter=path_filter)
    return sorted(expected)


def select_torrent_external_audio_files(torrent_files, allowed_episodes):
    requested = set(int(episode) for episode in allowed_episodes)
    selected = []
    for item in torrent_files:
        relative_path = str(item["path"])
        if Path(relative_path).suffix.lower() not in SUPPORTED_EXTERNAL_AUDIO_EXTENSIONS:
            continue
        episode = extract_episode_number(Path(relative_path).name)
        if episode in requested:
            selected.append({
                "episode": episode,
                "index": int(item["index"]),
                "path": relative_path,
            })
    return sorted(selected, key=lambda item: (item["episode"], item["index"]))


def prepare_torrent_episode_downloads(
    magnet,
    download_dir,
    allowed_episodes,
    path_filter=None,
    timeout=None,
    allow_missing_episodes=False,
):
    torrent_path, torrent_files = prepare_torrent_metadata(
        magnet,
        download_dir,
        path_filter=path_filter,
        timeout=timeout,
    )
    selected = select_torrent_episode_files(
        torrent_files,
        allowed_episodes,
        path_filter=path_filter,
        allow_missing_episodes=allow_missing_episodes,
    )
    return torrent_path, selected


def download_torrent_episode(torrent_path, slot_dir, selected, timeout=None):
    slot_dir = Path(slot_dir)
    slot_dir.mkdir(parents=True, exist_ok=True)
    print("[TORRENT SELECT] " + json.dumps([selected], ensure_ascii=False))
    run([
        "aria2c",
        *_aria_common_options(slot_dir),
        f"--select-file={int(selected['index'])}",
        "--bt-remove-unselected-file=true",
        str(torrent_path),
    ], timeout=timeout)


def download_selected_episodes(
    magnet,
    download_dir,
    allowed_episodes,
    path_filter=None,
    timeout=None,
    allow_missing_episodes=False,
):
    torrent_path, selected = prepare_torrent_episode_downloads(
        magnet,
        download_dir,
        allowed_episodes,
        path_filter=path_filter,
        timeout=timeout,
        allow_missing_episodes=allow_missing_episodes,
    )
    selected_episode_numbers = {item["episode"] for item in selected}
    external_audio = select_torrent_external_audio_files(
        list_torrent_files(torrent_path),
        selected_episode_numbers,
    )
    downloads = selected + external_audio
    indices = ",".join(str(item["index"]) for item in downloads)
    print("[TORRENT SELECT] " + json.dumps(downloads, ensure_ascii=False))
    run([
        "aria2c",
        *_aria_common_options(download_dir),
        f"--select-file={indices}",
        "--bt-remove-unselected-file=true",
        str(torrent_path),
    ], timeout=timeout)
    return selected


def download_selected_episodes_from_sources(sources, download_dir, allowed_episodes, timeout=None):
    sources = list(sources or [])
    if len(sources) < 2:
        raise RuntimeError("Multi-source download requires at least two magnet sources")

    requested = sorted(int(episode) for episode in allowed_episodes)
    prepared = []
    for index, source in enumerate(sources, start=1):
        magnet = str((source or {}).get("magnet") or "").strip()
        if not magnet.startswith("magnet:?"):
            raise RuntimeError(f"Invalid magnet source #{index}")
        path_filter = str((source or {}).get("path_filter") or "").strip() or None
        part_dir = Path(download_dir) / f"part_{index:02d}"
        _, torrent_files = prepare_torrent_metadata(
            magnet,
            part_dir,
            path_filter=path_filter,
            timeout=timeout,
        )
        available = set()
        filter_text = str(path_filter or "").casefold()
        for item in torrent_files:
            relative_path = str(item["path"])
            if Path(relative_path).suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                continue
            if filter_text and filter_text not in relative_path.casefold():
                continue
            episode = extract_episode_number(Path(relative_path).name)
            if episode in allowed_episodes:
                available.add(episode)
        prepared.append({
            "magnet": magnet,
            "path_filter": path_filter,
            "download_dir": part_dir,
            "available": available,
        })

    assignments = {index: set() for index in range(len(prepared))}
    missing = []
    overlaps = []
    for episode in requested:
        candidates = [index for index, item in enumerate(prepared) if episode in item["available"]]
        if not candidates:
            missing.append(episode)
            continue
        assignments[candidates[0]].add(episode)
        if len(candidates) > 1:
            overlaps.append({
                "episode": episode,
                "selected_source": candidates[0] + 1,
                "candidate_sources": [index + 1 for index in candidates],
            })

    if missing:
        raise RuntimeError(f"Torrent files not found for episodes across sources: {missing}")
    if overlaps:
        print("[TORRENT SOURCE OVERLAP] " + json.dumps(overlaps, ensure_ascii=False))

    selected = []
    for index, episodes in assignments.items():
        if not episodes:
            continue
        item = prepared[index]
        selected.extend(download_selected_episodes(
            item["magnet"],
            item["download_dir"],
            episodes,
            path_filter=item["path_filter"],
            timeout=timeout,
        ))
    return sorted(selected, key=lambda item: item["episode"])
