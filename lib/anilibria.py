import os
import re

import requests


API_BASE_URL = "https://aniliberty.top/api/v1"
TORRENTS_PAGE_URLS = [
    "https://www.anilibria.top/anime/torrents",
    "https://aniliberty.top/anime/torrents",
    "https://anilibria.top/anime/torrents",
]
DEFAULT_HEADERS = {
    "User-Agent": "workspace-gojo-satoru/1.0 (+https://aniliberty.top)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}


def get_anilibria_proxy_url():
    return (os.getenv("ANILIBERTY_PROXY_URL") or "").strip()


def _build_request_kwargs(timeout, params=None):
    kwargs = {
        "timeout": timeout,
        "headers": DEFAULT_HEADERS,
    }
    if params is not None:
        kwargs["params"] = params

    proxy_url = get_anilibria_proxy_url()
    if proxy_url:
        kwargs["proxies"] = {
            "http": proxy_url,
            "https": proxy_url,
        }
    return kwargs


def _extract_names(payload):
    values = []

    names = payload.get("names") or payload.get("name") or {}
    if isinstance(names, dict):
        for key in ["ru", "en", "english", "alternative", "main"]:
            value = names.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())

    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        values.append(title.strip())

    alias = payload.get("alias")
    if isinstance(alias, str) and alias.strip():
        values.append(alias.strip())

    deduped = []
    seen = set()
    for value in values:
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(value)
    return deduped


def _normalize_skip_interval(skip_type, payload):
    if not isinstance(payload, dict):
        return None

    start = (
        payload.get("start")
        or payload.get("from")
        or payload.get("startTime")
        or payload.get("start_time")
    )
    end = (
        payload.get("end")
        or payload.get("to")
        or payload.get("stop")
        or payload.get("endTime")
        or payload.get("end_time")
    )
    if start is None or end is None:
        return None

    try:
        return {
            "type": skip_type,
            "start": float(start),
            "end": float(end),
            "source": "anilibria_exact",
            "confidence": "high",
        }
    except (TypeError, ValueError):
        return None


def _collect_skip_segments(payload):
    if not isinstance(payload, dict):
        return []

    candidates = []
    skips = payload.get("skips")
    if isinstance(skips, dict):
        candidates.append(skips)
    candidates.append(payload)

    segments = []
    for item in candidates:
        for skip_type, aliases in {
            "op": ["op", "opening"],
            "ed": ["ed", "ending"],
        }.items():
            for alias in aliases:
                if alias not in item:
                    continue
                segment = _normalize_skip_interval(skip_type, item.get(alias))
                if segment:
                    segments.append(segment)
                    break

    deduped = {}
    for segment in segments:
        deduped[(segment["type"], segment["start"], segment["end"])] = segment
    return sorted(deduped.values(), key=lambda item: (item["start"], item["type"]))


def _match_title(payload, title, aliases):
    wanted = {title.strip().lower()}
    wanted.update(
        alias.strip().lower()
        for alias in aliases
        if isinstance(alias, str) and alias.strip()
    )
    available = {name.lower() for name in _extract_names(payload)}
    return bool(wanted & available)


def _request(url, params=None):
    response = requests.get(url, **_build_request_kwargs(timeout=20, params=params))
    response.raise_for_status()
    return response.json(), response.url


def _request_text(url, params=None):
    response = requests.get(url, **_build_request_kwargs(timeout=30, params=params))
    response.raise_for_status()
    return response.text, response.url


def _normalize_release_list(data):
    releases = []
    for candidate in _iter_release_candidates(data):
        if isinstance(candidate, dict):
            releases.append(candidate)
    return releases


def _extract_release_aliases_from_torrents_page(html):
    aliases = []
    seen = set()
    patterns = [
        r"/anime/releases/release/([^/\"'?]+)",
        r"/anime/releases/(?!release/)([^/\"'?]+)",
    ]
    for pattern in patterns:
        for alias in re.findall(pattern, html or ""):
            normalized = alias.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            aliases.append(normalized)
    return aliases


def _build_recent_releases_from_torrents_page(limit, urls, errors):
    aliases = []
    for page_url in TORRENTS_PAGE_URLS:
        try:
            html, request_url = _request_text(page_url)
            urls.append(str(request_url))
            aliases = _extract_release_aliases_from_torrents_page(html)
            if aliases:
                break
        except requests.RequestException as exc:
            errors.append(f"torrents_page: {exc}")

    if not aliases:
        return []

    releases = []
    for alias in aliases:
        if len(releases) >= int(limit):
            break
        try:
            release_details = get_release_details(alias)
            urls.append(release_details["request_url"])
            release_payload = release_details["release"]
            if isinstance(release_payload, dict):
                releases.append(release_payload)
        except requests.RequestException as exc:
            errors.append(f"anime/releases/{alias}: {exc}")

    return releases


