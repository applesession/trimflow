import unittest
from pathlib import Path
from unittest.mock import patch

from lib import reset_test_db  # noqa: F401 - initializes src on sys.path
import telegram_bot


class TelegramBotProcessTests(unittest.TestCase):
    @patch("telegram_bot.release_lock")
    @patch("telegram_bot.update_telegram_state_progress", return_value={"last_update_id": 1})
    @patch("telegram_bot.load_telegram_state", return_value={"last_update_id": 0})
    @patch("telegram_bot.load_config", return_value={})
    @patch("telegram_bot.handle_update", return_value=False)
    @patch("telegram_bot.fetch_updates")
    @patch("telegram_bot.log_line")
    @patch("telegram_bot.acquire_lock", return_value={"acquired": True})
    @patch("telegram_bot.ensure_runtime_paths")
    def test_empty_message_does_not_stop_polling(
        self,
        mock_paths,
        _mock_acquire,
        mock_log,
        mock_fetch,
        mock_handle,
        _mock_config,
        _mock_state,
        mock_progress,
        mock_release,
    ):
        mock_paths.return_value = {
            "runtime_dir": Path(".runtime"),
            "telegram_log_path": Path("logs/telegram_bot.log"),
        }
        mock_fetch.side_effect = [
            [{"update_id": 1, "message": {}}],
            KeyboardInterrupt,
        ]

        with self.assertRaises(KeyboardInterrupt):
            telegram_bot.main()

        mock_handle.assert_called_once()
        mock_progress.assert_called_once_with(last_update_id=1, last_handled_at=None)
        self.assertTrue(any("command=''" in str(call.args[1]) for call in mock_log.call_args_list))
        mock_release.assert_called_once()

    @patch("telegram_bot.fetch_updates")
    @patch("telegram_bot.log_line")
    @patch("telegram_bot.acquire_lock")
    @patch("telegram_bot.ensure_runtime_paths")
    def test_second_bot_exits_when_lock_is_busy(
        self,
        mock_paths,
        mock_acquire,
        _mock_log,
        mock_fetch,
    ):
        mock_paths.return_value = {
            "runtime_dir": Path(".runtime"),
            "telegram_log_path": Path("logs/telegram_bot.log"),
        }
        mock_acquire.return_value = {"acquired": False}

        self.assertEqual(telegram_bot.main(), 0)
        mock_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
