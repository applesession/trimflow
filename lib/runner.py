from datetime import datetime, timezone

from lib.config import (
    deep_merge,
    load_completed_jobs,
    save_completed_jobs,
    save_jobs,
)
from lib.pipeline import process_job
from lib.runtime import (
    append_runtime_error,
    load_runtime_status,
    mark_runtime_job_finish,
    mark_runtime_job_start,
)
from lib.validation import (
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
    merged_jobs = [deep_merge(defaults, job) for job in active_jobs]

    validate_required_env(config, merged_jobs)
    validate_required_tools(config, merged_jobs)
    validate_required_files(config)

    completed_jobs = load_completed_jobs(config)
    summary = {
        "jobs_found": len(active_jobs),
        "jobs_processed": 0,
        "jobs_failed": 0,
        "jobs_skipped": 0,
        "failed_titles": [],
    }

    for index, merged_job in enumerate(merged_jobs, start=1):
        log("\n" + "=" * 80)
        log(f"START JOB {index}/{len(merged_jobs)}: {merged_job['title']}")
        log("=" * 80)
        if runtime_status_path:
            mark_runtime_job_start(
                runtime_status_path,
                merged_job,
                current_job_index=index,
                total_jobs=len(merged_jobs),
                jobs_processed=summary["jobs_processed"],
                jobs_failed=summary["jobs_failed"],
            )

        try:
            job_result = process_job(merged_job, runtime_status_path=runtime_status_path)
            if is_job_completed(job_result):
                active_jobs, removed = remove_job_from_queue(active_jobs, merged_job)
                if removed:
                    save_jobs(config, active_jobs)
                completed_jobs.append(build_completed_job_entry(merged_job, job_result))
                save_completed_jobs(config, completed_jobs)

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
            log(f"\n[JOB FAILED] {merged_job.get('title')}")
            log(repr(exc))
            summary["jobs_failed"] += 1
            summary["failed_titles"].append(merged_job.get("title"))
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

    log("\n=== ALL JOBS FINISHED ===")
    return summary
