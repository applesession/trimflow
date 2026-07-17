from datetime import datetime, timezone

from shared.config import (
    deep_merge,
    load_completed_jobs,
    load_jobs,
    load_state,
    save_completed_jobs,
    save_jobs,
    save_state,
)
from modules.autojobs import get_job_processing_mode, get_job_release_id, mark_job_episodes_completed, mark_ongoing_full_publish
from shared.db import claim_job, remove_completed_job as _db_remove_completed_job, return_job_to_pending
from core.pipeline import process_job
from shared.runtime import (
    append_runtime_error,
    load_runtime_status,
    mark_runtime_job_finish,
    mark_runtime_job_start,
)
from shared.validation import (
    validate_required_env,
    validate_required_files,
    validate_required_tools,
)


def build_job_identity(job):
    source = job.get("source", {})
    source_type = str(source.get("type", "")).strip().lower()
    if source_type == "magnet":
        source_signature = str(source.get("magnet", "")).strip()
    elif source_type == "local":
        source_signature = str(source.get("input_dir", "")).strip()
    else:
        source_signature = ""

    return "|".join([
        str(job.get("title", "")).strip().lower(),
        str(job.get("season", "")).strip(),
        str(job.get("episodes_range", "")).strip(),
        get_job_processing_mode(job),
        source_type,
        source_signature,
    ])


def remove_job_from_queue(jobs, completed_job):
    target_identity = build_job_identity(completed_job)
    remaining = []
    removed = False
    for job in jobs:
        if not removed and build_job_identity(job) == target_identity:
            removed = True
            continue
        remaining.append(job)
    return remaining, removed


def is_job_completed(job_result):
    delivery_summary = job_result.get("delivery_summary", {})
    vk_summary = delivery_summary.get("vk", {})
    s3_summary = delivery_summary.get("s3", {})

    if vk_summary.get("enabled"):
        return bool(vk_summary.get("video_uploaded"))
    if s3_summary.get("enabled"):
        return bool(s3_summary.get("uploaded"))
    return bool(job_result.get("output_video"))


def build_completed_job_entry(job, job_result):
    vk_summary = job_result.get("delivery_summary", {}).get("vk", {})
    return {
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "job": job,
        "output_display_name": job_result.get("output_display_name"),
        "output_video": job_result.get("output_video"),
        "output_timestamps": job_result.get("output_timestamps"),
        "output_manifest": job_result.get("output_manifest"),
        "delivery_summary": job_result.get("delivery_summary", {}),
        "partial_vk": bool(
            vk_summary.get("video_uploaded")
            and (not vk_summary.get("post_created", False) or not vk_summary.get("comment_created", False))
        ),
    }


def get_job_publish_strategy(job):
    return str((job.get("automation") or {}).get("publish_strategy", "") or "").strip().lower()


def get_job_ongoing_progress_key(job):
    return str((job.get("automation") or {}).get("ongoing_progress_key", "") or "").strip()


def is_ongoing_compilation_job(job):
    automation = job.get("automation") or {}
    return (
        bool(automation.get("is_ongoing"))
        and get_job_processing_mode(job) == "compilation"
        and get_job_publish_strategy(job) in {"initial_full", "full_refresh"}
    )


def is_incremental_full_refresh_job(job):
    return get_job_publish_strategy(job) == "full_refresh"


def build_execution_priority(job):
    automation = job.get("automation") or {}
    is_ongoing = bool(automation.get("is_ongoing"))
    processing_mode = get_job_processing_mode(job)
    publish_strategy = get_job_publish_strategy(job)

    if is_ongoing and processing_mode == "single_episode":
        return 0
    if is_ongoing and publish_strategy == "full_refresh":
        return 1
    if is_ongoing:
        return 2
    return 3


def build_execution_order(jobs, defaults=None):
    defaults = defaults or {}
    merged_jobs = [deep_merge(defaults, job) for job in list(jobs or [])]
    return [
        item[2]
        for item in sorted(
            (
                (build_execution_priority(job), index, job)
                for index, job in enumerate(merged_jobs)
            ),
            key=lambda item: (item[0], item[1]),
        )
    ]


def update_state_after_successful_job(config, job):
    if get_job_release_id(job) is None and not is_ongoing_compilation_job(job):
        return
    state = load_state(config)
    updated_state = mark_job_episodes_completed(state, job)
    if is_ongoing_compilation_job(job):
        updated_state = mark_ongoing_full_publish(updated_state, job)
    save_state(config, updated_state)


