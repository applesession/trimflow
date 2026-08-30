import json
import os
import shutil
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from lib import reset_test_db
import shared.db as db
from lib.config import load_completed_jobs, load_jobs, save_completed_jobs, save_jobs, save_state
from shared.db import (
    add_to_blacklist,
    claim_job,
    get_discovery_blacklist,
    get_episode_tracking_dicts,
    mark_episodes_completed,
    mark_episodes_queued,
)
from lib.telegram_bot import (
    add_job_from_command,
    add_multi_job_from_command,
    add_seasons_job_from_command,
    add_upscale_job_from_command,
    build_main_keyboard,
    build_notification_details_payload,
    build_notification_details_reply_markup,
    build_help_message,
    format_current_message,
    format_discovery_message,
    format_elapsed_ru,
    format_error_message,
    format_errors_message,
    format_log_message,
    format_log_message_markdown,
    format_jobs_message,
    format_jobs_message_markdown,
    format_main_worker_section,
    format_next_message,
    format_publish_success_message,
    format_render_interrupted_message,
    format_vk_publish_error_message,
    format_vk_publish_success_message,
    format_upscale_worker_section,
    get_jobs_pagination_page,
    get_pending_action,
    get_display_title,
    handle_command,
    handle_update,
    is_allowed_chat,
    load_telegram_state,
    normalize_command_text,
    parse_add_command,
    parse_addmulti_command,
    parse_addseasons_command,
    parse_add4k_command,
    retry_job_to_queue,
    stop_job,
    update_job_navigation_label,
    update_job_audio_recovery,
    save_telegram_state,
    send_message_to_allowed_chats,
    get_telegram_proxy_url,
    _telegram_request,
    telegram_force_ipv4_enabled,
    update_telegram_state_progress,
)


