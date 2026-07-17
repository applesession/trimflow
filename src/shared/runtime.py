import json
import os
from datetime import datetime, timezone
from pathlib import Path

from shared.constants import (
    DEFAULT_CRON_LOCK_NAME,
    DEFAULT_CRON_LOG_NAME,
    DEFAULT_DISCOVERY_LOCK_NAME,
    DEFAULT_LOGS_DIR,
    DEFAULT_RUNTIME_DIR,
    DEFAULT_RUNTIME_ERRORS_LIMIT,
    DEFAULT_RUNTIME_ERRORS_NAME,
    DEFAULT_RUNTIME_STATUS_NAME,
    DEFAULT_TELEGRAM_LOG_NAME,
)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_runtime_paths():
    runtime_dir = Path(DEFAULT_RUNTIME_DIR)
    logs_dir = Path(DEFAULT_LOGS_DIR)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return {
        "runtime_dir": runtime_dir,
        "logs_dir": logs_dir,
        "lock_path": runtime_dir / DEFAULT_CRON_LOCK_NAME,
        "discovery_lock_path": runtime_dir / DEFAULT_DISCOVERY_LOCK_NAME,
        "log_path": logs_dir / DEFAULT_CRON_LOG_NAME,
        "telegram_log_path": logs_dir / DEFAULT_TELEGRAM_LOG_NAME,
        "status_path": runtime_dir / DEFAULT_RUNTIME_STATUS_NAME,
        "errors_path": runtime_dir / DEFAULT_RUNTIME_ERRORS_NAME,
    }


def get_runtime_status_path():
    return ensure_runtime_paths()["status_path"]


def get_runtime_errors_path():
    return ensure_runtime_paths()["errors_path"]


def build_default_runtime_status():
    return {
        "schema_version": 1,
        "updated_at": None,
        "run_status": "idle",
        "run_started_at": None,
        "run_finished_at": None,
        "current_stage": None,
        "queue_progress": {
            "current_job_index": 0,
            "total_jobs": 0,
            "jobs_processed": 0,
            "jobs_failed": 0,
        },
        "current_job": None,
        "last_run": None,
    }


def build_default_runtime_errors():
    return {
        "schema_version": 1,
        "updated_at": None,
        "errors": [],
    }


def load_runtime_status(status_path=None):
    path = Path(status_path) if status_path else get_runtime_status_path()
    if not path.exists():
        return build_default_runtime_status()

    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    status = build_default_runtime_status()
    status.update(data)
    status.setdefault("queue_progress", build_default_runtime_status()["queue_progress"])
    return status


def save_runtime_status(status, status_path=None):
    path = Path(status_path) if status_path else get_runtime_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(status, file, indent=2, ensure_ascii=False)
        file.write("\n")


def load_runtime_errors(errors_path=None):
    path = Path(errors_path) if errors_path else get_runtime_errors_path()
    if not path.exists():
        return build_default_runtime_errors()

    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    errors = build_default_runtime_errors()
    errors.update(data)
    if not isinstance(errors.get("errors"), list):
        errors["errors"] = []
    return errors


def save_runtime_errors(errors_payload, errors_path=None):
    path = Path(errors_path) if errors_path else get_runtime_errors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(errors_payload, file, indent=2, ensure_ascii=False)
        file.write("\n")


def _merge_runtime_value(current, update):
    if isinstance(current, dict) and isinstance(update, dict):
        merged = dict(current)
        for key, value in update.items():
            if value is None and key in merged:
                merged[key] = None
            else:
                merged[key] = _merge_runtime_value(merged.get(key), value)
        return merged
    return update


def update_runtime_status(status_path=None, **changes):
    status = load_runtime_status(status_path)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(status.get(key), dict):
            status[key] = _merge_runtime_value(status.get(key), value)
        else:
            status[key] = value
    status["updated_at"] = utc_now_iso()
    save_runtime_status(status, status_path)
    return status


def trim_runtime_errors(errors, limit=DEFAULT_RUNTIME_ERRORS_LIMIT):
    return list(errors[:limit])


