import json
import os
import re
import socket
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

import requests
from urllib3.util import connection as urllib3_connection

from lib.autojobs import find_matching_job, format_episodes_range
from lib.config import load_jobs, load_state, save_jobs
from lib.constants import DEFAULT_TELEGRAM_STATE_PATH
from lib.helpers import ensure_non_empty_slug, parse_episodes_range
from lib.runtime import ensure_runtime_paths


TELEGRAM_API_BASE = "https://api.telegram.org"


def get_display_title(job):
    if not isinstance(job, dict):
        return "Без названия"
    for key in ["title_ru", "title"]:
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Без названия"


def format_bool_ru(value):
    return "да" if value else "нет"


def format_reason_ru(reason):
    mapping = {
        "duplicate_job": "такое аниме уже есть в очереди",
    }
    return mapping.get(reason, str(reason))


def build_main_keyboard():
    return {
        "keyboard": [
            [{"text": "Статус"}, {"text": "Очередь"}],
            [{"text": "Помощь"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def get_telegram_token():
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def get_telegram_proxy_url():
    return (os.getenv("TELEGRAM_PROXY_URL") or "").strip()


def telegram_force_ipv4_enabled():
    return str(os.getenv("TELEGRAM_FORCE_IPV4", "true")).strip().lower() not in {"0", "false", "no", "off"}


@contextmanager
def telegram_ipv4_only():
    if not telegram_force_ipv4_enabled():
        yield
        return

    original_allowed_gai_family = urllib3_connection.allowed_gai_family
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
    try:
        yield
    finally:
        urllib3_connection.allowed_gai_family = original_allowed_gai_family


def parse_allowed_chat_ids(raw_value=None):
    raw_value = raw_value if raw_value is not None else os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    result = set()
    for item in str(raw_value).split(","):
        value = item.strip()
        if not value:
            continue
        result.add(value)
    return result


def is_allowed_chat(chat_id, allowed_chat_ids=None):
    allowed_chat_ids = allowed_chat_ids if allowed_chat_ids is not None else parse_allowed_chat_ids()
    return str(chat_id) in allowed_chat_ids


def telegram_notifications_enabled():
    return bool(get_telegram_token() and parse_allowed_chat_ids())


def get_telegram_state_path():
    path_value = os.getenv("TELEGRAM_STATE_PATH")
    path = Path(path_value) if path_value else DEFAULT_TELEGRAM_STATE_PATH
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def build_default_telegram_state():
    return {
        "schema_version": 1,
        "last_update_id": None,
        "last_handled_at": None,
    }


def load_telegram_state():
    path = get_telegram_state_path()
    if not path.exists():
        return build_default_telegram_state()

    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    state = build_default_telegram_state()
    state.update(data)
    return state


def save_telegram_state(state):
    path = get_telegram_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")


def _telegram_request(method, payload=None, timeout=60):
    token = get_telegram_token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    proxy_url = get_telegram_proxy_url()
    request_kwargs = {
        "json": payload or {},
        "timeout": timeout,
    }
    if proxy_url:
        request_kwargs["proxies"] = {
            "http": proxy_url,
            "https": proxy_url,
        }
    with telegram_ipv4_only():
        response = requests.post(url, **request_kwargs)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    return data


def fetch_updates(offset=None, timeout=30):
    payload = {
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = offset
    return _telegram_request("getUpdates", payload=payload, timeout=timeout + 10).get("result", [])


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _telegram_request(
        "sendMessage",
        payload=payload,
        timeout=20,
    )


def send_reply(chat_id, text, include_keyboard=True):
    reply_markup = build_main_keyboard() if include_keyboard else None
    return send_message(chat_id, text, reply_markup=reply_markup)


def send_message_to_allowed_chats(text):
    if not telegram_notifications_enabled():
        return []

    results = []
    for chat_id in sorted(parse_allowed_chat_ids()):
        results.append(send_message(chat_id, text))
    return results


def format_discovery_message(summary, jobs_added):
    titles = [get_display_title(job) for job in jobs_added]
    lines = [
        "Автодискавери завершён",
        "",
        f"Новых аниме: {summary.get('created_jobs', 0)}",
        f"Обновлено аниме: {summary.get('updated_jobs', 0)}",
    ]
    if titles:
        lines.extend(["", "Новые тайтлы:"])
        lines.extend(f"- {title}" for title in titles)
    return "\n".join(lines)


def format_error_message(context, error):
    return "\n".join([
        "Ошибка",
        "",
        f"Контекст: {context}",
        f"Детали: {error}",
    ])


def format_publish_success_message(job, output_path_or_key=None):
    title = get_display_title(job)
    episodes_range = job.get("episodes_range", "?")
    lines = [
        "Обработка завершена",
        "",
        f"Тайтл: {title}",
        f"Эпизоды: {episodes_range}",
    ]
    if output_path_or_key:
        lines.append(f"Результат: {output_path_or_key}")
    return "\n".join(lines)


def format_vk_publish_success_message(job, vk_result):
    title = get_display_title(job)
    episodes_range = job.get("episodes_range", "?")
    lines = [
        "Видео загружено в VK",
        "",
        f"Тайтл: {title}",
        f"Эпизоды: {episodes_range}",
    ]
    if vk_result.get("post_created"):
        lines.append("Пост на стене: создан")
    if vk_result.get("comment_created"):
        lines.append("Первый комментарий: создан")
    if vk_result.get("video_url"):
        lines.append(f"Ссылка: {vk_result['video_url']}")
    return "\n".join(lines)


def format_datetime_ru(iso_value):
    if not iso_value:
        return "ещё не запускалось"
    try:
        parsed = datetime.fromisoformat(str(iso_value))
    except ValueError:
        return str(iso_value)
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M")


def find_job_by_title(jobs, title):
    normalized = str(title or "").strip()
    if not normalized:
        return None
    for job in jobs:
        if str(job.get("title", "")).strip() == normalized:
            return job
    return None


def detect_active_job_title(jobs, runtime_paths):
    lock_path = runtime_paths["lock_path"]
    log_path = runtime_paths["log_path"]
    if not lock_path.exists() or not log_path.exists():
        return None

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    pattern = re.compile(r"START JOB \d+/\d+: (?P<title>.+)$")
    for line in reversed(lines):
        match = pattern.search(line)
        if not match:
            continue
        matched_job = find_job_by_title(jobs, match.group("title").strip())
        return get_display_title(matched_job or {"title": match.group("title").strip()})

    return None


def format_status_message(config):
    jobs = load_jobs(config)
    state = load_state(config)
    runtime_paths = ensure_runtime_paths()
    active_title = detect_active_job_title(jobs, runtime_paths)

    lines = [
        "Статус пайплайна",
        "",
        f"Активная задача: {active_title or 'сейчас ничего не обрабатывается'}",
        f"Аниме в очереди: {len(jobs)}",
        f"Последнее обновление очереди: {format_datetime_ru(state.get('last_discovery_at'))}",
        f"Зафиксировано эпизодов: {len(state.get('seen_release_episodes', {}))}",
    ]
    return "\n".join(lines)


def format_jobs_message(config, limit=10):
    jobs = load_jobs(config)
    if not jobs:
        return "Очередь пуста"

    tail = jobs[-int(limit):]
    lines = [
        "Очередь аниме",
        "",
        f"Всего: {len(jobs)}",
        f"Показываю последних: {len(tail)}",
        "",
    ]
    for job in tail:
        lines.append(
            f"- {get_display_title(job)}"
        )
        lines.append(f"  Сезон: {job.get('season', 1)}")
        lines.append(f"  Эпизоды: {job.get('episodes_range', '?')}")
    return "\n".join(lines)


def normalize_command_text(text):
    normalized = str(text or "").strip()
    aliases = {
        "Статус": "/status",
        "Очередь": "/jobs",
        "Помощь": "/help",
    }
    return aliases.get(normalized, normalized)


def parse_add_command(text):
    if not text.startswith("/add "):
        raise RuntimeError("Команда должна начинаться с /add ")

    raw_payload = text[len("/add "):]
    parts = [part.strip() for part in re.split(r"\s+:\s+", raw_payload)]
    if len(parts) not in {3, 4}:
        raise RuntimeError("Формат: /add Название : 001-003 : magnet:?xt=... : 2")

    title, episodes_range, magnet = parts[:3]
    season = parts[3] if len(parts) == 4 else "1"

    if not title:
        raise RuntimeError("Нужно указать название тайтла")
    if not magnet.startswith("magnet:?"):
        raise RuntimeError("Magnet-ссылка должна начинаться с magnet:?")

    validated_episodes = parse_episodes_range(episodes_range)
    try:
        season_value = int(str(season).strip())
    except ValueError as exc:
        raise RuntimeError("Сезон должен быть целым числом") from exc
    if season_value < 1:
        raise RuntimeError("Сезон должен быть не меньше 1")

    return {
        "title": title,
        "season": season_value,
        "episodes_range": format_episodes_range(validated_episodes),
        "magnet": magnet,
    }


def build_manual_job(command_payload):
    title = command_payload["title"]
    slug = ensure_non_empty_slug(title)
    return {
        "title": title,
        "season": command_payload["season"],
        "episodes_range": command_payload["episodes_range"],
        "source": {
            "type": "magnet",
            "magnet": command_payload["magnet"],
            "download_dir": f"downloads/{slug}",
        },
    }


def add_job_from_command(config, text):
    command_payload = parse_add_command(text)
    candidate_job = build_manual_job(command_payload)
    jobs = load_jobs(config)

    if find_matching_job(jobs, candidate_job) is not None:
        return {
            "added": False,
            "job": candidate_job,
            "reason": "duplicate_job",
        }

    jobs.append(candidate_job)
    save_jobs(config, jobs)
    return {
        "added": True,
        "job": candidate_job,
        "reason": None,
    }


def format_add_result(result):
    job = result["job"]
    if not result["added"]:
        return "\n".join([
            "Аниме не добавлено",
            "",
            f"Причина: {format_reason_ru(result['reason'])}",
            f"Тайтл: {get_display_title(job)}",
            f"Эпизоды: {job['episodes_range']}",
        ])

    return "\n".join([
        "Аниме добавлено",
        "",
        f"Тайтл: {get_display_title(job)}",
        f"Сезон: {job['season']}",
        f"Эпизоды: {job['episodes_range']}",
    ])


def build_help_message():
    return "\n".join([
        "Команды бота",
        "",
        "/start - краткая справка",
        "/status - статус очереди и runtime",
        "/jobs - показать последние аниме в очереди",
        "",
        "Пример:",
        "/add Мой тайтл : 001-003 : magnet:?xt=urn:btih:... : 1",
    ])


def handle_command(config, text):
    text = normalize_command_text(text)
    if text.startswith("/start"):
        return build_help_message()
    if text.startswith("/help"):
        return build_help_message()
    if text.startswith("/status"):
        return format_status_message(config)
    if text.startswith("/jobs"):
        return format_jobs_message(config)
    if text.startswith("/add "):
        return format_add_result(add_job_from_command(config, text))
    return "Неизвестная команда. Напиши /help"


def handle_update(config, update):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or not is_allowed_chat(chat_id):
        return False

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return False

    try:
        response_text = handle_command(config, text.strip())
    except Exception as exc:
        response_text = "\n".join([
            "Команда не выполнена",
            "",
            f"Причина: {exc}",
        ])
    send_reply(chat_id, response_text, include_keyboard=True)
    return True
