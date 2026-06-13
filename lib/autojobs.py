from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from lib.anilibria import get_release_details, list_recent_releases
from lib.config import normalize_automation_config
from lib.helpers import ensure_non_empty_slug, parse_episodes_range


def build_seen_episode_key(release_id, episode_number):
    return f"{release_id}:{int(episode_number):03d}"


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

    return "|".join([
        str(job.get("title", "")).strip().lower(),
        str(job.get("season", "")).strip(),
        get_job_processing_mode(job),
        str(source_type).strip().lower(),
        str(source_signature).strip(),
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


def mark_ongoing_full_publish(state, job):
    progress = dict(state.get("ongoing_progress", {}))
    key = build_ongoing_progress_key_from_job(job)
    if not key:
        return state

    episodes = sorted(parse_episodes_range(job.get("episodes_range", "")))
    progress[key] = {
        "has_full_publish": True,
        "last_full_episode": max(episodes) if episodes else None,
        "last_full_range": job.get("episodes_range"),
        "updated_at": utc_now_iso(),
    }
    state["ongoing_progress"] = progress
    return state


def build_discovery_job_context(*, provider, release_id, is_ongoing, ongoing_progress_key, publish_strategy):
    return {
        "provider": provider,
        "release_id": release_id,
        "is_ongoing": bool(is_ongoing),
        "ongoing_progress_key": ongoing_progress_key,
        "publish_strategy": publish_strategy,
    }


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
    updated_state.setdefault("schema_version", 1)
    updated_state.setdefault("seen_release_episodes", {})
    updated_state.setdefault("job_index", {})
    updated_state.setdefault("skipped_items", [])
    updated_state.setdefault("ongoing_progress", {})

    if not automation.get("enabled", True):
        updated_state["job_index"] = build_default_job_index(updated_jobs)
        return {
            "jobs": updated_jobs,
            "state": updated_state,
            "summary": {
                "created_jobs": 0,
                "updated_jobs": 0,
                "skipped_items": len(updated_state["skipped_items"]),
                "seen_release_episodes": len(updated_state["seen_release_episodes"]),
                "request_urls": [],
                "status": "disabled",
            },
        }

    releases_result = list_recent_releases(limit=automation.get("poll_limit", 25))
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

        episode_numbers = collect_release_episode_numbers(release_payload)
        new_episode_numbers = [
            episode_number
            for episode_number in episode_numbers
            if build_seen_episode_key(release_id, episode_number) not in updated_state["seen_release_episodes"]
        ]

        if not new_episode_numbers:
            continue

        try:
            base_job = build_job_from_release(release_payload, episode_numbers, automation)
        except RuntimeError as exc:
            mark_release_episodes_seen(updated_state, release_id, new_episode_numbers)
            updated_state["skipped_items"].append({
                "release_id": release_id,
                "alias": release_payload.get("alias"),
                "title": extract_release_title(release_payload),
                "episodes": list(new_episode_numbers),
                "reason": str(exc),
                "recorded_at": utc_now_iso(),
            })
            continue

        ongoing_progress_key = build_ongoing_progress_key_from_job(base_job)
        ongoing_progress = updated_state["ongoing_progress"].get(ongoing_progress_key, {})
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

        mark_release_episodes_seen(updated_state, release_id, new_episode_numbers)

    updated_state["last_discovery_at"] = utc_now_iso()
    updated_state["job_index"] = build_default_job_index(updated_jobs)
    return {
        "jobs": updated_jobs,
        "state": updated_state,
        "summary": {
            "created_jobs": created_jobs,
            "updated_jobs": updated_jobs_count,
            "skipped_items": len(updated_state["skipped_items"]),
            "seen_release_episodes": len(updated_state["seen_release_episodes"]),
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

    for job in jobs:
        if (
            str(job.get("title", "")).strip().lower() == candidate_title
            and str(job.get("season", "")).strip() == candidate_season
            and job.get("source", {}).get("type", "").strip().lower() == candidate_source_type
            and get_job_processing_mode(job) == candidate_processing_mode
        ):
            return job
    return None


def mark_release_episodes_seen(state, release_id, episode_numbers):
    for episode_number in episode_numbers:
        state["seen_release_episodes"][build_seen_episode_key(release_id, episode_number)] = {
            "release_id": release_id,
            "episode": int(episode_number),
            "seen_at": utc_now_iso(),
        }


def build_job_from_release(
    release_payload,
    new_episode_numbers,
    automation,
    *,
    processing_mode="compilation",
    automation_context=None,
):
    title = extract_release_title(release_payload)
    title_ru = extract_release_title_ru(release_payload)
    mal_id = extract_release_mal_id(release_payload)

    source_type = automation.get("default_source_type", "magnet")
    if source_type != "magnet":
        raise RuntimeError(f"unsupported_source_type:{source_type}")

    magnet = extract_release_magnet(release_payload)
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
