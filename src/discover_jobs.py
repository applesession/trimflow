from pathlib import Path
import json
import sys

from dotenv import load_dotenv


SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.autojobs import discover_jobs  # noqa: E402
from shared.config import load_config, load_jobs, load_state, save_state  # noqa: E402
from shared.db import init_db, sync_discovered_jobs  # noqa: E402
from shared.runtime import acquire_lock, ensure_runtime_paths, release_lock  # noqa: E402


load_dotenv()


def _job_identity(job):
    return (
        job.get("title"),
        job.get("season"),
        job.get("episodes_range"),
        (job.get("source") or {}).get("magnet"),
    )


def run_discovery_once():
    init_db()
    lock_path = ensure_runtime_paths()["discovery_lock_path"]
    lock_result = acquire_lock(lock_path, "python src/discover_jobs.py")
    if not lock_result["acquired"]:
        return {"status": "already_running", "summary": {}, "jobs_added": []}

    try:
        config = load_config()
        jobs = load_jobs(config)
        state = load_state(config)
        existing_keys = {_job_identity(job) for job in jobs}

        result = discover_jobs(config, jobs, state)
        sync_discovered_jobs(result["jobs"])
        save_state(config, result["state"])
        result["status"] = "completed"
        result["jobs_added"] = [
            job for job in result["jobs"] if _job_identity(job) not in existing_keys
        ]
        return result
    finally:
        release_lock(lock_path)


def main():
    result = run_discovery_once()

    print("[DISCOVERY SUMMARY]")
    print(json.dumps(result.get("summary", {}), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
