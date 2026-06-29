import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from lib.anilibria import get_release_details, list_recent_releases
from lib.config import normalize_automation_config
from lib.helpers import ensure_non_empty_slug, parse_episodes_range
from lib.db import (
    build_seen_episode_key,
    get_tracked_episode_keys,
    mark_episodes_queued,
    mark_episodes_completed,
    unmark_episodes_queued,
    get_discovery_blacklist as _db_get_discovery_blacklist,
    find_blacklist_item as _db_find_blacklist_item,
    add_to_blacklist as _db_add_to_blacklist,
    remove_from_blacklist as _db_remove_from_blacklist,
    record_skipped_item as _db_record_skipped_item,
    save_ongoing_progress as _db_save_ongoing_progress,
    load_ongoing_progress as _db_load_ongoing_progress,
)


def build_seen_episode_key(release_id, episode_number):
    return f"{release_id}:{int(episode_number):03d}"


def _get_episode_tracking_maps(state):
    """Returns queued and completed episode maps for legacy compatibility.
    Now reads from SQLite instead of state.json."""
    tracked_keys = get_tracked_episode_keys()
    # Build dicts for backward compat
    queued = {}
    completed = {}
    # The state dict keys are no longer used directly; tracking is in SQLite
    state.setdefault("queued_release_episodes", {})
    state.setdefault("completed_release_episodes", {})
    return state["queued_release_episodes"], state["completed_release_episodes"]


def get_job_release_id(job):
    automation = job.get("automation") or {}
    release_id = automation.get("release_id")
    return _parse_positive_int(release_id)


def get_job_episode_numbers(job):
    return sorted(parse_episodes_range(job.get("episodes_range", "")))


def mark_release_episodes_queued(state, release_id, episode_numbers):
    mark_episodes_queued(release_id, episode_numbers)


def unmark_release_episodes_queued(state, release_id, episode_numbers):
    unmark_episodes_queued(release_id, episode_numbers)


def mark_release_episodes_completed(state, release_id, episode_numbers):
    mark_episodes_completed(release_id, episode_numbers)


def mark_job_episodes_queued(state, job):
    release_id = get_job_release_id(job)
    if release_id is None:
        return state
    mark_release_episodes_queued(state, release_id, get_job_episode_numbers(job))
    return state


def unmark_job_episodes_queued(state, job):
    release_id = get_job_release_id(job)
    if release_id is None:
        return state
    unmark_release_episodes_queued(state, release_id, get_job_episode_numbers(job))
    return state


def mark_job_episodes_completed(state, job):
    release_id = get_job_release_id(job)
    if release_id is None:
        return state
    mark_release_episodes_completed(state, release_id, get_job_episode_numbers(job))
    return state


def format_episodes_range(episodes):
    normalized = sorted({int(episode) for episode in episodes})
    if not normalized:
        raise RuntimeError("episodes must contain at least one value")

    parts = []
    start = normalized[0]
    previous = normalized[0]

    for episode in normalized[1:]:
        if episode == previous + 1:
            previous = episode
            continue

        parts.append(_format_episode_range_part(start, previous))
        start = episode
        previous = episode

    parts.append(_format_episode_range_part(start, previous))
    return ",".join(parts)


def _format_episode_range_part(start, end):
    if start == end:
        return f"{start:03d}"
    return f"{start:03d}-{end:03d}"


def merge_episode_ranges(existing_range, new_episodes):
    merged = set(parse_episodes_range(existing_range))
    merged.update(int(episode) for episode in new_episodes)
    return format_episodes_range(merged)


def build_default_job_index(jobs):
    return {
        build_job_key(job): {
            "title": job.get("title"),
            "season": job.get("season"),
            "episodes_range": job.get("episodes_range"),
        }
        for job in jobs
    }


def get_job_processing_mode(job):
    return str(job.get("processing_mode", "compilation") or "compilation").strip().lower()


