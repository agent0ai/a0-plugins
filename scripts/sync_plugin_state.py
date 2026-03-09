import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml

from plugin_resolution import PLUGINS_DIR, REPO_ROOT, PluginResolutionError, get_plugin_names

INDEX_JSON_PATH = REPO_ROOT / "index.json"
AUTHORS_DIR = REPO_ROOT / "authors"
ALLOWED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
DISCUSSIONS_CATEGORY_NAME = "Plugins"
DISCUSSION_MARKER = "<!-- a0-plugins-discussion -->"
PLUGIN_MARKER_PREFIX = "<!-- a0-plugins-plugin:"
DISCUSSION_TEMPLATE_PATH = REPO_ROOT / "scripts" / "plugin_discussion_template.md"


class SyncPluginStateError(Exception):
    pass


class GitHubHttpError(SyncPluginStateError):
    def __init__(
        self,
        *,
        status: int,
        method: str,
        url: str,
        request_id: str,
        scopes: str,
        body: str,
    ) -> None:
        super().__init__(f"HTTP {status} {method} {url}")
        self.status = status
        self.method = method
        self.url = url
        self.request_id = request_id
        self.scopes = scopes
        self.body = body


def _fail(msg: str) -> NoReturn:
    raise SyncPluginStateError(msg)


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        _fail("GITHUB_TOKEN is required")
    return token


def _with_retries(label: str, fn: Any, max_attempts: int = 3) -> Any:
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except GitHubHttpError as e:
            last_err = e
            if e.status not in {408, 429, 500, 502, 503, 504} or attempt == max_attempts:
                raise
            print(
                f"WARN: transient HTTP error during {label} attempt {attempt}/{max_attempts}: "
                f"status={e.status} request_id={e.request_id}"
            )
            time.sleep(2)
        except Exception as e:
            last_err = e
            if attempt == max_attempts:
                raise
            print(f"WARN: transient error during {label} attempt {attempt}/{max_attempts}: {e}")
            time.sleep(2)
    if last_err is not None:
        raise last_err
    raise RuntimeError("unreachable")


