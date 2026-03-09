import json
import os
import re
import subprocess
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml
from PIL import Image

from plugin_resolution import REPO_ROOT

PLUGINS_DIR = REPO_ROOT / "plugins"
ALLOWED_FIELDS = {"title", "description", "github", "tags", "screenshots"}
REQUIRED_FIELDS = {"title", "description", "github"}
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
THUMBNAIL_MAX_BYTES = 20 * 1024
SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024
PLUGIN_YAML_MAX_CHARS = 2000
TITLE_MAX_LEN = 50
DESCRIPTION_MAX_LEN = 500
MAX_TAGS = 5
MAX_SCREENSHOTS = 5


class ValidatePluginSubmissionError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise ValidatePluginSubmissionError(msg)


def _run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=REPO_ROOT)
    return out.decode("utf-8", errors="replace")


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _base_head() -> tuple[str, str]:
    base = os.environ.get("BASE_SHA", "").strip()
    head = os.environ.get("HEAD_SHA", "").strip()
    if not base or not head:
        _fail("BASE_SHA and HEAD_SHA are required")
    return base, head


def _changed_files() -> list[str]:
    base, head = _base_head()
    raw = _run(["git", "diff", "--name-only", f"{base}..{head}"])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _submission_plugin_name(paths: list[str]) -> str:
    plugin_names: set[str] = set()
    for path in paths:
        parts = Path(path).parts
        if not parts:
            continue
        if parts[0] != "plugins":
            _fail(f"Only files under plugins/ are allowed in plugin PRs: {path}")
        if len(parts) < 2:
            _fail(f"Invalid plugin path: {path}")
        plugin_name = parts[1]
        if not plugin_name or plugin_name.startswith("_"):
            _fail(f"Plugin folder names starting with '_' are reserved: {path}")
        plugin_names.add(plugin_name)
    if len(plugin_names) != 1:
        _fail("PR must modify exactly one plugin folder under plugins/")
    return next(iter(plugin_names))


def _plugin_dir(plugin_name: str) -> Path:
    return PLUGINS_DIR / plugin_name


def _read_plugin_yaml(plugin_name: str) -> dict[str, Any]:
    plugin_yaml = _plugin_dir(plugin_name) / "plugin.yaml"
    if not plugin_yaml.exists():
        _fail(f"Missing plugin.yaml: {plugin_yaml.relative_to(REPO_ROOT)}")
    raw_text = plugin_yaml.read_text(encoding="utf-8")
    if len(raw_text) > PLUGIN_YAML_MAX_CHARS:
        _fail(f"plugin.yaml exceeds max total length {PLUGIN_YAML_MAX_CHARS} characters")
    try:
        loaded = yaml.safe_load(raw_text)
    except Exception as e:
        _fail(f"Invalid YAML in {plugin_yaml.relative_to(REPO_ROOT)}: {e}")
    if not isinstance(loaded, dict):
        _fail("plugin.yaml must be a YAML mapping/object")
    return cast(dict[str, Any], loaded)


def _validate_fields(meta: dict[str, Any]) -> None:
    keys = set(meta.keys())
    unknown = sorted(k for k in keys if k not in ALLOWED_FIELDS)
    if unknown:
        _fail(f"plugin.yaml contains unsupported fields: {', '.join(unknown)}")
    missing = sorted(k for k in REQUIRED_FIELDS if not isinstance(meta.get(k), str) or not cast(str, meta.get(k)).strip())
    if missing:
        _fail(f"plugin.yaml is missing required non-empty fields: {', '.join(missing)}")

    title = cast(str, meta.get("title"))
    description = cast(str, meta.get("description"))
    github = cast(str, meta.get("github"))
    if len(title.strip()) > TITLE_MAX_LEN:
        _fail(f"title exceeds max length {TITLE_MAX_LEN}")
    if len(description.strip()) > DESCRIPTION_MAX_LEN:
        _fail(f"description exceeds max length {DESCRIPTION_MAX_LEN}")
    _validate_github_repo(github)

    tags = meta.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            _fail("tags must be a list of non-empty strings")
        if len(tags) > MAX_TAGS:
            _fail(f"tags must contain at most {MAX_TAGS} items")

    screenshots = meta.get("screenshots")
    if screenshots is not None:
        _validate_screenshot_urls(screenshots)


