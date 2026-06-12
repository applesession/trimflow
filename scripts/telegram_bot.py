from pathlib import Path
import sys
import time

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.config import load_config  # noqa: E402
from lib.runtime import ensure_runtime_paths, log_line  # noqa: E402
from lib.telegram_bot import fetch_updates, handle_update, load_telegram_state, save_telegram_state  # noqa: E402


load_dotenv()


def main():
    config = load_config()
    state = load_telegram_state()
    paths = ensure_runtime_paths()
    log_path = paths["telegram_log_path"]
    log_line(log_path, "start telegram_bot")
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
            try:
                update_id = update.get("update_id")
                if update_id is not None:
                    state["last_update_id"] = update_id
                handled = handle_update(config, update)
                if handled:
                    state["last_handled_at"] = update.get("message", {}).get("date")
                save_telegram_state(state)
            except Exception as exc:
                log_line(log_path, f"telegram_update_failed error={repr(exc)} update_id={update.get('update_id')}")
                save_telegram_state(state)


if __name__ == "__main__":
    main()
