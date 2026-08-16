import json
import os
import re
import secrets
import socket
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

import requests
from urllib3.util import connection as urllib3_connection

from modules.autojobs import (
    add_release_to_blacklist,
    build_blacklist_item,
    find_matching_job,
    find_blacklist_item,
    format_episodes_range,
    get_job_release_id,
    mark_job_episodes_completed,
    mark_job_episodes_queued,
    remove_release_from_blacklist,
    unmark_job_episodes_queued,
)
from shared.config import load_completed_jobs, load_jobs, load_state, save_completed_jobs, save_jobs, save_state
from shared.constants import DEFAULT_TELEGRAM_STATE_PATH
from shared.db import (
    get_discovery_blacklist,
    get_episode_tracking_counts,
    insert_one_job,
    remove_job as _db_cancel_job,
    remove_pending_job as _db_remove_job,
    update_job_processing,
)
from shared.helpers import (
    clear_navigation_label,
    ensure_non_empty_slug,
    get_display_title,
    get_navigation_label,
    parse_episodes_range,
    set_navigation_label,
)
from core.runner import build_execution_order
from shared.runtime import ensure_runtime_paths, load_runtime_errors, load_runtime_status


TELEGRAM_API_BASE = "https://api.telegram.org"


def format_bool_ru(value):
    return "да" if value else "нет"


def _navigation_lines(job, prefix="Метка: "):
    label = get_navigation_label(job)
    return [f"{prefix}{label}"] if label else []


def _job_inline_details(job):
    return " ".join(
        part for part in [get_navigation_label(job), job.get("episodes_range", "?")] if part
    )


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


def build_jobs_inline_keyboard(has_previous, has_next, page, total_pages):
    row = []

    if has_previous:
        row.append({"text": "« Назад", "callback_data": f"jobs:page:{page - 1}"})
    else:
        row.append({"text": f"{page}/{total_pages}", "callback_data": f"jobs:page:{page}"})

    if has_next:
        row.append({"text": "Вперед »", "callback_data": f"jobs:page:{page + 1}"})
    elif has_previous:
        row.append({"text": f"{page}/{total_pages}", "callback_data": f"jobs:page:{page}"})

    return {"inline_keyboard": [row]} if row else None


def build_confirmation_keyboard(action_type):
    if action_type == "remove":
        buttons = [[{"text": "Подтвердить удаление"}, {"text": "Отменить удаление"}]]
    elif action_type == "complete":
        buttons = [[{"text": "Подтвердить завершение"}, {"text": "Отменить завершение"}]]
    elif action_type == "blacklist":
        buttons = [[{"text": "Подтвердить blacklist"}, {"text": "Отменить blacklist"}]]
    elif action_type == "unblacklist":
        buttons = [[{"text": "Подтвердить снятие blacklist"}, {"text": "Отменить снятие blacklist"}]]
    else:
        buttons = [[{"text": "Подтвердить повтор"}, {"text": "Отменить повтор"}]]
    return {
        "keyboard": buttons,
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def build_inline_details_keyboard(token):
    return {
        "inline_keyboard": [
            [{"text": "Подробно", "callback_data": f"details:{token}"}],
        ]
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
        "notification_details": {},
    }


def load_telegram_state():
    path = get_telegram_state_path()
    if not path.exists():
        return build_default_telegram_state()

    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        backup = path.with_name(
            f"{path.name}.corrupt.{datetime.now().strftime('%Y%m%dT%H%M%S%f')}"
        )
        os.replace(path, backup)
        print(f"[TELEGRAM STATE] Corrupt state moved to {backup}: {exc}")
        return build_default_telegram_state()

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    state = build_default_telegram_state()
    state.update(data)
    state.setdefault("pending_actions", {})
    state.setdefault("jobs_pagination", {})
    state.setdefault("notification_details", {})
    return state


def save_telegram_state(state):
    path = get_telegram_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        description = None
        try:
            error_payload = response.json()
            description = error_payload.get("description") or error_payload
        except ValueError:
            description = response.text.strip() or None
        if description is not None:
            raise RuntimeError(
                f"Telegram API {method} HTTP {response.status_code}: {description}",
            ) from exc
        raise
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data.get('description') or data}")
    return data


