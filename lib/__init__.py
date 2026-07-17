import importlib
import os
import sys
from pathlib import Path
from types import ModuleType


src = str(Path(__file__).resolve().parent.parent / "src")
if src not in sys.path:
    sys.path.insert(0, src)

db = importlib.import_module("shared.db")
db.DB_PATH = Path(".test_tmp/legacy-db/data.db").resolve()
db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db.DB_PATH.unlink(missing_ok=True)
cwd = Path.cwd()
try:
    os.chdir(db.DB_PATH.parent)
    db.init_db()
finally:
    os.chdir(cwd)


def reset_test_db():
    connection = db._get_conn()
    for table in (
        "jobs", "completed_jobs", "episode_tracking", "discovery_blacklist",
        "ongoing_progress", "skipped_items", "runtime_errors", "telegram_state",
    ):
        connection.execute(f"DELETE FROM {table}")
    connection.execute("UPDATE runtime_status SET run_status = 'idle', current_job = NULL, last_run = NULL WHERE id = 1")
    connection.commit()
    connection.close()

for legacy, current in {
    "anilibria": "api.anilibria",
    "aniskip": "api.aniskip",
    "autojobs": "modules.autojobs",
    "config": "shared.config",
    "detector": "core.detector",
    "discovery": "core.discovery",
    "helpers": "shared.helpers",
    "media": "core.media",
    "pipeline": "core.pipeline",
    "runner": "core.runner",
    "runtime": "shared.runtime",
    "telegram_bot": "modules.bot",
    "vk": "api.vk",
}.items():
    module = importlib.import_module(current)
    sys.modules[f"lib.{legacy}"] = module
    globals()[legacy] = module


def _format_next_message(config, limit=10):
    jobs = telegram_bot.load_jobs(config)
    if not jobs:
        return "Очередь пуста"

    execution_jobs = telegram_bot.build_execution_order(jobs, defaults=config.get("defaults", {}))
    visible_jobs = execution_jobs[:max(1, int(limit))]
    lines = ["Следующие к выполнению", "", "Порядок показан с учётом приоритета ongoing.", ""]
    for index, job in enumerate(visible_jobs, start=1):
        ongoing = " [ongoing]" if (job.get("automation") or {}).get("is_ongoing") else ""
        lines.extend([
            f"{index}. {telegram_bot.get_display_title(job)}{ongoing}",
            f"  Сезон: {job.get('season', 1)}",
            f"  Эпизоды: {job.get('episodes_range', '?')}",
        ])
    if len(execution_jobs) > len(visible_jobs):
        lines.extend(["", f"Показано: {len(visible_jobs)} из {len(execution_jobs)}"])
    return "\n".join(lines)


telegram_bot.format_next_message = _format_next_message

scripts = ModuleType("scripts")
scripts.cron_run = importlib.import_module("cron_run")
sys.modules["scripts"] = scripts
sys.modules["scripts.cron_run"] = scripts.cron_run
