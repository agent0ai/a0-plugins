#!/usr/bin/env python3
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
import urllib.parse
import urllib.request
from typing import Any, cast


ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}
DEFAULT_WORKFLOW_NAMES = [
    "Generate Plugin State",
    "Generate Missing Thumbnails",
    "Update Index Stars & Repo Stats (auto)",
]
DEFAULT_STALE_QUEUED_SECONDS = 24 * 60 * 60


class WaitForIndexSerializationError(Exception):
    pass


def _fail(message: str) -> None:
    raise WaitForIndexSerializationError(message)


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        _fail(f"{name} is required")
    return value


def _workflow_names() -> set[str]:
    raw = os.environ.get("INDEX_SERIALIZATION_WORKFLOW_NAMES", "").strip()
    names = [item.strip() for item in raw.split(",") if item.strip()] if raw else DEFAULT_WORKFLOW_NAMES
    if not names:
        _fail("INDEX_SERIALIZATION_WORKFLOW_NAMES must not be empty")
    return set(names)


def _load_runs(url: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        return []
    return [cast(dict[str, Any], run) for run in runs if isinstance(run, dict)]


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_job_count(
    repository: str, run_id: int, headers: dict[str, str]
) -> int | None:
    url = (
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs"
        "?filter=all&per_page=1"
    )
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None
    count = payload.get("total_count")
    return count if isinstance(count, int) and count >= 0 else None


def _is_stale_jobless_queued_run(
    run: dict[str, Any],
    *,
    repository: str,
    headers: dict[str, str],
    now: datetime,
    stale_after_seconds: int,
) -> bool:
    if run.get("status") != "queued":
        return False
    created_at = _parse_timestamp(run.get("created_at"))
    updated_at = _parse_timestamp(run.get("updated_at"))
    if created_at is None or updated_at is None or created_at != updated_at:
        return False
    if (now - created_at).total_seconds() < stale_after_seconds:
        return False
    run_id = run.get("id")
    if not isinstance(run_id, int):
        return False
    return _load_job_count(repository, run_id, headers) == 0


def main() -> int:
    token = _env("GITHUB_TOKEN")
    repository = _env("GITHUB_REPOSITORY")
    current_run_id_raw = _env("GITHUB_RUN_ID")
    ref_name = os.environ.get("GITHUB_REF_NAME", "").strip()
    workflow_names = _workflow_names()
    deadline = time.time() + int(os.environ.get("INDEX_SERIALIZATION_TIMEOUT_SECONDS", "1800"))
    poll_interval = int(os.environ.get("INDEX_SERIALIZATION_POLL_SECONDS", "15"))
    stale_queued_seconds = int(
        os.environ.get(
            "INDEX_SERIALIZATION_STALE_QUEUED_SECONDS",
            str(DEFAULT_STALE_QUEUED_SECONDS),
        )
    )

    current_run_id = 0
    try:
        current_run_id = int(current_run_id_raw)
    except ValueError as e:
        _fail(f"Invalid GITHUB_RUN_ID: {e}")

    query = {"per_page": "100"}
    if ref_name:
        query["branch"] = ref_name
    url = f"https://api.github.com/repos/{repository}/actions/runs?{urllib.parse.urlencode(query)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "a0-plugins-actions",
    }

    while True:
        blocking_runs: list[str] = []
        for run in _load_runs(url, headers):
            run_id = run.get("id")
            run_name = run.get("name")
            run_status = run.get("status")
            head_branch = run.get("head_branch")

            if not isinstance(run_id, int):
                continue
            if run_id == current_run_id:
                continue
            if run_id > current_run_id:
                continue
            if ref_name and head_branch != ref_name:
                continue
            if not isinstance(run_name, str) or run_name not in workflow_names:
                continue
            if not isinstance(run_status, str) or run_status not in ACTIVE_STATUSES:
                continue

            if _is_stale_jobless_queued_run(
                run,
                repository=repository,
                headers=headers,
                now=datetime.now(timezone.utc),
                stale_after_seconds=stale_queued_seconds,
            ):
                print(
                    "Ignoring stale queued workflow run with no jobs: "
                    f"{run_id}:{run_name}:{run_status}"
                )
                continue

            blocking_runs.append(f"{run_id}:{run_name}:{run_status}")

        if not blocking_runs:
            print("No older index-mutating workflow runs are active.")
            return 0

        if time.time() >= deadline:
            print(
                "Timed out waiting for older index-mutating workflow runs to finish: " + ", ".join(blocking_runs),
                file=sys.stderr,
            )
            return 1

        print("Waiting for older index-mutating workflow runs: " + ", ".join(blocking_runs))
        time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WaitForIndexSerializationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
