import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"
DISCUSSIONS_CATEGORY_NAME = "Plugins"
DISCUSSION_MARKER = "<!-- a0-plugins-discussion -->"
PLUGIN_MARKER_PREFIX = "<!-- a0-plugins-plugin:"
DISCUSSION_TEMPLATE_PATH = REPO_ROOT / "scripts" / "plugin_discussion_template.md"
DEFAULT_MAX_PLUGINS = 100


class UpdatePluginDiscussionsError(Exception):
    pass


class GitHubHttpError(UpdatePluginDiscussionsError):
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
    raise UpdatePluginDiscussionsError(msg)


def _is_transient_http_status(status: int) -> bool:
    return status in {408, 429, 500, 502, 503, 504}


def _with_retries(label: str, fn: Any, max_attempts: int = 3) -> Any:
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except GitHubHttpError as e:
            last_err = e
            if not _is_transient_http_status(e.status) or attempt == max_attempts:
                raise
            print(
                f"WARN: transient HTTP error during {label} attempt {attempt}/{max_attempts}: "
                f"status={e.status} request_id={e.request_id}"
            )
        except Exception as e:
            last_err = e
            if attempt == max_attempts:
                raise
            print(f"WARN: transient error during {label} attempt {attempt}/{max_attempts}: {e}")
    if last_err is not None:
        raise last_err
    raise RuntimeError("unreachable")


def _run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=REPO_ROOT)
    return out.decode("utf-8", errors="replace")


def _plugin_exists(plugin_name: str) -> bool:
    plugin_yaml = PLUGINS_DIR / plugin_name / "plugin.yaml"
    return plugin_yaml.exists()


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


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _fail("GITHUB_TOKEN is required")
    return token


def _token_type_hint(token: str) -> str:
    # Do not print the token. Only provide a coarse hint based on public prefixes.
    if token.startswith("ghs_"):
        return "ghs_* (GitHub Actions / app installation token style)"
    if token.startswith("ghp_"):
        return "ghp_* (classic PAT style)"
    if token.startswith("github_pat_"):
        return "github_pat_* (fine-grained PAT style)"
    return "(unknown token prefix)"


