import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lib import reset_test_db  # noqa: F401 - initializes src on sys.path
from shared.helpers import run
from shared.runtime import trim_log_file


class LogLimitTests(unittest.TestCase):
    def test_run_streams_and_keeps_failed_process_output_tail(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(
            RuntimeError,
            "specific failure",
        ) as raised:
            run([
                sys.executable,
                "-c",
                "import sys; print('visible output'); "
                "print('specific failure', file=sys.stderr); raise SystemExit(9)",
            ])

        self.assertIn("visible output", output.getvalue())
        self.assertIn("specific failure", str(raised.exception))

    def test_trim_log_file_keeps_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cron.log"
            path.write_bytes(b"old line\n" + b"x" * 100 + b"\nlast line\n")

            self.assertTrue(trim_log_file(path, max_bytes=50, keep_bytes=20))
            self.assertEqual(path.read_bytes(), b"last line\n")


if __name__ == "__main__":
    unittest.main()
