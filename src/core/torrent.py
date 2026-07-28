import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from core.discovery import extract_episode_number
from shared.constants import SUPPORTED_VIDEO_EXTENSIONS
from shared.helpers import run


SOURCE_MARKER_NAME = ".torrent_source.json"


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


def _looks_1080p(path):
    return re.search(r"(?<!\d)(?:1080p?|1920[x×]1080)(?!\d)", str(path), flags=re.IGNORECASE) is not None


def _is_episode_path(path):
    return "episodes" in (part.casefold() for part in PurePosixPath(str(path).replace("\\", "/")).parts[:-1])


def select_torrent_episode_files(torrent_files, allowed_episodes, path_filter=None):
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
    if missing:
        suffix = f" after path filter {path_filter!r}" if filter_text else ""
        raise RuntimeError(f"Torrent files not found for episodes: {missing}{suffix}")

    selected = []
    for episode in requested:
        items = candidates[episode]
        if len(items) > 1:
            episode_paths = [item for item in items if _is_episode_path(item["path"])]
            if episode_paths:
                items = episode_paths
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
    return selected


def prepare_torrent_episode_downloads(
    magnet,
    download_dir,
    allowed_episodes,
    path_filter=None,
    timeout=None,
):
    download_dir, marker_path, marker = _prepare_download_dir(download_dir, magnet, path_filter)
    torrent_path = _ensure_torrent_metadata(magnet, download_dir, marker_path, marker, timeout=timeout)
    selected = select_torrent_episode_files(
        list_torrent_files(torrent_path),
        allowed_episodes,
        path_filter=path_filter,
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


def download_selected_episodes(magnet, download_dir, allowed_episodes, path_filter=None, timeout=None):
    torrent_path, selected = prepare_torrent_episode_downloads(
        magnet,
        download_dir,
        allowed_episodes,
        path_filter=path_filter,
        timeout=timeout,
    )
    indices = ",".join(str(item["index"]) for item in selected)
    print("[TORRENT SELECT] " + json.dumps(selected, ensure_ascii=False))
    run([
        "aria2c",
        *_aria_common_options(download_dir),
        f"--select-file={indices}",
        "--bt-remove-unselected-file=true",
        str(torrent_path),
    ], timeout=timeout)
    return selected
