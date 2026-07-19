import json
import sys
from pathlib import Path

from dotenv import load_dotenv


SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.runner import build_completed_job_entry  # noqa: E402
from core.upscale import cleanup_cancelled_upscale_job, process_upscale_job  # noqa: E402
from modules.bot import send_message_to_allowed_chats  # noqa: E402
from shared.config import load_completed_jobs, load_config, load_jobs, save_completed_jobs  # noqa: E402
from shared.db import (  # noqa: E402
    claim_job,
    init_db,
    job_exists,
    recover_running_jobs,
    remove_completed_job,
    return_job_to_pending,
)
from shared.helpers import JobCancelled, cancellation_scope, get_display_title, raise_if_cancelled  # noqa: E402
from shared.runtime import (  # noqa: E402
    acquire_lock,
    append_runtime_error,
    ensure_runtime_paths,
    load_runtime_status,
    log_line,
    mark_runtime_job_finish,
    mark_runtime_job_start,
    mark_runtime_run_finish,
    release_lock,
    update_runtime_status,
    utc_now_iso,
)


load_dotenv()


def _notify(log_path, text, context):
    try:
        send_message_to_allowed_chats(text)
    except Exception as exc:
        log_line(log_path, f"warning telegram_notify_failed context={context} error={repr(exc)}")


def _format_episode_success(job, episode, vk_result):
    lines = [
        "✅ 4K-серия опубликована в VK",
        "",
        f"🎬 {get_display_title(job)}",
        f"📺 Сезон {job.get('season', 1)}, серия {episode}",
    ]
    if vk_result.get("video_url"):
        lines.extend(["", f"🔗 {vk_result['video_url']}"])
    if not vk_result.get("post_created") and (vk_result.get("errors_by_stage") or {}).get("wall_post"):
        lines.extend(["", "⚠️ Видео загружено, но Donut-пост не создан"])
    return "\n".join(lines)