def build_job_key(job):
    source = job.get("source", {})
    source_type = source.get("type", "")
    source_signature = ""
    if source_type == "magnet":
        source_signature = source.get("magnet", "")
    elif source_type == "local":
        source_signature = source.get("input_dir", "")

    variant_identity = build_source_variant_identity(source)

    return "|".join([
        str(job.get("title", "")).strip().lower(),
        str(job.get("season", "")).strip(),
        get_job_processing_mode(job),
        str(source_type).strip().lower(),
        str(source_signature).strip(),
        variant_identity,
    ])


def build_ongoing_progress_key(title, season, source_type):
    return "|".join([
        str(title or "").strip().lower(),
        str(season or "").strip(),
        str(source_type or "").strip().lower(),
    ])


def build_ongoing_progress_key_from_job(job):
    source = job.get("source", {})
    return build_ongoing_progress_key(
        job.get("title"),
        job.get("season"),
        source.get("type"),
    )


def build_source_variant_identity(source):
    source = source or {}
    codec = str(source.get("variant_codec", "")).strip().lower()
    label = str(source.get("variant_label", "")).strip().lower()
    if not codec and not label:
        return ""
    return "|".join([codec, label])


def mark_ongoing_full_publish(state, job):
    key = build_ongoing_progress_key_from_job(job)
    if not key:
        return state

    episodes = sorted(parse_episodes_range(job.get("episodes_range", "")))
    _db_save_ongoing_progress(key, {
        "has_full_publish": True,
        "last_full_episode": max(episodes) if episodes else None,
        "last_full_range": job.get("episodes_range"),
        "updated_at": utc_now_iso(),
    })
    return state


def build_discovery_job_context(*, provider, release_id, is_ongoing, ongoing_progress_key, publish_strategy):
    return {
        "provider": provider,
        "release_id": release_id,
        "is_ongoing": bool(is_ongoing),
        "ongoing_progress_key": ongoing_progress_key,
        "publish_strategy": publish_strategy,
    }


def _normalize_skipped_episodes(episodes):
    return [int(episode) for episode in episodes or []]


def build_skipped_item_identity(item):
    return (
        str(item.get("release_id") or "").strip(),
        str(item.get("alias") or "").strip(),
        str(item.get("title") or "").strip(),
        str(item.get("reason") or "").strip(),
        tuple(_normalize_skipped_episodes(item.get("episodes"))),
    )


def record_skipped_item(state, item):
    _db_record_skipped_item(item)


def get_discovery_blacklist(state):
    return _db_get_discovery_blacklist()


def build_blacklist_item(release_id, *, title, title_ru=None, season=1, source="telegram"):
    return {
        "release_id": int(release_id),
        "title": str(title or "").strip(),
        "title_ru": str(title_ru or "").strip() or None,
        "season": int(season or 1),
        "added_at": utc_now_iso(),
        "source": str(source or "telegram").strip() or "telegram",
    }


def find_blacklist_item(state, release_id):
    return _db_find_blacklist_item(release_id)


def add_release_to_blacklist(state, blacklist_item):
    release_id = _parse_positive_int((blacklist_item or {}).get("release_id"))
    if release_id is None:
        raise RuntimeError("missing_release_id_for_blacklist")
    already = _db_add_to_blacklist(dict(blacklist_item))
    return state, already


def remove_release_from_blacklist(state, release_id):
    release_id = _parse_positive_int(release_id)
    if release_id is None:
        raise RuntimeError("missing_release_id_for_blacklist")
    return state, _db_remove_from_blacklist(release_id)


def queue_discovered_job(jobs, candidate_job):
    existing_job = find_matching_job(jobs, candidate_job)
    if existing_job is None:
        jobs.append(candidate_job)
        return "created"

    if existing_job != candidate_job:
        existing_job.clear()
        existing_job.update(candidate_job)
        return "updated"

    return "unchanged"