def _build_recent_releases_from_api(limit, urls, errors):
    api_attempts = [
        ("anime/releases/latest", {"limit": int(limit)}),
        ("anime/releases/latest", None),
    ]

    for path, params in api_attempts:
        try:
            data, request_url = _request(f"{API_BASE_URL}/{path}", params=params)
            urls.append(str(request_url))
        except requests.RequestException as exc:
            errors.append(f"{path}: {exc}")
            continue

        releases = _normalize_release_list(data)
        if releases:
            return releases

    return []


def list_recent_releases(limit=25):
    urls = []
    errors = []
    releases = _build_recent_releases_from_api(limit, urls, errors)
    if not releases:
        releases = _build_recent_releases_from_torrents_page(limit, urls, errors)

    if not releases:
        error = "; ".join(errors) if errors else "no_releases_found"
        raise RuntimeError(f"AniLibria recent releases lookup failed: {error}")

    def sort_key(item):
        return (
            item.get("fresh_at")
            or item.get("updated_at")
            or item.get("created_at")
            or ""
        )

    releases.sort(key=sort_key, reverse=True)
    return {
        "releases": releases[: int(limit)],
        "request_urls": urls,
    }


def _iter_release_candidates(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ["data", "items", "list", "releases"]:
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    return []


def _find_release(title, season, aliases):
    urls = []
    errors = []
    candidates = []

    search_attempts = [
        ("app/search/releases", {"query": title}),
        ("anime/releases/list", {"aliases": ",".join(filter(None, [title, *aliases]))}),
    ]

    for path, params in search_attempts:
        try:
            data, request_url = _request(f"{API_BASE_URL}/{path}", params=params)
            urls.append(str(request_url))
        except requests.RequestException as exc:
            errors.append(f"{path}: {exc}")
            continue

        candidates.extend(_iter_release_candidates(data))

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if not _match_title(candidate, title, aliases):
            continue
        if season is not None:
            season_value = candidate.get("season_number")
            if season_value is not None and str(season_value) != str(season):
                continue
        release_id = candidate.get("id") or candidate.get("release_id")
        release_alias = candidate.get("alias")
        if release_id is not None or release_alias:
            return candidate, urls, None

    error = "; ".join(errors) if errors else "release_not_found"
    return None, urls, error


def _get_release_payload(id_or_alias):
    data, request_url = _request(f"{API_BASE_URL}/anime/releases/{id_or_alias}")
    return data, str(request_url)


def get_release_details(id_or_alias):
    payload, request_url = _get_release_payload(id_or_alias)
    return {
        "release": payload,
        "request_url": request_url,
    }


def _find_episode_payload(release_payload, episode_number):
    episodes = release_payload.get("episodes")
    if not isinstance(episodes, list):
        return None

    for candidate in episodes:
        if not isinstance(candidate, dict):
            continue
        candidate_number = (
            candidate.get("number")
            or candidate.get("episode")
            or candidate.get("ordinal")
        )
        if candidate_number is None:
            continue
        try:
            if int(candidate_number) == int(episode_number):
                return candidate
        except (TypeError, ValueError):
            continue
    return None


def get_anilibria_segments(title, season, episode_number, source=None, aliases=None):
    aliases = aliases or []
    request_urls = []

    release_stub, release_urls, release_error = _find_release(title, season, aliases)
    request_urls.extend(release_urls)
    if release_stub is None:
        return {
            "segments": [],
            "request_error": f"AniLibria release lookup failed: {release_error}",
            "request_urls": request_urls,
            "provider": "anilibria",
        }

    release_id_or_alias = release_stub.get("alias") or release_stub.get("id") or release_stub.get("release_id")
    try:
        release_details = get_release_details(release_id_or_alias)
        release_payload = release_details["release"]
        request_urls.append(release_details["request_url"])
    except requests.RequestException as exc:
        return {
            "segments": [],
            "request_error": f"AniLibria release details failed: {exc}",
            "request_urls": request_urls,
            "provider": "anilibria",
        }

    episode_payload = _find_episode_payload(release_payload, episode_number)
    if episode_payload is None:
        return {
            "segments": [],
            "request_error": "AniLibria episode lookup failed: episode_not_found",
            "request_urls": request_urls,
            "provider": "anilibria",
        }

    segments = _collect_skip_segments(episode_payload)
    if not segments:
        segments = _collect_skip_segments(release_payload)

    return {
        "segments": segments,
        "request_error": None if segments else "AniLibria returned no skip data",
        "request_urls": request_urls,
        "provider": "anilibria",
    }