def _graphql_request(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    url = "https://api.github.com/graphql"
    body = {"query": query, "variables": variables}
    data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-sync-plugin-state",
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
        req_id = ""
        scopes = ""
        if e.headers:
            req_id = e.headers.get("x-github-request-id", "")
            scopes = e.headers.get("x-oauth-scopes", "")
        raise GitHubHttpError(
            status=int(e.code),
            method="POST",
            url=url,
            request_id=req_id,
            scopes=scopes,
            body=msg,
        )
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
        errs = parsed_dict.get("errors")
        _fail(f"GitHub GraphQL errors: {json.dumps(errs, indent=2, sort_keys=True)[:4000]}")

    data_obj = parsed_dict.get("data")
    if not isinstance(data_obj, dict):
        _fail("GitHub GraphQL response missing data")

    return cast(dict[str, Any], data_obj)


def _get_owner_repo() -> tuple[str, str]:
    repo_full = os.environ.get("GITHUB_REPOSITORY")
    if not repo_full or "/" not in repo_full:
        _fail("GITHUB_REPOSITORY is required (owner/repo)")
    owner, repo = repo_full.split("/", 1)
    return owner, repo


def _plugin_exists(plugin_name: str) -> bool:
    return (PLUGINS_DIR / plugin_name / "plugin.yaml").exists()


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


def _load_index() -> dict[str, Any]:
    if not INDEX_JSON_PATH.exists():
        return {"version": 1, "plugins": {}}

    try:
        loaded = json.loads(INDEX_JSON_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"Unable to parse {INDEX_JSON_PATH.name}: {e}")

    if not isinstance(loaded, dict):
        _fail(f"{INDEX_JSON_PATH.name} must contain a JSON object")

    if not isinstance(loaded.get("plugins"), dict):
        loaded["plugins"] = {}

    loaded["version"] = 1
    return cast(dict[str, Any], loaded)


def _save_index(index: dict[str, Any]) -> None:
    plugins = index.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    index["plugins"] = {k: plugins[k] for k in sorted(plugins.keys())}
    INDEX_JSON_PATH.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_authors() -> dict[str, Any]:
    if not AUTHORS_DIR.exists() or not AUTHORS_DIR.is_dir():
        return {}

    authors: dict[str, Any] = {}
    for author_dir in sorted(AUTHORS_DIR.iterdir(), key=lambda x: x.name):
        if not author_dir.is_dir():
            continue
        author_yaml = author_dir / "author.yaml"
        if not author_yaml.exists():
            continue
        try:
            loaded = yaml.safe_load(author_yaml.read_text(encoding="utf-8"))
        except Exception as e:
            _fail(f"Invalid author metadata in {author_yaml.relative_to(REPO_ROOT)}: {e}")
        if not isinstance(loaded, dict):
            _fail(f"{author_yaml.relative_to(REPO_ROOT)} must contain a YAML mapping/object")
        authors[author_dir.name] = loaded
    return authors


def _repo_file_url(rel_path: str) -> str:
    owner, repo = _get_owner_repo()
    ref = os.environ.get("GITHUB_REF_NAME") or "main"
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{rel_path.lstrip('/')}"


def _thumbnail_rel_path(plugin_name: str) -> str | None:
    plugin_dir = PLUGINS_DIR / plugin_name
    if not plugin_dir.exists():
        return None
    for ext in ALLOWED_IMAGE_EXTS:
        p = plugin_dir / f"thumbnail{ext}"
        if p.exists():
            return p.relative_to(REPO_ROOT).as_posix()
    return None


def _parse_github_owner_from_url(url: str) -> str | None:
    s = url.strip()
    if not s:
        return None
    s = s.removeprefix("https://")
    s = s.removeprefix("http://")
    if s.startswith("github.com/"):
        s = s[len("github.com/") :]
    if s.startswith("www.github.com/"):
        s = s[len("www.github.com/") :]
    if s.startswith("git@github.com:"):
        s = s[len("git@github.com:") :]
    s = s.strip("/")
    if s.endswith(".git"):
        s = s[: -len(".git")]
    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2:
        return parts[0] or None
    return None


def _index_plugin_entry(plugin_name: str, meta: dict[str, Any], discussion_url: str) -> dict[str, Any]:
    title = meta.get("title") if isinstance(meta.get("title"), str) else None
    description = meta.get("description") if isinstance(meta.get("description"), str) else None
    gh = meta.get("github") if isinstance(meta.get("github"), str) else None
    gh_str = gh if isinstance(gh, str) else ""
    author = _parse_github_owner_from_url(gh_str) if gh_str else None
    tags_val = meta.get("tags")
    tags = [t for t in tags_val if isinstance(t, str) and t.strip()] if isinstance(tags_val, list) else None
    screenshots_val = meta.get("screenshots")
    screenshots = [s.strip() for s in screenshots_val if isinstance(s, str) and s.strip()] if isinstance(screenshots_val, list) else []
    thumb_rel = _thumbnail_rel_path(plugin_name)
    return {
        "title": title,
        "description": description,
        "github": gh,
        "author": author,
        "tags": tags,
        "thumbnail": _repo_file_url(thumb_rel) if isinstance(thumb_rel, str) else None,
        "screenshots": screenshots,
        "discussion": discussion_url,
    }


def _upsert_index_plugin(index: dict[str, Any], plugin_name: str, entry: dict[str, Any]) -> None:
    plugins = index.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        index["plugins"] = plugins
    existing = plugins.get(plugin_name)
    if isinstance(existing, dict) and isinstance(existing.get("stars"), int) and not isinstance(entry.get("stars"), int):
        entry["stars"] = existing.get("stars")
    plugins[plugin_name] = entry


def _remove_index_plugin(index: dict[str, Any], plugin_name: str) -> bool:
    plugins = index.get("plugins")
    if not isinstance(plugins, dict) or plugin_name not in plugins:
        return False
    del plugins[plugin_name]
    return True


def _discussion_title(plugin_name: str) -> str:
    return f"Plugin: {plugin_name}"


def _load_discussion_template() -> str:
    if not DISCUSSION_TEMPLATE_PATH.exists():
        _fail(f"Missing discussion template: {DISCUSSION_TEMPLATE_PATH.relative_to(REPO_ROOT)}")
    return DISCUSSION_TEMPLATE_PATH.read_text(encoding="utf-8")


def _render_discussion_body(plugin_name: str, meta: dict[str, Any], owner: str, repo: str) -> str:
    title_val = meta.get("title")
    title = title_val if isinstance(title_val, str) else ""
    description_val = meta.get("description")
    description = description_val if isinstance(description_val, str) else ""
    gh_val = meta.get("github")
    gh_str = gh_val if isinstance(gh_val, str) else ""
    author = _parse_github_owner_from_url(gh_str) or ""
    body = _load_discussion_template()
    body = body.replace("{{PLUGIN_MARKER}}", f"{PLUGIN_MARKER_PREFIX}{plugin_name} -->")
    body = body.replace("{{TITLE}}", title.strip() if title else "Plugin")
    body = body.replace("{{DESCRIPTION_BLOCK}}", description.strip())
    body = body.replace("{{INDEX_ENTRY_URL}}", f"https://github.com/{owner}/{repo}/tree/main/plugins/{plugin_name}")
    body = body.replace("{{PLUGIN_REPO_LINK_LINE}}", f"- Plugin repository: {gh_str.strip()}" if gh_str else "")
    body = body.replace("{{AUTHOR_LINE}}", f"- Author: @{author}" if author else "")
    if DISCUSSION_MARKER not in body:
        body = f"{DISCUSSION_MARKER}\n{body.lstrip()}"
    return body.strip() + "\n"


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
        _fail(f"Unable to access repository {owner}/{repo}")
    repo_id = repository.get("id")
    if not isinstance(repo_id, str) or not repo_id:
        _fail("Unable to determine repository id")
    cats = repository.get("discussionCategories", {}).get("nodes")
    if not isinstance(cats, list):
        _fail("Unable to list discussion categories")
    for c in cats:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        cid = c.get("id")
        if isinstance(name, str) and isinstance(cid, str) and name.strip().lower() == DISCUSSIONS_CATEGORY_NAME.lower():
            return repo_id, cid
    _fail(f"Discussion category '{DISCUSSIONS_CATEGORY_NAME}' not found in {owner}/{repo}")


def _search_discussion(owner: str, repo: str, query_str: str) -> dict[str, Any] | None:
    query = """
    query($q: String!) {
      search(query: $q, type: DISCUSSION, first: 5) {
        nodes {
          __typename
          ... on Discussion {
            id
            title
            url
            closed
          }
        }
      }
    }
    """
    data = _graphql_request(query, {"q": query_str})
    search = data.get("search")
    if not isinstance(search, dict):
        return None
    nodes = search.get("nodes")
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, dict) and node.get("__typename") == "Discussion":
            return node
    return None