def discover_jobs(config, jobs, state):
    automation = normalize_automation_config(config)
    updated_jobs = deepcopy(jobs)
    updated_state = deepcopy(state)
    updated_state.setdefault("schema_version", 3)
    updated_state.setdefault("job_index", {})
    updated_state.setdefault("skipped_items", [])
    updated_state.setdefault("ongoing_progress", {})
    updated_state.setdefault("discovery_blacklist", [])
    _get_episode_tracking_maps(updated_state)

    if not automation.get("enabled", True):
        updated_state["job_index"] = build_default_job_index(updated_jobs)
        return {
            "jobs": updated_jobs,
            "state": updated_state,
            "summary": {
                "created_jobs": 0,
                "updated_jobs": 0,
                "skipped_items": len(updated_state["skipped_items"]),
                "queued_release_episodes": len(updated_state["queued_release_episodes"]),
                "completed_release_episodes": len(updated_state["completed_release_episodes"]),
                "blacklisted_releases": len(updated_state["discovery_blacklist"]),
                "request_urls": [],
                "status": "disabled",
            },
        }

    releases_result = list_recent_releases(limit=automation.get("poll_limit", 50))
    created_jobs = 0
    updated_jobs_count = 0

    for release_stub in releases_result["releases"]:
        if not isinstance(release_stub, dict):
            continue
        if not release_stub.get("is_ongoing", False):
            continue

        release_id_or_alias = release_stub.get("alias") or release_stub.get("id") or release_stub.get("release_id")
        if not release_id_or_alias:
            continue

        release_details = get_release_details(release_id_or_alias)
        release_payload = release_details["release"]
        release_id = release_payload.get("id") or release_stub.get("id") or release_stub.get("release_id")
        if release_id is None:
            continue
        if _db_find_blacklist_item(release_id) is not None:
            _db_record_skipped_item({
                "release_id": release_id,
                "alias": release_payload.get("alias"),
                "title": extract_release_title(release_payload),
                "episodes": [],
                "reason": "blacklisted_release",
                "recorded_at": utc_now_iso(),
            })
            continue

        try:
            selected_variant = select_release_source_variant(release_payload)
        except RuntimeError as exc:
            _db_record_skipped_item({
                "release_id": release_id,
                "alias": release_payload.get("alias"),
                "title": extract_release_title(release_payload),
                "episodes": [],
                "reason": str(exc),
                "recorded_at": utc_now_iso(),
            })
            continue

        episode_numbers = list(selected_variant["available_episodes"])
        queued_release_episodes, completed_release_episodes = _get_episode_tracking_maps(updated_state)
        tracked_episode_keys = set(queued_release_episodes).union(completed_release_episodes)
        new_episode_numbers = [
            episode_number
            for episode_number in episode_numbers
            if build_seen_episode_key(release_id, episode_number) not in tracked_episode_keys
        ]

        if not new_episode_numbers:
            continue

        try:
            base_job = build_job_from_release(
                release_payload,
                episode_numbers,
                automation,
                selected_variant=selected_variant,
            )
        except RuntimeError as exc:
            _db_record_skipped_item({
                "release_id": release_id,
                "alias": release_payload.get("alias"),
                "title": extract_release_title(release_payload),
                "episodes": list(new_episode_numbers),
                "reason": str(exc),
                "recorded_at": utc_now_iso(),
            })
            continue

        ongoing_progress_key = build_ongoing_progress_key_from_job(base_job)
        ongoing_progress = _db_load_ongoing_progress().get(ongoing_progress_key, {})
        has_full_publish = bool(ongoing_progress.get("has_full_publish"))
        release_jobs = []
        existing_compilation_job = find_matching_job(updated_jobs, {
            **base_job,
            "episodes_range": format_episodes_range(episode_numbers),
            "processing_mode": "compilation",
        })

        if has_full_publish:
            latest_episode = max(new_episode_numbers)
            release_jobs.append(build_job_from_release(
                release_payload,
                [latest_episode],
                automation,
                selected_variant=selected_variant,
                processing_mode="single_episode",
                automation_context=build_discovery_job_context(
                    provider=automation.get("provider", "aniliberty"),
                    release_id=release_id,
                    is_ongoing=True,
                    ongoing_progress_key=ongoing_progress_key,
                    publish_strategy="single_update",
                ),
            ))
            release_jobs.append(build_job_from_release(
                release_payload,
                episode_numbers,
                automation,
                selected_variant=selected_variant,
                processing_mode="compilation",
                automation_context=build_discovery_job_context(
                    provider=automation.get("provider", "aniliberty"),
                    release_id=release_id,
                    is_ongoing=True,
                    ongoing_progress_key=ongoing_progress_key,
                    publish_strategy="full_refresh",
                ),
            ))
        else:
            full_episodes = list(episode_numbers)
            if existing_compilation_job is not None:
                full_episodes = sorted(
                    parse_episodes_range(existing_compilation_job["episodes_range"]).union(
                        int(episode_number) for episode_number in episode_numbers
                    ),
                )
            release_jobs.append(build_job_from_release(
                release_payload,
                full_episodes,
                automation,
                selected_variant=selected_variant,
                processing_mode="compilation",
                automation_context=build_discovery_job_context(
                    provider=automation.get("provider", "aniliberty"),
                    release_id=release_id,
                    is_ongoing=True,
                    ongoing_progress_key=ongoing_progress_key,
                    publish_strategy="initial_full",
                ),
            ))

        for candidate_job in release_jobs:
            queue_result = queue_discovered_job(updated_jobs, candidate_job)
            if queue_result == "created":
                created_jobs += 1
            elif queue_result == "updated":
                updated_jobs_count += 1
            mark_job_episodes_queued(updated_state, candidate_job)

    updated_state["last_discovery_at"] = utc_now_iso()
    updated_state["job_index"] = build_default_job_index(updated_jobs)
    return {
        "jobs": updated_jobs,
        "state": updated_state,
        "summary": {
            "created_jobs": created_jobs,
            "updated_jobs": updated_jobs_count,
            "skipped_items": len(updated_state["skipped_items"]),
            "queued_release_episodes": len(updated_state["queued_release_episodes"]),
            "completed_release_episodes": len(updated_state["completed_release_episodes"]),
            "blacklisted_releases": len(updated_state["discovery_blacklist"]),
            "request_urls": releases_result.get("request_urls", []),
        },
    }