def main():
    paths = ensure_runtime_paths()
    lock_path = paths["runtime_dir"] / "upscale.lock"
    status_path = paths["runtime_dir"] / "upscale_status.json"
    errors_path = paths["runtime_dir"] / "upscale_errors.json"
    log_path = paths["logs_dir"] / "upscale.log"
    lock_result = acquire_lock(lock_path, "python src/upscale_run.py")
    if not lock_result["acquired"]:
        log_line(log_path, "already_running")
        return 0

    try:
        init_db()
        recovered = recover_running_jobs(processing_mode="upscale_4k")
        if recovered:
            log_line(log_path, f"recovered_stale_jobs count={len(recovered)}")
            _notify(
                log_path,
                "⚠️ 4K-worker был прерван\n\nЗадача возвращена в очередь и продолжится с checkpoint",
                "upscale_recovery",
            )

        config = load_config()
        attempted = set()
        processed = 0
        failed = 0
        update_runtime_status(
            status_path,
            run_status="running",
            run_started_at=utc_now_iso(),
            run_finished_at=None,
            current_stage="processing",
            current_job=None,
        )

        while True:
            pending = [
                job for job in load_jobs(config, status="pending", processing_mode="upscale_4k")
                if job.get("_queue_id") not in attempted
            ]
            if not pending:
                break
            job = pending[0]
            queue_id = job.get("_queue_id")
            if queue_id is None or not claim_job(queue_id):
                continue
            mark_runtime_job_start(
                status_path,
                job,
                current_job_index=processed + failed + 1,
                total_jobs=processed + failed + len(pending),
                jobs_processed=processed,
                jobs_failed=failed,
            )
            log_line(log_path, f"START 4K JOB: {get_display_title(job)}")
            try:
                with cancellation_scope(lambda: not job_exists(queue_id)):
                    result = process_upscale_job(
                        config,
                        job,
                        runtime_status_path=status_path,
                        on_episode_success=lambda current_job, episode, vk_result: _notify(
                            log_path,
                            _format_episode_success(current_job, episode, vk_result),
                            f"upscale_episode_success:{get_display_title(current_job)}:{episode}",
                        ),
                    )
                    raise_if_cancelled()
                if not result.get("completed"):
                    raise RuntimeError("4K job did not complete all episodes")
                if not remove_completed_job(job):
                    raise JobCancelled("Job removed before completion was recorded")
                completed_jobs = load_completed_jobs(config)
                completed_entry = build_completed_job_entry(job, result)
                completed_entry["partial_vk"] = False
                completed_jobs.append(completed_entry)
                save_completed_jobs(config, completed_jobs)
                processed += 1
                current_job = load_runtime_status(status_path).get("current_job") or {}
                mark_runtime_job_finish(
                    status_path,
                    job,
                    status="completed",
                    stage="job_completed",
                    current_episode=current_job.get("current_episode"),
                    total_episodes=current_job.get("total_episodes"),
                    jobs_processed=processed,
                    jobs_failed=failed,
                )
                log_line(log_path, f"FINISH 4K JOB: {get_display_title(job)}")
            except JobCancelled:
                cleanup_cancelled_upscale_job(job)
                current_job = load_runtime_status(status_path).get("current_job") or {}
                mark_runtime_job_finish(
                    status_path,
                    job,
                    status="cancelled",
                    stage="job_cancelled",
                    current_episode=current_job.get("current_episode"),
                    total_episodes=current_job.get("total_episodes"),
                    jobs_processed=processed,
                    jobs_failed=failed,
                )
                log_line(log_path, f"CANCEL 4K JOB: {get_display_title(job)}")
                continue
            except Exception as exc:
                if not job_exists(queue_id):
                    cleanup_cancelled_upscale_job(job)
                    log_line(log_path, f"CANCEL 4K JOB: {get_display_title(job)}")
                    continue
                return_job_to_pending(queue_id)
                attempted.add(queue_id)
                failed += 1
                current_job = load_runtime_status(status_path).get("current_job") or {}
                append_runtime_error(
                    context="upscale_job_failed",
                    message=repr(exc),
                    error_type=type(exc).__name__,
                    stage=current_job.get("stage") or "upscale_failed",
                    title=job.get("title"),
                    title_ru=job.get("title_ru"),
                    season=job.get("season"),
                    episodes_range=job.get("episodes_range"),
                    current_episode=current_job.get("current_episode"),
                    total_episodes=current_job.get("total_episodes"),
                    run_status="running",
                    status_path=status_path,
                    errors_path=errors_path,
                )
                mark_runtime_job_finish(
                    status_path,
                    job,
                    status="failed",
                    stage="upscale_failed",
                    current_episode=current_job.get("current_episode"),
                    total_episodes=current_job.get("total_episodes"),
                    jobs_processed=processed,
                    jobs_failed=failed,
                )
                log_line(log_path, "upscale_job_failed " + json.dumps({
                    "title": get_display_title(job),
                    "error": repr(exc),
                }, ensure_ascii=False))
                _notify(
                    log_path,
                    "\n".join([
                        "❌ Ошибка 4K-worker",
                        "",
                        f"🎬 {get_display_title(job)}",
                        f"📺 Серия: {current_job.get('current_episode') or '?'}",
                        f"Причина: {exc}",
                        "",
                        "Задача возвращена в очередь и продолжится с checkpoint",
                    ]),
                    f"upscale_job_failed:{get_display_title(job)}",
                )

        mark_runtime_run_finish(
            status_path,
            status="completed",
            current_stage="completed",
            jobs_processed=processed,
            jobs_failed=failed,
        )
        log_line(log_path, f"upscale_summary processed={processed} failed={failed}")
        return 0
    except Exception as exc:
        log_line(log_path, f"upscale_run_failed error={repr(exc)}")
        mark_runtime_run_finish(
            status_path,
            status="failed",
            current_stage="failed",
            jobs_processed=0,
            jobs_failed=1,
        )
        return 1
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
