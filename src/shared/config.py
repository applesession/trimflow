import json
from copy import deepcopy
from pathlib import Path

from shared.constants import CONFIG_PATH, DEFAULT_AUTOMATION, DEFAULT_COMPLETED_JOBS_PATH, DEFAULT_JOBS_PATH, DEFAULT_STATE_PATH
from shared.db import load_jobs as _db_load_jobs, save_jobs as _db_save_jobs
from shared.db import load_completed_jobs as _db_load_completed_jobs, save_completed_jobs as _db_save_completed_jobs


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def normalize_automation_config(config):
    automation = deepcopy(DEFAULT_AUTOMATION)
    automation.update(config.get("automation", {}))
    return automation


def _resolve_json_path(path_value, fallback_path):
    path = Path(path_value) if path_value else Path(fallback_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def get_jobs_path(config):
    automation = normalize_automation_config(config)
    return _resolve_json_path(automation.get("jobs_path"), DEFAULT_JOBS_PATH)


def get_state_path(config):
    automation = normalize_automation_config(config)
    return _resolve_json_path(automation.get("state_path"), DEFAULT_STATE_PATH)


def get_completed_jobs_path(config):
    automation = normalize_automation_config(config)
    return _resolve_json_path(automation.get("completed_jobs_path"), DEFAULT_COMPLETED_JOBS_PATH)


def build_default_state():
    return {
        "schema_version": 3,
        "last_discovery_at": None,
        "queued_release_episodes": {},
        "completed_release_episodes": {},
        "discovery_blacklist": [],
        "job_index": {},
        "skipped_items": [],
        "ongoing_progress": {},
    }


def load_jobs(config, status=None):
    return _db_load_jobs(status=status)


def load_state(config):
    state_path = get_state_path(config)
    if not state_path.exists():
        return build_default_state()

    with open(state_path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(f"{state_path} must contain a JSON object")

    state = build_default_state()
    legacy_seen_release_episodes = data.get("seen_release_episodes", {})
    state.update(data)
    state.setdefault("queued_release_episodes", {})
    state.setdefault("completed_release_episodes", {})
    state.setdefault("discovery_blacklist", [])
    if not state.get("completed_release_episodes") and isinstance(legacy_seen_release_episodes, dict):
        state["completed_release_episodes"] = legacy_seen_release_episodes
    state["schema_version"] = max(int(state.get("schema_version", 1)), 3)
    state.setdefault("job_index", {})
    state.setdefault("skipped_items", [])
    state.setdefault("ongoing_progress", {})
    state.pop("seen_release_episodes", None)
    return state


def load_completed_jobs(config):
    return _db_load_completed_jobs()


def save_jobs(config, jobs):
    _db_save_jobs(jobs)


def save_state(config, state):
    state_path = get_state_path(config)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")


def save_completed_jobs(config, completed_jobs):
    _db_save_completed_jobs(completed_jobs)


def deep_merge(defaults, job):
    result = deepcopy(defaults)

    for key, value in job.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value

    return result