def find_matching_job(jobs, candidate_job):
    candidate_key = build_job_key(candidate_job)
    for job in jobs:
        if build_job_key(job) == candidate_key:
            return job

    candidate_title = str(candidate_job.get("title", "")).strip().lower()
    candidate_season = str(candidate_job.get("season", "")).strip()
    candidate_source_type = candidate_job.get("source", {}).get("type", "").strip().lower()
    candidate_processing_mode = get_job_processing_mode(candidate_job)
    candidate_variant_identity = build_source_variant_identity(candidate_job.get("source", {}))

    for job in jobs:
        job_variant_identity = build_source_variant_identity(job.get("source", {}))
        if (
            str(job.get("title", "")).strip().lower() == candidate_title
            and str(job.get("season", "")).strip() == candidate_season
            and job.get("source", {}).get("type", "").strip().lower() == candidate_source_type
            and get_job_processing_mode(job) == candidate_processing_mode
            and (
                job_variant_identity == candidate_variant_identity
                or not job_variant_identity
                or not candidate_variant_identity
            )
        ):
            return job
    return None

def build_job_from_release(
    release_payload,
    new_episode_numbers,
    automation,
    *,
    selected_variant=None,
    processing_mode="compilation",
    automation_context=None,
):
    title = extract_release_title(release_payload)
    title_ru = extract_release_title_ru(release_payload)
    mal_id = extract_release_mal_id(release_payload)

    source_type = automation.get("default_source_type", "magnet")
    if source_type != "magnet":
        raise RuntimeError(f"unsupported_source_type:{source_type}")

    variant = selected_variant or select_release_source_variant(release_payload)
    magnet = variant.get("magnet")
    if not magnet:
        raise RuntimeError("missing_magnet")

    slug = ensure_non_empty_slug(title)
    download_root = Path(automation.get("download_root", "./downloads"))
    download_dir = download_root / slug

    job = {
        "title": title,
        "season": extract_release_season(release_payload),
        "episodes_range": format_episodes_range(new_episode_numbers),
        "processing_mode": processing_mode,
        "source": {
            "type": "magnet",
            "magnet": magnet,
            "download_dir": str(download_dir).replace("\\", "/"),
            "variant_codec": variant.get("codec"),
            "variant_label": variant.get("label"),
        },
    }
    if title_ru:
        job["title_ru"] = title_ru
    if mal_id is not None:
        job["mal_id"] = mal_id
    if automation_context:
        job["automation"] = automation_context
    return job


