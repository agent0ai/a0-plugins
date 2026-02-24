import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = REPO_ROOT / "index.json"
DEFAULT_ASSET_NAME = "index.json"
DEFAULT_RELEASE_TAG = "generated-index"


class DownloadIndexError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise DownloadIndexError(msg)


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _fail("GITHUB_TOKEN is required")
    return token


def _request_json_allow_404(url: str) -> dict[str, Any] | None:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-plugins-index-downloader",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"GitHub API request failed ({e.code}) GET {url}: {msg}")
    except Exception as e:
        _fail(f"GitHub API request failed GET {url}: {e}")

    try:
        parsed = json.loads(payload)
    except Exception as e:
        _fail(f"GitHub API returned invalid JSON for {url}: {e}: {payload[:500]}")

    if not isinstance(parsed, dict):
        _fail(f"GitHub API returned non-object JSON for {url}")

    return cast(dict[str, Any], parsed)


def _download_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/octet-stream",
            "User-Agent": "a0-plugins-index-downloader",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"Download failed ({e.code}) GET {url}: {msg}")
    except Exception as e:
        _fail(f"Download failed GET {url}: {e}")


def main() -> int:
    repo_full = os.environ.get("GITHUB_REPOSITORY")
    if not repo_full or "/" not in repo_full:
        _fail("GITHUB_REPOSITORY is required (owner/repo)")

    owner, repo = repo_full.split("/", 1)
    asset_name = os.environ.get("INDEX_ASSET_NAME", DEFAULT_ASSET_NAME)

    tag = os.environ.get("INDEX_RELEASE_TAG", DEFAULT_RELEASE_TAG)

    rel = _request_json_allow_404(
        f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    )
    if not rel:
        _fail(f"Release tag '{tag}' not found. Generate/publish index.json first.")

    assets = rel.get("assets")
    if not isinstance(assets, list):
        _fail("Release response missing assets")

    download_url: str | None = None
    for a in assets:
        if not isinstance(a, dict):
            continue
        if a.get("name") == asset_name and isinstance(a.get("browser_download_url"), str):
            download_url = cast(str, a.get("browser_download_url"))
            break

    if not download_url:
        _fail(f"Release '{tag}' does not contain asset '{asset_name}'")

    content = _download_bytes(download_url)
    INDEX_PATH.write_bytes(content)
    print(f"Downloaded {asset_name} -> {INDEX_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DownloadIndexError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