def run_jobs(
    config,
    jobs,
    runtime_status_path=None,
    runtime_errors_path=None,
    log=None,
    on_job_success=None,
    on_job_failure=None,
):
    log = log or print
    active_jobs = list(jobs or [])

    if not active_jobs:
        log("No jobs found in jobs.json")
        return {
            "jobs_found": 0,
            "jobs_processed": 0,
            "jobs_failed": 0,
            "jobs_skipped": 0,
            "failed_titles": [],
        }

    defaults = config.get("defaults", {})
    merged_jobs = build_execution_order(active_jobs, defaults=defaults)
    dynamic_queue = any(job.get("_queue_id") is not None for job in active_jobs)

    if not dynamic_queue:
        validate_required_env(config, merged_jobs)
        validate_required_tools(config, merged_jobs)
        validate_required_files(config)

    completed_jobs = load_completed_jobs(config)
    blocked_full_refresh_keys = set()
    summary = {
        "jobs_found": len(active_jobs),
        "jobs_processed": 0,
        "jobs_failed": 0,
        "jobs_skipped": 0,
        "failed_titles": [],
    }
    attempted_queue_ids = set()
    seen_queue_ids = {job.get("_queue_id") for job in active_jobs if job.get("_queue_id") is not None}
    static_jobs = iter(merged_jobs)
    index = 0

    while True:
        if dynamic_queue:
            pending_jobs = [
                job
                for job in load_jobs(config, status="pending")
                if job.get("_queue_id") not in attempted_queue_ids
            ]
            seen_queue_ids.update(job.get("_queue_id") for job in pending_jobs)
            if not pending_jobs:
                break
            ordered_jobs = build_execution_order(pending_jobs, defaults=defaults)
            merged_job = ordered_jobs[0]
            queue_id = merged_job.get("_queue_id")
            total_jobs = summary["jobs_processed"] + summary["jobs_failed"] + len(ordered_jobs)
        else:
            try:
                merged_job = next(static_jobs)
            except StopIteration:
                break
            queue_id = None
            total_jobs = len(merged_jobs)

        index += 1
        if is_incremental_full_refresh_job(merged_job) and get_job_ongoing_progress_key(merged_job) in blocked_full_refresh_keys:
            log(f"SKIP JOB {index}/{total_jobs} after failed single publish: {merged_job['title']}")
            summary["jobs_skipped"] += 1
            if queue_id is not None:
                attempted_queue_ids.add(queue_id)
            continue

        if queue_id is not None and not claim_job(queue_id):
            continue

        log("\n" + "=" * 80)
        log(f"START JOB {index}/{total_jobs}: {merged_job['title']}")
        log("=" * 80)
        if runtime_status_path:
            mark_runtime_job_start(
                runtime_status_path,
                merged_job,
                current_job_index=index,
                total_jobs=total_jobs,
                jobs_processed=summary["jobs_processed"],
                jobs_failed=summary["jobs_failed"],
            )

        try:
            if dynamic_queue:
                validate_required_env(config, [merged_job])
                validate_required_tools(config, [merged_job])
                validate_required_files(config)
            job_result = process_job(merged_job, runtime_status_path=runtime_status_path)
            if is_job_completed(job_result):
                _db_remove_completed_job(merged_job)
                completed_jobs.append(build_completed_job_entry(merged_job, job_result))
                save_completed_jobs(config, completed_jobs)
                update_state_after_successful_job(config, merged_job)
            elif queue_id is not None:
                return_job_to_pending(queue_id)
                attempted_queue_ids.add(queue_id)

            summary["jobs_processed"] += 1
            if runtime_status_path:
                current_job = load_runtime_status(runtime_status_path).get("current_job") or {}
                mark_runtime_job_finish(
                    runtime_status_path,
                    merged_job,
                    status="completed",
                    stage="job_completed",
                    current_episode=current_job.get("current_episode"),
                    total_episodes=current_job.get("total_episodes"),
                    jobs_processed=summary["jobs_processed"],
                    jobs_failed=summary["jobs_failed"],
                )
            if on_job_success:
                on_job_success(merged_job, job_result)
        except Exception as exc:
            if queue_id is not None:
                return_job_to_pending(queue_id)
                attempted_queue_ids.add(queue_id)
            log(f"\n[JOB FAILED] {merged_job.get('title')}")
            log(repr(exc))
            summary["jobs_failed"] += 1
            summary["failed_titles"].append(merged_job.get("title"))
            if get_job_processing_mode(merged_job) == "single_episode":
                ongoing_progress_key = get_job_ongoing_progress_key(merged_job)
                if ongoing_progress_key:
                    blocked_full_refresh_keys.add(ongoing_progress_key)
            if runtime_status_path:
                current_job = load_runtime_status(runtime_status_path).get("current_job") or {}
                append_runtime_error(
                    context="job_failed",
                    message=repr(exc),
                    error_type=type(exc).__name__,
                    stage=current_job.get("stage") or "job_failed",
                    title=merged_job.get("title"),
                    title_ru=merged_job.get("title_ru"),
                    season=merged_job.get("season"),
                    episodes_range=merged_job.get("episodes_range"),
                    current_episode=current_job.get("current_episode"),
                    total_episodes=current_job.get("total_episodes"),
                    run_status="running",
                    status_path=runtime_status_path,
                    errors_path=runtime_errors_path,
                )
                mark_runtime_job_finish(
                    runtime_status_path,
                    merged_job,
                    status="failed",
                    stage="job_failed",
                    current_episode=current_job.get("current_episode"),
                    total_episodes=current_job.get("total_episodes"),
                    jobs_processed=summary["jobs_processed"],
                    jobs_failed=summary["jobs_failed"],
                )
            if on_job_failure:
                on_job_failure(merged_job, exc)
            continue

    if dynamic_queue:
        summary["jobs_found"] = len(seen_queue_ids)
    log("\n=== ALL JOBS FINISHED ===")
    return summary
