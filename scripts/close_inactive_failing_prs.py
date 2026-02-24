import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn, cast


class CloseInactivePRsError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise CloseInactivePRsError(msg)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso8601(dt: str) -> datetime:
    # GitHub uses RFC3339 timestamps like 2026-02-24T09:00:00Z
    try:
        if dt.endswith("Z"):
            return datetime.fromisoformat(dt.replace("Z", "+00:00"))
        return datetime.fromisoformat(dt)
    except Exception as e:
        _fail(f"Unable to parse timestamp '{dt}': {e}")


def _request_json(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _fail("GITHUB_TOKEN is required")

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-maintenance-bot",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"GitHub API request failed ({e.code}) {method} {url}: {msg}")
    except Exception as e:
        _fail(f"GitHub API request failed {method} {url}: {e}")

    if not payload.strip():
        return {}

    try:
        parsed = json.loads(payload)
    except Exception as e:
        _fail(f"GitHub API returned invalid JSON for {url}: {e}: {payload[:500]}")

    if not isinstance(parsed, dict):
        _fail(f"GitHub API returned non-object JSON for {url}")

    return cast(dict[str, Any], parsed)


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    return _request_json(
        "POST",
        "https://api.github.com/graphql",
        {"query": query, "variables": variables},
    ).get("data", {})  # type: ignore[return-value]


def _close_pr(owner: str, repo: str, number: int, comment: str, dry_run: bool) -> None:
    if dry_run:
        print(f"DRY_RUN: would close PR #{number}")
        return

    _request_json(
        "PATCH",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}",
        {"state": "closed"},
    )

    _request_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
        {"body": comment},
    )


def main() -> int:
    owner = os.environ.get("OWNER") or os.environ.get("GITHUB_REPOSITORY_OWNER")
    repo = os.environ.get("REPO")
    if not owner:
        _fail("OWNER (or GITHUB_REPOSITORY_OWNER) is required")
    if not repo:
        _fail("REPO is required")

    inactivity_days = int(os.environ.get("INACTIVITY_DAYS", "7"))
    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"

    cutoff = _utcnow() - timedelta(days=inactivity_days)
    print(f"Cutoff (updatedAt must be <): {cutoff.isoformat()}")

    comment = os.environ.get(
        "CLOSE_COMMENT",
        "Closing due to failing checks and no activity for 7+ days. Comment or push to keep it alive; reopen if you'd like to continue.",
    )

    query = """
    query($owner:String!, $repo:String!, $cursor:String) {
      repository(owner:$owner, name:$repo) {
        pullRequests(states:OPEN, first:100, after:$cursor, orderBy:{field:UPDATED_AT, direction:ASC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            number
            updatedAt
            isDraft
            commits(last: 1) {
              nodes {
                commit {
                  statusCheckRollup { state }
                }
              }
            }
          }
        }
      }
    }
    """

    closed = 0
    scanned = 0

    cursor: str | None = None
    while True:
        variables: dict[str, Any] = {"owner": owner, "repo": repo, "cursor": cursor}
        data = _graphql(query, variables)

        repository = data.get("repository")
        if not isinstance(repository, dict):
            _fail("GraphQL: missing repository")

        prs = repository.get("pullRequests")
        if not isinstance(prs, dict):
            _fail("GraphQL: missing pullRequests")

        nodes = prs.get("nodes")
        if not isinstance(nodes, list):
            _fail("GraphQL: pullRequests.nodes is not a list")

        for pr in nodes:
            if not isinstance(pr, dict):
                continue

            scanned += 1

            if pr.get("isDraft") is True:
                continue

            updated_at = pr.get("updatedAt")
            if not isinstance(updated_at, str):
                continue

            updated_dt = _parse_iso8601(updated_at)
            if updated_dt >= cutoff:
                # Because we order by UPDATED_AT ascending, we can stop early.
                print("Reached active PRs; stopping pagination.")
                print(f"Scanned={scanned} closed={closed}")
                return 0

            number = pr.get("number")
            if not isinstance(number, int):
                continue

            commits = pr.get("commits")
            rollup_state = None
            if isinstance(commits, dict):
                c_nodes = commits.get("nodes")
                if isinstance(c_nodes, list) and c_nodes:
                    c0 = c_nodes[0]
                    if isinstance(c0, dict):
                        commit = c0.get("commit")
                        if isinstance(commit, dict):
                            scr = commit.get("statusCheckRollup")
                            if isinstance(scr, dict):
                                state = scr.get("state")
                                if isinstance(state, str):
                                    rollup_state = state

            if rollup_state not in {"FAILURE", "ERROR"}:
                continue

            print(f"Closing PR #{number} (updatedAt={updated_at}, checks={rollup_state})")
            _close_pr(owner, repo, number, comment, dry_run=dry_run)
            closed += 1

        page = prs.get("pageInfo")
        if not isinstance(page, dict):
            _fail("GraphQL: missing pageInfo")

        has_next = page.get("hasNextPage")
        if has_next is not True:
            break

        end_cursor = page.get("endCursor")
        if not isinstance(end_cursor, str) or not end_cursor:
            break

        cursor = end_cursor

    print(f"Done. Scanned={scanned} closed={closed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CloseInactivePRsError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
