import json
from copy import deepcopy
from pathlib import Path

from lib.constants import CONFIG_PATH, DEFAULT_AUTOMATION, DEFAULT_COMPLETED_JOBS_PATH, DEFAULT_JOBS_PATH, DEFAULT_STATE_PATH


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
        "schema_version": 1,
        "last_discovery_at": None,
        "seen_release_episodes": {},
        "job_index": {},
        "skipped_items": [],
    }


def load_jobs(config):
    jobs_path = get_jobs_path(config)
    if not jobs_path.exists():
        return []

    with open(jobs_path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise RuntimeError(f"{jobs_path} must contain a JSON array of jobs")

    return data


def load_state(config):
    state_path = get_state_path(config)
    if not state_path.exists():
        return build_default_state()

    with open(state_path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(f"{state_path} must contain a JSON object")

    state = build_default_state()
    state.update(data)
    state.setdefault("seen_release_episodes", {})
    state.setdefault("job_index", {})
    state.setdefault("skipped_items", [])
    return state


def load_completed_jobs(config):
    completed_jobs_path = get_completed_jobs_path(config)
    if not completed_jobs_path.exists():
        return []

    with open(completed_jobs_path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise RuntimeError(f"{completed_jobs_path} must contain a JSON array of completed jobs")

    return data


def save_jobs(config, jobs):
    jobs_path = get_jobs_path(config)
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jobs_path, "w", encoding="utf-8") as file:
        json.dump(jobs, file, indent=2, ensure_ascii=False)
        file.write("\n")


def save_state(config, state):
    state_path = get_state_path(config)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")


def save_completed_jobs(config, completed_jobs):
    completed_jobs_path = get_completed_jobs_path(config)
    completed_jobs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(completed_jobs_path, "w", encoding="utf-8") as file:
        json.dump(completed_jobs, file, indent=2, ensure_ascii=False)
        file.write("\n")


def deep_merge(defaults, job):
    result = deepcopy(defaults)

    for key, value in job.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value

    return result
