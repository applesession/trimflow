import tempfile
import unittest
from pathlib import Path

from lib import reset_test_db  # noqa: F401 - initializes src on sys.path
from shared.runtime import trim_log_file


class LogLimitTests(unittest.TestCase):
    def test_trim_log_file_keeps_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cron.log"
            path.write_bytes(b"old line\n" + b"x" * 100 + b"\nlast line\n")

            self.assertTrue(trim_log_file(path, max_bytes=50, keep_bytes=20))
            self.assertEqual(path.read_bytes(), b"last line\n")


if __name__ == "__main__":
    unittest.main()
