import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--max-plugins", type=int, default=None)
    parser.add_argument(
        "--tasks",
        default="",
        help="Comma-separated tasks to run. If omitted, runs all registered tasks.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    sys.path.insert(0, str(scripts_dir))
    sys.path.insert(0, str(repo_root))

    if args.all:
        os.environ["RUN_ALL"] = "1"
    if args.max_plugins is not None:
        os.environ["MAX_PLUGINS"] = str(args.max_plugins)

    registered_tasks = ["discussions"]

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    if not tasks:
        tasks = registered_tasks

    exit_code = 0
    for task in tasks:
        if task == "discussions":
            import ensure_plugin_discussions

            exit_code = max(exit_code, int(ensure_plugin_discussions.main()))
            continue
        raise SystemExit(f"Unknown task: {task}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
