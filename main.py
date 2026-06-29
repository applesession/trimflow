import sys
from pathlib import Path

# Make src/ importable
SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

from shared.config import load_config, load_jobs
from shared.db import init_db
from core.runner import run_jobs


load_dotenv()
init_db()


def main():
    config = load_config()
    jobs = load_jobs(config)
    run_jobs(config, jobs)


if __name__ == "__main__":
    main()