def append_runtime_error(
    *,
    context,
    message,
    error_type,
    stage=None,
    title=None,
    title_ru=None,
    season=None,
    episodes_range=None,
    current_episode=None,
    total_episodes=None,
    run_status=None,
    status_path=None,
    errors_path=None,
):
    runtime_status = load_runtime_status(status_path)
    current_job = runtime_status.get("current_job") or {}
    last_run = runtime_status.get("last_run") or {}
    source_job = current_job or last_run

    entry = {
        "id": f"{utc_now_iso()}|{context}",
        "created_at": utc_now_iso(),
        "run_status": run_status or runtime_status.get("run_status") or "idle",
        "context": context,
        "stage": stage or current_job.get("stage") or runtime_status.get("current_stage"),
        "title": title if title is not None else source_job.get("title"),
        "title_ru": title_ru if title_ru is not None else source_job.get("title_ru"),
        "season": season if season is not None else source_job.get("season"),
        "episodes_range": episodes_range if episodes_range is not None else source_job.get("episodes_range"),
        "current_episode": current_episode if current_episode is not None else current_job.get("current_episode"),
        "total_episodes": total_episodes if total_episodes is not None else current_job.get("total_episodes"),
        "message": message,
        "error_type": error_type,
    }

    payload = load_runtime_errors(errors_path)
    payload["errors"] = trim_runtime_errors([entry, *payload.get("errors", [])])
    payload["updated_at"] = entry["created_at"]
    save_runtime_errors(payload, errors_path)
    return entry


def mark_runtime_job_start(
    status_path,
    job,
    *,
    current_job_index,
    total_jobs,
    jobs_processed,
    jobs_failed,
):
    return update_runtime_status(
        status_path,
        current_stage="job_start",
        queue_progress={
            "current_job_index": current_job_index,
            "total_jobs": total_jobs,
            "jobs_processed": jobs_processed,
            "jobs_failed": jobs_failed,
        },
        current_job={
            "title": job.get("title"),
            "title_ru": job.get("title_ru"),
            "season": job.get("season"),
            "episodes_range": job.get("episodes_range"),
            "stage": "job_start",
            "started_at": utc_now_iso(),
            "current_episode": None,
            "total_episodes": None,
            "current_episode_file": None,
            "current_chunk_index": None,
            "total_chunks": None,
            "current_chunk_episode_range": None,
        },
    )


def mark_runtime_job_finish(
    status_path,
    job,
    *,
    status,
    stage,
    current_episode,
    total_episodes,
    jobs_processed,
    jobs_failed,
):
    current_job = load_runtime_status(status_path).get("current_job") or {}
    return update_runtime_status(
        status_path,
        current_stage=stage,
        queue_progress={
            "jobs_processed": jobs_processed,
            "jobs_failed": jobs_failed,
        },
        current_job=None,
        last_run={
            "status": status,
            "finished_at": utc_now_iso(),
            "title": job.get("title"),
            "title_ru": job.get("title_ru"),
            "season": job.get("season"),
            "episodes_range": job.get("episodes_range"),
            "stage": stage,
            "current_episode": current_episode,
            "total_episodes": total_episodes,
            "jobs_processed": jobs_processed,
            "jobs_failed": jobs_failed,
            "started_at": current_job.get("started_at"),
        },
    )


def mark_runtime_run_finish(status_path, *, status, current_stage, jobs_processed, jobs_failed):
    return update_runtime_status(
        status_path,
        run_status=status,
        run_finished_at=utc_now_iso(),
        current_stage=current_stage,
        queue_progress={
            "jobs_processed": jobs_processed,
            "jobs_failed": jobs_failed,
            "current_job_index": 0,
        },
        current_job=None,
    )


def build_lock_payload(command):
    return {
        "pid": os.getpid(),
        "started_at": utc_now_iso(),
        "command": command,
    }


def _is_process_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def is_lock_stale(lock_path):
    if not lock_path.exists():
        return False, None

    try:
        with open(lock_path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (json.JSONDecodeError, OSError):
        return True, None

    if not isinstance(payload, dict):
        return True, None

    pid = payload.get("pid")
    if _is_process_alive(pid):
        return False, payload
    return True, payload


def acquire_lock(lock_path, command):
    stale, payload = is_lock_stale(lock_path)
    if lock_path.exists() and not stale:
        return {
            "acquired": False,
            "already_running": True,
            "lock_payload": payload,
        }

    if lock_path.exists() and stale:
        try:
            lock_path.unlink()
        except OSError as exc:
            raise RuntimeError(f"Failed to remove stale lock {lock_path}: {exc}") from exc

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_lock_payload(command)
    try:
        with open(lock_path, "x", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)
            file.write("\n")
    except FileExistsError:
        return {
            "acquired": False,
            "already_running": True,
            "lock_payload": None,
        }
    except OSError as exc:
        raise RuntimeError(f"Failed to create lock {lock_path}: {exc}") from exc

    return {
        "acquired": True,
        "already_running": False,
        "lock_payload": payload,
    }


def release_lock(lock_path):
    if not lock_path.exists():
        return
    try:
        lock_path.unlink()
    except OSError as exc:
        raise RuntimeError(f"Failed to release lock {lock_path}: {exc}") from exc


def format_log_line(message):
    return f"[{utc_now_iso()}] {message}"


def log_line(log_path, message):
    line = format_log_line(message)
    print(line)
    with open(log_path, "a", encoding="utf-8") as file:
        file.write(line + "\n")
    return line
