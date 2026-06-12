import io
import json
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from lib.runner import build_job_identity, run_jobs
from lib.runtime import (
    acquire_lock,
    append_runtime_error,
    build_default_runtime_errors,
    build_default_runtime_status,
    is_lock_stale,
    load_runtime_errors,
    load_runtime_status,
    log_line,
    release_lock,
    update_runtime_status,
)
from scripts import cron_run


class CronRuntimeTests(unittest.TestCase):
    def make_workspace_temp_dir(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        temp_dir = root / f"cron_{uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def test_lock_acquisition_succeeds_on_first_run(self):
        tmp_dir = self.make_workspace_temp_dir()
        lock_path = tmp_dir / "cron.lock"

        result = acquire_lock(lock_path, "python scripts/cron_run.py")

        self.assertTrue(result["acquired"])
        self.assertTrue(lock_path.exists())
        release_lock(lock_path)

    def test_second_lock_attempt_skips_when_active(self):
        tmp_dir = self.make_workspace_temp_dir()
        lock_path = tmp_dir / "cron.lock"

        first = acquire_lock(lock_path, "python scripts/cron_run.py")
        second = acquire_lock(lock_path, "python scripts/cron_run.py")

        self.assertTrue(first["acquired"])
        self.assertFalse(second["acquired"])
        self.assertTrue(second["already_running"])
        release_lock(lock_path)

    def test_stale_lock_is_detected(self):
        tmp_dir = self.make_workspace_temp_dir()
        lock_path = tmp_dir / "cron.lock"
        with open(lock_path, "w", encoding="utf-8") as file:
            json.dump({"pid": 999999, "started_at": "2026-01-01T00:00:00+00:00"}, file)

        stale, payload = is_lock_stale(lock_path)

        self.assertTrue(stale)
        self.assertEqual(payload["pid"], 999999)

    def test_log_line_writes_to_stdout_and_file(self):
        tmp_dir = self.make_workspace_temp_dir()
        log_path = tmp_dir / "cron.log"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            line = log_line(log_path, "hello world")

        self.assertIn("hello world", line)
        self.assertIn("hello world", stdout.getvalue())
        self.assertIn("hello world", log_path.read_text(encoding="utf-8"))

    def test_build_default_runtime_status_creates_expected_shape(self):
        status = build_default_runtime_status()

        self.assertEqual(status["run_status"], "idle")
        self.assertEqual(status["queue_progress"]["jobs_processed"], 0)
        self.assertIsNone(status["current_job"])
        self.assertIsNone(status["last_run"])

    def test_build_default_runtime_errors_creates_expected_shape(self):
        errors = build_default_runtime_errors()

        self.assertEqual(errors["errors"], [])
        self.assertIsNone(errors["updated_at"])

    def test_append_runtime_error_trims_to_last_20(self):
        tmp_dir = self.make_workspace_temp_dir()
        status_path = tmp_dir / "runtime_status.json"
        errors_path = tmp_dir / "runtime_errors.json"

        update_runtime_status(
            status_path,
            run_status="running",
            current_stage="processing",
            current_job={"title": "A", "stage": "render_segments", "current_episode": 2, "total_episodes": 12},
        )
        for index in range(25):
            append_runtime_error(
                context=f"ctx_{index}",
                message=f"boom_{index}",
                error_type="RuntimeError",
                status_path=status_path,
                errors_path=errors_path,
            )

        payload = load_runtime_errors(errors_path)

        self.assertEqual(len(payload["errors"]), 20)
        self.assertEqual(payload["errors"][0]["context"], "ctx_24")
        self.assertEqual(payload["errors"][-1]["context"], "ctx_5")

    def test_append_runtime_error_uses_runtime_status_context(self):
        tmp_dir = self.make_workspace_temp_dir()
        status_path = tmp_dir / "runtime_status.json"
        errors_path = tmp_dir / "runtime_errors.json"

        update_runtime_status(
            status_path,
            run_status="running",
            current_stage="render_segments",
            current_job={
                "title": "A",
                "title_ru": "А",
                "season": 1,
                "episodes_range": "001-010",
                "stage": "render_segments",
                "current_episode": 4,
                "total_episodes": 10,
            },
        )
        append_runtime_error(
            context="job_failed",
            message="RuntimeError('boom')",
            error_type="RuntimeError",
            status_path=status_path,
            errors_path=errors_path,
        )

        payload = load_runtime_errors(errors_path)
        entry = payload["errors"][0]

        self.assertEqual(entry["title_ru"], "А")
        self.assertEqual(entry["current_episode"], 4)
        self.assertEqual(entry["total_episodes"], 10)
        self.assertEqual(entry["stage"], "render_segments")

    def test_update_runtime_status_merges_nested_values(self):
        tmp_dir = self.make_workspace_temp_dir()
        status_path = tmp_dir / "runtime_status.json"

        update_runtime_status(
            status_path,
            run_status="running",
            current_stage="processing",
            queue_progress={"total_jobs": 5},
            current_job={"title": "A", "stage": "job_start"},
        )
        update_runtime_status(
            status_path,
            current_job={"current_episode": 3},
            queue_progress={"jobs_processed": 1},
        )
        status = load_runtime_status(status_path)

        self.assertEqual(status["run_status"], "running")
        self.assertEqual(status["queue_progress"]["total_jobs"], 5)
        self.assertEqual(status["queue_progress"]["jobs_processed"], 1)
        self.assertEqual(status["current_job"]["title"], "A")
        self.assertEqual(status["current_job"]["current_episode"], 3)

    @patch("lib.runner.process_job")
    @patch("lib.runner.save_completed_jobs")
    @patch("lib.runner.load_completed_jobs")
    @patch("lib.runner.save_jobs")
    @patch("lib.runner.validate_required_files")
    @patch("lib.runner.validate_required_tools")
    @patch("lib.runner.validate_required_env")
    def test_run_jobs_returns_empty_summary_when_no_jobs(
        self,
        mock_validate_env,
        mock_validate_tools,
        mock_validate_files,
        mock_save_jobs,
        mock_load_completed_jobs,
        mock_save_completed_jobs,
        mock_process_job,
    ):
        summary = run_jobs({"defaults": {}}, [])

        self.assertEqual(summary["jobs_found"], 0)
        self.assertEqual(summary["jobs_processed"], 0)
        self.assertEqual(summary["jobs_failed"], 0)
        mock_validate_env.assert_not_called()
        mock_validate_tools.assert_not_called()
        mock_validate_files.assert_not_called()
        mock_process_job.assert_not_called()

    @patch("lib.runner.process_job")
    @patch("lib.runner.save_completed_jobs")
    @patch("lib.runner.load_completed_jobs")
    @patch("lib.runner.save_jobs")
    @patch("lib.runner.validate_required_files")
    @patch("lib.runner.validate_required_tools")
    @patch("lib.runner.validate_required_env")
    def test_run_jobs_removes_successful_job_and_archives_it(
        self,
        mock_validate_env,
        mock_validate_tools,
        mock_validate_files,
        mock_save_jobs,
        mock_load_completed_jobs,
        mock_save_completed_jobs,
        mock_process_job,
    ):
        jobs = [{"title": "A", "season": 1, "episodes_range": "001", "source": {"type": "magnet", "magnet": "m1"}}]
        mock_load_completed_jobs.return_value = []
        mock_process_job.return_value = {
            "output_video": "/tmp/out.mkv",
            "output_display_name": "A",
            "delivery_summary": {
                "vk": {"enabled": True, "video_uploaded": True, "post_created": False, "comment_created": False},
                "s3": {"enabled": False, "uploaded": False},
            },
        }

        summary = run_jobs({"defaults": {}}, jobs)

        self.assertEqual(summary["jobs_processed"], 1)
        mock_save_jobs.assert_called_once_with({"defaults": {}}, [])
        saved_archive = mock_save_completed_jobs.call_args.args[1]
        self.assertEqual(len(saved_archive), 1)
        self.assertEqual(saved_archive[0]["job"]["title"], "A")
        self.assertTrue(saved_archive[0]["partial_vk"])

    @patch("lib.runner.process_job")
    @patch("lib.runner.save_completed_jobs")
    @patch("lib.runner.load_completed_jobs")
    @patch("lib.runner.save_jobs")
    @patch("lib.runner.validate_required_files")
    @patch("lib.runner.validate_required_tools")
    @patch("lib.runner.validate_required_env")
    def test_run_jobs_keeps_failed_job_in_active_queue(
        self,
        mock_validate_env,
        mock_validate_tools,
        mock_validate_files,
        mock_save_jobs,
        mock_load_completed_jobs,
        mock_save_completed_jobs,
        mock_process_job,
    ):
        jobs = [{"title": "A", "season": 1, "episodes_range": "001", "source": {"type": "magnet", "magnet": "m1"}}]
        mock_load_completed_jobs.return_value = []
        mock_process_job.side_effect = RuntimeError("boom")

        summary = run_jobs({"defaults": {}}, jobs)

        self.assertEqual(summary["jobs_failed"], 1)
        mock_save_jobs.assert_not_called()
        mock_save_completed_jobs.assert_not_called()

    @patch("lib.runner.process_job")
    @patch("lib.runner.save_completed_jobs")
    @patch("lib.runner.load_completed_jobs")
    @patch("lib.runner.save_jobs")
    @patch("lib.runner.validate_required_files")
    @patch("lib.runner.validate_required_tools")
    @patch("lib.runner.validate_required_env")
    def test_run_jobs_updates_runtime_status_for_success(
        self,
        mock_validate_env,
        mock_validate_tools,
        mock_validate_files,
        mock_save_jobs,
        mock_load_completed_jobs,
        mock_save_completed_jobs,
        mock_process_job,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        status_path = tmp_dir / "runtime_status.json"
        jobs = [{"title": "A", "title_ru": "А", "season": 1, "episodes_range": "001", "source": {"type": "magnet", "magnet": "m1"}}]
        mock_load_completed_jobs.return_value = []
        mock_process_job.return_value = {
            "output_video": "/tmp/out.mkv",
            "output_display_name": "A",
            "delivery_summary": {
                "vk": {"enabled": True, "video_uploaded": True},
                "s3": {"enabled": False, "uploaded": False},
            },
        }

        summary = run_jobs({"defaults": {}}, jobs, runtime_status_path=status_path)
        status = load_runtime_status(status_path)

        self.assertEqual(summary["jobs_processed"], 1)
        self.assertEqual(status["current_stage"], "job_completed")
        self.assertEqual(status["last_run"]["status"], "completed")
        self.assertEqual(status["last_run"]["title_ru"], "А")

    @patch("lib.runner.process_job")
    @patch("lib.runner.save_completed_jobs")
    @patch("lib.runner.load_completed_jobs")
    @patch("lib.runner.save_jobs")
    @patch("lib.runner.validate_required_files")
    @patch("lib.runner.validate_required_tools")
    @patch("lib.runner.validate_required_env")
    def test_run_jobs_writes_runtime_error_for_failure(
        self,
        mock_validate_env,
        mock_validate_tools,
        mock_validate_files,
        mock_save_jobs,
        mock_load_completed_jobs,
        mock_save_completed_jobs,
        mock_process_job,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        status_path = tmp_dir / "runtime_status.json"
        errors_path = tmp_dir / "runtime_errors.json"
        jobs = [{"title": "A", "title_ru": "А", "season": 1, "episodes_range": "001", "source": {"type": "magnet", "magnet": "m1"}}]
        mock_load_completed_jobs.return_value = []
        mock_process_job.side_effect = RuntimeError("boom")

        run_jobs(
            {"defaults": {}},
            jobs,
            runtime_status_path=status_path,
            runtime_errors_path=errors_path,
        )
        payload = load_runtime_errors(errors_path)

        self.assertEqual(len(payload["errors"]), 1)
        self.assertEqual(payload["errors"][0]["context"], "job_failed")
        self.assertEqual(payload["errors"][0]["title_ru"], "А")

    def test_build_job_identity_uses_source_signature_and_range(self):
        first = build_job_identity({
            "title": "A",
            "season": 1,
            "episodes_range": "001-003",
            "source": {"type": "magnet", "magnet": "m1"},
        })
        second = build_job_identity({
            "title": "A",
            "season": 1,
            "episodes_range": "004-006",
            "source": {"type": "magnet", "magnet": "m1"},
        })

        self.assertNotEqual(first, second)

    @patch("scripts.cron_run.release_lock")
    @patch("scripts.cron_run.log_line")
    @patch("scripts.cron_run.acquire_lock")
    @patch("scripts.cron_run.ensure_runtime_paths")
    def test_cron_run_skips_when_lock_is_active(
        self,
        mock_paths,
        mock_acquire_lock,
        mock_log_line,
        mock_release_lock,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        mock_paths.return_value = {
            "runtime_dir": tmp_dir,
            "logs_dir": tmp_dir,
            "lock_path": tmp_dir / "cron.lock",
            "log_path": tmp_dir / "cron.log",
            "telegram_log_path": tmp_dir / "telegram_bot.log",
            "status_path": tmp_dir / "runtime_status.json",
            "errors_path": tmp_dir / "runtime_errors.json",
        }
        mock_acquire_lock.return_value = {
            "acquired": False,
            "already_running": True,
            "lock_payload": {"pid": 123, "started_at": "2026-06-03T00:00:00+00:00"},
        }

        result = cron_run.main()

        self.assertEqual(result, 0)
        mock_log_line.assert_called()
        mock_release_lock.assert_not_called()

    @patch("scripts.cron_run.release_lock")
    @patch("scripts.cron_run.send_message_to_allowed_chats")
    @patch("scripts.cron_run.run_jobs")
    @patch("scripts.cron_run.save_state")
    @patch("scripts.cron_run.save_jobs")
    @patch("scripts.cron_run.discover_jobs")
    @patch("scripts.cron_run.load_state")
    @patch("scripts.cron_run.load_jobs")
    @patch("scripts.cron_run.load_config")
    @patch("scripts.cron_run.log_line")
    @patch("scripts.cron_run.acquire_lock")
    @patch("scripts.cron_run.ensure_runtime_paths")
    def test_cron_run_calls_discovery_then_processing(
        self,
        mock_paths,
        mock_acquire_lock,
        mock_log_line,
        mock_load_config,
        mock_load_jobs,
        mock_load_state,
        mock_discover_jobs,
        mock_save_jobs,
        mock_save_state,
        mock_run_jobs,
        mock_send_message,
        mock_release_lock,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        mock_paths.return_value = {
            "runtime_dir": tmp_dir,
            "logs_dir": tmp_dir,
            "lock_path": tmp_dir / "cron.lock",
            "log_path": tmp_dir / "cron.log",
            "telegram_log_path": tmp_dir / "telegram_bot.log",
            "status_path": tmp_dir / "runtime_status.json",
            "errors_path": tmp_dir / "runtime_errors.json",
        }
        mock_acquire_lock.return_value = {
            "acquired": True,
            "already_running": False,
            "lock_payload": {"pid": 1},
        }
        mock_load_config.return_value = {"defaults": {}}
        mock_load_jobs.return_value = []
        mock_load_state.return_value = {}
        mock_discover_jobs.return_value = {
            "jobs": [{"title": "A"}],
            "state": {"schema_version": 1},
            "summary": {"created_jobs": 1},
        }
        mock_run_jobs.return_value = {
            "jobs_found": 1,
            "jobs_processed": 1,
            "jobs_failed": 0,
            "jobs_skipped": 0,
            "failed_titles": [],
        }

        result = cron_run.main()

        self.assertEqual(result, 0)
        mock_discover_jobs.assert_called_once()
        mock_save_jobs.assert_called_once()
        mock_save_state.assert_called_once()
        mock_run_jobs.assert_called_once()
        self.assertEqual(mock_run_jobs.call_args.args[1], [{"title": "A"}])
        mock_send_message.assert_called_once()
        mock_release_lock.assert_called_once()

    @patch("scripts.cron_run.release_lock")
    @patch("scripts.cron_run.send_message_to_allowed_chats")
    @patch("scripts.cron_run.run_jobs")
    @patch("scripts.cron_run.save_state")
    @patch("scripts.cron_run.save_jobs")
    @patch("scripts.cron_run.discover_jobs")
    @patch("scripts.cron_run.load_state")
    @patch("scripts.cron_run.load_jobs")
    @patch("scripts.cron_run.load_config")
    @patch("scripts.cron_run.log_line")
    @patch("scripts.cron_run.acquire_lock")
    @patch("scripts.cron_run.ensure_runtime_paths")
    def test_cron_run_survives_telegram_notification_failure(
        self,
        mock_paths,
        mock_acquire_lock,
        mock_log_line,
        mock_load_config,
        mock_load_jobs,
        mock_load_state,
        mock_discover_jobs,
        mock_save_jobs,
        mock_save_state,
        mock_run_jobs,
        mock_send_message,
        mock_release_lock,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        mock_paths.return_value = {
            "runtime_dir": tmp_dir,
            "logs_dir": tmp_dir,
            "lock_path": tmp_dir / "cron.lock",
            "log_path": tmp_dir / "cron.log",
            "telegram_log_path": tmp_dir / "telegram_bot.log",
            "status_path": tmp_dir / "runtime_status.json",
            "errors_path": tmp_dir / "runtime_errors.json",
        }
        mock_acquire_lock.return_value = {
            "acquired": True,
            "already_running": False,
            "lock_payload": {"pid": 1},
        }
        mock_load_config.return_value = {"defaults": {}}
        mock_load_jobs.return_value = []
        mock_load_state.return_value = {}
        mock_discover_jobs.return_value = {
            "jobs": [{"title": "A"}],
            "state": {"schema_version": 1},
            "summary": {"created_jobs": 1},
        }
        mock_run_jobs.return_value = {
            "jobs_found": 1,
            "jobs_processed": 1,
            "jobs_failed": 0,
            "jobs_skipped": 0,
            "failed_titles": [],
        }
        mock_send_message.side_effect = RuntimeError("telegram down")

        result = cron_run.main()

        self.assertEqual(result, 0)
        self.assertTrue(
            any(
                "telegram_notify_failed" in str(call.args[1])
                for call in mock_log_line.call_args_list
                if len(call.args) > 1
            ),
        )
        mock_release_lock.assert_called_once()

    @patch("scripts.cron_run.release_lock")
    @patch("scripts.cron_run.send_message_to_allowed_chats")
    @patch("scripts.cron_run.run_jobs")
    @patch("scripts.cron_run.save_state")
    @patch("scripts.cron_run.save_jobs")
    @patch("scripts.cron_run.discover_jobs")
    @patch("scripts.cron_run.load_state")
    @patch("scripts.cron_run.load_jobs")
    @patch("scripts.cron_run.load_config")
    @patch("scripts.cron_run.log_line")
    @patch("scripts.cron_run.acquire_lock")
    @patch("scripts.cron_run.ensure_runtime_paths")
    def test_cron_run_continues_processing_when_discovery_fails(
        self,
        mock_paths,
        mock_acquire_lock,
        mock_log_line,
        mock_load_config,
        mock_load_jobs,
        mock_load_state,
        mock_discover_jobs,
        mock_save_jobs,
        mock_save_state,
        mock_run_jobs,
        mock_send_message,
        mock_release_lock,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        mock_paths.return_value = {
            "runtime_dir": tmp_dir,
            "logs_dir": tmp_dir,
            "lock_path": tmp_dir / "cron.lock",
            "log_path": tmp_dir / "cron.log",
            "telegram_log_path": tmp_dir / "telegram_bot.log",
            "status_path": tmp_dir / "runtime_status.json",
            "errors_path": tmp_dir / "runtime_errors.json",
        }
        mock_acquire_lock.return_value = {
            "acquired": True,
            "already_running": False,
            "lock_payload": {"pid": 1},
        }
        mock_load_config.return_value = {"defaults": {}}
        mock_load_jobs.return_value = [{"title": "Queued Job"}]
        mock_load_state.return_value = {}
        mock_discover_jobs.side_effect = RuntimeError("discovery down")
        mock_run_jobs.return_value = {
            "jobs_found": 1,
            "jobs_processed": 1,
            "jobs_failed": 0,
            "jobs_skipped": 0,
            "failed_titles": [],
        }

        result = cron_run.main()

        self.assertEqual(result, 0)
        mock_save_jobs.assert_not_called()
        mock_save_state.assert_not_called()
        mock_run_jobs.assert_called_once()
        self.assertEqual(mock_run_jobs.call_args.args[1], [{"title": "Queued Job"}])
        self.assertTrue(
            any(
                "warning discovery_failed" in str(call.args[1])
                for call in mock_log_line.call_args_list
                if len(call.args) > 1
            ),
        )
        self.assertGreaterEqual(mock_send_message.call_count, 1)
        mock_release_lock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