class TelegramBotTests(unittest.TestCase):
    def setUp(self):
        reset_test_db()

    def test_render_interrupted_message_includes_recovery_context(self):
        message = format_render_interrupted_message(
            {"title": "Chunk Test", "title_ru": "Тест", "episodes_range": "001-024"},
            "OOM killer завершил процесс PID 123",
            {
                "current_stage": "final_render",
                "current_job": {
                    "title": "Chunk Test",
                    "stage": "final_render",
                    "current_chunk_index": 2,
                    "total_chunks": 2,
                    "current_episode": 18,
                    "total_episodes": 24,
                },
            },
            {"pid": 123, "started_at": "2026-07-17T14:00:00+00:00"},
        )

        self.assertIn("Render аварийно прерван", message)
        self.assertIn("OOM killer", message)
        self.assertIn("Чанк:", message)
        self.assertIn("Серия:", message)
        self.assertIn("checkpoint", message)

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

    def test_add_command_stores_optional_source_path_filter(self):
        command = "/add Test Title ; 001-003 ; magnet:?xt=urn:btih:testhash ; 2 ; 0 ; HEVC/TV"
        payload = parse_add_command(command)
        result = add_job_from_command(self.make_config(self.make_workspace_temp_dir()), command)

        self.assertEqual(payload["source_path_contains"], "HEVC/TV")
        self.assertEqual(result["job"]["processing"]["source_path_contains"], "HEVC/TV")

    def test_addmulti_command_stores_ordered_sources(self):
        command = "\n".join([
            "/addmulti Hunter x Hunter ; 001-148 ; 2 ; 3",
            "magnet:?xt=urn:btih:first",
            "magnet:?xt=urn:btih:second ; HEVC/TV",
        ])

        payload = parse_addmulti_command(command)
        result = add_multi_job_from_command(
            self.make_config(self.make_workspace_temp_dir()),
            command,
        )
        stored = load_jobs({})[0]

        self.assertEqual(payload["episodes_range"], "001-148")
        self.assertEqual(payload["season"], 2)
        self.assertEqual(payload["privacy_view"], 3)
        self.assertEqual(payload["sources"][1]["path_filter"], "HEVC/TV")
        self.assertTrue(result["added"])
        self.assertEqual(stored["source"]["parts"], payload["sources"])
        self.assertEqual(stored["delivery"]["vk_privacy_view"], 3)

    def test_addmulti_command_requires_two_sources(self):
        with self.assertRaisesRegex(RuntimeError, "минимум две"):
            parse_addmulti_command("\n".join([
                "/addmulti Hunter x Hunter ; 001-148 ; 1 ; 0",
                "magnet:?xt=urn:btih:first",
            ]))

    def test_addseasons_command_maps_one_source_per_season(self):
        command = "\n".join([
            "/addseasons Hunter x Hunter ; 1-3 ; 3",
            "magnet:?xt=urn:btih:first",
            "magnet:?xt=urn:btih:second ; HEVC/TV",
            "magnet:?xt=urn:btih:third",
        ])

        payload = parse_addseasons_command(command)
        result = add_seasons_job_from_command(
            self.make_config(self.make_workspace_temp_dir()),
            command,
        )
        stored = load_jobs({})[0]

        self.assertEqual(payload["season_range"], "1-3")
        self.assertEqual([part["season"] for part in payload["sources"]], [1, 2, 3])
        self.assertEqual(payload["sources"][1]["path_filter"], "HEVC/TV")
        self.assertTrue(result["added"])
        self.assertEqual(stored["processing_mode"], "multi_season")
        self.assertEqual(stored["source"]["parts"], payload["sources"])
        self.assertEqual(stored["delivery"]["vk_privacy_view"], 3)

    def test_addseasons_command_requires_source_for_every_season(self):
        with self.assertRaisesRegex(RuntimeError, "минимум 3 magnet-ссылок"):
            parse_addseasons_command("\n".join([
                "/addseasons Hunter x Hunter ; 1-3 ; 0",
                "magnet:?xt=urn:btih:first",
                "magnet:?xt=urn:btih:second",
            ]))

    def test_addseasons_extra_positional_sources_belong_to_last_season(self):
        payload = parse_addseasons_command("\n".join([
            "/addseasons Hunter x Hunter ; 1-3 ; 0",
            "magnet:?xt=urn:btih:season1",
            "magnet:?xt=urn:btih:season2",
            "magnet:?xt=urn:btih:season3part1",
            "magnet:?xt=urn:btih:season3part2",
            "magnet:?xt=urn:btih:season3part3",
        ]))

        self.assertEqual([source["season"] for source in payload["sources"]], [1, 2, 3, 3, 3])

    def test_addseasons_command_allows_multiple_sources_for_one_season(self):
        payload = parse_addseasons_command("\n".join([
            "/addseasons Hunter x Hunter ; 1-3 ; 0",
            "1 ; magnet:?xt=urn:btih:season1",
            "2 ; magnet:?xt=urn:btih:season2",
            "3 ; magnet:?xt=urn:btih:season3part1",
            "3 ; magnet:?xt=urn:btih:season3part2 ; HEVC",
        ]))

        self.assertEqual([source["season"] for source in payload["sources"]], [1, 2, 3, 3])
        self.assertEqual(payload["sources"][-1]["path_filter"], "HEVC")

    def test_addseasons_explicit_format_requires_every_season(self):
        with self.assertRaisesRegex(RuntimeError, r"Не указаны источники для сезонов: \[2\]"):
            parse_addseasons_command("\n".join([
                "/addseasons Hunter x Hunter ; 1-3 ; 0",
                "1 ; magnet:?xt=urn:btih:season1",
                "3 ; magnet:?xt=urn:btih:season3part1",
                "3 ; magnet:?xt=urn:btih:season3part2",
            ]))

    def test_db_v2_migration_adds_source_parts_column(self):
        db_path = self.make_workspace_temp_dir() / "data.db"
        connection = sqlite3.connect(db_path)
        connection.executescript("""
            CREATE TABLE jobs (id INTEGER PRIMARY KEY);
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (1);
        """)
        connection.close()

        with patch.object(db, "DB_PATH", db_path):
            db.init_db()
            connection = sqlite3.connect(db_path)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
            connection.close()

        self.assertIn("source_parts", columns)
        self.assertEqual(version, 2)

    def test_add4k_command_builds_direct_donut_job(self):
        payload = parse_add4k_command(
            "/add4k Test Title ; 25 ; magnet:?xt=urn:btih:testhash ; 2",
        )
        result = add_upscale_job_from_command(
            self.make_config(self.make_workspace_temp_dir()),
            "/add4k Test Title ; 25 ; magnet:?xt=urn:btih:testhash ; 2",
        )

        self.assertEqual(payload["episodes_range"], "001-025")
        self.assertTrue(result["added"])
        self.assertEqual(result["job"]["processing_mode"], "upscale_4k")
        self.assertTrue(result["job"]["delivery"]["vk_direct_donut"])
        self.assertEqual(result["job"]["delivery"]["vk_privacy_view"], 5)

    def test_add4k_command_stores_optional_source_path_filter(self):
        command = "/add4k Test Title ; 001-003 ; magnet:?xt=urn:btih:testhash ; 2 ; HEVC/TV"
        payload = parse_add4k_command(command)
        result = add_upscale_job_from_command(self.make_config(self.make_workspace_temp_dir()), command)

        self.assertEqual(payload["source_path_contains"], "HEVC/TV")
        self.assertEqual(result["job"]["processing"]["source_path_contains"], "HEVC/TV")

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

    def test_corrupt_telegram_state_is_quarantined_and_reset(self):
        tmp_dir = self.make_workspace_temp_dir()
        state_path = (tmp_dir / "telegram_state.json").resolve()
        state_path.write_text("", encoding="utf-8")

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            loaded = load_telegram_state()

        self.assertIsNone(loaded["last_update_id"])
        self.assertFalse(state_path.exists())
        self.assertEqual(len(list(tmp_dir.glob("telegram_state.json.corrupt.*"))), 1)

    def test_failed_atomic_state_replace_preserves_previous_file(self):
        tmp_dir = self.make_workspace_temp_dir()
        state_path = (tmp_dir / "telegram_state.json").resolve()
        original = '{"last_update_id": 41}\n'
        state_path.write_text(original, encoding="utf-8")

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            with patch("lib.telegram_bot.os.replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    save_telegram_state({"last_update_id": 42})

        self.assertEqual(state_path.read_text(encoding="utf-8"), original)
        self.assertEqual(list(tmp_dir.glob(".*.tmp")), [])

    def test_add_job_from_command_creates_and_deduplicates_job(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        command = "/add Test Title ; 001-003 ; magnet:?xt=urn:btih:testhash ; 2"

        first = add_job_from_command(config, command)
        second = add_job_from_command(config, command)

        self.assertTrue(first["added"])
        self.assertFalse(second["added"])
        self.assertEqual(second["reason"], "duplicate_job")

    def test_add_job_allows_same_title_with_different_or_overlapping_ranges(self):
        config = self.make_config(self.make_workspace_temp_dir())

        first = add_job_from_command(
            config,
            "/add One Piece ; 001-120 ; magnet:?xt=urn:btih:first ; 1",
        )
        overlapping = add_job_from_command(
            config,
            "/add One Piece ; 100-170 ; magnet:?xt=urn:btih:second ; 1",
        )

        self.assertTrue(first["added"])
        self.assertTrue(overlapping["added"])
        jobs = load_jobs(config)
        self.assertEqual([job["episodes_range"] for job in jobs], ["001-120", "100-170"])
        self.assertNotEqual(jobs[0]["source"]["download_dir"], jobs[1]["source"]["download_dir"])

    def test_add_job_treats_same_range_as_duplicate_regardless_of_source(self):
        config = self.make_config(self.make_workspace_temp_dir())
        first = add_job_from_command(
            config,
            "/add One Piece ; 1-3 ; magnet:?xt=urn:btih:first ; 1 ; 0 ; AVC",
        )
        duplicate = add_job_from_command(
            config,
            "/add One Piece ; 001-003 ; magnet:?xt=urn:btih:second ; 1 ; 0 ; HEVC",
        )

        self.assertTrue(first["added"])
        self.assertFalse(duplicate["added"])
        self.assertEqual(duplicate["reason"], "duplicate_job")

    def test_add_job_allows_same_range_for_different_processing_mode(self):
        config = self.make_config(self.make_workspace_temp_dir())

        regular = add_job_from_command(
            config,
            "/add One Piece ; 001-003 ; magnet:?xt=urn:btih:first ; 1",
        )
        upscale = add_upscale_job_from_command(
            config,
            "/add4k One Piece ; 001-003 ; magnet:?xt=urn:btih:second ; 1",
        )

        self.assertTrue(regular["added"])
        self.assertTrue(upscale["added"])

    def test_add_job_allows_same_range_for_different_season(self):
        config = self.make_config(self.make_workspace_temp_dir())

        first = add_job_from_command(
            config,
            "/add One Piece ; 001-003 ; magnet:?xt=urn:btih:first ; 1",
        )
        second = add_job_from_command(
            config,
            "/add One Piece ; 001-003 ; magnet:?xt=urn:btih:second ; 2",
        )

        self.assertTrue(first["added"])
        self.assertTrue(second["added"])

    def test_retry_allows_same_title_when_range_differs(self):
        config = self.make_config(self.make_workspace_temp_dir())
        add_job_from_command(
            config,
            "/add One Piece ; 001-120 ; magnet:?xt=urn:btih:first ; 1",
        )
        archived_job = {
            "title": "One Piece",
            "season": 1,
            "episodes_range": "121-240",
            "source": {"type": "magnet", "magnet": "magnet:?xt=urn:btih:second"},
        }

        self.assertTrue(retry_job_to_queue(config, archived_job))
        self.assertEqual(len(load_jobs(config)), 2)

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

    def test_error_message_includes_ffmpeg_output_tail(self):
        message = format_error_message(
            "job_failed:Ван-Пис",
            "ffmpeg exited with code 228: ffmpeg -y input.mkv\n"
            "ffmpeg output tail:\n"
            "corrupt decoded frame\n"
            "Too many packets buffered for output stream",
        )

        self.assertIn("ffmpeg code 228", message)
        self.assertIn("Too many packets buffered", message)

    def test_error_message_does_not_treat_codec_tag_as_http_status(self):
        error = (
            "RuntimeError('Audio stream not found: "
            "Kuroko no Basket - 21 [BDRip 1920x1080 x264 FLAC].mkv')"
        )

        message = format_error_message("job_failed:Баскетбол Куроко", error)

        self.assertIn("Audio stream not found", message)
        self.assertIn("x264 FLAC", message)
        self.assertNotIn("Причина: `264 FLAC`", message)

    def test_error_message_formats_explicit_http_client_error(self):
        message = format_error_message(
            "job_failed:Test",
            "HTTPError('404 Client Error: Not Found for url: https://example.test/file')",
        )

        self.assertIn("Причина: `404 Not Found`", message)

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

    def test_jobs_message_prefers_title_ru(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [{
            "title": "English Title",
            "title_ru": "Русский тайтл",
            "season": 1,
            "episodes_range": "001",
            "automation": {"release_id": 1, "is_ongoing": True, "release_type": "TV"},
        }])

        message = format_jobs_message(config)

        self.assertIn("Очередь аниме", message)
        self.assertIn("Русский тайтл [ongoing]", message)
        self.assertNotIn("English Title |", message)
        self.assertNotIn("Метка:", message)
        self.assertIn("Эпизоды: 001", message)

    def test_label_command_persists_release_override_and_auto_clears_it(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [{
            "title": "Named sequel",
            "title_ru": "Именное продолжение",
            "season": 1,
            "episodes_range": "001",
            "automation": {"release_id": 77, "is_ongoing": True, "release_type": "TV"},
        }])

        result = update_job_navigation_label(config, "/label 1; Перерождение")

        self.assertIn("Метка: Перерождение", result)
        self.assertEqual(
            load_jobs(config)[0]["processing"]["naming"]["navigation_label"],
            "Перерождение",
        )
        state = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["release_naming_overrides"]["77"], "Перерождение")

        result = update_job_navigation_label(config, "/label 1; auto")

        self.assertIn("Метка удалена", result)
        self.assertNotIn("naming", load_jobs(config)[0].get("processing", {}))
        state = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("77", state["release_naming_overrides"])

    def test_audiofix_updates_release_jobs_and_persists_override(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        automation = {"release_id": 77, "is_ongoing": True}
        save_jobs(config, [
            {
                "title": "Ongoing",
                "season": 1,
                "episodes_range": "010",
                "processing_mode": "single_episode",
                "automation": automation,
            },
            {
                "title": "Ongoing",
                "season": 1,
                "episodes_range": "001-010",
                "processing_mode": "compilation",
                "processing": {"source_path_contains": "AVC"},
                "automation": automation,
            },
        ])

        result = update_job_audio_recovery(config, "/audiofix-on 1", True)

        self.assertIn("Задач обновлено: 2", result)
        jobs = load_jobs(config)
        self.assertTrue(all(job["processing"]["audio_recovery_enabled"] for job in jobs))
        compilation = next(job for job in jobs if job["processing_mode"] == "compilation")
        self.assertEqual(compilation["processing"]["source_path_contains"], "AVC")
        self.assertIn("[audiofix]", format_jobs_message(config))
        state = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["release_audio_recovery_overrides"]["77"])

        off_result = update_job_audio_recovery(config, "/audiofix-off 1", False)

        jobs = load_jobs(config)
        self.assertIn("автокоррекция хвоста до 3 секунд", off_result)
        self.assertTrue(all("audio_recovery_enabled" not in job.get("processing", {}) for job in jobs))
        compilation = next(job for job in jobs if job["processing_mode"] == "compilation")
        self.assertEqual(compilation["processing"]["source_path_contains"], "AVC")
        state = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("77", state["release_audio_recovery_overrides"])

    def test_audiofix_supports_ranges_and_rejects_4k(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [
            {"title": "A", "season": 1, "episodes_range": "001"},
            {"title": "B", "season": 1, "episodes_range": "001"},
        ])

        update_job_audio_recovery(config, "/audiofix-on 1-2", True)
        self.assertTrue(all(job["processing"]["audio_recovery_enabled"] for job in load_jobs(config)))

        reset_test_db()
        save_jobs(config, [{
            "title": "4K",
            "season": 1,
            "episodes_range": "001",
            "processing_mode": "upscale_4k",
        }])
        with self.assertRaisesRegex(RuntimeError, "не поддерживается для 4K"):
            update_job_audio_recovery(config, "/audiofix-on 1", True)

    def test_stop_marks_running_job_without_deleting_it(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [{
            "title": "Active",
            "season": 1,
            "episodes_range": "001",
        }])
        job = load_jobs(config)[0]
        self.assertTrue(claim_job(job["_queue_id"]))

        result = stop_job(config, "/stop 1")

        self.assertIn("Остановка запрошена", result)
        jobs = load_jobs(config)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["_queue_status"], "stopping")

    def test_stop_rejects_pending_job(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [{
            "title": "Pending",
            "season": 1,
            "episodes_range": "001",
        }])

        with self.assertRaisesRegex(RuntimeError, "сейчас не выполняется"):
            stop_job(config, "/stop 1")

    def test_get_display_title_falls_back_to_title(self):
        self.assertEqual(get_display_title({"title_ru": "Русский", "title": "English"}), "Русский")
        self.assertEqual(get_display_title({"title": "English"}), "English")

    def test_help_message_hides_help_command_and_button_phrase(self):
        message = build_help_message()

        self.assertNotIn("/help - показать команды", message)
        self.assertNotIn("Кнопки:", message)
        self.assertNotIn("Статус, Очередь, Помощь", message)
        self.assertNotIn("/next", message)
        self.assertIn("/blacklist - показать discovery blacklist", message)
        self.assertIn("/unblacklist <номер> - убрать тайтл из discovery blacklist", message)
        self.assertIn("Порядок в /jobs — по приоритету выполнения", message)

    def test_build_main_keyboard_returns_reply_markup(self):
        keyboard = build_main_keyboard()

        self.assertTrue(keyboard["resize_keyboard"])
        self.assertFalse(keyboard["one_time_keyboard"])
        self.assertEqual(keyboard["keyboard"][0][0]["text"], "Текущая")
        self.assertEqual(keyboard["keyboard"][0][1]["text"], "Очередь")
        self.assertEqual(keyboard["keyboard"][1][0]["text"], "Ошибки")
        self.assertEqual(keyboard["keyboard"][1][1]["text"], "Лог")
        self.assertEqual(keyboard["keyboard"][2], [{"text": "Помощь"}])

    def test_normalize_command_text_maps_button_aliases(self):
        self.assertEqual(normalize_command_text("Статус"), "Статус")
        self.assertEqual(normalize_command_text("Текущая"), "/current")
        self.assertEqual(normalize_command_text("Очередь"), "/jobs")
        self.assertEqual(normalize_command_text("Ошибки"), "/errors")
        self.assertEqual(normalize_command_text("Лог"), "/log")
        self.assertEqual(normalize_command_text("4K"), "4K")
        self.assertEqual(normalize_command_text("Помощь"), "/help")
        self.assertEqual(normalize_command_text("/jobs"), "/jobs")

    def test_status_command_is_removed(self):
        self.assertEqual(handle_command({}, "/status"), "Неизвестная команда. Напиши /help")
        self.assertNotIn("/status", build_help_message())

    def test_upscale_command_is_removed(self):
        self.assertEqual(handle_command({}, "/upscale"), "Неизвестная команда. Напиши /help")
        self.assertNotIn("/upscale", build_help_message())

    @patch("lib.telegram_bot.get_main_worker_lock_state", return_value=("missing", None))
    @patch("lib.telegram_bot.load_state", return_value={})
    @patch("lib.telegram_bot.load_runtime_status", return_value={"run_status": "idle"})
    def test_current_command_returns_markdown_payload(self, _mock_runtime, _mock_state, _mock_lock):
        response = handle_command({}, "/current")

        self.assertEqual(response["parse_mode"], "MarkdownV2")
        self.assertIn("Основной worker свободен", response["text"])
        self.assertIn("4K worker свободен", response["text"])

    def test_elapsed_format_supports_minutes_and_hours(self):
        now = datetime(2026, 6, 13, 12, 31, tzinfo=timezone.utc)

        self.assertEqual(format_elapsed_ru("2026-06-13T12:00:00+00:00", now=now), "31 мин")
        self.assertEqual(format_elapsed_ru("2026-06-13T10:00:00+00:00", now=now), "2 ч 31 мин")

    @patch("lib.telegram_bot.get_main_worker_lock_state", return_value=("alive", {"pid": 123}))
    @patch("lib.telegram_bot.load_runtime_status")
    def test_current_message_shows_active_job(self, mock_load_runtime_status, _mock_lock):
        mock_load_runtime_status.return_value = {
            "run_status": "running",
            "run_started_at": "2026-06-13T10:00:00+00:00",
            "updated_at": "2026-06-13T10:03:00+00:00",
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

        message = format_main_worker_section({})

        self.assertIn("Основной worker", message)
        self.assertIn("Русский тайтл", message)
        self.assertIn("Сезон 1 · серии `001-010`", message)
        self.assertIn("Этап: `вырезка сегментов`", message)
        self.assertIn("Серия: `3`/`10`", message)
        self.assertNotIn("ep03.mkv", message)

    @patch("lib.telegram_bot.get_main_worker_lock_state", return_value=("alive", {"pid": 123}))
    @patch("lib.telegram_bot.load_runtime_status")
    def test_current_message_shows_multi_season_range(self, mock_load_runtime_status, _mock_lock):
        mock_load_runtime_status.return_value = {
            "run_status": "running",
            "updated_at": "2026-06-13T10:03:00+00:00",
            "queue_progress": {"current_job_index": 1, "total_jobs": 1},
            "current_job": {
                "title": "Hero * Academy",
                "season": 1,
                "processing_mode": "multi_season",
                "season_range": "1-5",
                "episodes_range": "001",
                "stage": "season_render",
                "started_at": "2026-06-13T10:01:00+00:00",
                "current_episode": 3,
                "total_episodes": 100,
            },
        }

        message = format_main_worker_section({})

        self.assertIn("Hero \\* Academy", message)
        self.assertIn("Сезоны `1-5`", message)
        self.assertNotIn("серии `001`", message)

    @patch("lib.telegram_bot.get_main_worker_lock_state", return_value=("alive", {"pid": 123}))
    @patch("lib.telegram_bot.load_runtime_status")
    def test_current_message_shows_worker_preparation(self, mock_load_runtime_status, _mock_lock):
        mock_load_runtime_status.return_value = {
            "run_status": "running",
            "run_started_at": "2026-06-13T10:00:00+00:00",
            "updated_at": "2026-06-13T10:00:00+00:00",
            "current_stage": "processing",
            "current_job": None,
        }

        message = format_main_worker_section({})

        self.assertIn("Основной worker запущен", message)
        self.assertIn("Этап: `обработка очереди`", message)

    @patch("lib.telegram_bot.get_main_worker_lock_state", return_value=("missing", None))
    @patch("lib.telegram_bot.load_runtime_status")
    def test_current_message_detects_dead_worker(self, mock_load_runtime_status, _mock_lock):
        mock_load_runtime_status.return_value = {
            "run_status": "running",
            "updated_at": "2026-06-13T10:03:00+00:00",
            "current_stage": "render_episode",
            "current_job": {
                "title": "Interrupted",
                "season": 1,
                "episodes_range": "001-010",
                "stage": "render_episode",
                "current_episode": 3,
                "total_episodes": 10,
            },
        }

        message = format_main_worker_section({})

        self.assertIn("Основной worker аварийно остановлен", message)
        self.assertIn("Последняя серия: `3`/`10`", message)

    @patch("lib.telegram_bot.get_main_worker_lock_state", return_value=("missing", None))
    @patch("lib.telegram_bot.load_state", return_value={"last_discovery_at": "2026-06-13T09:00:00+00:00"})
    @patch("lib.telegram_bot.load_runtime_status")
    def test_current_message_shows_last_run_when_idle(self, mock_load_runtime_status, _mock_state, _mock_lock):
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

        message = format_main_worker_section({})

        self.assertIn("Основной worker свободен", message)
        self.assertIn("Очередь пуста", message)
        self.assertIn("Последнее выполнение", message)
        self.assertIn("Русский тайтл", message)
        self.assertIn("`ошибка` · `серия 8/12`", message)

    @patch("lib.telegram_bot.get_main_worker_lock_state", return_value=("missing", None))
    @patch("lib.telegram_bot.load_state", return_value={})
    @patch("lib.telegram_bot.load_runtime_status", return_value={"run_status": "completed"})
    def test_current_message_shows_pending_queue_when_idle(self, _mock_runtime, _mock_state, _mock_lock):
        save_jobs({}, [{"title": "Waiting", "season": 1, "episodes_range": "001"}])

        message = format_main_worker_section({})

        self.assertIn("Основной worker ожидает запуска", message)
        self.assertIn("В очереди: `1`", message)

    @patch("lib.telegram_bot.get_upscale_worker_lock_state", return_value=("alive", {"pid": 456}))
    @patch("lib.telegram_bot.load_runtime_status")
    def test_upscale_section_shows_active_job(self, mock_load_runtime_status, _mock_lock):
        mock_load_runtime_status.return_value = {
            "run_status": "running",
            "updated_at": "2026-06-13T10:03:00+00:00",
            "current_job": {
                "title": "Hero * 4K",
                "season": 2,
                "episodes_range": "001-025",
                "stage": "upscale_render",
                "started_at": "2026-06-13T10:01:00+00:00",
                "current_episode": 7,
                "total_episodes": 25,
            },
        }

        message = format_upscale_worker_section({})

        self.assertIn("🚀 *4K worker*\n\n🎬", message)
        self.assertIn("`001-025`\n\n📍 Этап", message)
        self.assertIn("4K worker", message)
        self.assertIn("Hero \\* 4K", message)
        self.assertIn("Сезон 2 · серии `001-025`", message)
        self.assertIn("Этап: `4K upscale`", message)
        self.assertIn("Серия: `7`/`25`", message)

    @patch("lib.telegram_bot.get_upscale_worker_lock_state", return_value=("missing", None))
    @patch("lib.telegram_bot.load_runtime_status")
    def test_upscale_section_detects_dead_worker(self, mock_load_runtime_status, _mock_lock):
        mock_load_runtime_status.return_value = {
            "run_status": "running",
            "current_stage": "upscale_render",
            "current_job": {
                "title": "Interrupted 4K",
                "season": 1,
                "episodes_range": "001-012",
                "current_episode": 4,
                "total_episodes": 12,
            },
        }

        message = format_upscale_worker_section({})

        self.assertIn("4K worker аварийно остановлен", message)
        self.assertIn("Последняя серия: `4`/`12`", message)

    @patch("lib.telegram_bot.get_upscale_worker_lock_state", return_value=("missing", None))
    @patch("lib.telegram_bot.load_runtime_status", return_value={"run_status": "idle"})
    def test_upscale_section_shows_pending_queue(self, _mock_runtime, _mock_lock):
        save_jobs({}, [{
            "title": "Waiting 4K",
            "season": 1,
            "episodes_range": "001",
            "processing_mode": "upscale_4k",
        }])

        message = format_upscale_worker_section({})

        self.assertIn("4K worker ожидает запуска", message)
        self.assertIn("4K worker ожидает запуска*\n\n📋 В очереди: `1`", message)

    @patch("lib.telegram_bot.get_upscale_worker_lock_state", return_value=("missing", None))
    @patch("lib.telegram_bot.load_runtime_status")
    def test_upscale_section_shows_last_run_compactly(self, mock_load_runtime_status, _mock_lock):
        mock_load_runtime_status.return_value = {
            "run_status": "completed",
            "last_run": {
                "title": "Finished 4K",
                "status": "completed",
                "current_episode": 12,
                "total_episodes": 12,
            },
        }

        message = format_upscale_worker_section({})

        self.assertIn("4K worker свободен", message)
        self.assertIn("4K worker свободен*\n\n📋 Очередь пуста", message)
        self.assertIn("*Последнее выполнение*\n🎬 *Finished 4K*", message)
        self.assertIn("`успешно` · `серия 12/12`", message)

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
                    "current_chunk_index": 1,
                    "total_chunks": 2,
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
        self.assertIn("Чанк: 1 / 2", message)
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

    def test_jobs_messages_show_season_range_for_multi_season_job(self):
        job = {
            "title": "Kuroko",
            "title_ru": "Баскетбол Куроко",
            "season": 1,
            "episodes_range": "001",
            "processing_mode": "multi_season",
            "processing": {"season_range": "1-5"},
        }

        plain = format_jobs_message({}, jobs=[job])
        markdown = format_jobs_message_markdown({}, jobs=[job])

        self.assertIn("Сезоны: 1-5", plain)
        self.assertNotIn("Эпизоды: 001", plain)
        self.assertIn("Сезоны `1-5`", markdown)
        self.assertNotIn("серии `001`", markdown)
        self.assertNotIn("Сезон 1", markdown)

    def test_jobs_markdown_has_no_blank_line_between_titles_in_same_group(self):
        jobs = [
            {"title": "A", "season": 1, "episodes_range": "001"},
            {"title": "B", "season": 2, "episodes_range": "002"},
        ]

        message = format_jobs_message_markdown({}, jobs=jobs)

        self.assertIn("серии `001`\n*2\\. B*", message)
        self.assertNotIn("серии `001`\n\n*2\\. B*", message)

    def test_jobs_message_keeps_storage_order_even_for_ongoing_items(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [
            {"title": "Manual", "title_ru": "Ручной", "season": 1, "episodes_range": "001"},
            {
                "title": "Ongoing",
                "title_ru": "Онгоинг",
                "season": 1,
                "episodes_range": "010",
                "automation": {"is_ongoing": True},
            },
        ])

        message = format_jobs_message(config)

        self.assertIn("1. Ручной", message)
        self.assertIn("2. Онгоинг [ongoing]", message)

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

    def test_next_message_uses_execution_order_with_ongoing_priority(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        save_jobs(config, [
            {"title": "Manual", "title_ru": "Ручной", "season": 1, "episodes_range": "001"},
            {
                "title": "Ongoing Full",
                "title_ru": "Онгоинг фулл",
                "season": 1,
                "episodes_range": "001-010",
                "processing_mode": "compilation",
                "automation": {"is_ongoing": True, "publish_strategy": "full_refresh"},
            },
            {
                "title": "Ongoing Single",
                "title_ru": "Онгоинг сингл",
                "season": 1,
                "episodes_range": "010",
                "processing_mode": "single_episode",
                "automation": {"is_ongoing": True},
            },
        ])

        jobs_message = format_jobs_message(config)
        next_message = format_next_message(config)

        self.assertIn("1. Ручной", jobs_message)
        self.assertIn("2. Онгоинг фулл [ongoing]", jobs_message)
        self.assertIn("3. Онгоинг сингл [ongoing]", jobs_message)
        self.assertLess(next_message.index("1. Онгоинг сингл [ongoing]"), next_message.index("2. Онгоинг фулл [ongoing]"))
        self.assertLess(next_message.index("2. Онгоинг фулл [ongoing]"), next_message.index("3. Ручной"))

    def test_next_message_returns_empty_queue_message(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)

        self.assertEqual(format_next_message(config), "Очередь пуста")

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

    def test_log_message_reads_carriage_return_tail(self):
        tmp_dir = self.make_workspace_temp_dir()
        runtime_paths = {
            "log_path": tmp_dir / "cron.log",
        }
        runtime_paths["log_path"].write_bytes(
            b"old\r" + b"x" * (128 * 1024) + b"\rframe=1\rframe=2\r"
        )

        with patch("lib.telegram_bot.ensure_runtime_paths", return_value=runtime_paths):
            message = format_log_message(lines_limit=2)

        self.assertIn("frame=2", message)
        self.assertNotIn("old", message)

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

        self.assertEqual(load_jobs(config), [])
        queued, _ = get_episode_tracking_dicts()
        self.assertEqual(queued, {})

    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_remove_deletes_running_job(self, _mock_allowed, mock_send_message):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        save_jobs(config, [{"title": "Active", "season": 1, "episodes_range": "001"}])
        job = load_jobs(config)[0]
        self.assertTrue(claim_job(job["_queue_id"]))

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handle_update(config, {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/remove 1"},
            })
            handle_update(config, {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Подтвердить удаление"},
            })

        self.assertEqual(load_jobs(config), [])
        self.assertIn("Активная обработка останавливается", mock_send_message.call_args.args[1])

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

        self.assertEqual(load_jobs(config), [])
        self.assertEqual(get_episode_tracking_dicts()[0], {})
        blacklist = get_discovery_blacklist()
        self.assertEqual(len(blacklist), 1)
        self.assertEqual(blacklist[0]["release_id"], 42)

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
        add_to_blacklist({
            "release_id": 42, "title": "A", "title_ru": "А", "season": 1,
            "added_at": "2026-06-27T00:00:00+00:00", "source": "telegram",
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

        self.assertEqual(get_discovery_blacklist(), [])

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
        add_to_blacklist({
            "release_id": 42, "title": "A", "title_ru": "А", "season": 1,
            "added_at": "2026-06-27T00:00:00+00:00", "source": "telegram",
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

        self.assertEqual(load_jobs(config), [])

    @patch("lib.telegram_bot.send_reply")
    @patch("lib.telegram_bot.send_message")
    @patch("lib.telegram_bot.is_allowed_chat", return_value=True)
    def test_retry_flow_adds_completed_job_back_to_queue(self, _mock_allowed, mock_send_message, mock_send_reply):
        tmp_dir = self.make_workspace_temp_dir()
        config = self.make_config(tmp_dir)
        state_path = (tmp_dir / "telegram_state.json").resolve()
        completed_path = tmp_dir / "completed_jobs.json"
        save_completed_jobs(config, [{
            "status": "completed",
            "job": {
                "title": "A", "title_ru": "А", "season": 1, "episodes_range": "001",
                "source": {
                    "type": "magnet", "magnet": "magnet:?xt=urn:btih:testhash",
                    "download_dir": "downloads/A",
                },
                "automation": {"release_id": 1},
            },
        }])
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

        jobs_data = load_jobs(config)
        self.assertEqual(len(jobs_data), 1)
        self.assertEqual(jobs_data[0]["title_ru"], "А")
        self.assertIn("1:001", get_episode_tracking_dicts()[0])

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

        jobs_data = load_jobs(config)
        completed_data = load_completed_jobs(config)
        queued, completed = get_episode_tracking_dicts()
        self.assertEqual(jobs_data, [])
        self.assertEqual(len(completed_data), 1)
        self.assertEqual(queued, {})
        self.assertIn("1:001", completed)

    @patch("lib.telegram_bot.send_formatted_message")
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
            self.assertIn("Страница `1/2`", first_message)
            self.assertIn("*1\\. Тайтл 1*", first_message)
            self.assertEqual(first_markup["inline_keyboard"][0][-1]["text"], "Вперед »")

            with patch("lib.telegram_bot.edit_message_text") as mock_edit, patch(
                "lib.telegram_bot.answer_callback_query"
            ):
                handle_update(config, {
                    "callback_query": {
                        "id": "next", "data": "jobs:page:2",
                        "message": {"chat": {"id": 123}, "message_id": 7},
                    },
                })
                second_message = mock_edit.call_args.args[2]
                second_markup = mock_edit.call_args.kwargs["reply_markup"]
                self.assertIn("Страница `2/2`", second_message)
                self.assertIn("*16\\. Тайтл 16*", second_message)
                self.assertEqual(second_markup["inline_keyboard"][0][0]["text"], "« Назад")

                handle_update(config, {
                    "callback_query": {
                        "id": "previous", "data": "jobs:page:1",
                        "message": {"chat": {"id": 123}, "message_id": 7},
                    },
                })
                self.assertIn("Страница `1/2`", mock_edit.call_args.args[2])

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
        save_completed_jobs(config, [{
            "status": "completed",
            "completed_at": "2026-06-13T10:00:00+00:00",
            "job": job,
            "delivery_summary": {},
        }])

        with patch.dict(os.environ, {"TELEGRAM_STATE_PATH": str(state_path)}):
            handle_update(config, {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/complete 1"},
            })
            handle_update(config, {
                "update_id": 2,
                "message": {"chat": {"id": 123}, "text": "Подтвердить завершение"},
            })

        jobs_data = load_jobs(config)
        completed_data = load_completed_jobs(config)
        queued, completed = get_episode_tracking_dicts()
        self.assertEqual(jobs_data, [])
        self.assertEqual(len(completed_data), 1)
        self.assertEqual(queued, {})
        self.assertIn("1:001", completed)

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
            "source": {
                "type": "magnet",
                "magnet": "magnet:?xt=urn:btih:testhash",
            },
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
                "episodes_audio_recovery": [141, 142],
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
                    "enabled": True,
                    "uploaded": True,
                    "uploaded_files": {
                        "manifest": "animonster/tokyo-ghoul-re/S02/result_manifest.json",
                    },
                },
            },
        }

        with patch.dict(os.environ, {
            "TELEGRAM_STATE_PATH": str(state_path),
            "S3_BUCKET_NAME": "anime-bucket",
        }):
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
        self.assertIn("🎧 Audio recovery: `141,142`", message)
        self.assertIn("• anilibria\\_only: `18`", message)
        self.assertIn("• anilibria\\_with\\_detector: `4`", message)
        self.assertIn("• VK video: `ok`", message)
        self.assertIn("• VK comment: `failed`", message)
        self.assertIn("• S3 upload: `ok`", message)
        self.assertIn("🧲 Torrent: `magnet:?xt=urn:btih:testhash`", message)
        self.assertIn(
            "📄 S3 manifest: `s3://anime-bucket/animonster/tokyo-ghoul-re/S02/result_manifest.json`",
            message,
        )
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
