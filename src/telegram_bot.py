from pathlib import Path
import sys
import time

from dotenv import load_dotenv


SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shared.config import load_config  # noqa: E402
from shared.runtime import acquire_lock, ensure_runtime_paths, log_line, release_lock  # noqa: E402
from modules.bot import fetch_updates, handle_update, load_telegram_state, update_telegram_state_progress  # noqa: E402


load_dotenv()


def main():
    paths = ensure_runtime_paths()
    log_path = paths["telegram_log_path"]
    lock_path = paths["runtime_dir"] / "telegram_bot.lock"
    lock_result = acquire_lock(lock_path, "python src/telegram_bot.py")
    if not lock_result["acquired"]:
        log_line(log_path, "telegram_bot already_running")
        return 0

    log_line(log_path, "start telegram_bot")
    try:
        config = load_config()
        state = load_telegram_state()
        failure_delay_seconds = 5

        while True:
            try:
                updates = fetch_updates(offset=(state.get("last_update_id") or 0) + 1, timeout=30)
                failure_delay_seconds = 5
            except Exception as exc:
                log_line(log_path, f"telegram_poll_failed error={repr(exc)} retry_in={failure_delay_seconds}s")
                time.sleep(failure_delay_seconds)
                failure_delay_seconds = min(failure_delay_seconds * 2, 60)
                continue

            for update in updates:
                started_at = time.monotonic()
                command = ""
                try:
                    message = update.get("message") or {}
                    text = str(message.get("text") or "").strip()
                    command = text.split(maxsplit=1)[0][:32] if text else ""
                    update_id = update.get("update_id")
                    handled = handle_update(config, update)
                    state = update_telegram_state_progress(
                        last_update_id=update_id,
                        last_handled_at=update.get("message", {}).get("date") if handled else None,
                    )
                    elapsed_ms = round((time.monotonic() - started_at) * 1000)
                    log_line(
                        log_path,
                        f"telegram_update_completed update_id={update_id} command={command!r} "
                        f"handled={handled} elapsed_ms={elapsed_ms}",
                    )
                except Exception as exc:
                    elapsed_ms = round((time.monotonic() - started_at) * 1000)
                    log_line(
                        log_path,
                        f"telegram_update_failed error={repr(exc)} update_id={update.get('update_id')} "
                        f"command={command!r} elapsed_ms={elapsed_ms}",
                    )
                    state = update_telegram_state_progress(last_update_id=update.get("update_id"))
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    main()
