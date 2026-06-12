from pathlib import Path
import json
import sys

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.autojobs import discover_jobs  # noqa: E402
from lib.config import load_config, load_jobs, load_state, save_jobs, save_state  # noqa: E402
from lib.runner import run_jobs  # noqa: E402
from lib.runtime import (
    acquire_lock,
    ensure_runtime_paths,
    log_line,
    mark_runtime_run_finish,
    release_lock,
    utc_now_iso,
    update_runtime_status,
)  # noqa: E402
from lib.telegram_bot import (  # noqa: E402
    format_discovery_message,
    format_error_message,
    format_publish_success_message,
    format_vk_publish_success_message,
    send_message_to_allowed_chats,
)


load_dotenv()


def notify_best_effort(log_path, text, context):
    try:
        send_message_to_allowed_chats(text)
    except Exception as exc:
        log_line(log_path, f"warning telegram_notify_failed context={context} error={repr(exc)}")


def main():
    paths = ensure_runtime_paths()
    lock_path = paths["lock_path"]
    log_path = paths["log_path"]
    status_path = paths["status_path"]
    command = "python scripts/cron_run.py"

    lock_result = acquire_lock(lock_path, command)
    if not lock_result["acquired"]:
        payload = lock_result.get("lock_payload") or {}
        log_line(
            log_path,
            "already_running"
            + (f" pid={payload.get('pid')} started_at={payload.get('started_at')}" if payload else ""),
        )
        return 0

    try:
        log_line(log_path, "start cron_run")
        update_runtime_status(
            status_path,
            run_status="running",
            run_started_at=utc_now_iso(),
            run_finished_at=None,
            current_stage="cron_start",
            queue_progress={
                "current_job_index": 0,
                "total_jobs": 0,
                "jobs_processed": 0,
                "jobs_failed": 0,
            },
            current_job=None,
        )

        config = load_config()
        jobs = load_jobs(config)
        state = load_state(config)
        update_runtime_status(status_path, queue_progress={"total_jobs": len(jobs)})

        try:
            update_runtime_status(status_path, current_stage="discovery")
            discovery_result = discover_jobs(config, jobs, state)
            save_jobs(config, discovery_result["jobs"])
            save_state(config, discovery_result["state"])

            log_line(
                log_path,
                "discovery_summary " + json.dumps(discovery_result["summary"], ensure_ascii=False),
            )
            if discovery_result["summary"].get("created_jobs", 0) > 0 or discovery_result["summary"].get("updated_jobs", 0) > 0:
                existing_keys = {
                    (
                        job.get("title"),
                        job.get("season"),
                        job.get("episodes_range"),
                        job.get("source", {}).get("magnet"),
                    )
                    for job in jobs
                }
                jobs_added = [
                    job
                    for job in discovery_result["jobs"]
                    if (
                        job.get("title"),
                        job.get("season"),
                        job.get("episodes_range"),
                        job.get("source", {}).get("magnet"),
                    ) not in existing_keys
                ]
                notify_best_effort(
                    log_path,
                    format_discovery_message(discovery_result["summary"], jobs_added),
                    "discovery_summary",
                )

            jobs = discovery_result["jobs"]
            update_runtime_status(status_path, queue_progress={"total_jobs": len(jobs)})
        except Exception as exc:
            log_line(log_path, f"warning discovery_failed error={repr(exc)}")
            notify_best_effort(
                log_path,
                format_error_message("discovery", repr(exc)),
                "discovery_error",
            )

        update_runtime_status(status_path, current_stage="processing", queue_progress={"total_jobs": len(jobs)})
        processing_summary = run_jobs(
            config,
            jobs,
            runtime_status_path=status_path,
            log=lambda message: log_line(log_path, message),
            on_job_success=lambda job, result: notify_best_effort(
                log_path,
                format_error_message(
                    f"vk_publish:{job.get('title')}",
                    result.get("delivery_summary", {}).get("vk", {}).get("error"),
                )
                if result.get("delivery_summary", {}).get("vk", {}).get("enabled")
                and result.get("delivery_summary", {}).get("vk", {}).get("error")
                else format_vk_publish_success_message(job, result["delivery_summary"]["vk"])
                if result.get("delivery_summary", {}).get("vk", {}).get("uploaded")
                else format_publish_success_message(
                    job,
                    result.get("output_video"),
                ),
                f"job_success:{job.get('title')}",
            ),
            on_job_failure=lambda job, exc: notify_best_effort(
                log_path,
                format_error_message(f"job_failed:{job.get('title')}", repr(exc)),
                f"job_failed:{job.get('title')}",
            ),
        )

        log_line(
            log_path,
            "processing_summary " + json.dumps(processing_summary, ensure_ascii=False),
        )
        mark_runtime_run_finish(
            status_path,
            status="completed",
            current_stage="completed",
            jobs_processed=processing_summary.get("jobs_processed", 0),
            jobs_failed=processing_summary.get("jobs_failed", 0),
        )
        log_line(log_path, "finish cron_run")
        return 0
    except Exception as exc:
        mark_runtime_run_finish(
            status_path,
            status="failed",
            current_stage="failed",
            jobs_processed=0,
            jobs_failed=0,
        )
        log_line(log_path, f"error {repr(exc)}")
        notify_best_effort(
            log_path,
            format_error_message("cron_run", repr(exc)),
            "cron_run_error",
        )
        raise
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
