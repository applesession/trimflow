from pathlib import Path
import json
import sys

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.autojobs import discover_jobs  # noqa: E402
from lib.config import load_config, load_jobs, load_state, save_jobs, save_state  # noqa: E402


load_dotenv()


def main():
    config = load_config()
    jobs = load_jobs(config)
    state = load_state(config)

    result = discover_jobs(config, jobs, state)
    save_jobs(config, result["jobs"])
    save_state(config, result["state"])

    print("[DISCOVERY SUMMARY]")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
