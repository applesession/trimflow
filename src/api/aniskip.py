import requests


def _request_aniskip_segments(mal_id, episode_number, episode_length, skip_types):
    types_query = "&".join([f"types={skip_type}" for skip_type in skip_types])

    url = (
        f"https://api.aniskip.com/v2/skip-times/"
        f"{mal_id}/{episode_number}"
        f"?{types_query}&episodeLength={episode_length}"
    )

    print(f"[AniSkip] {url}")

    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        results = []

        for item in data.get("results", []):
            interval = item.get("interval", {})
            start = interval.get("startTime")
            end = interval.get("endTime")
            skip_type = item.get("skipType") or item.get("skip_type")

            if start is not None and end is not None:
                results.append({
                    "type": skip_type,
                    "start": float(start),
                    "end": float(end),
                })

        results.sort(key=lambda item: item["start"])
        return {
            "segments": results,
            "error": None,
            "requested_episode_length": float(episode_length),
            "request_url": url,
        }

    except requests.RequestException as exc:
        error = f"AniSkip request failed: {exc}"
    except ValueError as exc:
        error = f"AniSkip returned invalid JSON: {exc}"
    except Exception as exc:
        error = f"AniSkip unexpected error: {exc}"

    print("[AniSkip ERROR]", error)
    return {
        "segments": [],
        "error": error,
        "requested_episode_length": float(episode_length),
        "request_url": url,
    }


def _group_segments_by_type(segments):
    grouped = {}
    for segment in segments:
        grouped[segment["type"]] = {
            **segment,
            "source": segment["source"],
            "confidence": segment["confidence"],
        }
    return grouped


def get_aniskip_segments(mal_id, episode_number, episode_length, skip_types):
    primary_result = _request_aniskip_segments(
        mal_id=mal_id,
        episode_number=episode_number,
        episode_length=episode_length,
        skip_types=skip_types,
    )

    exact_segments = [
        {
            **segment,
            "source": "aniskip_exact",
            "confidence": "high",
        }
        for segment in primary_result["segments"]
    ]
    segments_by_type = _group_segments_by_type(exact_segments)
    request_urls = [primary_result["request_url"]]
    errors = []

    if primary_result["error"]:
        errors.append(primary_result["error"])
        return {
            "segments": list(segments_by_type.values()),
            "per_type_sources": {
                skip_type: segments_by_type.get(skip_type, {}).get("source", "not_found")
                for skip_type in skip_types
            },
            "used_fallback": False,
            "request_error": "; ".join(errors),
            "requested_episode_length": float(episode_length),
            "fallback_from_episode_length": None,
            "request_urls": request_urls,
        }

    missing_types = [
        skip_type
        for skip_type in skip_types
        if skip_type not in segments_by_type
    ]

    fallback_used = False
    fallback_from_episode_length = None

    if missing_types:
        fallback_used = True
        fallback_from_episode_length = float(episode_length)
        fallback_result = _request_aniskip_segments(
            mal_id=mal_id,
            episode_number=episode_number,
            episode_length=0,
            skip_types=missing_types,
        )
        request_urls.append(fallback_result["request_url"])

        if fallback_result["error"]:
            errors.append(fallback_result["error"])
        else:
            fallback_segments = [
                {
                    **segment,
                    "source": "aniskip_lengthless",
                    "confidence": "high",
                }
                for segment in fallback_result["segments"]
            ]
            for segment in fallback_segments:
                segments_by_type.setdefault(segment["type"], segment)

    return {
        "segments": list(segments_by_type.values()),
        "per_type_sources": {
            skip_type: segments_by_type.get(skip_type, {}).get("source", "not_found")
            for skip_type in skip_types
        },
        "used_fallback": fallback_used,
        "request_error": "; ".join(errors) if errors else None,
        "requested_episode_length": float(episode_length),
        "fallback_from_episode_length": fallback_from_episode_length,
        "request_urls": request_urls,
    }


def summarize_skips(remove_segments, skip_types, per_type_info, request_error=None):
    total_removed = 0.0
    warnings = []

    for segment in remove_segments:
        start = float(segment.get("start", 0))
        end = float(segment.get("end", 0))
        total_removed += max(0.0, end - start)

    for skip_type in skip_types:
        type_info = per_type_info.get(skip_type, {})
        source = type_info.get("source", "not_found")
        if source == "not_found":
            warnings.append(f"{skip_type.upper()} not found")
        elif type_info.get("review_required"):
            confidence = type_info.get("confidence", "unknown")
            warnings.append(f"{skip_type.upper()} requires manual review ({confidence})")

    if request_error:
        warnings.append(request_error)

    summary = {
        "total_removed_seconds": round(total_removed, 2),
        "warnings": warnings,
    }

    for skip_type in skip_types:
        type_info = per_type_info.get(skip_type, {})
        summary[skip_type] = bool(type_info.get("removed"))
        summary[f"{skip_type}_source"] = type_info.get("source", "not_found")
        summary[f"{skip_type}_confidence"] = type_info.get("confidence", "none")

    return summary