def _parse_repo_url(url: str) -> tuple[str, str] | None:
    match = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def _request_json(url: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "a0-plugins-validate-plugin-submission",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
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


def _validate_screenshot_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _fail("screenshots entries must be full http/https URLs")
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTS:
        _fail("screenshots URLs must end with png/jpg/jpeg/webp")

    req = urllib.request.Request(
        url.strip(),
        method="HEAD",
        headers={"User-Agent": "a0-plugins-validate-plugin-submission"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_length = resp.headers.get("Content-Length", "").strip()
            if content_length:
                try:
                    if int(content_length) > SCREENSHOT_MAX_BYTES:
                        _fail("screenshot exceeds 2 MB")
                except ValueError:
                    pass
            return
    except urllib.error.HTTPError as e:
        if e.code not in {405, 501}:
            msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
            _fail(f"screenshot URL is not reachable ({e.code}): {msg}")
    except Exception as e:
        _fail(f"screenshot URL is not reachable: {e}")

    req = urllib.request.Request(
        url.strip(),
        method="GET",
        headers={
            "User-Agent": "a0-plugins-validate-plugin-submission",
            "Range": f"bytes=0-{SCREENSHOT_MAX_BYTES}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(SCREENSHOT_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"screenshot URL is not reachable ({e.code}): {msg}")
    except Exception as e:
        _fail(f"screenshot URL is not reachable: {e}")
    if len(data) > SCREENSHOT_MAX_BYTES:
        _fail("screenshot exceeds 2 MB")


def _validate_screenshot_urls(screenshots: Any) -> None:
    if not isinstance(screenshots, list):
        _fail("screenshots must be a list of full image URLs")
    if len(screenshots) > MAX_SCREENSHOTS:
        _fail(f"screenshots must contain at most {MAX_SCREENSHOTS} items")
    for screenshot in screenshots:
        if not isinstance(screenshot, str) or not screenshot.strip():
            _fail("screenshots must be a list of non-empty strings")
        _validate_screenshot_url(screenshot)


def _validate_github_repo(url: str) -> None:
    parsed = _parse_repo_url(url)
    if not parsed:
        _fail("github must be a valid GitHub repository URL")
    owner, repo = parsed
    repo_obj = _request_json(f"https://api.github.com/repos/{owner}/{repo}")
    if not isinstance(repo_obj.get("full_name"), str):
        _fail("github repository does not exist or is inaccessible")
    content_obj = _request_json(f"https://api.github.com/repos/{owner}/{repo}/contents/plugin.yaml")
    if content_obj.get("type") != "file":
        _fail("github repository must contain plugin.yaml at repository root")


def _validate_thumbnail(plugin_dir: Path) -> None:
    thumbnails = [p for p in plugin_dir.iterdir() if p.is_file() and p.stem == "thumbnail"]
    if len(thumbnails) > 1:
        _fail("Only one thumbnail file is allowed")
    if not thumbnails:
        return
    thumbnail = thumbnails[0]
    if thumbnail.suffix.lower() not in ALLOWED_IMAGE_EXTS:
        _fail("thumbnail must be png/jpg/jpeg/webp")
    if thumbnail.stat().st_size > THUMBNAIL_MAX_BYTES:
        _fail("thumbnail exceeds 20 KB")
    with Image.open(thumbnail) as img:
        width, height = img.size
    if width != height:
        _fail("thumbnail must be square")


def _validate_allowed_files(plugin_dir: Path) -> None:
    for path in plugin_dir.iterdir():
        if path.is_dir():
            _fail(f"Unexpected directory in plugin folder: {path.name}")
        if path.name == "plugin.yaml":
            continue
        if path.stem == "thumbnail" and path.suffix.lower() in ALLOWED_IMAGE_EXTS:
            continue
        _fail(f"Unexpected file in plugin folder: {path.name}")


def main() -> int:
    paths = _changed_files()
    if not paths:
        _fail("No changed files detected")
    plugin_name = _submission_plugin_name(paths)
    plugin_dir = _plugin_dir(plugin_name)
    if not plugin_dir.exists() or not plugin_dir.is_dir():
        _fail(f"Plugin directory does not exist in PR head: plugins/{plugin_name}")
    meta = _read_plugin_yaml(plugin_name)
    _validate_fields(meta)
    _validate_allowed_files(plugin_dir)
    _validate_thumbnail(plugin_dir)
    print(f"Validation passed for plugin: {plugin_name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidatePluginSubmissionError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
