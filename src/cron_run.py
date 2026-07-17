from pathlib import Path
import json
import sys

from dotenv import load_dotenv


# Ensure src/ is importable when run from anywhere
SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from discover_jobs import run_discovery_once  # noqa: E402
from shared.config import load_config, load_jobs  # noqa: E402
from shared.db import init_db, reset_running_jobs  # noqa: E402
from core.runner import run_jobs  # noqa: E402
from shared.runtime import (
    acquire_lock,
    append_runtime_error,
    ensure_runtime_paths,
    log_line,
    mark_runtime_run_finish,
    release_lock,
    utc_now_iso,
    update_runtime_status,
)  # noqa: E402
from modules.bot import (  # noqa: E402
    build_notification_details_payload,
    build_notification_details_reply_markup,
    format_discovery_message,
    format_download_timeout_message,
    format_error_message,
    format_publish_success_message,
    format_vk_publish_error_message,
    format_vk_publish_success_message,
    send_message_to_allowed_chats,
)


load_dotenv()


def notify_best_effort(log_path, text, context, parse_mode=None, reply_markup=None):
    try:
        send_message_to_allowed_chats(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as exc:
        log_line(log_path, f"warning telegram_notify_failed context={context} error={repr(exc)}")


def main():
    paths = ensure_runtime_paths()
    lock_path = paths["lock_path"]
    log_path = paths["log_path"]
    status_path = paths["status_path"]
    errors_path = paths["errors_path"]
    command = "python src/cron_run.py"

    init_db()
    log_line(log_path, "start discovery")
    try:
        discovery_result = run_discovery_once()
        if discovery_result.get("status") == "already_running":
            log_line(log_path, "discovery_already_running")
        else:
            log_line(
                log_path,
                "discovery_summary " + json.dumps(discovery_result["summary"], ensure_ascii=False),
            )
            if discovery_result["summary"].get("created_jobs", 0) > 0 or discovery_result["summary"].get("updated_jobs", 0) > 0:
                notify_best_effort(
                    log_path,
                    format_discovery_message(discovery_result["summary"], discovery_result["jobs_added"]),
                    "discovery_summary",
                    parse_mode="MarkdownV2",
                )
    except Exception as exc:
        log_line(log_path, f"warning discovery_failed error={repr(exc)}")
        append_runtime_error(
            context="discovery_failed",
            message=repr(exc),
            error_type=type(exc).__name__,
            stage="discovery",
            status_path=status_path,
            errors_path=errors_path,
        )
        notify_best_effort(
            log_path,
            format_error_message("discovery", repr(exc)),
            "discovery_error",
            parse_mode="MarkdownV2",
        )

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
        recovered_jobs = reset_running_jobs()
        if recovered_jobs:
            log_line(log_path, f"recovered_stale_jobs count={recovered_jobs}")
        log_line(log_path, "start render")
        update_runtime_status(
            status_path,
            run_status="running",
            run_started_at=utc_now_iso(),
            run_finished_at=None,
            current_stage="processing",
            queue_progress={
                "current_job_index": 0,
                "total_jobs": 0,
                "jobs_processed": 0,
                "jobs_failed": 0,
            },
            current_job=None,
        )

        config = load_config()
        jobs = load_jobs(config, status="pending")

        update_runtime_status(status_path, current_stage="processing", queue_progress={"total_jobs": len(jobs)})
        processing_summary = run_jobs(
            config,
            jobs,
            runtime_status_path=status_path,
            runtime_errors_path=errors_path,
            log=lambda message: log_line(log_path, message),
            on_job_success=lambda job, result: notify_best_effort(
                log_path,
                format_vk_publish_success_message(
                    job,
                    result["delivery_summary"]["vk"],
                    result.get("quality_summary"),
                )
                if result.get("delivery_summary", {}).get("vk", {}).get("uploaded")
                else format_vk_publish_error_message(
                    job,
                    result.get("delivery_summary", {}).get("vk", {}).get("error"),
                )
                if result.get("delivery_summary", {}).get("vk", {}).get("enabled")
                else format_publish_success_message(
                    job,
                    result.get("output_video"),
                    quality_summary=result.get("quality_summary"),
                ),
                f"job_success:{job.get('title')}",
                parse_mode="MarkdownV2",
                reply_markup=build_notification_details_reply_markup(
                    build_notification_details_payload(job, result),
                ),
            ),
            on_job_failure=lambda job, exc: notify_best_effort(
                log_path,
                format_download_timeout_message(job)
                if "timed out" in str(exc)
                else format_error_message(f"job_failed:{job.get('title')}", repr(exc)),
                f"job_failed:{job.get('title')}",
                parse_mode="MarkdownV2",
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
        log_line(log_path, "finish render")
        return 0
    except Exception as exc:
        mark_runtime_run_finish(
            status_path,
            status="failed",
            current_stage="failed",
            jobs_processed=0,
            jobs_failed=0,
        )
        append_runtime_error(
            context="cron_run_error",
            message=repr(exc),
            error_type=type(exc).__name__,
            stage="failed",
            run_status="failed",
            status_path=status_path,
            errors_path=errors_path,
        )
        log_line(log_path, f"error {repr(exc)}")
        notify_best_effort(
            log_path,
            format_error_message("cron_run", repr(exc)),
            "cron_run_error",
            parse_mode="MarkdownV2",
        )
        raise
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
