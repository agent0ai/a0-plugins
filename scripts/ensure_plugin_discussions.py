import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"
DISCUSSIONS_CATEGORY_NAME = "Plugins"
DISCUSSION_MARKER = "<!-- a0-plugins-discussion -->"
DEFAULT_MAX_PLUGINS = 100


class EnsureDiscussionsError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise EnsureDiscussionsError(msg)


def _run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=REPO_ROOT)
    return out.decode("utf-8", errors="replace")


def _git_diff_names(before: str, after: str) -> list[str]:
    raw = _run(["git", "diff", "--name-only", f"{before}..{after}"])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _git_all_plugin_paths(commit: str) -> list[str]:
    raw = _run(["git", "ls-tree", "-r", "--name-only", commit, "--", "plugins"])  # noqa: E501
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _is_zero_sha(sha: str | None) -> bool:
    if not sha:
        return True
    s = sha.strip()
    return bool(s) and set(s) == {"0"}


def _detected_plugin_names(before: str | None, after: str, run_all: bool) -> list[str]:
    if run_all:
        paths = _git_all_plugin_paths(after)
    else:
        if _is_zero_sha(before):
            paths = _git_all_plugin_paths(after)
        else:
            assert before is not None
            paths = _git_diff_names(cast(str, before), after)

    plugin_names: set[str] = set()
    for p in paths:
        parts = Path(p).parts
        if len(parts) >= 2 and parts[0] == "plugins":
            plugin_names.add(parts[1])

    out = [n for n in sorted(plugin_names) if n and not n.startswith("_")]
    return out


def _read_plugin_yaml(plugin_name: str) -> dict[str, Any]:
    plugin_yaml = PLUGINS_DIR / plugin_name / "plugin.yaml"
    if not plugin_yaml.exists():
        _fail(f"Missing plugin.yaml for plugin '{plugin_name}': {plugin_yaml.relative_to(REPO_ROOT)}")

    loaded: Any = None
    try:
        loaded = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"Invalid YAML for plugin '{plugin_name}': {e}")

    if not isinstance(loaded, dict):
        _fail(f"plugin.yaml for '{plugin_name}' must be a YAML mapping/object")

    return cast(dict[str, Any], loaded)


def _graphql_request(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _fail("GITHUB_TOKEN is required")

    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-discussions-bot",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    payload = ""
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"GitHub GraphQL request failed ({e.code}): {msg}")
    except Exception as e:
        _fail(f"GitHub GraphQL request failed: {e}")

    parsed: Any = None
    try:
        parsed = json.loads(payload)
    except Exception as e:
        _fail(f"GitHub GraphQL returned invalid JSON: {e}: {payload[:500]}")

    if not isinstance(parsed, dict):
        _fail("GitHub GraphQL returned non-object JSON")

    parsed_dict = cast(dict[str, Any], parsed)
    if parsed_dict.get("errors"):
        _fail(f"GitHub GraphQL errors: {parsed_dict['errors']}")

    data = parsed_dict.get("data")
    if not isinstance(data, dict):
        _fail("GitHub GraphQL response missing data")

    return cast(dict[str, Any], data)


def _get_repo_and_category(owner: str, repo: str) -> tuple[str, str]:
    query = """
    query($owner: String!, $repo: String!) {
      repository(owner: $owner, name: $repo) {
        id
        discussionCategories(first: 100) {
          nodes {
            id
            name
          }
        }
      }
    }
    """

    data = _graphql_request(query, {"owner": owner, "repo": repo})
    repository = data.get("repository")
    if not isinstance(repository, dict):
        _fail(f"Unable to access repository {owner}/{repo}. Is GITHUB_TOKEN permitted?")

    repo_id = repository.get("id")
    if not isinstance(repo_id, str) or not repo_id:
        _fail("Unable to determine repository id")

    cats = repository.get("discussionCategories", {}).get("nodes")
    if not isinstance(cats, list):
        _fail("Unable to list discussion categories")

    category_id: str | None = None
    for c in cats:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        cid = c.get("id")
        if isinstance(name, str) and isinstance(cid, str) and name.strip().lower() == DISCUSSIONS_CATEGORY_NAME.lower():
            category_id = cid
            break

    if not category_id:
        _fail(
            f"Discussion category '{DISCUSSIONS_CATEGORY_NAME}' not found in {owner}/{repo}. "
            "Create it in GitHub Discussions settings."
        )

    return repo_id, category_id