def fetch_updates(offset=None, timeout=30):
    payload = {
        "timeout": timeout,
        "allowed_updates": ["message", "callback_query"],
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


def send_message_with_fallback(chat_id, text, *, parse_mode=None, reply_markup=None):
    if parse_mode:
        try:
            return send_formatted_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
        except RuntimeError as exc:
            if parse_mode == "MarkdownV2" and telegram_markdown_retryable_error(exc):
                return send_message(chat_id, demote_markdown_v2_to_plain_text(text), reply_markup=reply_markup)
            raise
    return send_message(chat_id, text, reply_markup=reply_markup)


def send_reply(chat_id, text, include_keyboard=True):
    reply_markup = build_main_keyboard() if include_keyboard else None
    return send_message(chat_id, text, reply_markup=reply_markup)


def answer_callback_query(callback_query_id, text=None):
    payload = {
        "callback_query_id": str(callback_query_id),
    }
    if text:
        payload["text"] = text
    return _telegram_request("answerCallbackQuery", payload=payload, timeout=20)


def edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    payload = {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "text": text,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _telegram_request("editMessageText", payload=payload, timeout=20)


def send_message_to_allowed_chats(text, *, parse_mode=None, reply_markup=None):
    if not telegram_notifications_enabled():
        return []

    results = []
    for chat_id in sorted(parse_allowed_chat_ids()):
        results.append(send_message_with_fallback(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup))
    return results


def format_discovery_message(summary, jobs_added):
    titles = [get_display_title(job) for job in jobs_added]
    lines = [
        "🛰️ *Автодискавери завершён*",
        "",
        f"🆕 Новых аниме: {format_markdown_code(summary.get('created_jobs', 0))}",
        f"🔄 Обновлено аниме: {format_markdown_code(summary.get('updated_jobs', 0))}",
    ]
    if titles:
        lines.extend(["", "🎬 *Новые тайтлы:*"])
        lines.extend(f"• {escape_markdown_v2(title)}" for title in titles)
    return "\n".join(lines)


def split_notification_context(context):
    normalized = str(context or "").strip()
    if ":" in normalized:
        stage, title = normalized.split(":", 1)
        return stage.strip(), title.strip()
    return normalized, None


def format_error_message(context, error):
    stage, title = split_notification_context(context)
    reason = normalize_notification_error_reason(error)

    lines = [
        "❌ *Ошибка выполнения*",
        "",
    ]
    if title:
        lines.append(f"🎬 *{escape_markdown_v2(title)}*")
    lines.append(f"🔧 Этап: {format_markdown_code(stage or 'unknown')}")
    lines.extend([
        "",
        f"Причина: {format_markdown_code(reason)}",
    ])
    return "\n".join(lines)


def format_render_interrupted_message(job, reason, runtime_status, lock_payload=None):
    runtime_status = runtime_status or {}
    current_job = runtime_status.get("current_job") or {}
    if current_job.get("title") != job.get("title"):
        current_job = {}
    lock_payload = lock_payload or {}

    lines = [
        "⚠️ *Render аварийно прерван*",
        "",
        f"🎬 *{escape_markdown_v2(get_display_title(job))}*",
        f"📺 Серии: {format_markdown_code(job.get('episodes_range') or '?')}",
        f"Причина: {format_markdown_code(reason)}",
    ]
    if lock_payload.get("pid"):
        lines.append(f"PID: {format_markdown_code(lock_payload['pid'])}")
    if lock_payload.get("started_at"):
        lines.append(f"Старт процесса: {format_markdown_code(format_datetime_ru(lock_payload['started_at']))}")

    stage = current_job.get("stage") or runtime_status.get("current_stage")
    if stage:
        lines.append(f"Этап: {format_markdown_code(format_runtime_stage_ru(stage))}")
    if current_job.get("current_chunk_index"):
        lines.append(
            "Чанк: "
            f"{format_markdown_code(current_job['current_chunk_index'])}/"
            f"{format_markdown_code(current_job.get('total_chunks') or '?')}"
        )
    if current_job.get("current_episode"):
        lines.append(
            "Серия: "
            f"{format_markdown_code(current_job['current_episode'])}/"
            f"{format_markdown_code(current_job.get('total_episodes') or '?')}"
        )

    lines.extend([
        "",
        "Задача возвращена в очередь",
        "Следующий запуск продолжит с checkpoint",
    ])
    return "\n".join(lines)


def format_download_timeout_message(job):
    display_title = get_display_title(job)
    episodes_range = job.get("episodes_range", "")
    episode_count = len(parse_episodes_range(episodes_range))

    download_cfg = job.get("download") or {}
    timeout_minutes = max(
        int(download_cfg.get("timeout_minutes_minimum", 30)),
        min(
            episode_count * int(download_cfg.get("timeout_minutes_per_episode", 20)),
            int(download_cfg.get("timeout_minutes_maximum", 1440)),
        ),
    )

    lines = [
        "⏰ *Пропущена загрузка*",
        "",
        f"🎬 *{escape_markdown_v2(display_title)}*",
        f"📦 Эпизодов: {format_markdown_code(episode_count)}",
        f"⏱️ Таймаут: {format_markdown_code(timeout_minutes)} мин",
    ]
    return "\n".join(lines)


def normalize_notification_error_reason(error):
    text = str(error or "").strip()
    if not text:
        return "неизвестная ошибка"

    ffmpeg_wrap_match = re.search(r"ffmpeg exited with code (\d+): (.+)", text)
    if ffmpeg_wrap_match:
        exit_code = ffmpeg_wrap_match.group(1)
        cmd_tail = ffmpeg_wrap_match.group(2)
        file_match = re.search(r"([^\s\\/]+\.m(?:kv|p4|ov))(?:\s|$)", cmd_tail)
        detail = file_match.group(1) if file_match else cmd_tail[-80:]
        return f"ffmpeg code {exit_code} — {detail}"

    called_process_match = re.search(r"CalledProcessError\((\d+),", text)
    if called_process_match:
        return f"ffmpeg exited with code {called_process_match.group(1)}"

    gateway_match = re.search(r"(\d{3})\s+Server Error:\s*([^']+?)\s+for url:", text)
    if gateway_match:
        return f"{gateway_match.group(1)} {gateway_match.group(2).strip()}"

    http_match = re.search(r"(\d{3}\s+[A-Za-z][A-Za-z -]+)", text)
    if http_match:
        return http_match.group(1).strip()

    runtime_vk_match = re.search(r"failed:\s*(.+)", text, re.DOTALL)
    if runtime_vk_match:
        return runtime_vk_match.group(1).strip()

    return text


def format_markdown_code(text):
    return "`" + str(text or "").replace("\\", "\\\\").replace("`", "\\`") + "`"


def format_skip_counts_line(quality_summary):
    summary = quality_summary or {}
    episodes_count = summary.get("episodes_count")
    op_removed = summary.get("episodes_with_op_removed")
    ed_removed = summary.get("episodes_with_ed_removed")
    if episodes_count is None or op_removed is None or ed_removed is None:
        return None
    return (
        f"✂️ OP: {format_markdown_code(f'{op_removed}/{episodes_count}')}"
        f" • ED: {format_markdown_code(f'{ed_removed}/{episodes_count}')}"
    )


def _build_strategy_lines(quality_summary):
    summary = quality_summary or {}
    strategy_labels = {
        "episodes_anilibria_only": "anilibria_only",
        "episodes_anilibria_with_detector": "anilibria_with_detector",
        "episodes_aniskip_only": "aniskip_only",
        "episodes_aniskip_with_detector": "aniskip_with_detector",
        "episodes_detector_only": "detector_only",
        "episodes_manual_review": "manual_review",
    }
    lines = []
    for key, label in strategy_labels.items():
        value = int(summary.get(key, 0) or 0)
        if value > 0:
            lines.append(f"• {escape_markdown_v2(label)}: {format_markdown_code(value)}")
    return lines


def _format_delivery_status_line(label, value):
    normalized = "ok" if value else "failed"
    return f"• {escape_markdown_v2(label)}: {format_markdown_code(normalized)}"


def format_job_details_message(payload):
    job = payload.get("job") or {}
    quality_summary = payload.get("quality_summary") or {}
    delivery_summary = payload.get("delivery_summary") or {}
    title = get_display_title(job)
    episodes_range = job.get("episodes_range", "?")
    lines = [
        "ℹ️ *Подробно*",
        "",
        f"🎬 *{escape_markdown_v2(title)}*",
        f"📺 Эпизоды: {format_markdown_code(episodes_range)}",
    ]

    skip_counts_line = format_skip_counts_line(quality_summary)
    if skip_counts_line:
        lines.extend(["", skip_counts_line])

    warnings_count = len(quality_summary.get("episodes_with_warnings", []) or [])
    manual_review_count = int(quality_summary.get("episodes_manual_review", 0) or 0)
    lines.extend([
        f"⚠️ Warnings: {format_markdown_code(warnings_count)}",
        f"🛠 Manual review: {format_markdown_code(manual_review_count)}",
    ])
    recovered_episodes = quality_summary.get("episodes_audio_recovery", []) or []
    if recovered_episodes:
        lines.append(
            f"🎧 Audio recovery: {format_markdown_code(','.join(map(str, recovered_episodes)))}"
        )

    strategy_lines = _build_strategy_lines(quality_summary)
    if strategy_lines:
        lines.extend(["", "🧭 *Стратегии:*", *strategy_lines])

    delivery_lines = []
    vk_summary = delivery_summary.get("vk", {})
    s3_summary = delivery_summary.get("s3", {})
    if vk_summary.get("enabled"):
        delivery_lines.append(_format_delivery_status_line("VK video", vk_summary.get("video_uploaded")))
        delivery_lines.append(_format_delivery_status_line("VK post", vk_summary.get("post_created")))
        delivery_lines.append(_format_delivery_status_line("VK comment", vk_summary.get("comment_created")))
        if vk_summary.get("preview_attempted"):
            delivery_lines.append(_format_delivery_status_line("VK preview", vk_summary.get("preview_attached")))
    if s3_summary.get("enabled"):
        delivery_lines.append(_format_delivery_status_line("S3 upload", s3_summary.get("uploaded")))
    if delivery_lines:
        lines.extend(["", "🚚 *Delivery:*", *delivery_lines])

    error_reason = None
    if vk_summary.get("error"):
        error_reason = normalize_notification_error_reason(vk_summary.get("error"))
    elif s3_summary.get("error"):
        error_reason = normalize_notification_error_reason(s3_summary.get("error"))
    if error_reason:
        lines.extend(["", f"Причина partial failure: {format_markdown_code(error_reason)}"])
    preview_error = vk_summary.get("preview_error")
    if preview_error:
        lines.append(f"Ошибка VK preview: {format_markdown_code(normalize_notification_error_reason(preview_error))}")

    return "\n".join(lines)


def build_notification_details_payload(job, result):
    return {
        "type": "job_result_details",
        "created_at": datetime.now().astimezone().isoformat(),
        "job": {
            "title": job.get("title"),
            "title_ru": job.get("title_ru"),
            "season": job.get("season"),
            "episodes_range": job.get("episodes_range"),
        },
        "quality_summary": result.get("quality_summary", {}),
        "delivery_summary": result.get("delivery_summary", {}),
    }


def trim_notification_details(details_map, limit=50):
    items = sorted(
        details_map.items(),
        key=lambda item: item[1].get("created_at", ""),
        reverse=True,
    )
    return dict(items[:limit])


def save_notification_details(payload):
    state = load_telegram_state()
    details = dict(state.get("notification_details", {}))
    token = secrets.token_hex(8)
    details[token] = payload
    state["notification_details"] = trim_notification_details(details)
    save_telegram_state(state)
    return token


def load_notification_details(token):
    state = load_telegram_state()
    return state.get("notification_details", {}).get(str(token))


def build_notification_details_reply_markup(payload):
    token = save_notification_details(payload)
    return build_inline_details_keyboard(token)


def format_publish_success_message(job, output_path_or_key=None, quality_summary=None):
    title = get_display_title(job)
    episodes_range = job.get("episodes_range", "?")
    lines = [
        "✅ *Обработка завершена*",
        "",
        f"🎬 *{escape_markdown_v2(title)}*",
        f"📺 Эпизоды: {format_markdown_code(episodes_range)}",
    ]
    skip_counts_line = format_skip_counts_line(quality_summary)
    if skip_counts_line:
        lines.extend(["", skip_counts_line])
    if output_path_or_key:
        lines.extend(["", f"📦 Результат: {format_markdown_code(output_path_or_key)}"])
    return "\n".join(lines)


def format_vk_publish_success_message(job, vk_result, quality_summary=None):
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
    skip_counts_line = format_skip_counts_line(quality_summary)
    if skip_counts_line:
        lines.append(skip_counts_line)
        lines.append("")
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
    if vk_result.get("preview_attempted") and not vk_result.get("preview_attached"):
        lines.append("⚠️ AI preview не собралась, пост опубликован без неё")
    if warning_reason and not comment_created:
        lines.append(f"└ {escape_markdown_v2(warning_reason)}")
    elif vk_result.get("preview_error"):
        lines.append(
            f"└ {escape_markdown_v2(normalize_notification_error_reason(vk_result.get('preview_error')))}"
        )
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
        "render_episode": "рендер серии",
        "concat": "склейка частей",
        "final_render": "финальный рендер",
        "delivery_s3": "сохранение манифеста",
        "delivery_vk": "публикация в VK",
        "job_done": "аниме готово",
        "upscale_download": "загрузка исходников 4K",
        "upscale_render": "4K upscale",
        "upscale_delivery_vk": "публикация 4K в VK",
        "upscale_failed": "ошибка 4K-worker",
        "job_cancelled": "отменено пользователем",
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
    processing_mode = str(job.get("processing_mode", "compilation") if isinstance(job, dict) else "compilation").strip().lower()
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
        processing_mode,
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
    limit = max(0, int(limit))
    if not limit:
        return []

    with open(path, "rb") as file:
        file.seek(0, os.SEEK_END)
        position = file.tell()
        chunks = []
        separators = 0
        while position > 0 and separators <= limit:
            chunk_size = min(64 * 1024, position)
            position -= chunk_size
            file.seek(position)
            chunk = file.read(chunk_size)
            chunks.append(chunk)
            separators += chunk.count(b"\n") + chunk.count(b"\r")

    return b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-limit:]


def escape_markdown_v2(text):
    value = str(text or "")
    for char in ["\\", "_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        value = value.replace(char, f"\\{char}")
    return value


def demote_markdown_v2_to_plain_text(text):
    value = str(text or "")
    value = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", value)
    value = value.replace("*", "").replace("`", "")
    return value


def telegram_markdown_retryable_error(error):
    message = str(error or "").lower()
    return (
        "can't parse entities" in message
        or "cannot parse entities" in message
        or "can't find end of" in message
    )


def sanitize_code_block_content(text):
    return str(text or "").replace("```", "``\u200b`")


def format_status_message(config):
    jobs = load_jobs(config)
    state = load_state(config)
    runtime_status = load_runtime_status()
    current_job = runtime_status.get("current_job") or {}
    active_title = (
        get_display_title(current_job)
        if runtime_status.get("run_status") == "running" and current_job
        else None
    )
    episode_counts = get_episode_tracking_counts()

    lines = [
        "Статус пайплайна",
        "",
        f"Активная задача: {active_title or 'сейчас ничего не обрабатывается'}",
        f"Аниме в очереди: {len(jobs)}",
        f"Последнее обновление очереди: {format_datetime_ru(state.get('last_discovery_at'))}",
        f"Эпизодов в очереди: {episode_counts['queued']}",
        f"Завершённых эпизодов: {episode_counts['completed']}",
        f"В blacklist discovery: {len(get_blacklist_entries(config))}",
        "Выполнение может идти с приоритетом ongoing, даже если порядок в /jobs другой",
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
            *_navigation_lines(current_job),
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


def format_upscale_message():
    status_path = ensure_runtime_paths()["runtime_dir"] / "upscale_status.json"
    runtime_status = load_runtime_status(status_path)
    current_job = runtime_status.get("current_job") or {}
    last_run = runtime_status.get("last_run") or {}
    if runtime_status.get("run_status") == "running" and current_job:
        return "\n".join([
            "Текущий 4K upscale",
            "",
            f"Тайтл: {get_display_title(current_job)}",
            *_navigation_lines(current_job),
            f"Серия: {current_job.get('current_episode') or 'подготовка'} / {current_job.get('total_episodes') or '?'}",
            f"Этап: {format_runtime_stage_ru(current_job.get('stage') or runtime_status.get('current_stage'))}",
            f"Старт: {format_datetime_ru(current_job.get('started_at') or runtime_status.get('run_started_at'))}",
        ])
    if last_run:
        return "\n".join([
            "4K-worker сейчас свободен",
            "",
            f"Последний тайтл: {get_display_title(last_run)}",
            f"Статус: {'успешно' if last_run.get('status') == 'completed' else 'с ошибкой'}",
            f"Последняя серия: {last_run.get('current_episode') or '?'} / {last_run.get('total_episodes') or '?'}",
        ])
    return "4K-worker сейчас свободен\nИстория запусков пока пуста"


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
        chunk_text = ""
        if item.get("current_chunk_index"):
            total_chunks = item.get("total_chunks") or "?"
            chunk_text = f"Чанк: {item.get('current_chunk_index')} / {total_chunks}\n"
        lines.append(
            "\n".join([
                f"{format_datetime_ru(item.get('created_at'))}",
                f"Контекст: {item.get('context') or 'неизвестно'}",
                f"Этап: {format_runtime_stage_ru(item.get('stage'))}",
                f"Тайтл: {title}",
                chunk_text + series_text + f"Ошибка: {shorten_error_message(item.get('message'))}",
            ])
        )
        lines.append("")

    return "\n".join(lines).rstrip()


def get_jobs_page_data(config, page=1, page_size=15, jobs=None):
    jobs = load_jobs(config) if jobs is None else jobs
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


def format_jobs_message(config, page=1, page_size=15, numbered=True, jobs=None):
    page_data = get_jobs_page_data(config, page=page, page_size=page_size, jobs=jobs)
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
        ongoing_marker = " [ongoing]" if (job.get("automation") or {}).get("is_ongoing") else ""
        upscale_marker = " [4K]" if job.get("processing_mode") == "upscale_4k" else ""
        audiofix_marker = (
            " [audiofix]"
            if (job.get("processing") or {}).get("audio_recovery_enabled")
            else ""
        )
        lines.append(
            f"{prefix}{get_display_title(job)}{ongoing_marker}{upscale_marker}{audiofix_marker}"
        )
        lines.extend(_navigation_lines(job, "  Метка: "))
        lines.append(f"  Эпизоды: {job.get('episodes_range', '?')}")
    return "\n".join(lines)


def format_jobs_message_markdown(config, page=1, page_size=15, jobs=None):
    jobs = load_jobs(config) if jobs is None else jobs
    if not jobs:
        return "Очередь пуста"

    # Sort by execution priority
    sorted_jobs = build_execution_order(jobs, defaults=config.get("defaults", {}))
    total = len(sorted_jobs)
    total_pages = max(1, (total + page_size - 1) // page_size)
    p = max(1, min(page, total_pages))
    start = (p - 1) * page_size
    end = min(start + page_size, total)
    page_jobs = sorted_jobs[start:end]

    donut = []
    ongoing = []
    manual = []
    for i, job in enumerate(page_jobs):
        is_donut = (job.get("delivery") or {}).get("vk_privacy_view") == 5
        is_ongoing = (job.get("automation") or {}).get("is_ongoing")
        idx = start + i + 1
        if is_donut:
            donut.append((idx, job))
        elif is_ongoing:
            ongoing.append((idx, job))
        else:
            manual.append((idx, job))

    def _job_line(index, job):
        title = escape_markdown_v2(get_display_title(job))
        navigation_label = get_navigation_label(job)
        eps = job.get("episodes_range", "?")
        is_single = (job.get("processing_mode") or "").strip().lower() == "single_episode"
        eps_label = "серия" if is_single else "серии"
        mode_labels = []
        if job.get("processing_mode") == "upscale_4k":
            mode_labels.append("`4K`")
        if (job.get("processing") or {}).get("audio_recovery_enabled"):
            mode_labels.append("`audiofix`")
        mode_label = " · " + " · ".join(mode_labels) if mode_labels else ""
        details = [f"{eps_label} `{eps}`"]
        if navigation_label:
            details.insert(0, escape_markdown_v2(navigation_label))
        return [
            f"*{index}\\. {title}*",
            f"└ {' · '.join(details)}{mode_label}",
        ]

    title_word = "тайтл" if total == 1 else ("тайтла" if 2 <= total <= 4 else "тайтлов")
    header = f"📋 Очередь аниме\n\n{total} {title_word} · Страница `{p}/{total_pages}` · \\#{start + 1}–{end}"
    lines = [header, ""]

    for group_title, group_jobs in [
        ("🔄 Онгоинги", ongoing),
        ("💎 Доны", donut),
        ("✏️ Вручную", manual),
    ]:
        if not group_jobs:
            continue
        lines.append(group_title)
        lines.append("")
        for idx, job in group_jobs:
            lines.extend(_job_line(idx, job))
        lines.append("")

    return "\n".join(lines).rstrip()


def build_jobs_message_response(config, chat_id, page=1, page_size=15):
    jobs = load_jobs(config)
    page_data = get_jobs_page_data(config, page=page, page_size=page_size, jobs=jobs)
    if not page_data["jobs"]:
        clear_jobs_pagination_page(chat_id)
        return "Очередь пуста"

    set_jobs_pagination_page(chat_id, page_data["page"])
    return {
        "text": format_jobs_message(
            config,
            page=page_data["page"],
            page_size=page_data["page_size"],
            jobs=jobs,
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


def parse_index_range(text, command_name):
    """Parse index range: '5', '1-10', '1,5,8-10' → sorted list of ints."""
    normalized = str(text or "").strip()
    prefix = f"/{command_name}"
    if normalized == prefix:
        raise RuntimeError(f"Формат: /{command_name} <номер> или <1\\-10> или <1,5,8\\-10>")
    if not normalized.startswith(prefix + " "):
        raise RuntimeError(f"Формат: /{command_name} <номер>")

    raw = normalized[len(prefix):].strip()
    indices = set()
    for part in re.split(r"\s*,\s*", raw):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a.strip())
            end = int(b.strip())
            if start < 1 or start > end:
                raise RuntimeError(f"Неверный диапазон: {part}")
            indices.update(range(start, end + 1))
        else:
            idx = int(part)
            if idx < 1:
                raise RuntimeError("Номер должен быть не меньше 1")
            indices.add(idx)

    if not indices:
        raise RuntimeError("Не указано ни одного номера")
    return sorted(indices)


def parse_index_command(text, command_name):
    """Parse a single index. Kept for /unblacklist, /retry."""
    return parse_index_range(text, command_name)[0]


def parse_label_command(text):
    if not str(text or "").startswith("/label "):
        raise RuntimeError("Формат: /label <номер> ; <метка|auto>")
    parts = [part.strip() for part in text[len("/label "):].split(";", 1)]
    if len(parts) != 2:
        raise RuntimeError("Формат: /label <номер> ; <метка|auto>")
    try:
        index = int(parts[0])
    except ValueError as exc:
        raise RuntimeError("Номер должен быть целым числом") from exc
    if index < 1:
        raise RuntimeError("Номер должен быть не меньше 1")
    label = parts[1]
    if not label:
        raise RuntimeError("Метка не должна быть пустой")
    if len(label) > 50:
        raise RuntimeError("Метка должна быть не длиннее 50 символов")
    return index, label


def update_job_navigation_label(config, text):
    index, label = parse_label_command(text)
    job, _ = get_job_by_index(config, index)
    release_id = get_job_release_id(job)
    state = load_state(config)
    overrides = dict(state.get("release_naming_overrides", {}))

    if label.casefold() == "auto":
        clear_navigation_label(job)
        if release_id is not None:
            overrides.pop(str(release_id), None)
    else:
        set_navigation_label(job, label, source="manual")
        if release_id is not None:
            overrides[str(release_id)] = label

    queue_id = job.get("_queue_id")
    if queue_id is None or not update_job_processing(queue_id, job.get("processing")):
        raise RuntimeError("Задача уже изменилась; обнови /jobs и повтори")

    state["release_naming_overrides"] = overrides
    save_state(config, state)
    lines = [
        "Навигационная метка обновлена",
        "",
        f"Тайтл: {get_display_title(job)}",
    ]
    lines.extend(_navigation_lines(job))
    if not get_navigation_label(job):
        lines.append("Метка удалена")
    return "\n".join(lines)


def update_job_audio_recovery(config, text, enabled):
    command_name = "audiofix-on" if enabled else "audiofix-off"
    indices = parse_index_range(text, command_name)
    selected_jobs, all_jobs = get_jobs_by_indices(config, indices)
    if any(job.get("processing_mode") == "upscale_4k" for job in selected_jobs):
        raise RuntimeError("Audio recovery не поддерживается для 4K job")

    release_ids = {
        get_job_release_id(job)
        for job in selected_jobs
        if get_job_release_id(job) is not None
    }
    selected_queue_ids = {
        job.get("_queue_id") for job in selected_jobs if job.get("_queue_id") is not None
    }
    jobs_to_update = [
        job
        for job in all_jobs
        if job.get("processing_mode") != "upscale_4k"
        and (
            job.get("_queue_id") in selected_queue_ids
            or get_job_release_id(job) in release_ids
        )
    ]

    for job in jobs_to_update:
        processing = dict(job.get("processing") or {})
        if enabled:
            processing["audio_recovery_enabled"] = True
        else:
            processing.pop("audio_recovery_enabled", None)
        if not update_job_processing(job.get("_queue_id"), processing or None):
            raise RuntimeError("Задача уже изменилась; обнови /jobs и повтори")

    state = load_state(config)
    overrides = dict(state.get("release_audio_recovery_overrides", {}))
    for release_id in release_ids:
        if enabled:
            overrides[str(release_id)] = True
        else:
            overrides.pop(str(release_id), None)
    state["release_audio_recovery_overrides"] = overrides
    save_state(config, state)

    running = any(job.get("_queue_status") == "running" for job in jobs_to_update)
    lines = [
        "Audio recovery включён" if enabled else "Audio recovery выключен",
        "",
        f"Задач обновлено: {len(jobs_to_update)}",
        "Режим применяется только к аномальным сериям" if enabled else "Следующий render будет строгим",
    ]
    if running:
        lines.extend(["", "Активный render не изменится; настройка действует со следующего запуска"])
    return "\n".join(lines)


def _get_execution_order(config):
    jobs = load_jobs(config)
    return build_execution_order(jobs, defaults=config.get("defaults", {}))


def get_job_by_index(config, index):
    sorted_jobs = _get_execution_order(config)
    if index < 1 or index > len(sorted_jobs):
        raise RuntimeError(f"Аниме с номером {index} не найдено")
    return sorted_jobs[index - 1], sorted_jobs


def get_jobs_by_indices(config, indices):
    sorted_jobs = _get_execution_order(config)
    result = []
    for idx in indices:
        if idx < 1 or idx > len(sorted_jobs):
            raise RuntimeError(f"Аниме с номером {idx} не найдено")
        result.append(sorted_jobs[idx - 1])
    return result, sorted_jobs


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
        lines.extend(_navigation_lines(job, "  Метка: "))
        lines.append(f"  Эпизоды: {job.get('episodes_range', '?')}")
    lines.extend(["", "Формат: /retry <номер>"])
    return "\n".join(lines)


def build_pending_action_payload(action_type, source, index_or_indices, job_snapshot):
    indices = index_or_indices if isinstance(index_or_indices, list) else [index_or_indices]
    jobs = job_snapshot if isinstance(job_snapshot, list) else [job_snapshot]
    return {
        "type": action_type,
        "source": source,
        "indices": indices,
        "job_identities": [build_job_identity(j) for j in jobs],
        "created_at": datetime.now().astimezone().isoformat(),
        "job_snapshots": jobs,
        # legacy fields for backward compat
        "index": indices[0],
        "job_identity": build_job_identity(jobs[0]),
        "job_snapshot": jobs[0],
    }


def get_blacklist_entries(config):
    entries = get_discovery_blacklist()
    entries.sort(key=lambda item: (
        str(item.get("title_ru") or item.get("title") or "").strip().lower(),
        int(item.get("season") or 1),
        int(item.get("release_id") or 0),
    ))
    return entries


def build_blacklist_pending_action_payload(action_type, index, blacklist_item):
    return {
        "type": action_type,
        "source": "blacklist",
        "index": index,
        "created_at": datetime.now().astimezone().isoformat(),
        "blacklist_item": dict(blacklist_item or {}),
    }


def _format_job_list(jobs, indices):
    """Format a list of jobs with their indices for confirmation dialogs."""
    lines = []
    for idx, job in zip(indices, jobs):
        lines.append(f"{idx}. {get_display_title(job)} — {_job_inline_details(job)}")
    return "\n".join(lines)


def format_remove_confirmation(jobs, indices):
    label = "удаления" if len(indices) > 1 else "удаления"
    count_note = f"\n\nАниме к удалению: {len(indices)} шт." if len(indices) > 1 else ""
    return "\n".join([
        f"Подтверждение {label}",
        "",
        _format_job_list(jobs, indices),
        count_note,
        "",
        "Подтверди удаление кнопкой ниже",
    ]).rstrip()


def format_retry_confirmation(candidate, index):
    job = candidate["job"]
    return "\n".join([
        "Подтверждение повтора",
        "",
        f"Номер: {index}",
        f"Источник: {candidate['label']}",
        f"Тайтл: {get_display_title(job)}",
        *_navigation_lines(job),
        f"Эпизоды: {job.get('episodes_range', '?')}",
        "",
        "Подтверди повтор кнопкой ниже",
    ])


def format_remove_result(jobs):
    if isinstance(jobs, list):
        if len(jobs) == 0:
            return "Ничего не удалено"
        titles = [get_display_title(j) for j in jobs]
        message = f"Удалено из очереди: {len(jobs)} шт.\n\n" + "\n".join(f"• {t}" for t in titles)
        if any(job.get("_queue_status") == "running" for job in jobs):
            message += "\n\nАктивная обработка останавливается"
        return message
    return "\n".join([
        "Аниме удалено из очереди",
        "",
        f"Тайтл: {get_display_title(jobs)}",
        *_navigation_lines(jobs),
        f"Эпизоды: {jobs.get('episodes_range', '?')}",
    ])


def format_complete_confirmation(jobs, indices):
    label = "завершения" if len(indices) > 1 else "завершения"
    count_note = f"\n\nАниме к завершению: {len(indices)} шт." if len(indices) > 1 else ""
    return "\n".join([
        f"Подтверждение {label}",
        "",
        _format_job_list(jobs, indices),
        count_note,
        "",
        "Аниме будет убрано из очереди и перенесено в completed_jobs.json" if len(indices) == 1 else "Аниме будут убраны из очереди и перенесены в completed_jobs.json",
        "Подтверди завершение кнопкой ниже",
    ]).rstrip()


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
        *_navigation_lines(job),
        f"Эпизоды: {job.get('episodes_range', '?')}",
    ])


def format_blacklist_confirmation(jobs, indices):
    label = "blacklist" if len(indices) > 1 else "blacklist"
    count_note = f"\n\nАниме к blacklist: {len(indices)} шт." if len(indices) > 1 else ""
    lines = [
        f"Подтверждение {label}",
        "",
        _format_job_list(jobs, indices),
        count_note,
        "",
        "Тайтл будет добавлен в discovery blacklist и удалён из активной очереди" if len(indices) == 1 else "Тайтлы будут добавлены в discovery blacklist и удалены из активной очереди",
        "Подтверди действие кнопкой ниже",
    ]
    return "\n".join(lines)


def format_blacklist_list(config):
    entries = get_blacklist_entries(config)
    if not entries:
        return "Discovery blacklist пуст"

    lines = [
        "Discovery blacklist",
        "",
        f"Всего: {len(entries)}",
        "",
    ]
    for index, item in enumerate(entries, start=1):
        title = item.get("title_ru") or item.get("title") or "Без названия"
        lines.append(f"{index}. {title}")
        lines.append(f"  Release ID: {item.get('release_id')}")
        lines.extend(_navigation_lines(item, "  Метка: "))
    lines.extend(["", "Формат: /unblacklist <номер>"])
    return "\n".join(lines)


def format_blacklist_result(results):
    if isinstance(results, list):
        if len(results) == 0:
            return "Ничего не добавлено в blacklist"
        titles = []
        for job, _already in results:
            titles.append(f"• {get_display_title(job)}")
        return f"Добавлено в blacklist: {len(results)} шт.\n\n" + "\n".join(titles)

    job, already_blacklisted = results, False
    lines = [
        "Тайтл добавлен в discovery blacklist" if not already_blacklisted else "Тайтл уже был в discovery blacklist",
        "",
        f"Тайтл: {get_display_title(job)}",
        *_navigation_lines(job),
    ]
    return "\n".join(lines)


def format_unblacklist_confirmation(item, index):
    title = item.get("title_ru") or item.get("title") or "Без названия"
    return "\n".join([
        "Подтверждение снятия blacklist",
        "",
        f"Номер: {index}",
        f"Тайтл: {title}",
        f"Release ID: {item.get('release_id')}",
        "",
        "После снятия blacklist autodiscovery снова сможет добавить релиз в очередь",
        "Подтверди действие кнопкой ниже",
    ])


def format_unblacklist_result(item):
    title = item.get("title_ru") or item.get("title") or "Без названия"
    return "\n".join([
        "Тайтл убран из discovery blacklist",
        "",
        f"Тайтл: {title}",
        f"Release ID: {item.get('release_id')}",
    ])


def format_complete_result(results):
    if isinstance(results, list):
        if len(results) == 0:
            return "Ничего не завершено"
        titles = []
        for job, _already in results:
            titles.append(f"• {get_display_title(job)} — {_job_inline_details(job)}")
        return f"Завершено: {len(results)} шт.\n\n" + "\n".join(titles)

    job, already_archived = results, False
    if already_archived:
        return "\n".join([
            "Аниме убрано из очереди",
            "",
            "Запись уже была в completed_jobs.json, дубль не добавлен",
            f"Тайтл: {get_display_title(job)}",
            *_navigation_lines(job),
            f"Эпизоды: {job.get('episodes_range', '?')}",
        ])
    return "\n".join([
        "Аниме перенесено в completed_jobs.json",
        "",
        f"Тайтл: {get_display_title(job)}",
        *_navigation_lines(job),
        f"Эпизоды: {job.get('episodes_range', '?')}",
    ])


def add_job_to_blacklist(config, job):
    release_id = get_job_release_id(job)
    if release_id is None:
        raise RuntimeError("Этот job нельзя добавить в discovery blacklist: отсутствует release_id")

    blacklist_item = build_blacklist_item(
        release_id,
        title=job.get("title"),
        title_ru=job.get("title_ru"),
        season=job.get("season", 1),
        source="telegram",
    )
    _, already_blacklisted = add_release_to_blacklist({}, blacklist_item)

    jobs = load_jobs(config)
    if find_matching_job(jobs, job) is not None:
        remove_job_by_identity(config, build_job_identity(job), cancel_running=True)
    return already_blacklisted


def get_blacklist_entry_by_index(config, index):
    entries = get_blacklist_entries(config)
    if index > len(entries):
        raise RuntimeError(f"Запись blacklist с номером {index} не найдена")
    return entries[index - 1]


def remove_job_by_identity(config, job_identity, cancel_running=False):
    jobs = load_jobs(config)
    removed_job = None
    for job in jobs:
        if removed_job is None and build_job_identity(job) == job_identity:
            removed_job = job
    if not removed_job:
        raise RuntimeError("Актуальная запись для удаления не найдена")
    removed = _db_cancel_job(removed_job) if cancel_running else _db_remove_job(removed_job)
    if not removed:
        raise RuntimeError("Задача уже изменилась; обнови /jobs и повтори удаление")
    state = load_state(config)
    updated_state = unmark_job_episodes_queued(state, removed_job)
    save_state(config, updated_state)
    return removed_job


def archive_job_to_completed(config, job, source="telegram_complete"):
    completed_jobs = load_completed_jobs(config)
    job_identity = build_job_identity(job)
    for item in completed_jobs:
        archived_job = item.get("job") or {}
        if build_job_identity(archived_job) == job_identity:
            state = load_state(config)
            updated_state = mark_job_episodes_completed(state, job)
            save_state(config, updated_state)
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
    state = load_state(config)
    updated_state = mark_job_episodes_completed(state, job)
    save_state(config, updated_state)
    return False


def retry_job_to_queue(config, job):
    jobs = load_jobs(config)
    if find_matching_job(jobs, job) is not None:
        return False
    insert_one_job(job)
    state = load_state(config)
    updated_state = mark_job_episodes_queued(state, job)
    save_state(config, updated_state)
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
        "Подтвердить blacklist",
        "Отменить blacklist",
        "Подтвердить снятие blacklist",
        "Отменить снятие blacklist",
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
        "blacklist": "Подтвердить blacklist",
        "unblacklist": "Подтвердить снятие blacklist",
    }
    cancel_map = {
        "remove": "Отменить удаление",
        "complete": "Отменить завершение",
        "retry": "Отменить повтор",
        "blacklist": "Отменить blacklist",
        "unblacklist": "Отменить снятие blacklist",
    }
    action_type = pending.get("type")
    if text == cancel_map.get(action_type):
        clear_pending_action(chat_id)
        return "Действие отменено"

    if text != confirm_map.get(action_type):
        return None

    clear_pending_action(chat_id)
    if action_type == "remove":
        removed = []
        for job in pending.get("job_snapshots", []):
            removed.append(remove_job_by_identity(config, build_job_identity(job), cancel_running=True))
        return format_remove_result(removed)

    if action_type == "complete":
        results = []
        for job in pending.get("job_snapshots", []):
            removed_job = remove_job_by_identity(config, build_job_identity(job))
            already_archived = archive_job_to_completed(config, removed_job)
            results.append((removed_job, already_archived))
        return format_complete_result(results)

    if action_type == "retry":
        job = pending.get("job_snapshot") or {}
        added = retry_job_to_queue(config, job)
        return format_retry_result(job, already_exists=not added)

    if action_type == "blacklist":
        results = []
        for job in pending.get("job_snapshots", []):
            already_blacklisted = add_job_to_blacklist(config, job)
            results.append((job, already_blacklisted))
        return format_blacklist_result(results)

    if action_type == "unblacklist":
        blacklist_item = pending.get("blacklist_item") or {}
        _, removed_item = remove_release_from_blacklist({}, blacklist_item.get("release_id"))
        return format_unblacklist_result(removed_item)

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


VALID_PRIVACY_VALUES = {0, 1, 2, 3, 5}


def parse_add_command(text):
    if not text.startswith("/add "):
        raise RuntimeError("Команда должна начинаться с /add")

    raw_payload = text[len("/add "):]
    parts = [part.strip() for part in re.split(r"\s*;\s*", raw_payload)]
    if len(parts) not in {3, 4, 5, 6}:
        raise RuntimeError("Формат: /add Название ; 001-003 ; magnet:?xt=... ; сезон ; privacy ; фильтр пути")

    title, episodes_range, magnet = parts[:3]
    season = parts[3] if len(parts) >= 4 else "1"
    privacy_view = parts[4] if len(parts) >= 5 else "0"
    path_filter = parts[5] if len(parts) == 6 else None

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

    try:
        privacy_value = int(str(privacy_view).strip())
    except ValueError as exc:
        raise RuntimeError("privacy_view должен быть целым числом") from exc
    if privacy_value not in VALID_PRIVACY_VALUES:
        raise RuntimeError(f"privacy_view должен быть одним из: {', '.join(map(str, sorted(VALID_PRIVACY_VALUES)))}")
    if path_filter is not None and not path_filter:
        raise RuntimeError("Фильтр пути не должен быть пустым")

    return {
        "title": title,
        "season": season_value,
        "episodes_range": format_episodes_range(validated_episodes),
        "magnet": magnet,
        "privacy_view": privacy_value,
        "source_path_contains": path_filter,
    }


def parse_add4k_command(text):
    if not text.startswith("/add4k "):
        raise RuntimeError("Команда должна начинаться с /add4k")

    parts = [part.strip() for part in re.split(r"\s*;\s*", text[len("/add4k "):])]
    if len(parts) not in {3, 4, 5}:
        raise RuntimeError("Формат: /add4k Название ; количество серий ; magnet:?xt=... ; сезон ; фильтр пути")

    title, episode_count_or_range, magnet = parts[:3]
    season = parts[3] if len(parts) >= 4 else "1"
    path_filter = parts[4] if len(parts) == 5 else None
    if not title:
        raise RuntimeError("Нужно указать название тайтла")
    if not magnet.startswith("magnet:?"):
        raise RuntimeError("Magnet-ссылка должна начинаться с magnet:?")

    if episode_count_or_range.isdigit():
        episode_count = int(episode_count_or_range)
        if episode_count < 1:
            raise RuntimeError("Количество серий должно быть не меньше 1")
        episodes_range = "001" if episode_count == 1 else f"001-{episode_count:03d}"
    else:
        episodes_range = format_episodes_range(parse_episodes_range(episode_count_or_range))

    try:
        season_value = int(season)
    except ValueError as exc:
        raise RuntimeError("Сезон должен быть целым числом") from exc
    if season_value < 1:
        raise RuntimeError("Сезон должен быть не меньше 1")
    if path_filter is not None and not path_filter:
        raise RuntimeError("Фильтр пути не должен быть пустым")

    return {
        "title": title,
        "season": season_value,
        "episodes_range": episodes_range,
        "magnet": magnet,
        "source_path_contains": path_filter,
    }


def build_manual_job(command_payload):
    title = command_payload["title"]
    slug = ensure_non_empty_slug(title)
    job = {
        "title": title,
        "season": command_payload["season"],
        "episodes_range": command_payload["episodes_range"],
        "source": {
            "type": "magnet",
            "magnet": command_payload["magnet"],
            "download_dir": f"downloads/{slug}",
        },
    }
    privacy_view = command_payload.get("privacy_view", 0)
    if privacy_view != 0:
        job["delivery"] = {"vk_privacy_view": privacy_view}
    if command_payload.get("source_path_contains"):
        job["processing"] = {"source_path_contains": command_payload["source_path_contains"]}
    return job


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

    insert_one_job(candidate_job)
    return {
        "added": True,
        "job": candidate_job,
        "reason": None,
    }


def build_upscale_job(command_payload):
    title = command_payload["title"]
    slug = ensure_non_empty_slug(title)
    job = {
        "title": title,
        "season": command_payload["season"],
        "episodes_range": command_payload["episodes_range"],
        "processing_mode": "upscale_4k",
        "source": {
            "type": "magnet",
            "magnet": command_payload["magnet"],
            "download_dir": f"upscale_downloads/{slug}",
        },
        "output_dir": "./upscale_output",
        "delivery": {
            "s3_enabled": False,
            "vk_enabled": True,
            "vk_wall_post_enabled": True,
            "vk_comment_enabled": False,
            "vk_privacy_view": 5,
            "vk_direct_donut": True,
            "vk_preview_enabled": False,
        },
        "cleanup": {
            "downloads": True,
            "output": True,
        },
    }
    if command_payload.get("source_path_contains"):
        job["processing"] = {"source_path_contains": command_payload["source_path_contains"]}
    return job


def add_upscale_job_from_command(config, text):
    candidate_job = build_upscale_job(parse_add4k_command(text))
    if find_matching_job(load_jobs(config), candidate_job) is not None:
        return {"added": False, "job": candidate_job, "reason": "duplicate_job"}
    insert_one_job(candidate_job)
    return {"added": True, "job": candidate_job, "reason": None}


def format_add4k_result(result):
    job = result["job"]
    if not result["added"]:
        return "\n".join([
            "4K-аниме не добавлено",
            "",
            f"Причина: {format_reason_ru(result['reason'])}",
            f"Тайтл: {get_display_title(job)}",
        ])
    return "\n".join([
        "4K-аниме добавлено",
        "",
        f"Тайтл: {get_display_title(job)}",
        *_navigation_lines(job),
        f"Эпизоды: {job['episodes_range']}",
        "Режим: 1080p → 4K, без вырезов и watermark",
        "VK доступ: только Donut",
    ])


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

    privacy_view = (job.get("delivery") or {}).get("vk_privacy_view", 0)
    privacy_labels = {0: "всем", 1: "участникам", 2: "редакторам", 3: "по ссылке", 5: "донам"}
    lines = [
        "Аниме добавлено",
        "",
        f"Тайтл: {get_display_title(job)}",
        *_navigation_lines(job),
        f"Эпизоды: {job['episodes_range']}",
        f"VK доступ: {privacy_labels.get(privacy_view, privacy_view)}",
    ]
    return "\n".join(lines)


def build_help_message():
    return "\n".join([
        "Команды бота",
        "",
        "/start - краткая справка",
        "/status - статус очереди и runtime",
        "/current - текущее или последнее выполнение",
        "/upscale - состояние 4K-worker",
        "/jobs - показать аниме в очереди (с приоритетом выполнения)",
        "/errors - последние ошибки выполнения",
        "/log - хвост cron.log",
        "/remove <номер> - удалить из очереди и остановить активную обработку (можно диапазон: 1-10, 1,5,8-10)",
        "/complete <номер> - завершить аниме (можно диапазон: 1-10, 1,5,8-10)",
        "/retry <номер> - повторно поставить аниме в очередь",
        "/label <номер> ; <метка|auto> - изменить навигационную метку",
        "/audiofix-on <номер> - включить восстановление аудио (поддерживает диапазоны)",
        "/audiofix-off <номер> - выключить восстановление аудио (поддерживает диапазоны)",
        "/blacklist - показать discovery blacklist",
        "/blacklist <номер> - добавить тайтл из очереди в discovery blacklist",
        "/unblacklist <номер> - убрать тайтл из discovery blacklist",
        "",
        "Порядок в /jobs — по приоритету выполнения (ongoing → manual).",
        "",
        "Пример:",
        "/add Название ; серии ; magnet ; сезон ; privacy ; необязательный фильтр пути",
        "/add4k Название ; серии ; magnet ; сезон ; необязательный фильтр пути",
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
    if text.startswith("/upscale"):
        return format_upscale_message()
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
        jobs = load_jobs(config)
        page_data = get_jobs_page_data(config, page=page, jobs=jobs)
        if not page_data["jobs"]:
            return "Очередь пуста"
        return {
            "text": format_jobs_message_markdown(config, page=page, jobs=jobs),
            "parse_mode": "MarkdownV2",
            "reply_markup": build_jobs_inline_keyboard(
                page_data["has_previous"], page_data["has_next"],
                page_data["page"], page_data["total_pages"],
            ),
        }
    if text.startswith("/label"):
        return update_job_navigation_label(config, text)
    if text.startswith("/audiofix-on"):
        return update_job_audio_recovery(config, text, True)
    if text.startswith("/audiofix-off"):
        return update_job_audio_recovery(config, text, False)
    if text.startswith("/remove"):
        indices = parse_index_range(text, "remove")
        jobs_list, _ = get_jobs_by_indices(config, indices)
        return {
            "text": format_remove_confirmation(jobs_list, indices),
            "reply_markup": build_confirmation_keyboard("remove"),
            "pending_action": build_pending_action_payload("remove", "jobs", indices, jobs_list),
        }
    if text.startswith("/complete"):
        indices = parse_index_range(text, "complete")
        jobs_list, _ = get_jobs_by_indices(config, indices)
        return {
            "text": format_complete_confirmation(jobs_list, indices),
            "reply_markup": build_confirmation_keyboard("complete"),
            "pending_action": build_pending_action_payload("complete", "jobs", indices, jobs_list),
        }
    if text == "/blacklist":
        return format_blacklist_list(config)
    if text.startswith("/blacklist "):
        indices = parse_index_range(text, "blacklist")
        jobs_list, _ = get_jobs_by_indices(config, indices)
        for idx, job in zip(indices, jobs_list):
            release_id = get_job_release_id(job)
            if release_id is None:
                raise RuntimeError(f"Аниме #{idx} нельзя добавить в discovery blacklist: отсутствует release_id")
        return {
            "text": format_blacklist_confirmation(jobs_list, indices),
            "reply_markup": build_confirmation_keyboard("blacklist"),
            "pending_action": build_pending_action_payload("blacklist", "jobs", indices, jobs_list),
        }
    if text.startswith("/unblacklist"):
        index = parse_index_command(text, "unblacklist")
        item = get_blacklist_entry_by_index(config, index)
        return {
            "text": format_unblacklist_confirmation(item, index),
            "reply_markup": build_confirmation_keyboard("unblacklist"),
            "pending_action": build_blacklist_pending_action_payload("unblacklist", index, item),
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
    if text.startswith("/add4k "):
        return format_add4k_result(add_upscale_job_from_command(config, text))
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


def _handle_jobs_callback(config, chat_id, message_id, callback_query_id, page):
    """Handle inline keyboard pagination for /jobs list."""
    answer_callback_query(callback_query_id)

    jobs = load_jobs(config)
    sorted_jobs = build_execution_order(jobs, defaults=config.get("defaults", {}))
    total = len(sorted_jobs)
    if total == 0:
        return

    page_size = 15
    total_pages = max(1, (total + page_size - 1) // page_size)
    p = max(1, min(page, total_pages))

    text = format_jobs_message_markdown(config, page=p, jobs=jobs)
    markup = build_jobs_inline_keyboard(p > 1, p < total_pages, p, total_pages)
    edit_message_text(chat_id, message_id, text, reply_markup=markup, parse_mode="MarkdownV2")


def handle_update(config, update):
    callback_query = update.get("callback_query") or {}
    if callback_query:
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or not is_allowed_chat(chat_id):
            return False

        data = str(callback_query.get("data") or "").strip()
        callback_query_id = callback_query.get("id")

        # Inline pagination for /jobs
        if data.startswith("jobs:page:"):
            page = int(data.split(":")[2])
            _handle_jobs_callback(config, chat_id, message.get("message_id"), callback_query_id, page)
            return True

        if data.startswith("details:"):
            token = data.split(":", 1)[1].strip()
            payload = load_notification_details(token)
            if callback_query_id is not None:
                answer_callback_query(
                    callback_query_id,
                    text="Открываю детали" if payload else "Детали уже недоступны",
                )
            if not payload:
                send_reply(chat_id, "Детали уже недоступны", include_keyboard=True)
                return True

            if payload.get("type") == "job_result_details":
                send_message_with_fallback(chat_id, format_job_details_message(payload), parse_mode="MarkdownV2")
                return True

            send_reply(chat_id, "Неизвестный тип деталей", include_keyboard=True)
            return True
        if callback_query_id is not None:
            answer_callback_query(callback_query_id, text="Неизвестное действие")
        return True

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
