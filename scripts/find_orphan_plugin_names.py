import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast

from plugin_resolution import PLUGINS_DIR, REPO_ROOT, is_valid_plugin_dirname

INDEX_JSON_PATH = REPO_ROOT / "index.json"
PLUGIN_MARKER_PREFIX = "<!-- a0-plugins-plugin:"
PLUGIN_MARKER_RE = re.compile(r"<!-- a0-plugins-plugin:([^>]+?) -->")


class FindOrphanPluginNamesError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise FindOrphanPluginNamesError(msg)


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        _fail("GITHUB_TOKEN is required")
    return token


def _graphql_request(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-find-orphan-plugin-names",
            "Content-Type": "application/json",
        },
    )
    payload = ""
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
        _fail(f"GitHub GraphQL errors: {json.dumps(parsed.get('errors'), indent=2, sort_keys=True)[:4000]}")
    data = parsed.get("data")
    if not isinstance(data, dict):
        _fail("GitHub GraphQL response missing data")
    return cast(dict[str, Any], data)


def _load_index() -> dict[str, Any]:
    if not INDEX_JSON_PATH.exists():
        return {"version": 1, "plugins": {}}
    try:
        loaded = json.loads(INDEX_JSON_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"Unable to parse {INDEX_JSON_PATH.name}: {e}")
    if not isinstance(loaded, dict):
        _fail(f"{INDEX_JSON_PATH.name} must contain a JSON object")
    plugins = loaded.get("plugins")
    if not isinstance(plugins, dict):
        loaded["plugins"] = {}
    return cast(dict[str, Any], loaded)


def _get_owner_repo() -> tuple[str, str]:
    repo_full = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo_full or "/" not in repo_full:
        _fail("GITHUB_REPOSITORY is required (owner/repo)")
    owner, repo = repo_full.split("/", 1)
    return owner, repo


def _index_plugin_names_and_discussions() -> tuple[set[str], set[str]]:
    index = _load_index()
    plugins = index.get("plugins")
    if not isinstance(plugins, dict):
        return set(), set()
    names: set[str] = set()
    discussion_urls: set[str] = set()
    for plugin_name, entry in plugins.items():
        if not isinstance(plugin_name, str) or not plugin_name.strip():
            continue
        names.add(plugin_name.strip())
        if isinstance(entry, dict):
            discussion = entry.get("discussion")
            if isinstance(discussion, str) and discussion.strip():
                discussion_urls.add(discussion.strip())
    return names, discussion_urls


def _plugin_exists(plugin_name: str) -> bool:
    return (PLUGINS_DIR / plugin_name / "plugin.yaml").exists()


def _discussion_marker_name(body: str) -> str | None:
    match = PLUGIN_MARKER_RE.search(body)
    if not match:
        return None
    plugin_name = match.group(1).strip()
    return plugin_name or None


def _discussion_marker_names_not_in_index(owner: str, repo: str, index_discussion_urls: set[str]) -> set[str]:
    query = """
    query($owner: String!, $repo: String!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        discussions(first: 100, after: $cursor) {
          nodes {
            url
            body
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
    """
    out: set[str] = set()
    cursor: str | None = None
    while True:
        data = _graphql_request(query, {"owner": owner, "repo": repo, "cursor": cursor})
        repository = data.get("repository")
        if not isinstance(repository, dict):
            _fail(f"Unable to access repository {owner}/{repo}")
        discussions = repository.get("discussions")
        if not isinstance(discussions, dict):
            _fail("Unable to list discussions")
        nodes = discussions.get("nodes")
        if not isinstance(nodes, list):
            _fail("Discussion listing missing nodes")
        for node in nodes:
            if not isinstance(node, dict):
                continue
            url = node.get("url")
            body = node.get("body")
            if isinstance(url, str) and url.strip() and url.strip() in index_discussion_urls:
                continue
            if not isinstance(body, str) or PLUGIN_MARKER_PREFIX not in body:
                continue
            plugin_name = _discussion_marker_name(body)
            if plugin_name:
                out.add(plugin_name)
        page_info = discussions.get("pageInfo")
        if not isinstance(page_info, dict) or page_info.get("hasNextPage") is not True:
            break
        end_cursor = page_info.get("endCursor")
        cursor = end_cursor if isinstance(end_cursor, str) and end_cursor else None
        if not cursor:
            break
    return out


def main() -> int:
    owner, repo = _get_owner_repo()
    index_plugin_names, index_discussion_urls = _index_plugin_names_and_discussions()
    discussion_plugin_names = _discussion_marker_names_not_in_index(owner, repo, index_discussion_urls)
    candidates = sorted(index_plugin_names | discussion_plugin_names)
    orphan_names = [name for name in candidates if is_valid_plugin_dirname(name) and not _plugin_exists(name)]
    print(",".join(orphan_names))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FindOrphanPluginNamesError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
