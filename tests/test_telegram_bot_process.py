import unittest
from pathlib import Path
from unittest.mock import patch

from lib import reset_test_db  # noqa: F401 - initializes src on sys.path
import telegram_bot


class TelegramBotProcessTests(unittest.TestCase):
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