def _rest_request_json(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "a0-plugins-discussion-updater",
            "Content-Type": "application/json",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            headers = {k.lower(): v for (k, v) in resp.headers.items()}
            try:
                parsed = json.loads(raw) if raw.strip() else None
            except Exception:
                parsed = raw
            return int(resp.status), headers, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        headers = {k.lower(): v for (k, v) in e.headers.items()} if e.headers else {}
        req_id = headers.get("x-github-request-id", "")
        scopes = headers.get("x-oauth-scopes", "")
        raise GitHubHttpError(
            status=int(e.code),
            method=method,
            url=url,
            request_id=req_id,
            scopes=scopes,
            body=raw,
        )
    except Exception as e:
        _fail(f"GitHub REST request failed method={method} url={url}: {e}")


def _print_auth_diagnostics() -> None:
    token = _token()
    print(f"Auth: token_hint={_token_type_hint(token)}")
    status, headers, parsed = _rest_request_json("GET", "https://api.github.com/user")
    login = None
    if isinstance(parsed, dict) and isinstance(parsed.get("login"), str):
        login = parsed.get("login")
    scopes = headers.get("x-oauth-scopes", "")
    fine_grained = headers.get("x-accepted-oauth-scopes", "")
    req_id = headers.get("x-github-request-id", "")
    print(
        "Auth: "
        f"rest_user_status={status} login={login!s} request_id={req_id!s} "
        f"x-oauth-scopes={scopes!s} x-accepted-oauth-scopes={fine_grained!s}"
    )


def _graphql_request(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-discussion-updater",
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
            url="https://api.github.com/graphql",
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
        if (
            isinstance(name, str)
            and isinstance(cid, str)
            and name.strip().lower() == DISCUSSIONS_CATEGORY_NAME.lower()
        ):
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


def _load_discussion_template() -> str:
    if not DISCUSSION_TEMPLATE_PATH.exists():
        _fail(f"Missing discussion template: {DISCUSSION_TEMPLATE_PATH.relative_to(REPO_ROOT)}")
    return DISCUSSION_TEMPLATE_PATH.read_text(encoding="utf-8")


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
        owner = parts[0]
        return owner if owner else None
    return None


def _discussion_number_from_url(url: str) -> int | None:
    # Expected: https://github.com/OWNER/REPO/discussions/123
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 4 and parts[2] == "discussions":
        try:
            n = int(parts[3])
            return n if n > 0 else None
        except Exception:
            return None
    return None


def _render_discussion_body(plugin_name: str, meta: dict[str, Any], owner: str, repo: str) -> str:
    title = meta.get("title") if isinstance(meta.get("title"), str) else ""
    description = meta.get("description") if isinstance(meta.get("description"), str) else ""
    gh_val = meta.get("github")
    gh_str = gh_val if isinstance(gh_val, str) else ""
    author = _parse_github_owner_from_url(gh_str) or ""

    index_entry_url = f"https://github.com/{owner}/{repo}/tree/main/plugins/{plugin_name}"
    plugin_marker = f"{PLUGIN_MARKER_PREFIX}{plugin_name} -->"

    description_block = description.strip() if description else ""

    plugin_repo_link_line = ""
    if gh_str:
        plugin_repo_link_line = f"- Plugin repository: {gh_str.strip()}"

    author_line = ""
    if author:
        author_line = f"- Author: @{author}"

    body = _load_discussion_template()
    body = body.replace("{{PLUGIN_MARKER}}", plugin_marker)
    body = body.replace("{{TITLE}}", title.strip() if title else "Plugin")
    body = body.replace("{{DESCRIPTION_BLOCK}}", description_block)
    body = body.replace("{{INDEX_ENTRY_URL}}", index_entry_url)
    body = body.replace("{{PLUGIN_REPO_LINK_LINE}}", plugin_repo_link_line)
    body = body.replace("{{AUTHOR_LINE}}", author_line)

    if DISCUSSION_MARKER not in body:
        body = f"{DISCUSSION_MARKER}\n{body.lstrip()}"

    return body.strip() + "\n"


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

    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("__typename") != "Discussion":
            continue
        return n

    return None


def _find_existing_discussion(owner: str, repo: str, plugin_name: str, expected_title: str) -> dict[str, Any] | None:
    marker = f"{PLUGIN_MARKER_PREFIX}{plugin_name} -->"
    by_marker = _search_discussion(owner, repo, f'repo:{owner}/{repo} in:body "{marker}"')
    if by_marker:
        return by_marker

    by_title = _search_discussion(owner, repo, f'repo:{owner}/{repo} in:title "{expected_title}"')
    if by_title and by_title.get("title") == expected_title:
        return by_title

    return None


def _reopen_discussion(discussion_id: str) -> None:
    query = """
    mutation($id: ID!) {
      reopenDiscussion(input: {discussionId: $id}) {
        discussion {
          id
          url
          closed
        }
      }
    }
    """

    data = _graphql_request(query, {"id": discussion_id})
    rd = data.get("reopenDiscussion")
    if not isinstance(rd, dict):
        _fail("Unexpected GraphQL response: missing reopenDiscussion")
    disc = rd.get("discussion")
    if not isinstance(disc, dict):
        _fail("Unexpected GraphQL response: missing discussion")
    if disc.get("closed") is True:
        _fail("Attempted to reopen discussion but it is still closed")


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


def _update_discussion(discussion_id: str, title: str, body: str) -> None:
    query = """
    mutation($id: ID!, $title: String!, $body: String!) {
      updateDiscussion(input: {discussionId: $id, title: $title, body: $body}) {
        discussion {
          id
          url
          title
        }
      }
    }
    """

    data = _graphql_request(query, {"id": discussion_id, "title": title, "body": body})
    ud = data.get("updateDiscussion")
    if not isinstance(ud, dict):
        _fail("Unexpected GraphQL response: missing updateDiscussion")
    disc = ud.get("discussion")
    if not isinstance(disc, dict):
        _fail("Unexpected GraphQL response: missing discussion")


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

    _print_auth_diagnostics()

    repo_id, category_id = _get_repo_and_category(owner, repo)

    created = 0
    updated = 0
    skipped = 0
    failed: list[str] = []

    for plugin_name in plugin_names:
        try:
            if not _plugin_exists(plugin_name):
                print(f"Plugin deleted; skipping discussion: {plugin_name}")
                continue

            meta = _read_plugin_yaml(plugin_name)
            expected_title = _discussion_title(plugin_name)

            def _find() -> dict[str, Any] | None:
                return _find_existing_discussion(owner, repo, plugin_name, expected_title)

            existing = _with_retries(f"search discussion {plugin_name}", _find)
            if existing:
                disc_id = existing.get("id")
                closed = existing.get("closed")
                existing_url = existing.get("url") if isinstance(existing.get("url"), str) else ""
                if isinstance(disc_id, str) and closed is True:
                    _with_retries(f"reopen discussion {plugin_name}", lambda: _reopen_discussion(disc_id))

                if isinstance(disc_id, str) and disc_id:
                    body = _render_discussion_body(plugin_name, meta, owner, repo)
                    _with_retries(
                        f"update discussion {plugin_name}",
                        lambda: _update_discussion(disc_id, expected_title, body),
                    )
                    updated += 1
                    print(f"Updated: {plugin_name} -> {existing.get('url')}")
                else:
                    skipped += 1
                    print(f"Exists (no id): {plugin_name} -> {existing.get('url')}")
                continue

            body = _render_discussion_body(plugin_name, meta, owner, repo)
            disc = _with_retries(
                f"create discussion {plugin_name}",
                lambda: _create_discussion(repo_id, category_id, expected_title, body),
            )
            created += 1
            print(f"Created: {plugin_name} -> {disc.get('url')}")
        except UpdatePluginDiscussionsError as e:
            failed.append(plugin_name)
            print(f"ERROR: plugin={plugin_name}: {e}")
        except Exception as e:
            failed.append(plugin_name)
            print(f"ERROR: plugin={plugin_name}: {e}")

    print(
        f"Done. created={created} updated={updated} skipped={skipped} "
        f"failed={len(failed)} total={len(plugin_names)}"
    )
    if failed:
        print("Failed plugins:")
        for n in failed:
            print(f"- {n}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdatePluginDiscussionsError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
