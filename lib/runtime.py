import json
import os
from datetime import datetime, timezone
from pathlib import Path

from lib.constants import (
    DEFAULT_CRON_LOCK_NAME,
    DEFAULT_CRON_LOG_NAME,
    DEFAULT_LOGS_DIR,
    DEFAULT_RUNTIME_DIR,
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
        "log_path": logs_dir / DEFAULT_CRON_LOG_NAME,
    }


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
