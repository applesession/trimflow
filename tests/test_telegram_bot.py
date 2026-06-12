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
    build_help_message,
    format_discovery_message,
    format_error_message,
    format_jobs_message,
    format_publish_success_message,
    format_status_message,
    get_display_title,
    handle_update,
    is_allowed_chat,
    load_telegram_state,
    normalize_command_text,
    parse_add_command,
    save_telegram_state,
    get_telegram_proxy_url,
    _telegram_request,
    telegram_force_ipv4_enabled,
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
            "/add Test Title : 001-003 : magnet:?xt=urn:btih:testhash : 2",
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
        command = "/add Test Title : 001-003 : magnet:?xt=urn:btih:testhash : 2"

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
        )

        self.assertIn("Автодискавери завершён", discovery)
        self.assertIn("Тестовый тайтл", discovery)
        self.assertIn("Новых аниме: 1", discovery)
        self.assertIn("Контекст: cron_run", error)
        self.assertIn("Детали: boom", error)
        self.assertIn("Обработка завершена", success)
        self.assertIn("s3://bucket/file.mkv", success)
        self.assertIn("Тестовый тайтл", success)

    def test_status_command_reads_jobs_and_state(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [{"title": "A", "season": 1, "episodes_range": "001"}])
        save_state(config, {
            "schema_version": 1,
            "last_discovery_at": "2026-06-07T10:00:00+00:00",
            "seen_release_episodes": {"1:1": True, "1:2": True},
            "job_index": {},
            "skipped_items": [],
        })

        runtime_paths = {
            "runtime_dir": tmp_dir,
            "logs_dir": tmp_dir,
            "lock_path": tmp_dir / "cron.lock",
            "log_path": tmp_dir / "cron.log",
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
        self.assertIn("Зафиксировано эпизодов: 2", message)
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

    def test_build_main_keyboard_returns_reply_markup(self):
        keyboard = build_main_keyboard()

        self.assertTrue(keyboard["resize_keyboard"])
        self.assertFalse(keyboard["one_time_keyboard"])
        self.assertEqual(keyboard["keyboard"][0][0]["text"], "Статус")
        self.assertEqual(keyboard["keyboard"][0][1]["text"], "Очередь")
        self.assertEqual(keyboard["keyboard"][1][0]["text"], "Помощь")

    def test_normalize_command_text_maps_button_aliases(self):
        self.assertEqual(normalize_command_text("Статус"), "/status")
        self.assertEqual(normalize_command_text("Очередь"), "/jobs")
        self.assertEqual(normalize_command_text("Помощь"), "/help")
        self.assertEqual(normalize_command_text("/jobs"), "/jobs")

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


if __name__ == "__main__":
    unittest.main()