def print_skip_log(episode_number, skip_summary, skip_types, review_required=False):
    parts = []

    for skip_type in skip_types:
        removed = skip_summary.get(skip_type)
        source = skip_summary.get(f"{skip_type}_source", "not_found")
        confidence = skip_summary.get(f"{skip_type}_confidence", "none")
        parts.append(
            f"{skip_type.upper()} {'✅' if removed else '⚠️'} "
            f"[{source}/{confidence}]"
        )

    warning_text = ""
    if skip_summary["warnings"]:
        warning_text = " | " + ", ".join(skip_summary["warnings"])

    review_text = " | manual_review" if review_required else ""

    print(
        f"[SKIP] EP{episode_number:03d} | "
        f"{' | '.join(parts)} | "
        f"removed {skip_summary['total_removed_seconds']}s"
        f"{review_text}"
        f"{warning_text}"
    )


def build_quality_summary(manifest_episodes, skip_types):
    summary = {
        "episodes_count": len(manifest_episodes),
        "episodes_with_warnings": [],
        "episodes_audio_recovery": [],
        "episodes_anilibria_only": 0,
        "episodes_anilibria_with_detector": 0,
        "episodes_aniskip_only": 0,
        "episodes_aniskip_with_detector": 0,
        "episodes_detector_only": 0,
        "episodes_manual_review": 0,
        "episodes_detector_completed_op_only": 0,
        "episodes_detector_completed_ed_only": 0,
        "episodes_detector_high": 0,
        "episodes_detector_medium": 0,
        "episodes_detector_low": 0,
        "episodes_detector_cache_hits": 0,
    }

    for skip_type in skip_types:
        summary[f"episodes_with_{skip_type}_removed"] = 0

    for episode in manifest_episodes:
        skip_summary = episode["skip_summary"]
        timing_info = episode["timing_info"]

        for skip_type in skip_types:
            if skip_summary.get(skip_type):
                summary[f"episodes_with_{skip_type}_removed"] += 1

        if skip_summary.get("warnings"):
            summary["episodes_with_warnings"].append(episode["episode"])

        if (episode.get("audio_recovery") or {}).get("applied"):
            summary["episodes_audio_recovery"].append(episode["episode"])

        strategy = timing_info.get("strategy")
        if strategy == "anilibria_only":
            summary["episodes_anilibria_only"] += 1
        elif strategy == "anilibria_with_detector":
            summary["episodes_anilibria_with_detector"] += 1
        elif strategy == "aniskip_only":
            summary["episodes_aniskip_only"] += 1
        elif strategy == "aniskip_with_detector":
            summary["episodes_aniskip_with_detector"] += 1
        elif strategy == "detector_only":
            summary["episodes_detector_only"] += 1
        elif strategy == "manual_review":
            summary["episodes_manual_review"] += 1

        per_type = timing_info.get("per_type", {})
        op_from_detector = per_type.get("op", {}).get("source") == "audio_fingerprint"
        ed_from_detector = per_type.get("ed", {}).get("source") == "audio_fingerprint"
        detector_types = [
            per_type.get(skip_type, {})
            for skip_type in skip_types
            if per_type.get(skip_type, {}).get("source") == "audio_fingerprint"
        ]

        if op_from_detector and not ed_from_detector:
            summary["episodes_detector_completed_op_only"] += 1
        if ed_from_detector and not op_from_detector:
            summary["episodes_detector_completed_ed_only"] += 1

        if detector_types:
            best_confidence = "low"
            for item in detector_types:
                confidence = item.get("confidence", "low")
                if confidence == "high":
                    best_confidence = "high"
                    break
                if confidence == "medium":
                    best_confidence = "medium"

            if best_confidence == "high":
                summary["episodes_detector_high"] += 1
            elif best_confidence == "medium":
                summary["episodes_detector_medium"] += 1
            else:
                summary["episodes_detector_low"] += 1

            if any(item.get("cache_hit") for item in detector_types):
                summary["episodes_detector_cache_hits"] += 1

    return summary
