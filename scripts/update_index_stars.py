import json
import os
import re
import argparse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = REPO_ROOT / "index.json"
DEFAULT_CHUNK_SIZE = 50
DEFAULT_UPDATES_PATH = REPO_ROOT / "stars_updates.json"


class UpdateStarsError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise UpdateStarsError(msg)


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _fail("GITHUB_TOKEN is required")
    return token


def _graphql(query: str) -> dict[str, Any]:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-stars-updater",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"GitHub GraphQL request failed ({e.code}): {msg}")
    except Exception as e:
        _fail(f"GitHub GraphQL request failed: {e}")

    try:
        parsed = json.loads(payload)
    except Exception as e:
        _fail(f"GitHub GraphQL returned invalid JSON: {e}: {payload[:500]}")

    if not isinstance(parsed, dict):
        _fail("GitHub GraphQL returned non-object JSON")

    if parsed.get("errors"):
        _fail(f"GitHub GraphQL errors: {parsed['errors']}")

    data = parsed.get("data")
    if not isinstance(data, dict):
        _fail("GitHub GraphQL response missing data")

    return cast(dict[str, Any], data)


def _parse_repo_url(url: str) -> tuple[str, str] | None:
    url = url.strip()
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    owner = m.group(1)
    repo = m.group(2)
    return owner, repo


def _chunks(items: list[Any], n: int) -> list[list[Any]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def _load_index() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        _fail("index.json not found; download or generate it first")

    loaded = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        _fail("index.json must be a JSON object")

    plugins = loaded.get("plugins")
    if not isinstance(plugins, dict):
        _fail("index.json.plugins must be an object")

    return cast(dict[str, Any], loaded)


def _save_index(index: dict[str, Any]) -> None:
    plugins = index.get("plugins")
    if isinstance(plugins, dict):
        index["plugins"] = {k: plugins[k] for k in sorted(plugins.keys())}
    INDEX_PATH.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _scan_and_write_updates(chunk_size: int, updates_path: Path) -> int:
    index = _load_index()
    plugins = cast(dict[str, Any], index.get("plugins"))

    items: list[tuple[str, str, str]] = []
    for plugin_name, entry in plugins.items():
        if not isinstance(plugin_name, str) or not isinstance(entry, dict):
            continue
        gh = entry.get("github")
        if not isinstance(gh, str):
            continue
        parsed = _parse_repo_url(gh)
        if not parsed:
            continue
        owner, repo = parsed
        items.append((plugin_name, owner, repo))

    if not items:
        print("No plugin github repos found to update")
        updates_path.write_text("{}\n", encoding="utf-8")
        return 0

    updated_at = datetime.now(timezone.utc).isoformat()
    updates: dict[str, Any] = {}

    for batch in _chunks(items, chunk_size):
        blocks: list[str] = []
        for i, (_, owner, repo) in enumerate(batch):
            blocks.append(
                f'r{i}: repository(owner: "{owner}", name: "{repo}") {{ stargazerCount }}'
            )
        query = "query {\n" + "\n".join(blocks) + "\n}"
        data = _graphql(query)

        for i, (plugin_name, owner, repo) in enumerate(batch):
            key = f"r{i}"
            repo_obj = data.get(key)
            if repo_obj is None:
                continue
            if not isinstance(repo_obj, dict):
                continue
            stars = repo_obj.get("stargazerCount")
            if not isinstance(stars, int):
                continue
            updates[plugin_name] = {
                "stars": stars,
                "stars_updated_at": updated_at,
                "repo": f"{owner}/{repo}",
            }

    updates_path.write_text(json.dumps(updates, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote stars updates for {len(updates)} plugins -> {updates_path.relative_to(REPO_ROOT)}")
    return 0


def _apply_updates(updates_path: Path) -> int:
    index = _load_index()
    plugins = index.get("plugins")
    if not isinstance(plugins, dict):
        _fail("index.json.plugins must be an object")

    if not updates_path.exists():
        _fail(f"Missing updates file: {updates_path.relative_to(REPO_ROOT)}")
    loaded_updates = json.loads(updates_path.read_text(encoding="utf-8"))
    if not isinstance(loaded_updates, dict):
        _fail("updates file must be a JSON object")

    applied = 0
    for plugin_name, upd in loaded_updates.items():
        if not isinstance(plugin_name, str) or not isinstance(upd, dict):
            continue
        entry = plugins.get(plugin_name)
        if not isinstance(entry, dict):
            # plugin may have been removed; skip
            continue
        stars = upd.get("stars")
        stars_updated_at = upd.get("stars_updated_at")
        if isinstance(stars, int):
            entry["stars"] = stars
        if isinstance(stars_updated_at, str):
            entry["stars_updated_at"] = stars_updated_at
        applied += 1

    _save_index(index)
    print(f"Applied stars updates to {applied} plugins")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["scan", "apply"],
        default=os.environ.get("STARS_MODE", "scan"),
    )
    parser.add_argument(
        "--updates-path",
        default=os.environ.get("STARS_UPDATES_PATH", str(DEFAULT_UPDATES_PATH)),
    )
    args = parser.parse_args()

    chunk_size = int(os.environ.get("STARS_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
    updates_path = Path(args.updates_path)
    if not updates_path.is_absolute():
        updates_path = (REPO_ROOT / updates_path).resolve()

    if args.mode == "scan":
        return _scan_and_write_updates(chunk_size, updates_path)
    return _apply_updates(updates_path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateStarsError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