def collect_release_episode_numbers(release_payload):
    episodes = release_payload.get("episodes")
    if not isinstance(episodes, list):
        return []

    numbers = set()
    for item in episodes:
        if not isinstance(item, dict):
            continue
        candidate_number = item.get("number") or item.get("episode") or item.get("ordinal")
        if candidate_number is None:
            continue
        try:
            parsed_number = int(candidate_number)
        except (TypeError, ValueError):
            continue
        if parsed_number <= 0:
            continue
        numbers.add(parsed_number)

    return sorted(numbers)


def _parse_variant_codec(value):
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(marker in text for marker in ["avc", "x264", "h.264", "h264"]):
        return "avc"
    if any(marker in text for marker in ["hevc", "x265", "h.265", "h265"]):
        return "hevc"
    return None


def _parse_variant_resolution(payload):
    candidates = [
        payload.get("resolution"),
        payload.get("quality"),
        payload.get("video_quality"),
        payload.get("videoQuality"),
    ]
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_variant_label(payload):
    candidates = [
        payload.get("label"),
        payload.get("quality_label"),
        payload.get("qualityLabel"),
        payload.get("title"),
        payload.get("name"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    codec = _parse_variant_codec(
        payload.get("codec")
        or payload.get("video_codec")
        or payload.get("videoCodec")
        or payload.get("label")
        or payload.get("title")
        or payload.get("name")
    )
    resolution = _parse_variant_resolution(payload)
    parts = [part for part in [codec.upper() if codec else None, resolution] if part]
    return " ".join(parts) if parts else None


def _extract_variant_codec(payload):
    for value in [
        payload.get("codec"),
        payload.get("video_codec"),
        payload.get("videoCodec"),
        payload.get("label"),
        payload.get("title"),
        payload.get("name"),
    ]:
        codec = _parse_variant_codec(value)
        if codec:
            return codec
    return None


def _extract_variant_label_episode_numbers(label):
    text = str(label or "").strip()
    if not text:
        return None

    matches = re.findall(r"\[(\d{1,3})(?:\s*-\s*(\d{1,3}))?\]", text)
    if not matches:
        return None

    for start_raw, end_raw in reversed(matches):
        start = int(start_raw)
        end = int(end_raw) if end_raw else start
        if start <= 0 or end <= 0 or start > end:
            continue
        return list(range(start, end + 1))

    return None


def _iter_release_variant_payloads(release_payload):
    variants = []
    seen = set()

    def push(candidate):
        if not isinstance(candidate, dict):
            return
        marker = id(candidate)
        if marker in seen:
            return
        seen.add(marker)
        variants.append(candidate)

    torrents = release_payload.get("torrents")
    if isinstance(torrents, list):
        for item in torrents:
            push(item)
    elif isinstance(torrents, dict):
        for value in torrents.values():
            if isinstance(value, list):
                for item in value:
                    push(item)
            else:
                push(value)

    for key in ["qualities", "quality", "variants", "versions"]:
        value = release_payload.get(key)
        if isinstance(value, list):
            for item in value:
                push(item)
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, list):
                    for item in nested:
                        push(item)
                else:
                    push(nested)

    legacy_magnet = None
    torrent = release_payload.get("torrent")
    if isinstance(torrent, dict):
        legacy_magnet = _find_magnet_value(torrent)

    if legacy_magnet:
        variants.append({
            "codec": "avc",
            "label": "legacy",
            "magnet": legacy_magnet,
            "episodes": release_payload.get("episodes"),
        })

    return variants


def extract_release_source_variants(release_payload):
    variants = []
    deduped = set()
    release_episodes = collect_release_episode_numbers(release_payload)

    for candidate in _iter_release_variant_payloads(release_payload):
        magnet = _find_magnet_value(candidate)
        codec = _extract_variant_codec(candidate)
        label = _extract_variant_label(candidate)
        label_episodes = _extract_variant_label_episode_numbers(label)
        episodes = release_episodes
        if label_episodes:
            label_episode_set = set(label_episodes)
            episodes = [
                episode_number
                for episode_number in release_episodes
                if episode_number in label_episode_set
            ]
        resolution = _parse_variant_resolution(candidate)

        if not magnet or not codec or not episodes:
            continue

        identity = (magnet, codec, tuple(episodes))
        if identity in deduped:
            continue
        deduped.add(identity)
        variants.append({
            "codec": codec,
            "resolution": resolution,
            "magnet": magnet,
            "available_episodes": episodes,
            "label": label,
        })

    return variants


def select_release_source_variant(release_payload):
    variants = extract_release_source_variants(release_payload)
    if not variants:
        raise RuntimeError("no_supported_torrent_variant")

    for preferred_codec in ["avc", "hevc"]:
        preferred = [
            variant
            for variant in variants
            if variant["codec"] == preferred_codec and variant.get("magnet") and variant.get("available_episodes")
        ]
        if preferred:
            preferred.sort(
                key=lambda item: (
                    -max(item["available_episodes"]),
                    -len(item["available_episodes"]),
                    str(item.get("resolution") or ""),
                    str(item.get("label") or ""),
                )
            )
            return preferred[0]

    raise RuntimeError("no_supported_torrent_variant")


def extract_release_title(release_payload):
    names = release_payload.get("name") or release_payload.get("names") or {}
    if isinstance(names, dict):
        for key in ["english", "en", "main", "ru", "alternative"]:
            value = names.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    title = release_payload.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()

    alias = release_payload.get("alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()

    raise RuntimeError("missing_title")


def extract_release_title_ru(release_payload):
    names = release_payload.get("name") or release_payload.get("names") or {}
    if isinstance(names, dict):
        for key in ["main", "ru"]:
            value = names.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_release_season(release_payload):
    season = release_payload.get("season_number") or release_payload.get("seasonNumber")
    try:
        if season is not None:
            return int(season)
    except (TypeError, ValueError):
        pass
    return 1


def extract_release_mal_id(release_payload):
    direct_keys = [
        "mal_id",
        "malId",
        "myanimelist_id",
        "myanimelistId",
    ]
    for key in direct_keys:
        value = release_payload.get(key)
        parsed = _parse_positive_int(value)
        if parsed is not None:
            return parsed

    nested_candidates = [
        ("external_ids", "mal_id"),
        ("external_ids", "malId"),
        ("external_ids", "myanimelist"),
        ("external_ids", "myanimelist_id"),
        ("externalIds", "mal_id"),
        ("externalIds", "myanimelist"),
        ("codes", "mal"),
        ("codes", "mal_id"),
        ("player", "mal_id"),
        ("player", "myanimelist"),
        ("metadata", "mal_id"),
    ]
    for parent_key, child_key in nested_candidates:
        parent = release_payload.get(parent_key)
        if not isinstance(parent, dict):
            continue
        parsed = _parse_positive_int(parent.get(child_key))
        if parsed is not None:
            return parsed

    return None


def _parse_positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def extract_release_magnet(release_payload):
    return _find_magnet_value(release_payload)


def _find_magnet_value(payload):
    if isinstance(payload, str):
        value = payload.strip()
        if value.startswith("magnet:?"):
            return value
        return None

    if isinstance(payload, dict):
        for value in payload.values():
            magnet = _find_magnet_value(value)
            if magnet:
                return magnet

    if isinstance(payload, list):
        for value in payload:
            magnet = _find_magnet_value(value)
            if magnet:
                return magnet

    return None


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()
