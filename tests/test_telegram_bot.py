import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from lib.config import save_jobs, save_state
from lib.telegram_bot import (
    add_job_from_command,
    build_main_keyboard,
    build_notification_details_payload,
    build_notification_details_reply_markup,
    build_help_message,
    format_current_message,
    format_discovery_message,
    format_error_message,
    format_errors_message,
    format_log_message,
    format_log_message_markdown,
    format_jobs_message,
    format_publish_success_message,
    format_status_message,
    format_vk_publish_error_message,
    format_vk_publish_success_message,
    get_jobs_pagination_page,
    get_pending_action,
    get_display_title,
    handle_update,
    is_allowed_chat,
    load_telegram_state,
    normalize_command_text,
    parse_add_command,
    save_telegram_state,
    send_message_to_allowed_chats,
    get_telegram_proxy_url,
    _telegram_request,
    telegram_force_ipv4_enabled,
    update_telegram_state_progress,
)


class TelegramBotTests(unittest.TestCase):
    def make_workspace_temp_dir(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        temp_dir = root / f"telegram_{uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def make_config(self, tmp_dir):
        return {
            "automation": {
                "jobs_path": str((tmp_dir / "jobs.json").resolve()),
                "state_path": str((tmp_dir / "state.json").resolve()),
            }
        }

    def test_parse_add_command_extracts_required_fields(self):
        payload = parse_add_command(
            "/add Test Title ; 001-003 ; magnet:?xt=urn:btih:testhash ; 2",
        )

        self.assertEqual(payload["title"], "Test Title")
        self.assertEqual(payload["episodes_range"], "001-003")
        self.assertEqual(payload["magnet"], "magnet:?xt=urn:btih:testhash")
        self.assertEqual(payload["season"], 2)

    def test_allowed_chat_filter_uses_whitelist(self):
        allowed = {"123", "456"}

        self.assertTrue(is_allowed_chat(123, allowed))
        self.assertFalse(is_allowed_chat(999, allowed))

    def test_telegram_force_ipv4_enabled_defaults_to_true(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_FORCE_IPV4", None)
            self.assertTrue(telegram_force_ipv4_enabled())

    def test_telegram_force_ipv4_enabled_respects_false_value(self):
        with patch.dict(os.environ, {"TELEGRAM_FORCE_IPV4": "false"}):
            self.assertFalse(telegram_force_ipv4_enabled())

    def test_get_telegram_proxy_url_returns_env_value(self):
        with patch.dict(os.environ, {"TELEGRAM_PROXY_URL": "socks5h://user:pass@host:1080"}):
            self.assertEqual(get_telegram_proxy_url(), "socks5h://user:pass@host:1080")

    def test_telegram_state_roundtrip_uses_custom_path(self):
        tmp_dir = self.make_workspace_temp_dir()
        state_path = (tmp_dir / "telegram_state.json").resolve()

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            save_telegram_state({
                "schema_version": 1,
                "last_update_id": 42,
                "last_handled_at": "2026-06-07T10:00:00+00:00",
            })
            loaded = load_telegram_state()

        self.assertEqual(loaded["last_update_id"], 42)
        self.assertEqual(loaded["last_handled_at"], "2026-06-07T10:00:00+00:00")

    def test_add_job_from_command_creates_and_deduplicates_job(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        command = "/add Test Title ; 001-003 ; magnet:?xt=urn:btih:testhash ; 2"

        first = add_job_from_command(config, command)
        second = add_job_from_command(config, command)

        self.assertTrue(first["added"])
        self.assertFalse(second["added"])
        self.assertEqual(second["reason"], "duplicate_job")

    def test_formatters_build_readable_messages(self):
        discovery = format_discovery_message(
            {"created_jobs": 1, "updated_jobs": 0},
            [{"title": "Test Title", "title_ru": "Тестовый тайтл"}],
        )
        error = format_error_message("cron_run", "boom")
        success = format_publish_success_message(
            {"title": "Test Title", "title_ru": "Тестовый тайтл", "episodes_range": "001-003"},
            "s3://bucket/file.mkv",
            quality_summary={
                "episodes_count": 3,
                "episodes_with_op_removed": 3,
                "episodes_with_ed_removed": 2,
            },
        )

        self.assertIn("Автодискавери завершён", discovery)
        self.assertIn("Тестовый тайтл", discovery)
        self.assertIn("🛰️ *Автодискавери завершён*", discovery)
        self.assertIn("🆕 Новых аниме: `1`", discovery)
        self.assertIn("❌ *Ошибка выполнения*", error)
        self.assertIn("🔧 Этап: `cron_run`", error)
        self.assertIn("Причина: `boom`", error)
        self.assertIn("✅ *Обработка завершена*", success)
        self.assertIn("✂️ OP: `3/3` • ED: `2/3`", success)
        self.assertIn("📦 Результат: `s3://bucket/file.mkv`", success)
        self.assertIn("🎬 *Тестовый тайтл*", success)

    def test_error_message_formats_job_failed_as_markdown(self):
        message = format_error_message(
            "job_failed:Вечная воля",
            "CalledProcessError(255, ['ffmpeg', '-y'])",
        )

        self.assertIn("❌ *Ошибка выполнения*", message)
        self.assertIn("🎬 *Вечная воля*", message)
        self.assertIn("🔧 Этап: `job_failed`", message)
        self.assertIn("Причина: `ffmpeg exited with code 255`", message)

    def test_vk_publish_success_message_formats_markdown_with_partial_warning(self):
        message = format_vk_publish_success_message(
            {
                "title": "Tokyo Ghoul:re",
                "title_ru": "Токийский Гуль: Перерождение",
                "episodes_range": "001-024",
            },
            {
                "post_created": True,
                "comment_created": False,
                "error": "HTTPError('504 Server Error: Gateway Time-out for url: https://pu.vk.com/upload.php')",
            },
            quality_summary={
                "episodes_count": 24,
                "episodes_with_op_removed": 24,
                "episodes_with_ed_removed": 22,
            },
        )

        self.assertIn("✅ *Видео опубликовано в VK*", message)
        self.assertIn("🎬 *Токийский Гуль: Перерождение*", message)
        self.assertIn("📺 Эпизоды: `001-024`", message)
        self.assertIn("✂️ OP: `24/24` • ED: `22/24`", message)
        self.assertIn("✔️ Пост на стене создан", message)
        self.assertIn("✖️ Первый комментарий не создан", message)
        self.assertIn("504 Gateway Time\\-out", message)

    def test_vk_publish_error_message_formats_markdown(self):
        message = format_vk_publish_error_message(
            {
                "title": "The Rising of the Shield Hero",
                "title_ru": "Восхождение героя щита",
            },
            "HTTPError('504 Server Error: Gateway Time-out for url: https://pu.vk.com/upload.php')",
        )

        self.assertIn("❌ *Ошибка публикации в VK*", message)
        self.assertIn("🎬 *Восхождение героя щита*", message)
        self.assertIn("🔧 Этап: `vk_publish`", message)
        self.assertIn("Причина: `504 Gateway Time-out`", message)

    def test_status_command_reads_jobs_and_state(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [{"title": "A", "season": 1, "episodes_range": "001"}])
        save_state(config, {
            "schema_version": 2,
            "last_discovery_at": "2026-06-07T10:00:00+00:00",
            "queued_release_episodes": {"1:001": True},
            "completed_release_episodes": {"1:001": True, "1:002": True},
            "job_index": {},
            "skipped_items": [],
        })

        runtime_paths = {
            "runtime_dir": tmp_dir,
            "logs_dir": tmp_dir,
            "lock_path": tmp_dir / "cron.lock",
            "log_path": tmp_dir / "cron.log",
            "telegram_log_path": tmp_dir / "telegram_bot.log",
            "status_path": tmp_dir / "runtime_status.json",
        }
        runtime_paths["lock_path"].write_text("locked", encoding="utf-8")
        runtime_paths["log_path"].write_text(
            "[2026-06-07T10:01:00+00:00] START JOB 1/1: A\n",
            encoding="utf-8",
        )
        with patch("lib.telegram_bot.ensure_runtime_paths", return_value=runtime_paths):
            message = format_status_message(config)

        self.assertIn("Активная задача: A", message)
        self.assertIn("Аниме в очереди: 1", message)
        self.assertIn("Последнее обновление очереди: ", message)
        self.assertIn("Эпизодов в очереди: 1", message)
        self.assertIn("Завершённых эпизодов: 2", message)
        self.assertIn("В blacklist discovery: 0", message)
        self.assertNotIn("Последний discovery", message)

    def test_jobs_message_prefers_title_ru(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [{
            "title": "English Title",
            "title_ru": "Русский тайтл",
            "season": 1,
            "episodes_range": "001",
        }])

        message = format_jobs_message(config)

        self.assertIn("Очередь аниме", message)
        self.assertIn("Русский тайтл", message)
        self.assertNotIn("English Title |", message)
        self.assertIn("Сезон: 1", message)
        self.assertIn("Эпизоды: 001", message)

    def test_get_display_title_falls_back_to_title(self):
        self.assertEqual(get_display_title({"title_ru": "Русский", "title": "English"}), "Русский")
        self.assertEqual(get_display_title({"title": "English"}), "English")

    def test_help_message_hides_help_command_and_button_phrase(self):
        message = build_help_message()

        self.assertNotIn("/help - показать команды", message)
        self.assertNotIn("Кнопки:", message)
        self.assertNotIn("Статус, Очередь, Помощь", message)
        self.assertIn("/blacklist - показать discovery blacklist", message)
        self.assertIn("/unblacklist <номер> - убрать тайтл из discovery blacklist", message)

    def test_build_main_keyboard_returns_reply_markup(self):
        keyboard = build_main_keyboard()

        self.assertTrue(keyboard["resize_keyboard"])
        self.assertFalse(keyboard["one_time_keyboard"])
        self.assertEqual(keyboard["keyboard"][0][0]["text"], "Статус")
        self.assertEqual(keyboard["keyboard"][0][1]["text"], "Текущая")
        self.assertEqual(keyboard["keyboard"][1][0]["text"], "Очередь")
        self.assertEqual(keyboard["keyboard"][1][1]["text"], "Ошибки")
        self.assertEqual(keyboard["keyboard"][2][0]["text"], "Лог")
        self.assertEqual(keyboard["keyboard"][2][1]["text"], "Помощь")

    def test_normalize_command_text_maps_button_aliases(self):
        self.assertEqual(normalize_command_text("Статус"), "/status")
        self.assertEqual(normalize_command_text("Текущая"), "/current")
        self.assertEqual(normalize_command_text("Очередь"), "/jobs")
        self.assertEqual(normalize_command_text("Ошибки"), "/errors")
        self.assertEqual(normalize_command_text("Лог"), "/log")
        self.assertEqual(normalize_command_text("Помощь"), "/help")
        self.assertEqual(normalize_command_text("/jobs"), "/jobs")

    @patch("lib.telegram_bot.load_runtime_status")
    def test_current_message_shows_active_job(self, mock_load_runtime_status):
        mock_load_runtime_status.return_value = {
            "run_status": "running",
            "run_started_at": "2026-06-13T10:00:00+00:00",
            "current_stage": "processing",
            "queue_progress": {
                "current_job_index": 2,
                "total_jobs": 5,
                "jobs_processed": 1,
                "jobs_failed": 0,
            },
            "current_job": {
                "title": "English Title",
                "title_ru": "Русский тайтл",
                "season": 1,
                "episodes_range": "001-010",
                "stage": "render_segments",
                "started_at": "2026-06-13T10:01:00+00:00",
                "current_episode": 3,
                "total_episodes": 10,
                "current_episode_file": "ep03.mkv",
            },
            "last_run": None,
        }

        message = format_current_message()

        self.assertIn("Текущая обработка", message)
        self.assertIn("Русский тайтл", message)
        self.assertIn("Этап: вырезка сегментов", message)
        self.assertIn("Текущая серия: 3", message)
        self.assertIn("Всего серий: 10", message)

    @patch("lib.telegram_bot.load_runtime_status")
    def test_current_message_shows_last_run_when_idle(self, mock_load_runtime_status):
        mock_load_runtime_status.return_value = {
            "run_status": "completed",
            "run_finished_at": "2026-06-13T12:00:00+00:00",
            "current_stage": "completed",
            "queue_progress": {
                "current_job_index": 0,
                "total_jobs": 5,
                "jobs_processed": 5,
                "jobs_failed": 1,
            },
            "current_job": None,
            "last_run": {
                "title": "English Title",
                "title_ru": "Русский тайтл",
                "status": "failed",
                "stage": "job_failed",
                "finished_at": "2026-06-13T12:00:00+00:00",
                "jobs_processed": 4,
                "jobs_failed": 1,
                "current_episode": 8,
                "total_episodes": 12,
            },
        }

        message = format_current_message()

        self.assertIn("Сейчас ничего не обрабатывается", message)
        self.assertIn("Последний запуск", message)
        self.assertIn("Русский тайтл", message)
        self.assertIn("Статус: с ошибкой", message)
        self.assertIn("Последняя серия: 8 / 12", message)

    @patch("lib.telegram_bot.load_runtime_errors")
    def test_errors_message_shows_empty_state(self, mock_load_runtime_errors):
        mock_load_runtime_errors.return_value = {
            "schema_version": 1,
            "updated_at": None,
            "errors": [],
        }

        message = format_errors_message()

        self.assertIn("Ошибок пока нет", message)
        self.assertIn("История сбоев ещё не накоплена", message)

    @patch("lib.telegram_bot.load_runtime_errors")
    def test_errors_message_shows_recent_items(self, mock_load_runtime_errors):
        mock_load_runtime_errors.return_value = {
            "schema_version": 1,
            "updated_at": "2026-06-13T12:00:00+00:00",
            "errors": [
                {
                    "created_at": "2026-06-13T12:00:00+00:00",
                    "context": "job_failed",
                    "stage": "render_segments",
                    "title": "English Title",
                    "title_ru": "Русский тайтл",
                    "current_episode": 3,
                    "total_episodes": 10,
                    "message": "RuntimeError('boom')",
                }
            ],
        }

        message = format_errors_message()

        self.assertIn("Последние ошибки", message)
        self.assertIn("Контекст: job_failed", message)
        self.assertIn("Этап: вырезка сегментов", message)
        self.assertIn("Тайтл: Русский тайтл", message)
        self.assertIn("Серия: 3 / 10", message)
        self.assertIn("RuntimeError('boom')", message)

    def test_jobs_message_shows_numbering(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [
            {"title": "A", "title_ru": "А", "season": 1, "episodes_range": "001"},
            {"title": "B", "title_ru": "Б", "season": 2, "episodes_range": "002"},
        ])

        message = format_jobs_message(config)

        self.assertIn("1. А", message)
        self.assertIn("2. Б", message)

    def test_jobs_message_uses_absolute_indexes_on_requested_page(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        jobs = []
        for index in range(1, 29):
            jobs.append({
                "title": f"Title {index}",
                "title_ru": f"Тайтл {index}",
                "season": 1,
                "episodes_range": f"{index:03d}",
            })
        save_jobs(config, jobs)

        message = format_jobs_message(config, page=2, page_size=15)

        self.assertIn("Страница: 2/2", message)
        self.assertIn("Показываю: 16-28", message)
        self.assertIn("16. Тайтл 16", message)
        self.assertIn("28. Тайтл 28", message)
        self.assertNotIn("1. Тайтл 16", message)

    def test_log_message_reads_tail_and_handles_missing_file(self):
        tmp_dir = self.make_workspace_temp_dir()
        runtime_paths = {
            "runtime_dir": tmp_dir,
            "logs_dir": tmp_dir,
            "lock_path": tmp_dir / "cron.lock",
            "log_path": tmp_dir / "cron.log",
            "telegram_log_path": tmp_dir / "telegram_bot.log",
            "status_path": tmp_dir / "runtime_status.json",
            "errors_path": tmp_dir / "runtime_errors.json",
        }

        with patch("lib.telegram_bot.ensure_runtime_paths", return_value=runtime_paths):
            self.assertEqual(format_log_message(), "Лог ещё не создан")

        runtime_paths["log_path"].write_text("\n".join([f"line {index}" for index in range(30)]), encoding="utf-8")
        with patch("lib.telegram_bot.ensure_runtime_paths", return_value=runtime_paths):
            message = format_log_message()

        self.assertIn("line 29", message)
        self.assertNotIn("line 0", message)

    def test_log_message_markdown_wraps_content_in_code_block(self):
        tmp_dir = self.make_workspace_temp_dir()
        runtime_paths = {
            "runtime_dir": tmp_dir,
            "logs_dir": tmp_dir,
            "lock_path": tmp_dir / "cron.lock",
            "log_path": tmp_dir / "cron.log",
            "telegram_log_path": tmp_dir / "telegram_bot.log",
            "status_path": tmp_dir / "runtime_status.json",
            "errors_path": tmp_dir / "runtime_errors.json",
        }
        runtime_paths["log_path"].write_text("line <1>\nline &2", encoding="utf-8")

        with patch("lib.telegram_bot.ensure_runtime_paths", return_value=runtime_paths):
            message = format_log_message_markdown(lines_limit=20)

        self.assertIn("Хвост лога", message)
        self.assertIn("```log", message)
        self.assertIn("line <1>", message)
        self.assertIn("line &2", message)

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_remove_flow_sets_pending_action_and_confirms(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        save_jobs(config, [{
            "title": "A",
            "title_ru": "А",
            "season": 1,
            "episodes_range": "001",
            "automation": {"release_id": 1},
        }])
        save_state(config, {
            "schema_version": 2,
            "queued_release_episodes": {"1:001": {"release_id": 1, "episode": 1}},
            "completed_release_episodes": {},
            "job_index": {},
            "skipped_items": [],
            "ongoing_progress": {},
        })

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            update = {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/remove 1"},
            }
            handled = handle_update(config, update)

            self.assertTrue(handled)
            pending = get_pending_action(123)
            self.assertIsNotNone(pending)
            self.assertEqual(pending["type"], "remove")

            confirm_update = {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Подтвердить удаление"},
            }
            handle_update(config, confirm_update)

            self.assertEqual(len(load_telegram_state()["pending_actions"]), 0)

        jobs_data = json.loads((tmp_dir / "jobs.json").read_text(encoding="utf-8"))
        state_data = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(jobs_data, [])
        self.assertEqual(state_data["queued_release_episodes"], {})

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_blacklist_flow_adds_release_and_removes_job(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        save_jobs(config, [{
            "title": "A",
            "title_ru": "А",
            "season": 1,
            "episodes_range": "001",
            "automation": {"release_id": 42},
        }])
        save_state(config, {
            "schema_version": 3,
            "queued_release_episodes": {"42:001": {"release_id": 42, "episode": 1}},
            "completed_release_episodes": {},
            "discovery_blacklist": [],
            "job_index": {},
            "skipped_items": [],
            "ongoing_progress": {},
        })

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handled = handle_update(config, {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/blacklist 1"},
            })
            self.assertTrue(handled)
            pending = get_pending_action(123)
            self.assertEqual(pending["type"], "blacklist")

            handle_update(config, {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Подтвердить blacklist"},
            })

        jobs_data = json.loads((tmp_dir / "jobs.json").read_text(encoding="utf-8"))
        state_data = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(jobs_data, [])
        self.assertEqual(state_data["queued_release_episodes"], {})
        self.assertEqual(len(state_data["discovery_blacklist"]), 1)
        self.assertEqual(state_data["discovery_blacklist"][0]["release_id"], 42)

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_blacklist_command_rejects_job_without_release_id(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        save_jobs(config, [{"title": "Manual", "season": 1, "episodes_range": "001"}])

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handled = handle_update(config, {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/blacklist 1"},
            })

        self.assertTrue(handled)
        self.assertIn("отсутствует release_id", mock_send_message.call_args.args[1])

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_unblacklist_flow_removes_blacklist_entry(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        save_state(config, {
            "schema_version": 3,
            "queued_release_episodes": {},
            "completed_release_episodes": {},
            "discovery_blacklist": [{
                "release_id": 42,
                "title": "A",
                "title_ru": "А",
                "season": 1,
                "added_at": "2026-06-27T00:00:00+00:00",
                "source": "telegram",
            }],
            "job_index": {},
            "skipped_items": [],
            "ongoing_progress": {},
        })

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handled = handle_update(config, {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/unblacklist 1"},
            })
            self.assertTrue(handled)
            pending = get_pending_action(123)
            self.assertEqual(pending["type"], "unblacklist")

            handle_update(config, {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Подтвердить снятие blacklist"},
            })

        state_data = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state_data["discovery_blacklist"], [])

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_blacklist_list_command_shows_entries(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        save_state(config, {
            "schema_version": 3,
            "queued_release_episodes": {},
            "completed_release_episodes": {},
            "discovery_blacklist": [{
                "release_id": 42,
                "title": "A",
                "title_ru": "А",
                "season": 1,
                "added_at": "2026-06-27T00:00:00+00:00",
                "source": "telegram",
            }],
            "job_index": {},
            "skipped_items": [],
            "ongoing_progress": {},
        })

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handle_update(config, {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/blacklist"},
            })

        self.assertIn("Discovery blacklist", mock_send_message.call_args.args[1])
        self.assertIn("Release ID: 42", mock_send_message.call_args.args[1])

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_progress_update_preserves_pending_actions_created_by_handle_update(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        save_jobs(config, [{"title": "A", "title_ru": "А", "season": 1, "episodes_range": "001"}])

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            initial_state = load_telegram_state()
            self.assertEqual(initial_state["pending_actions"], {})

            remove_update = {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/remove 1", "date": 111},
            }
            handle_update(config, remove_update)

            state_after_handle = load_telegram_state()
            self.assertIn("123", state_after_handle["pending_actions"])

            updated_state = update_telegram_state_progress(
                last_update_id=remove_update["update_id"],
                last_handled_at=remove_update["message"]["date"],
            )
            self.assertIn("123", updated_state["pending_actions"])
            self.assertEqual(updated_state["last_update_id"], 1)
            self.assertEqual(updated_state["last_handled_at"], 111)

            confirm_update = {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Подтвердить удаление", "date": 222},
            }
            handle_update(config, confirm_update)

            state_after_confirm = load_telegram_state()
            self.assertEqual(state_after_confirm["pending_actions"], {})

            updated_state = update_telegram_state_progress(
                last_update_id=confirm_update["update_id"],
                last_handled_at=confirm_update["message"]["date"],
            )
            self.assertEqual(updated_state["pending_actions"], {})
            self.assertEqual(updated_state["last_update_id"], 2)
            self.assertEqual(updated_state["last_handled_at"], 222)

        jobs_data = json.loads((tmp_dir / "jobs.json").read_text(encoding="utf-8"))
        self.assertEqual(jobs_data, [])

    @patch("lib.telegram_bot.send_reply")
    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_retry_flow_adds_completed_job_back_to_queue(self, _mock_allowed, mock_send_message, mock_send_reply):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        completed_path = tmp_dir / "completed_jobs.json"
        completed_path.write_text(
            """[
  {
    "status": "completed",
    "job": {
      "title": "A",
      "title_ru": "А",
      "season": 1,
      "episodes_range": "001",
      "source": {
        "type": "magnet",
        "magnet": "magnet:?xt=urn:btih:testhash",
        "download_dir": "downloads/A"
      },
      "automation": {
        "release_id": 1
      }
    }
  }
]""",
            encoding="utf-8",
        )
        config["automation"]["completed_jobs_path"] = str(completed_path.resolve())
        save_jobs(config, [])
        save_state(config, {
            "schema_version": 2,
            "queued_release_episodes": {},
            "completed_release_episodes": {},
            "job_index": {},
            "skipped_items": [],
            "ongoing_progress": {},
        })

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            list_update = {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/retry"},
            }
            handle_update(config, list_update)
            self.assertIn("Кандидаты для повтора", mock_send_reply.call_args.args[1])

            pick_update = {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "/retry 1"},
            }
            handle_update(config, pick_update)
            pending = get_pending_action(123)
            self.assertEqual(pending["type"], "retry")

            confirm_update = {
                "update_id": 3,
                "message": {"chat": {"id": 123}, "text": "Подтвердить повтор"},
            }
            handle_update(config, confirm_update)

        jobs_data = json.loads((tmp_dir / "jobs.json").read_text(encoding="utf-8"))
        state_data = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(jobs_data), 1)
        self.assertEqual(jobs_data[0]["title_ru"], "А")
        self.assertIn("1:001", state_data["queued_release_episodes"])

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_complete_flow_moves_job_from_queue_to_completed_archive(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        completed_path = tmp_dir / "completed_jobs.json"
        config["automation"]["completed_jobs_path"] = str(completed_path.resolve())
        save_jobs(config, [{
            "title": "A",
            "title_ru": "А",
            "season": 1,
            "episodes_range": "001",
            "source": {
                "type": "magnet",
                "magnet": "magnet:?xt=urn:btih:testhash",
                "download_dir": "downloads/A",
            },
            "automation": {"release_id": 1},
        }])
        save_state(config, {
            "schema_version": 2,
            "queued_release_episodes": {"1:001": {"release_id": 1, "episode": 1}},
            "completed_release_episodes": {},
            "job_index": {},
            "skipped_items": [],
            "ongoing_progress": {},
        })

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            update = {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/complete 1"},
            }
            handled = handle_update(config, update)

            self.assertTrue(handled)
            pending = get_pending_action(123)
            self.assertIsNotNone(pending)
            self.assertEqual(pending["type"], "complete")

            confirm_update = {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Подтвердить завершение"},
            }
            handle_update(config, confirm_update)

        jobs_data = json.loads((tmp_dir / "jobs.json").read_text(encoding="utf-8"))
        completed_data = json.loads(completed_path.read_text(encoding="utf-8"))
        state_data = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(jobs_data, [])
        self.assertEqual(len(completed_data), 1)
        self.assertEqual(state_data["queued_release_episodes"], {})
        self.assertIn("1:001", state_data["completed_release_episodes"])

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_jobs_pagination_navigation_uses_buttons_and_persists_page(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        jobs = []
        for index in range(1, 29):
            jobs.append({
                "title": f"Title {index}",
                "title_ru": f"Тайтл {index}",
                "season": 1,
                "episodes_range": f"{index:03d}",
            })
        save_jobs(config, jobs)

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handle_update(config, {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/jobs"},
            })
            first_message = mock_send_message.call_args_list[-1].args[1]
            first_markup = mock_send_message.call_args_list[-1].kwargs["reply_markup"]
            self.assertIn("Страница: 1/2", first_message)
            self.assertIn("1. Тайтл 1", first_message)
            self.assertEqual(first_markup["keyboard"][0][0]["text"], "Вперед")
            self.assertEqual(get_jobs_pagination_page(123), 1)

            handle_update(config, {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Вперед"},
            })
            second_message = mock_send_message.call_args_list[-1].args[1]
            second_markup = mock_send_message.call_args_list[-1].kwargs["reply_markup"]
            self.assertIn("Страница: 2/2", second_message)
            self.assertIn("16. Тайтл 16", second_message)
            self.assertEqual(second_markup["keyboard"][0][0]["text"], "Назад")
            self.assertEqual(get_jobs_pagination_page(123), 2)

            handle_update(config, {
                "update_id": 3,
                "message": {"chat": {"id": 123}, "text": "Назад"},
            })
            third_message = mock_send_message.call_args_list[-1].args[1]
            self.assertIn("Страница: 1/2", third_message)
            self.assertEqual(get_jobs_pagination_page(123), 1)

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_complete_flow_does_not_duplicate_existing_completed_entry(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        completed_path = tmp_dir / "completed_jobs.json"
        config["automation"]["completed_jobs_path"] = str(completed_path.resolve())
        job = {
            "title": "A",
            "title_ru": "А",
            "season": 1,
            "episodes_range": "001",
            "source": {
                "type": "magnet",
                "magnet": "magnet:?xt=urn:btih:testhash",
                "download_dir": "downloads/A",
            },
            "automation": {"release_id": 1},
        }
        save_jobs(config, [job])
        save_state(config, {
            "schema_version": 2,
            "queued_release_episodes": {"1:001": {"release_id": 1, "episode": 1}},
            "completed_release_episodes": {"1:001": {"release_id": 1, "episode": 1}},
            "job_index": {},
            "skipped_items": [],
            "ongoing_progress": {},
        })
        completed_path.write_text(
            json.dumps([
                {
                    "status": "completed",
                    "completed_at": "2026-06-13T10:00:00+00:00",
                    "job": job,
                    "delivery_summary": {},
                }
            ], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handle_update(config, {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/complete 1"},
            })
            handle_update(config, {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Подтвердить завершение"},
            })

        jobs_data = json.loads((tmp_dir / "jobs.json").read_text(encoding="utf-8"))
        completed_data = json.loads(completed_path.read_text(encoding="utf-8"))
        state_data = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(jobs_data, [])
        self.assertEqual(len(completed_data), 1)
        self.assertEqual(state_data["queued_release_episodes"], {})
        self.assertIn("1:001", state_data["completed_release_episodes"])

    @patch("lib.telegram_bot.send_formatted_message")
    @patch("lib.telegram_bot.answer_callback_query")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_notification_details_callback_sends_detailed_message(
        self,
        _mock_allowed,
        mock_answer_callback_query,
        mock_send_formatted_message,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        job = {
            "title": "Tokyo Ghoul:re",
            "title_ru": "Токийский Гуль: Перерождение",
            "season": 2,
            "episodes_range": "001-024",
        }
        result = {
            "quality_summary": {
                "episodes_count": 24,
                "episodes_with_op_removed": 24,
                "episodes_with_ed_removed": 22,
                "episodes_manual_review": 1,
                "episodes_anilibria_only": 18,
                "episodes_anilibria_with_detector": 4,
                "episodes_with_warnings": [{"episode": 8}],
            },
            "delivery_summary": {
                "vk": {
                    "enabled": True,
                    "video_uploaded": True,
                    "post_created": True,
                    "comment_created": False,
                    "error": "HTTPError('504 Server Error: Gateway Time-out for url: https://pu.vk.com/upload.php')",
                },
                "s3": {
                    "enabled": False,
                    "uploaded": False,
                },
            },
        }

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            reply_markup = build_notification_details_reply_markup(
                build_notification_details_payload(job, result),
            )
            callback_data = reply_markup["inline_keyboard"][0][0]["callback_data"]

            handled = handle_update(config, {
                "update_id": 1,
                "callback_query": {
                    "id": "callback-1",
                    "data": callback_data,
                    "message": {"chat": {"id": 123}},
                },
            })

        self.assertTrue(handled)
        mock_answer_callback_query.assert_called_once()
        self.assertEqual(mock_answer_callback_query.call_args.args[0], "callback-1")
        self.assertEqual(mock_send_formatted_message.call_args.args[0], 123)
        message = mock_send_formatted_message.call_args.args[1]
        self.assertIn("ℹ️ *Подробно*", message)
        self.assertIn("🎬 *Токийский Гуль: Перерождение*", message)
        self.assertIn("📺 Эпизоды: `001-024`", message)
        self.assertIn("✂️ OP: `24/24` • ED: `22/24`", message)
        self.assertIn("⚠️ Warnings: `1`", message)
        self.assertIn("🛠 Manual review: `1`", message)
        self.assertIn("• anilibria\\_only: `18`", message)
        self.assertIn("• anilibria\\_with\\_detector: `4`", message)
        self.assertIn("• VK video: `ok`", message)
        self.assertIn("• VK comment: `failed`", message)
        self.assertIn("Причина partial failure: `504 Gateway Time-out`", message)
        self.assertEqual(mock_send_formatted_message.call_args.kwargs["parse_mode"], "MarkdownV2")

    @patch("lib.telegram_bot.send_reply")
    @patch("lib.telegram_bot.answer_callback_query")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_notification_details_callback_handles_missing_token(
        self,
        _mock_allowed,
        mock_answer_callback_query,
        mock_send_reply,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handled = handle_update(config, {
                "update_id": 1,
                "callback_query": {
                    "id": "callback-2",
                    "data": "details:missing-token",
                    "message": {"chat": {"id": 123}},
                },
            })

        self.assertTrue(handled)
        mock_answer_callback_query.assert_called_once()
        self.assertEqual(mock_answer_callback_query.call_args.args[0], "callback-2")
        self.assertIn("Детали уже недоступны", mock_send_reply.call_args.args[1])

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.send_formatted_message")
    @patch("lib.telegram_bot.answer_callback_query")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_notification_details_callback_falls_back_to_plain_text_on_markdown_error(
        self,
        _mock_allowed,
        mock_answer_callback_query,
        mock_send_formatted_message,
        mock_send_message,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        job = {
            "title": "Black Clover",
            "title_ru": "Черный клевер",
            "season": 1,
            "episodes_range": "001-003",
        }
        result = {
            "quality_summary": {
                "episodes_count": 3,
                "episodes_with_op_removed": 3,
                "episodes_with_ed_removed": 0,
            },
            "delivery_summary": {
                "vk": {
                    "enabled": True,
                    "video_uploaded": True,
                    "post_created": True,
                    "comment_created": True,
                },
            },
        }
        mock_send_formatted_message.side_effect = RuntimeError(
            "Telegram API sendMessage HTTP 400: Bad Request: can't parse entities",
        )
        mock_send_message.return_value = {"ok": True}

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            reply_markup = build_notification_details_reply_markup(
                build_notification_details_payload(job, result),
            )
            callback_data = reply_markup["inline_keyboard"][0][0]["callback_data"]
            handled = handle_update(config, {
                "update_id": 1,
                "callback_query": {
                    "id": "callback-3",
                    "data": callback_data,
                    "message": {"chat": {"id": 123}},
                },
            })

        self.assertTrue(handled)
        mock_answer_callback_query.assert_called_once()
        mock_send_formatted_message.assert_called_once()
        mock_send_message.assert_called_once()
        fallback_message = mock_send_message.call_args.args[1]
        self.assertIn("ℹ️ Подробно", fallback_message)
        self.assertIn("Черный клевер", fallback_message)

    @patch("lib.telegram_bot.send_reply")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_handle_update_returns_error_message_for_invalid_command(self, _mock_allowed, mock_send_reply):
        config = {"automation": {}}
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 123},
                "text": "/add broken",
                "date": 1234567890,
            },
        }

        handled = handle_update(config, update)

        self.assertTrue(handled)
        self.assertTrue(mock_send_reply.called)
        self.assertIn("Команда не выполнена", mock_send_reply.call_args.args[1])

    @patch("lib.telegram_bot.requests.post")
    def test_telegram_request_uses_proxy_when_configured(self, mock_post):
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"ok": True, "result": []}

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_PROXY_URL": "socks5h://user:pass@host:1080",
        }):
            _telegram_request("getMe", payload={"a": 1}, timeout=12)

        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 12)
        self.assertEqual(kwargs["json"], {"a": 1})
        self.assertEqual(kwargs["proxies"]["http"], "socks5h://user:pass@host:1080")
        self.assertEqual(kwargs["proxies"]["https"], "socks5h://user:pass@host:1080")

    @patch("lib.telegram_bot.requests.post")
    def test_telegram_request_omits_proxy_when_not_configured(self, mock_post):
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"ok": True, "result": []}

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
        }, clear=False):
            os.environ.pop("TELEGRAM_PROXY_URL", None)
            _telegram_request("getMe", payload={"a": 1}, timeout=12)

        kwargs = mock_post.call_args.kwargs
        self.assertNotIn("proxies", kwargs)

    @patch("lib.telegram_bot.requests.post")
    def test_telegram_request_includes_api_description_on_http_error(self, mock_post):
        response = mock_post.return_value
        response.raise_for_status.side_effect = __import__("requests").HTTPError("400 Client Error")
        response.json.return_value = {
            "ok": False,
            "description": "Bad Request: can't parse entities",
        }

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token"}):
            with self.assertRaises(RuntimeError) as context:
                _telegram_request("sendMessage", payload={"text": "x"}, timeout=12)

        self.assertIn("can't parse entities", str(context.exception))

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.send_formatted_message")
    @patch("lib.telegram_bot.parse_allowed_chat_ids", return_value={"123"})
    @patch("lib.telegram_bot.telegram_notifications_enabled", return_value=True)
    def test_send_message_to_allowed_chats_falls_back_to_plain_text_on_markdown_parse_error(
        self,
        _mock_enabled,
        _mock_parse_allowed_chat_ids,
        mock_send_formatted_message,
        mock_send_message,
    ):
        mock_send_formatted_message.side_effect = RuntimeError(
            "Telegram API sendMessage HTTP 400: Bad Request: can't parse entities",
        )
        mock_send_message.return_value = {"ok": True}

        result = send_message_to_allowed_chats(
            "✅ *Видео опубликовано в VK*\n\n🎬 *Черный клевер*",
            parse_mode="MarkdownV2",
        )

        self.assertEqual(result, [{"ok": True}])
        mock_send_formatted_message.assert_called_once()
        mock_send_message.assert_called_once_with(
            "123",
            "✅ Видео опубликовано в VK\n\n🎬 Черный клевер",
            reply_markup=None,
        )


if __name__ == "__main__":
    unittest.main()