def _find_existing_discussion(owner: str, repo: str, plugin_name: str) -> dict[str, Any] | None:
    marker = f"{PLUGIN_MARKER_PREFIX}{plugin_name} -->"
    by_marker = _search_discussion(owner, repo, f'repo:{owner}/{repo} in:body "{marker}"')
    if by_marker:
        return by_marker
    expected_title = _discussion_title(plugin_name)
    by_title = _search_discussion(owner, repo, f'repo:{owner}/{repo} in:title "{expected_title}"')
    if by_title and by_title.get("title") == expected_title:
        return by_title
    return None


def _create_discussion(repo_id: str, category_id: str, title: str, body: str) -> dict[str, Any]:
    query = """
    mutation($repoId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
      createDiscussion(input: {repositoryId: $repoId, categoryId: $categoryId, title: $title, body: $body}) {
        discussion {
          id
          url
          title
          closed
        }
      }
    }
    """
    data = _graphql_request(
        query,
        {"repoId": repo_id, "categoryId": category_id, "title": title, "body": body},
    )
    payload = data.get("createDiscussion")
    if not isinstance(payload, dict) or not isinstance(payload.get("discussion"), dict):
        _fail("Unexpected GraphQL response: missing discussion")
    return cast(dict[str, Any], payload.get("discussion"))


def _update_discussion(discussion_id: str, title: str, body: str) -> dict[str, Any]:
    query = """
    mutation($id: ID!, $title: String!, $body: String!) {
      updateDiscussion(input: {discussionId: $id, title: $title, body: $body}) {
        discussion {
          id
          url
          title
          closed
        }
      }
    }
    """
    data = _graphql_request(query, {"id": discussion_id, "title": title, "body": body})
    payload = data.get("updateDiscussion")
    if not isinstance(payload, dict) or not isinstance(payload.get("discussion"), dict):
        _fail("Unexpected GraphQL response: missing discussion")
    return cast(dict[str, Any], payload.get("discussion"))


def _reopen_discussion(discussion_id: str) -> dict[str, Any]:
    query = """
    mutation($id: ID!) {
      reopenDiscussion(input: {discussionId: $id}) {
        discussion {
          id
          url
          title
          closed
        }
      }
    }
    """
    data = _graphql_request(query, {"id": discussion_id})
    payload = data.get("reopenDiscussion")
    if not isinstance(payload, dict) or not isinstance(payload.get("discussion"), dict):
        _fail("Unexpected GraphQL response: missing discussion")
    return cast(dict[str, Any], payload.get("discussion"))


