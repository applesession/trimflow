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
from lib.config import load_completed_jobs, load_jobs, load_state, save_completed_jobs, save_jobs
from lib.constants import DEFAULT_TELEGRAM_STATE_PATH
from lib.helpers import ensure_non_empty_slug, parse_episodes_range
from lib.runtime import ensure_runtime_paths, load_runtime_errors, load_runtime_status


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
            [{"text": "Статус"}, {"text": "Текущая"}],
            [{"text": "Очередь"}, {"text": "Ошибки"}],
            [{"text": "Лог"}, {"text": "Помощь"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def build_jobs_pagination_keyboard(has_previous, has_next):
    keyboard = []
    navigation_row = []
    if has_previous:
        navigation_row.append({"text": "Назад"})
    if has_next:
        navigation_row.append({"text": "Вперед"})
    if navigation_row:
        keyboard.append(navigation_row)

    keyboard.extend(build_main_keyboard()["keyboard"])
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def build_confirmation_keyboard(action_type):
    if action_type == "remove":
        buttons = [[{"text": "Подтвердить удаление"}, {"text": "Отменить удаление"}]]
    elif action_type == "complete":
        buttons = [[{"text": "Подтвердить завершение"}, {"text": "Отменить завершение"}]]
    else:
        buttons = [[{"text": "Подтвердить повтор"}, {"text": "Отменить повтор"}]]
    return {
        "keyboard": buttons,
        "resize_keyboard": True,
        "one_time_keyboard": True,
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
        "pending_actions": {},
        "jobs_pagination": {},
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
    state.setdefault("pending_actions", {})
    state.setdefault("jobs_pagination", {})
    return state


def save_telegram_state(state):
    path = get_telegram_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")


def update_telegram_state_progress(*, last_update_id=None, last_handled_at=None):
    state = load_telegram_state()
    if last_update_id is not None:
        state["last_update_id"] = last_update_id
    if last_handled_at is not None:
        state["last_handled_at"] = last_handled_at
    save_telegram_state(state)
    return state


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


def send_formatted_message(chat_id, text, *, parse_mode="MarkdownV2", reply_markup=None):
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": parse_mode,
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


def send_message_to_allowed_chats(text, *, parse_mode=None):
    if not telegram_notifications_enabled():
        return []

    results = []
    for chat_id in sorted(parse_allowed_chat_ids()):
        if parse_mode:
            results.append(send_formatted_message(chat_id, text, parse_mode=parse_mode))
        else:
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


def normalize_notification_error_reason(error):
    text = str(error or "").strip()
    if not text:
        return "неизвестная ошибка"

    gateway_match = re.search(r"(\d{3})\s+Server Error:\s*([^']+?)\s+for url:", text)
    if gateway_match:
        return f"{gateway_match.group(1)} {gateway_match.group(2).strip()}"

    http_match = re.search(r"(\d{3}\s+[A-Za-z][A-Za-z -]+)", text)
    if http_match:
        return http_match.group(1).strip()

    runtime_vk_match = re.search(r"failed:\s*(.+)$", text)
    if runtime_vk_match:
        return runtime_vk_match.group(1).strip()

    return text


def format_markdown_code(text):
    return "`" + str(text or "").replace("\\", "\\\\").replace("`", "\\`") + "`"


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
    comment_created = bool(vk_result.get("comment_created"))
    warning_reason = normalize_notification_error_reason(vk_result.get("error")) if vk_result.get("error") else None
    lines = [
        "✅ *Видео опубликовано в VK*",
        "",
        f"🎬 *{escape_markdown_v2(title)}*",
        f"📺 Эпизоды: {format_markdown_code(episodes_range)}",
        "",
    ]
    lines.append(
        "✔️ Пост на стене создан"
        if vk_result.get("post_created")
        else "✖️ Пост на стене не создан"
    )
    lines.append(
        "✔️ Первый комментарий создан"
        if comment_created
        else "✖️ Первый комментарий не создан"
    )
    if warning_reason and not comment_created:
        lines.append(f"└ {escape_markdown_v2(warning_reason)}")
    if vk_result.get("video_url"):
        lines.extend(["", f"🔗 {escape_markdown_v2(vk_result['video_url'])}"])
    return "\n".join(lines)


def format_vk_publish_error_message(job, error):
    title = get_display_title(job)
    reason = normalize_notification_error_reason(error)
    return "\n".join([
        "❌ *Ошибка публикации в VK*",
        "",
        f"🎬 *{escape_markdown_v2(title)}*",
        "🔧 Этап: `vk_publish`",
        "",
        f"Причина: {format_markdown_code(reason)}",
    ])


def format_datetime_ru(iso_value):
    if not iso_value:
        return "ещё не запускалось"
    try:
        parsed = datetime.fromisoformat(str(iso_value))
    except ValueError:
        return str(iso_value)
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M")


def format_runtime_stage_ru(stage):
    mapping = {
        "cron_start": "запуск cron",
        "discovery": "обновление очереди",
        "processing": "обработка очереди",
        "completed": "завершено",
        "failed": "завершено с ошибкой",
        "job_start": "старт аниме",
        "job_completed": "аниме обработано",
        "job_failed": "ошибка обработки",
        "validation": "подготовка",
        "download": "загрузка исходников",
        "episode_scan": "поиск серий",
        "detector": "поиск OP/ED",
        "render_segments": "вырезка сегментов",
        "concat": "склейка частей",
        "final_render": "финальный рендер",
        "delivery_s3": "сохранение манифеста",
        "delivery_vk": "публикация в VK",
        "job_done": "аниме готово",
    }
    return mapping.get(stage, stage or "неизвестно")


def shorten_error_message(message, limit=280):
    text = str(message or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_job_identity(job):
    source = job.get("source", {}) if isinstance(job, dict) else {}
    source_type = str(source.get("type", "")).strip().lower()
    if source_type == "magnet":
        source_signature = str(source.get("magnet", "")).strip()
    elif source_type == "local":
        source_signature = str(source.get("input_dir", "")).strip()
    else:
        source_signature = ""

    return "|".join([
        str(job.get("title", "")).strip().lower(),
        str(job.get("season", "")).strip(),
        str(job.get("episodes_range", "")).strip(),
        source_type,
        source_signature,
    ])


def get_pending_action(chat_id):
    state = load_telegram_state()
    return state.get("pending_actions", {}).get(str(chat_id))


def set_pending_action(chat_id, action):
    state = load_telegram_state()
    pending_actions = dict(state.get("pending_actions", {}))
    pending_actions[str(chat_id)] = action
    state["pending_actions"] = pending_actions
    save_telegram_state(state)


def clear_pending_action(chat_id):
    state = load_telegram_state()
    pending_actions = dict(state.get("pending_actions", {}))
    pending_actions.pop(str(chat_id), None)
    state["pending_actions"] = pending_actions
    save_telegram_state(state)


def get_jobs_pagination_page(chat_id):
    state = load_telegram_state()
    raw_value = state.get("jobs_pagination", {}).get(str(chat_id))
    if raw_value is None:
        return None
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return None


def set_jobs_pagination_page(chat_id, page):
    state = load_telegram_state()
    jobs_pagination = dict(state.get("jobs_pagination", {}))
    jobs_pagination[str(chat_id)] = max(1, int(page))
    state["jobs_pagination"] = jobs_pagination
    save_telegram_state(state)


def clear_jobs_pagination_page(chat_id):
    state = load_telegram_state()
    jobs_pagination = dict(state.get("jobs_pagination", {}))
    jobs_pagination.pop(str(chat_id), None)
    state["jobs_pagination"] = jobs_pagination
    save_telegram_state(state)


def tail_lines(path, limit):
    with open(path, "r", encoding="utf-8") as file:
        lines = file.read().splitlines()
    return lines[-int(limit):]


def escape_markdown_v2(text):
    value = str(text or "")
    for char in ["\\", "_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        value = value.replace(char, f"\\{char}")
    return value


def sanitize_code_block_content(text):
    return str(text or "").replace("```", "``\u200b`")


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


def format_current_message():
    runtime_status = load_runtime_status()
    run_status = runtime_status.get("run_status")
    current_job = runtime_status.get("current_job") or {}
    queue_progress = runtime_status.get("queue_progress") or {}
    last_run = runtime_status.get("last_run") or {}

    if run_status == "running" and current_job:
        return "\n".join([
            "Текущая обработка",
            "",
            f"Тайтл: {get_display_title(current_job)}",
            f"Сезон: {current_job.get('season', '?')}",
            f"Эпизоды: {current_job.get('episodes_range', '?')}",
            f"Этап: {format_runtime_stage_ru(current_job.get('stage') or runtime_status.get('current_stage'))}",
            (
                "Прогресс очереди: "
                f"{queue_progress.get('current_job_index', 0)}/{queue_progress.get('total_jobs', 0)}"
                f" | готово {queue_progress.get('jobs_processed', 0)}"
                f" | ошибок {queue_progress.get('jobs_failed', 0)}"
            ),
            f"Текущая серия: {current_job.get('current_episode') or 'ещё не началась'}",
            f"Всего серий: {current_job.get('total_episodes') or 'неизвестно'}",
            f"Файл серии: {current_job.get('current_episode_file') or 'ещё не выбран'}",
            f"Старт: {format_datetime_ru(current_job.get('started_at') or runtime_status.get('run_started_at'))}",
        ])

    if last_run:
        return "\n".join([
            "Сейчас ничего не обрабатывается",
            "",
            "Последний запуск",
            f"Тайтл: {get_display_title(last_run)}",
            f"Статус: {'успешно' if last_run.get('status') == 'completed' else 'с ошибкой'}",
            f"Финальный этап: {format_runtime_stage_ru(last_run.get('stage') or runtime_status.get('current_stage'))}",
            f"Завершено: {format_datetime_ru(last_run.get('finished_at') or runtime_status.get('run_finished_at'))}",
            (
                "Прогресс очереди: "
                f"готово {last_run.get('jobs_processed', 0)}"
                f" | ошибок {last_run.get('jobs_failed', 0)}"
            ),
            f"Последняя серия: {last_run.get('current_episode') or 'неизвестно'} / {last_run.get('total_episodes') or 'неизвестно'}",
        ])

    return "\n".join([
        "Сейчас ничего не обрабатывается",
        "История запусков пока пуста",
    ])


def format_errors_message(limit=5):
    runtime_errors = load_runtime_errors()
    items = list(runtime_errors.get("errors", []))[: int(limit)]
    if not items:
        return "\n".join([
            "Ошибок пока нет",
            "История сбоев ещё не накоплена",
        ])

    lines = ["Последние ошибки", ""]
    for item in items:
        title = get_display_title(item)
        series_text = ""
        if item.get("current_episode"):
            total = item.get("total_episodes") or "?"
            series_text = f"Серия: {item.get('current_episode')} / {total}\n"
        lines.append(
            "\n".join([
                f"{format_datetime_ru(item.get('created_at'))}",
                f"Контекст: {item.get('context') or 'неизвестно'}",
                f"Этап: {format_runtime_stage_ru(item.get('stage'))}",
                f"Тайтл: {title}",
                series_text + f"Ошибка: {shorten_error_message(item.get('message'))}",
            ])
        )
        lines.append("")

    return "\n".join(lines).rstrip()


def get_jobs_page_data(config, page=1, page_size=15):
    jobs = load_jobs(config)
    if not jobs:
        return {
            "jobs": [],
            "total_jobs": 0,
            "page": 1,
            "page_size": int(page_size),
            "total_pages": 1,
            "start_index": 0,
            "end_index": 0,
            "has_previous": False,
            "has_next": False,
        }

    page_size = max(1, int(page_size))
    total_jobs = len(jobs)
    total_pages = max(1, (total_jobs + page_size - 1) // page_size)
    page = max(1, min(int(page), total_pages))
    start_index = (page - 1) * page_size
    end_index = min(start_index + page_size, total_jobs)
    return {
        "jobs": jobs[start_index:end_index],
        "total_jobs": total_jobs,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "start_index": start_index,
        "end_index": end_index,
        "has_previous": page > 1,
        "has_next": page < total_pages,
    }


def format_jobs_message(config, page=1, page_size=15, numbered=True):
    page_data = get_jobs_page_data(config, page=page, page_size=page_size)
    if not page_data["jobs"]:
        return "Очередь пуста"

    lines = [
        "Очередь аниме",
        "",
        f"Всего: {page_data['total_jobs']}",
        f"Страница: {page_data['page']}/{page_data['total_pages']}",
        f"Показываю: {page_data['start_index'] + 1}-{page_data['end_index']}",
        "",
    ]
    for index, job in enumerate(page_data["jobs"], start=page_data["start_index"] + 1):
        prefix = f"{index}. " if numbered else "- "
        lines.append(f"{prefix}{get_display_title(job)}")
        lines.append(f"  Сезон: {job.get('season', 1)}")
        lines.append(f"  Эпизоды: {job.get('episodes_range', '?')}")
    return "\n".join(lines)


def build_jobs_message_response(config, chat_id, page=1, page_size=15):
    page_data = get_jobs_page_data(config, page=page, page_size=page_size)
    if not page_data["jobs"]:
        clear_jobs_pagination_page(chat_id)
        return "Очередь пуста"

    set_jobs_pagination_page(chat_id, page_data["page"])
    return {
        "text": format_jobs_message(
            config,
            page=page_data["page"],
            page_size=page_data["page_size"],
        ),
        "reply_markup": build_jobs_pagination_keyboard(
            page_data["has_previous"],
            page_data["has_next"],
        ),
    }


def format_log_message(lines_limit=20, max_chars=3500):
    log_path = ensure_runtime_paths()["log_path"]
    if not log_path.exists():
        return "Лог ещё не создан"

    lines = tail_lines(log_path, lines_limit)
    if not lines:
        return "Лог пока пуст"

    content = "\n".join(lines)
    if len(content) > max_chars:
        content = content[-max_chars:]
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 :]

    return "\n".join([
        f"Хвост лога ({min(lines_limit, len(lines))} строк)",
        "",
        content,
    ])


def format_log_message_markdown(lines_limit=20, max_chars=3500):
    raw_message = format_log_message(lines_limit=lines_limit, max_chars=max_chars)
    if raw_message in {"Лог ещё не создан", "Лог пока пуст"}:
        return raw_message

    lines = raw_message.splitlines()
    title = lines[0] if lines else "Хвост лога"
    content = "\n".join(lines[2:]) if len(lines) > 2 else ""
    return (
        f"{escape_markdown_v2(title)}\n\n"
        f"```log\n{sanitize_code_block_content(content)}\n```"
    )


def parse_index_command(text, command_name):
    normalized = str(text or "").strip()
    prefix = f"/{command_name}"
    if normalized == prefix:
        raise RuntimeError(f"Формат: /{command_name} <номер>")
    if not normalized.startswith(prefix + " "):
        raise RuntimeError(f"Формат: /{command_name} <номер>")

    raw_index = normalized[len(prefix):].strip()
    try:
        index = int(raw_index)
    except ValueError as exc:
        raise RuntimeError("Номер должен быть целым числом") from exc
    if index < 1:
        raise RuntimeError("Номер должен быть не меньше 1")
    return index


def get_job_by_index(config, index):
    jobs = load_jobs(config)
    if index > len(jobs):
        raise RuntimeError(f"Аниме с номером {index} не найдено")
    return jobs[index - 1], jobs


def build_failed_job_identities():
    runtime_errors = load_runtime_errors()
    identities = set()
    for item in runtime_errors.get("errors", []):
        if item.get("context") != "job_failed":
            continue
        job = {
            "title": item.get("title"),
            "season": item.get("season"),
            "episodes_range": item.get("episodes_range"),
            "source": {},
        }
        identities.add(build_job_identity(job))
    return identities


def build_retry_candidates(config):
    jobs = load_jobs(config)
    completed_jobs = load_completed_jobs(config)
    failed_identities = build_failed_job_identities()
    candidates = []

    for job in jobs:
        job_identity = build_job_identity(job)
        loose_identity = "|".join(job_identity.split("|")[:3] + ["", ""])
        if loose_identity in failed_identities or job_identity in failed_identities:
            candidates.append({
                "source": "failed_jobs",
                "label": "failed queue",
                "job": job,
                "job_identity": job_identity,
            })

    for item in completed_jobs:
        job = item.get("job") or {}
        candidates.append({
            "source": "completed_jobs",
            "label": "completed archive",
            "job": job,
            "job_identity": build_job_identity(job),
        })

    return candidates


def format_retry_candidates_message(config):
    candidates = build_retry_candidates(config)
    if not candidates:
        return "Кандидатов для повтора пока нет"

    lines = [
        "Кандидаты для повтора",
        "",
        f"Всего: {len(candidates)}",
        "",
    ]
    for index, candidate in enumerate(candidates, start=1):
        job = candidate["job"]
        lines.append(f"{index}. {get_display_title(job)}")
        lines.append(f"  Источник: {candidate['label']}")
        lines.append(f"  Сезон: {job.get('season', 1)}")
        lines.append(f"  Эпизоды: {job.get('episodes_range', '?')}")
    lines.extend(["", "Формат: /retry <номер>"])
    return "\n".join(lines)


def build_pending_action_payload(action_type, source, index, job_snapshot):
    return {
        "type": action_type,
        "source": source,
        "index": index,
        "job_identity": build_job_identity(job_snapshot),
        "created_at": datetime.now().astimezone().isoformat(),
        "job_snapshot": job_snapshot,
    }


def format_remove_confirmation(job, index):
    return "\n".join([
        "Подтверждение удаления",
        "",
        f"Номер: {index}",
        f"Тайтл: {get_display_title(job)}",
        f"Сезон: {job.get('season', 1)}",
        f"Эпизоды: {job.get('episodes_range', '?')}",
        "",
        "Подтверди удаление кнопкой ниже",
    ])


def format_retry_confirmation(candidate, index):
    job = candidate["job"]
    return "\n".join([
        "Подтверждение повтора",
        "",
        f"Номер: {index}",
        f"Источник: {candidate['label']}",
        f"Тайтл: {get_display_title(job)}",
        f"Сезон: {job.get('season', 1)}",
        f"Эпизоды: {job.get('episodes_range', '?')}",
        "",
        "Подтверди повтор кнопкой ниже",
    ])


def format_remove_result(job):
    return "\n".join([
        "Аниме удалено из очереди",
        "",
        f"Тайтл: {get_display_title(job)}",
        f"Сезон: {job.get('season', 1)}",
        f"Эпизоды: {job.get('episodes_range', '?')}",
    ])


def format_complete_confirmation(job, index):
    return "\n".join([
        "Подтверждение завершения",
        "",
        f"Номер: {index}",
        f"Тайтл: {get_display_title(job)}",
        f"Сезон: {job.get('season', 1)}",
        f"Эпизоды: {job.get('episodes_range', '?')}",
        "",
        "Аниме будет убрано из очереди и перенесено в completed_jobs.json",
        "Подтверди завершение кнопкой ниже",
    ])


def format_retry_result(job, already_exists=False):
    if already_exists:
        return "\n".join([
            "Повтор не выполнен",
            "",
            "Аниме уже находится в активной очереди",
            f"Тайтл: {get_display_title(job)}",
        ])

    return "\n".join([
        "Аниме повторно поставлено в очередь",
        "",
        f"Тайтл: {get_display_title(job)}",
        f"Сезон: {job.get('season', 1)}",
        f"Эпизоды: {job.get('episodes_range', '?')}",
    ])


def format_complete_result(job, already_archived=False):
    if already_archived:
        return "\n".join([
            "Аниме убрано из очереди",
            "",
            "Запись уже была в completed_jobs.json, дубль не добавлен",
            f"Тайтл: {get_display_title(job)}",
            f"Сезон: {job.get('season', 1)}",
            f"Эпизоды: {job.get('episodes_range', '?')}",
        ])

    return "\n".join([
        "Аниме перенесено в completed_jobs.json",
        "",
        f"Тайтл: {get_display_title(job)}",
        f"Сезон: {job.get('season', 1)}",
        f"Эпизоды: {job.get('episodes_range', '?')}",
    ])


def remove_job_by_identity(config, job_identity):
    jobs = load_jobs(config)
    remaining = []
    removed_job = None
    removed = False
    for job in jobs:
        if not removed and build_job_identity(job) == job_identity:
            removed_job = job
            removed = True
            continue
        remaining.append(job)
    if not removed_job:
        raise RuntimeError("Актуальная запись для удаления не найдена")
    save_jobs(config, remaining)
    return removed_job


def archive_job_to_completed(config, job, source="telegram_complete"):
    completed_jobs = load_completed_jobs(config)
    job_identity = build_job_identity(job)
    for item in completed_jobs:
        archived_job = item.get("job") or {}
        if build_job_identity(archived_job) == job_identity:
            return True

    completed_jobs.append({
        "status": "completed",
        "completed_at": datetime.now().astimezone().isoformat(),
        "job": job,
        "output_display_name": None,
        "output_video": None,
        "output_timestamps": None,
        "output_manifest": None,
        "delivery_summary": {},
        "partial_vk": False,
        "completion_source": source,
        "completion_note": "Manually archived from Telegram bot",
    })
    save_completed_jobs(config, completed_jobs)
    return False


def retry_job_to_queue(config, job):
    jobs = load_jobs(config)
    if find_matching_job(jobs, job) is not None:
        return False
    jobs.append(job)
    save_jobs(config, jobs)
    return True


def resolve_retry_candidate(config, index):
    candidates = build_retry_candidates(config)
    if index > len(candidates):
        raise RuntimeError(f"Кандидат с номером {index} не найден")
    return candidates[index - 1]


def confirm_pending_action(config, chat_id, text):
    actionable_texts = {
        "Подтвердить удаление",
        "Отменить удаление",
        "Подтвердить завершение",
        "Отменить завершение",
        "Подтвердить повтор",
        "Отменить повтор",
    }
    if text not in actionable_texts:
        return None

    pending = get_pending_action(chat_id)
    if not pending:
        return "Нет действия, ожидающего подтверждения"

    confirm_map = {
        "remove": "Подтвердить удаление",
        "complete": "Подтвердить завершение",
        "retry": "Подтвердить повтор",
    }
    cancel_map = {
        "remove": "Отменить удаление",
        "complete": "Отменить завершение",
        "retry": "Отменить повтор",
    }
    action_type = pending.get("type")
    if text == cancel_map.get(action_type):
        clear_pending_action(chat_id)
        return "Действие отменено"

    if text != confirm_map.get(action_type):
        return None

    clear_pending_action(chat_id)
    if action_type == "remove":
        removed_job = remove_job_by_identity(config, pending.get("job_identity"))
        return format_remove_result(removed_job)

    if action_type == "complete":
        removed_job = remove_job_by_identity(config, pending.get("job_identity"))
        already_archived = archive_job_to_completed(config, removed_job)
        return format_complete_result(removed_job, already_archived=already_archived)

    if action_type == "retry":
        job = pending.get("job_snapshot") or {}
        added = retry_job_to_queue(config, job)
        return format_retry_result(job, already_exists=not added)

    return "Неизвестное действие"


def normalize_command_text(text):
    normalized = str(text or "").strip()
    aliases = {
        "Статус": "/status",
        "Текущая": "/current",
        "Очередь": "/jobs",
        "Ошибки": "/errors",
        "Лог": "/log",
        "Помощь": "/help",
    }
    return aliases.get(normalized, normalized)


def parse_add_command(text):
    if not text.startswith("/add "):
        raise RuntimeError("Команда должна начинаться с /add ")

    raw_payload = text[len("/add "):]
    parts = [part.strip() for part in re.split(r"\s*;\s*", raw_payload)]
    if len(parts) not in {3, 4}:
        raise RuntimeError("Формат: /add Название ; 001-003 ; magnet:?xt=... ; 2")

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
        "/current - текущее или последнее выполнение",
        "/errors - последние ошибки выполнения",
        "/log - хвост cron.log",
        "/jobs - показать последние аниме в очереди",
        "/remove <номер> - удалить аниме из очереди",
        "/complete <номер> - убрать аниме из очереди и вручную пометить завершённым",
        "/retry <номер> - повторно поставить аниме в очередь",
        "",
        "Пример:",
        "/add Мой тайтл ; 001-003 ; magnet:?xt=urn:btih:... ; 1",
    ])


def handle_command(config, text):
    text = normalize_command_text(text)
    if text.startswith("/start"):
        return build_help_message()
    if text.startswith("/help"):
        return build_help_message()
    if text.startswith("/status"):
        return format_status_message(config)
    if text.startswith("/current"):
        return format_current_message()
    if text.startswith("/errors"):
        return format_errors_message()
    if text.startswith("/log"):
        return {
            "text": format_log_message_markdown(),
            "parse_mode": "MarkdownV2",
        }
    if text.startswith("/jobs"):
        parts = text.split(maxsplit=1)
        page = 1
        if len(parts) == 2:
            try:
                page = int(parts[1].strip())
            except ValueError as exc:
                raise RuntimeError("Формат: /jobs или /jobs <страница>") from exc
            if page < 1:
                raise RuntimeError("Номер страницы должен быть не меньше 1")
        return {
            "jobs_page": page,
        }
    if text.startswith("/remove"):
        index = parse_index_command(text, "remove")
        job, _jobs = get_job_by_index(config, index)
        return {
            "text": format_remove_confirmation(job, index),
            "reply_markup": build_confirmation_keyboard("remove"),
            "pending_action": build_pending_action_payload("remove", "jobs", index, job),
        }
    if text.startswith("/complete"):
        index = parse_index_command(text, "complete")
        job, _jobs = get_job_by_index(config, index)
        return {
            "text": format_complete_confirmation(job, index),
            "reply_markup": build_confirmation_keyboard("complete"),
            "pending_action": build_pending_action_payload("complete", "jobs", index, job),
        }
    if text == "/retry":
        return format_retry_candidates_message(config)
    if text.startswith("/retry "):
        index = parse_index_command(text, "retry")
        candidate = resolve_retry_candidate(config, index)
        return {
            "text": format_retry_confirmation(candidate, index),
            "reply_markup": build_confirmation_keyboard("retry"),
            "pending_action": build_pending_action_payload(
                "retry",
                candidate["source"],
                index,
                candidate["job"],
            ),
        }
    if text.startswith("/add "):
        return format_add_result(add_job_from_command(config, text))
    return "Неизвестная команда. Напиши /help"


def maybe_handle_jobs_navigation(config, chat_id, text):
    if text not in {"Назад", "Вперед"}:
        return None

    current_page = get_jobs_pagination_page(chat_id)
    if current_page is None:
        return "Список очереди не открыт. Напиши /jobs"

    if text == "Назад":
        return build_jobs_message_response(config, chat_id, page=max(1, current_page - 1))
    return build_jobs_message_response(config, chat_id, page=current_page + 1)


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
        normalized_text = normalize_command_text(text.strip())
        confirm_response = confirm_pending_action(config, chat_id, normalized_text)
        if confirm_response is not None:
            send_reply(chat_id, confirm_response, include_keyboard=True)
            return True

        navigation_response = maybe_handle_jobs_navigation(config, chat_id, normalized_text)
        if navigation_response is not None:
            if isinstance(navigation_response, dict):
                send_message(
                    chat_id,
                    navigation_response.get("text", ""),
                    reply_markup=navigation_response.get("reply_markup"),
                )
            else:
                send_reply(chat_id, navigation_response, include_keyboard=True)
            return True

        response = handle_command(config, normalized_text)
    except Exception as exc:
        response = "\n".join([
            "Команда не выполнена",
            "",
            f"Причина: {exc}",
        ])

    if isinstance(response, dict):
        if "jobs_page" in response:
            jobs_response = build_jobs_message_response(config, chat_id, page=response["jobs_page"])
            if isinstance(jobs_response, dict):
                send_message(
                    chat_id,
                    jobs_response.get("text", ""),
                    reply_markup=jobs_response.get("reply_markup"),
                )
            else:
                send_reply(chat_id, jobs_response, include_keyboard=True)
            return True

        pending_action = response.get("pending_action")
        if pending_action:
            set_pending_action(chat_id, pending_action)
        sender = send_formatted_message if response.get("parse_mode") else send_message
        sender(
            chat_id,
            response.get("text", ""),
            reply_markup=response.get("reply_markup"),
            **({"parse_mode": response.get("parse_mode")} if response.get("parse_mode") else {}),
        )
        return True

    send_reply(chat_id, response, include_keyboard=True)
    return True
