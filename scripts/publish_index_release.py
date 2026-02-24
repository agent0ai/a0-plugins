import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = REPO_ROOT / "index.json"
DEFAULT_RELEASE_TAG = "generated-index"
DEFAULT_RELEASE_NAME = "Generated Index"
DEFAULT_ASSET_NAME = "index.json"


class PublishReleaseError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise PublishReleaseError(msg)


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _fail("GITHUB_TOKEN is required")
    return token


def _request_json(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-index-publisher",
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


def _request_json_allow_404(method: str, url: str) -> dict[str, Any] | None:
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-index-publisher",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
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


def _request_nojson(method: str, url: str) -> None:
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-index-publisher",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30):
            return
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"GitHub API request failed ({e.code}) {method} {url}: {msg}")
    except Exception as e:
        _fail(f"GitHub API request failed {method} {url}: {e}")


def _get_owner_repo() -> tuple[str, str]:
    repo_full = os.environ.get("GITHUB_REPOSITORY")
    if not repo_full or "/" not in repo_full:
        _fail("GITHUB_REPOSITORY is required (owner/repo)")
    owner, repo = repo_full.split("/", 1)
    return owner, repo


def _get_latest_release(owner: str, repo: str) -> dict[str, Any] | None:
    # Backward compat: keep helper name, but prefer deterministic release-by-tag.
    tag = os.environ.get("INDEX_RELEASE_TAG", DEFAULT_RELEASE_TAG)
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    return _request_json_allow_404("GET", url)


def _get_release(owner: str, repo: str, release_id: int) -> dict[str, Any]:
    return _request_json("GET", f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}")


def _create_release(owner: str, repo: str) -> dict[str, Any]:
    tag = os.environ.get("INDEX_RELEASE_TAG", DEFAULT_RELEASE_TAG)
    name = os.environ.get("INDEX_RELEASE_NAME", DEFAULT_RELEASE_NAME)
    target = os.environ.get("INDEX_RELEASE_TARGET", "main")

    payload = {
        "tag_name": tag,
        "target_commitish": target,
        "name": name,
        "body": "Automated index release asset.",
        "draft": False,
        "prerelease": False,
        "generate_release_notes": False,
    }
    rel = _request_json("POST", f"https://api.github.com/repos/{owner}/{repo}/releases", payload)
    if "id" not in rel:
        _fail("Release creation did not return an id")
    return rel


def _upload_asset(owner: str, repo: str, release: dict[str, Any], asset_name: str, content: bytes) -> None:
    upload_url_tmpl = release.get("upload_url")
    if not isinstance(upload_url_tmpl, str) or "{" not in upload_url_tmpl:
        _fail("Release upload_url missing")

    upload_url = upload_url_tmpl.split("{", 1)[0]
    url = upload_url + "?" + urllib.parse.urlencode({"name": asset_name})

    req = urllib.request.Request(
        url,
        data=content,
        method="POST",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-index-publisher",
            "Content-Type": "application/octet-stream",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 422:
            # Asset already exists (common when two publishers race). Caller can delete and retry.
            raise
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"Asset upload failed ({e.code}) POST {url}: {msg}")
    except Exception as e:
        _fail(f"Asset upload failed POST {url}: {e}")

    try:
        parsed = json.loads(payload)
    except Exception:
        parsed = None

    if isinstance(parsed, dict) and parsed.get("name") == asset_name:
        print(f"Uploaded asset: {asset_name}")
        return

    print(f"Uploaded asset: {asset_name}")


def _delete_asset(owner: str, repo: str, asset_id: int) -> None:
    _request_nojson(
        "DELETE",
        f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}",
    )


def main() -> int:
    owner, repo = _get_owner_repo()

    asset_name = os.environ.get("INDEX_ASSET_NAME", DEFAULT_ASSET_NAME)

    if not INDEX_PATH.exists():
        _fail(f"Missing {INDEX_PATH.relative_to(REPO_ROOT)}. Generate it first.")

    content = INDEX_PATH.read_bytes()

    release_opt = _get_latest_release(owner, repo)
    if not release_opt:
        print("No releases found; creating one")
        release_opt = _create_release(owner, repo)

    release = cast(dict[str, Any], release_opt)

    rid = release.get("id")
    if not isinstance(rid, int):
        _fail("Release id missing")

    # Fetch full release payload (assets can be truncated/absent depending on endpoint used)
    release = _get_release(owner, repo, rid)

    def _delete_existing_assets(release_payload: dict[str, Any]) -> None:
        assets = release_payload.get("assets")
        if not isinstance(assets, list):
            return
        for a in assets:
            if not isinstance(a, dict):
                continue
            if a.get("name") != asset_name:
                continue
            aid = a.get("id")
            if isinstance(aid, int):
                print(f"Deleting existing asset: {asset_name} (id={aid})")
                _delete_asset(owner, repo, aid)

    # Safer replacement strategy: try upload first; if it already exists (422), delete and retry.
    try:
        _upload_asset(owner, repo, release, asset_name, content)
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise
        print(f"Asset already exists ({asset_name}); deleting and retrying")
        _delete_existing_assets(release)
        # Refresh release assets after deletion
        release = _get_release(owner, repo, rid)
        _upload_asset(owner, repo, release, asset_name, content)

    html = release.get("html_url")
    if isinstance(html, str):
        print(f"Release: {html}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublishReleaseError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