def _close_discussion(discussion_id: str) -> dict[str, Any]:
    query = """
    mutation($id: ID!) {
      closeDiscussion(input: {discussionId: $id}) {
        discussion {
          id
          url
          title
          closed
        }
      }
    }
    """
    data = _graphql_request(query, {"id": discussion_id})
    payload = data.get("closeDiscussion")
    if not isinstance(payload, dict) or not isinstance(payload.get("discussion"), dict):
        _fail("Unexpected GraphQL response: missing discussion")
    return cast(dict[str, Any], payload.get("discussion"))


def _sync_existing_plugin(
    owner: str,
    repo: str,
    repo_id: str,
    category_id: str,
    plugin_name: str,
) -> tuple[str, str]:
    meta = _read_plugin_yaml(plugin_name)
    title = _discussion_title(plugin_name)
    body = _render_discussion_body(plugin_name, meta, owner, repo)
    existing = _with_retries(
        f"search discussion {plugin_name}",
        lambda: _find_existing_discussion(owner, repo, plugin_name),
    )
    if existing and isinstance(existing.get("id"), str):
        discussion_id = cast(str, existing.get("id"))
        if existing.get("closed") is True:
            existing = _with_retries(
                f"reopen discussion {plugin_name}",
                lambda: _reopen_discussion(discussion_id),
            )
            discussion_id = cast(str, existing.get("id"))
        discussion = _with_retries(
            f"update discussion {plugin_name}",
            lambda: _update_discussion(discussion_id, title, body),
        )
        url = discussion.get("url") if isinstance(discussion.get("url"), str) else ""
        if not url:
            _fail(f"Updated discussion missing url for '{plugin_name}'")
        return "updated", url
    discussion = _with_retries(
        f"create discussion {plugin_name}",
        lambda: _create_discussion(repo_id, category_id, title, body),
    )
    url = discussion.get("url") if isinstance(discussion.get("url"), str) else ""
    if not url:
        _fail(f"Created discussion missing url for '{plugin_name}'")
    return "created", url


def _sync_deleted_plugin(owner: str, repo: str, plugin_name: str) -> str:
    existing = _with_retries(
        f"search discussion {plugin_name}",
        lambda: _find_existing_discussion(owner, repo, plugin_name),
    )
    if not existing or not isinstance(existing.get("id"), str):
        return "missing"
    if existing.get("closed") is True:
        return "already_closed"
    _with_retries(
        f"close discussion {plugin_name}",
        lambda: _close_discussion(cast(str, existing.get("id"))),
    )
    return "closed"


def main() -> int:
    owner, repo = _get_owner_repo()
    plugin_names = get_plugin_names()
    index = _load_index()
    index_before = json.dumps(index, sort_keys=True)
    index["authors"] = _read_authors()

    if not plugin_names:
        index_after = json.dumps(index, sort_keys=True)
        if index_after != index_before:
            _save_index(index)
            print(f"Updated {INDEX_JSON_PATH.name}")
        print("No plugin changes detected; nothing to do.")
        return 0

    repo_id, category_id = _get_repo_and_category(owner, repo)

    created = 0
    updated = 0
    closed = 0
    removed = 0
    failed: list[str] = []

    for plugin_name in plugin_names:
        try:
            if _plugin_exists(plugin_name):
                action, discussion_url = _sync_existing_plugin(owner, repo, repo_id, category_id, plugin_name)
                meta = _read_plugin_yaml(plugin_name)
                _upsert_index_plugin(index, plugin_name, _index_plugin_entry(plugin_name, meta, discussion_url))
                if action == "created":
                    created += 1
                else:
                    updated += 1
                print(f"{action.capitalize()}: {plugin_name} -> {discussion_url}")
                continue

            discussion_action = _sync_deleted_plugin(owner, repo, plugin_name)
            if discussion_action == "closed":
                closed += 1
            if _remove_index_plugin(index, plugin_name):
                removed += 1
                print(f"Removed from index: {plugin_name}")
            else:
                print(f"Missing from index: {plugin_name}")
            print(f"Discussion state for deleted plugin {plugin_name}: {discussion_action}")
        except SyncPluginStateError as e:
            failed.append(plugin_name)
            print(f"ERROR: plugin={plugin_name}: {e}")
        except Exception as e:
            failed.append(plugin_name)
            print(f"ERROR: plugin={plugin_name}: {e}")

    _save_index(index)
    print(
        f"Done. created={created} updated={updated} closed={closed} removed={removed} "
        f"failed={len(failed)} total={len(plugin_names)}"
    )
    if failed:
        print("Failed plugins:")
        for plugin_name in failed:
            print(f"- {plugin_name}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SyncPluginStateError, PluginResolutionError) as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