def _discussion_title(plugin_name: str) -> str:
    return f"Plugin: {plugin_name}"


def _discussion_body(plugin_name: str, meta: dict[str, Any], owner: str, repo: str) -> str:
    title = meta.get("title") if isinstance(meta.get("title"), str) else ""
    description = meta.get("description") if isinstance(meta.get("description"), str) else ""
    gh = meta.get("github") if isinstance(meta.get("github"), str) else ""

    lines: list[str] = []
    lines.append(DISCUSSION_MARKER)
    lines.append(f"## {title}" if title else "## Plugin")
    if description:
        lines.append("")
        lines.append(description)
    lines.append("")
    lines.append("### Links")
    lines.append("")
    lines.append(f"- Repository index entry: https://github.com/{owner}/{repo}/tree/main/plugins/{plugin_name}")
    if gh:
        lines.append(f"- Plugin repository: {gh}")
    lines.append("")
    lines.append("### Notes")
    lines.append("")
    lines.append("This discussion is auto-created by a workflow when a plugin is added to the index.")
    return "\n".join(lines).strip() + "\n"


def _find_existing_discussion(owner: str, repo: str, expected_title: str) -> dict[str, Any] | None:
    query = """
    query($q: String!) {
      search(query: $q, type: DISCUSSION, first: 5) {
        nodes {
          __typename
          ... on Discussion {
            id
            title
            url
          }
        }
      }
    }
    """

    q = f'repo:{owner}/{repo} in:title "{expected_title}"'
    data = _graphql_request(query, {"q": q})

    search = data.get("search")
    if not isinstance(search, dict):
        return None

    nodes = search.get("nodes")
    if not isinstance(nodes, list):
        return None

    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("__typename") != "Discussion":
            continue
        if n.get("title") == expected_title:
            return n

    return None


def _create_discussion(repo_id: str, category_id: str, title: str, body: str) -> dict[str, Any]:
    query = """
    mutation($repoId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
      createDiscussion(input: {repositoryId: $repoId, categoryId: $categoryId, title: $title, body: $body}) {
        discussion {
          id
          url
          title
        }
      }
    }
    """

    data = _graphql_request(
        query,
        {
            "repoId": repo_id,
            "categoryId": category_id,
            "title": title,
            "body": body,
        },
    )

    cd = data.get("createDiscussion", {})
    if not isinstance(cd, dict):
        _fail("Unexpected GraphQL response: missing createDiscussion")

    disc = cd.get("discussion")
    if not isinstance(disc, dict):
        _fail("Unexpected GraphQL response: missing discussion")

    return disc


def main() -> int:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    repo_full = os.environ.get("GITHUB_REPOSITORY")
    before = os.environ.get("BEFORE_SHA")
    after = os.environ.get("AFTER_SHA")
    run_all = os.environ.get("RUN_ALL", "").strip() == "1"

    if not owner or not repo_full or "/" not in repo_full:
        _fail("GITHUB_REPOSITORY_OWNER and GITHUB_REPOSITORY are required")

    if not after:
        _fail("AFTER_SHA is required")

    repo = repo_full.split("/", 1)[1]

    max_plugins = int(os.environ.get("MAX_PLUGINS", str(DEFAULT_MAX_PLUGINS)))

    plugin_names = _detected_plugin_names(before, after, run_all)
    if not plugin_names:
        print("No plugin changes detected; nothing to do.")
        return 0

    if len(plugin_names) > max_plugins:
        _fail(
            f"Detected {len(plugin_names)} plugins in scope, which exceeds MAX_PLUGINS={max_plugins}. "
            "Increase MAX_PLUGINS or run multiple smaller pushes."
        )

    repo_id, category_id = _get_repo_and_category(owner, repo)

    created = 0
    skipped = 0

    for plugin_name in plugin_names:
        meta = _read_plugin_yaml(plugin_name)
        expected_title = _discussion_title(plugin_name)

        existing = _find_existing_discussion(owner, repo, expected_title)
        if existing:
            skipped += 1
            print(f"Exists: {plugin_name} -> {existing.get('url')}")
            continue

        body = _discussion_body(plugin_name, meta, owner, repo)
        disc = _create_discussion(repo_id, category_id, expected_title, body)
        created += 1
        print(f"Created: {plugin_name} -> {disc.get('url')}")

    print(f"Done. created={created} skipped={skipped} total={len(plugin_names)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnsureDiscussionsError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
