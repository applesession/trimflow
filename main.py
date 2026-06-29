from dotenv import load_dotenv

from lib.config import load_config, load_jobs
from lib.db import init_db
from lib.runner import run_jobs


load_dotenv()
init_db()


def main():
    config = load_config()
    jobs = load_jobs(config)
    run_jobs(config, jobs)


if __name__ == "__main__":
    main()
