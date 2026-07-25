import base64
import io
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

from plugin_resolution import INDEX_YAML_NAME, REPO_ROOT, is_valid_plugin_dirname

PLUGINS_DIR = REPO_ROOT / "plugins"
INDEX_JSON_PATH = REPO_ROOT / "index.json"
ALLOWED_FIELDS = {"title", "description", "github", "tags", "screenshots"}
REQUIRED_FIELDS = {"title", "description", "github"}
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
THUMBNAIL_MAX_BYTES = 20 * 1024
SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024
INDEX_YAML_MAX_CHARS = 2000
TITLE_MAX_LEN = 50
DESCRIPTION_MAX_LEN = 500
MAX_TAGS = 5
MAX_SCREENSHOTS = 5


class ValidatePluginSubmissionError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise ValidatePluginSubmissionError(msg)


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}")


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


def _head_sha() -> str:
    _, head = _base_head()
    return head


def _changed_entries() -> list[tuple[str, list[str]]]:
    base, head = _base_head()
    raw = _run(["git", "diff", "--name-status", f"{base}..{head}"])
    entries: list[tuple[str, list[str]]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if len(parts) < 2:
            continue
        status = parts[0]
        paths = parts[1:]
        entries.append((status, paths))
    return entries


def _all_changed_paths(entries: list[tuple[str, list[str]]]) -> list[str]:
    out: list[str] = []
    for _, paths in entries:
        out.extend(paths)
    return out


def _validate_plugin_name_format(plugin_name: str, *, context: str) -> None:
    if is_valid_plugin_dirname(plugin_name):
        return
    _fail(
        f"{context} '{plugin_name}' is invalid: "
        "expected lowercase letters, numbers, and underscores only "
        "(dashes are not allowed)"
    )


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
        _validate_plugin_name_format(plugin_name, context="Plugin folder name")
        plugin_names.add(plugin_name)
    if len(plugin_names) != 1:
        _fail("PR must modify exactly one plugin folder under plugins/")
    return next(iter(plugin_names))


def _is_deletion_pr(entries: list[tuple[str, list[str]]], plugin_name: str) -> bool:
    if not entries:
        return False
    for status, paths in entries:
        if not status.startswith("D"):
            return False
        for path in paths:
            parts = Path(path).parts
            if len(parts) < 2 or parts[0] != "plugins" or parts[1] != plugin_name:
                return False
    return True


def _plugin_dir(plugin_name: str) -> Path:
    return PLUGINS_DIR / plugin_name


def _git_path_exists(commit: str, rel_path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{rel_path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _git_read_text(commit: str, rel_path: str) -> str:
    try:
        out = subprocess.check_output(["git", "show", f"{commit}:{rel_path}"], cwd=REPO_ROOT)
    except subprocess.CalledProcessError:
        _fail(f"Missing {Path(rel_path).name}: {rel_path}")
    return out.decode("utf-8", errors="replace")


def _git_read_bytes(commit: str, rel_path: str) -> bytes:
    try:
        return subprocess.check_output(["git", "show", f"{commit}:{rel_path}"], cwd=REPO_ROOT)
    except subprocess.CalledProcessError:
        _fail(f"Missing file in PR head: {rel_path}")


def _git_plugin_files(commit: str, plugin_name: str) -> list[str]:
    raw = _run(["git", "ls-tree", "--name-only", f"{commit}:plugins/{plugin_name}"])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _read_plugin_yaml(plugin_name: str) -> dict[str, Any]:
    commit = _head_sha()
    rel_path = f"plugins/{plugin_name}/{INDEX_YAML_NAME}"
    if not _git_path_exists(commit, rel_path):
        _fail(f"Missing {INDEX_YAML_NAME}: {rel_path}")
    raw_text = _git_read_text(commit, rel_path)
    if len(raw_text) > INDEX_YAML_MAX_CHARS:
        _fail(f"{INDEX_YAML_NAME} exceeds max total length {INDEX_YAML_MAX_CHARS} characters")
    try:
        loaded = yaml.safe_load(raw_text)
    except Exception as e:
        _fail(f"Invalid YAML in {rel_path}: {e}")
    if not isinstance(loaded, dict):
        _fail(f"{INDEX_YAML_NAME} must be a YAML mapping/object")
    return cast(dict[str, Any], loaded)


def _validate_fields(meta: dict[str, Any], plugin_name: str) -> None:
    keys = set(meta.keys())
    unknown = sorted(k for k in keys if k not in ALLOWED_FIELDS)
    if unknown:
        _fail(f"{INDEX_YAML_NAME} contains unsupported fields: {', '.join(unknown)}")
    missing = sorted(k for k in REQUIRED_FIELDS if not isinstance(meta.get(k), str) or not cast(str, meta.get(k)).strip())
    if missing:
        _fail(f"{INDEX_YAML_NAME} is missing required non-empty fields: {', '.join(missing)}")

    title = cast(str, meta.get("title"))
    description = cast(str, meta.get("description"))
    github = cast(str, meta.get("github"))
    if len(title.strip()) > TITLE_MAX_LEN:
        _fail(f"title exceeds max length {TITLE_MAX_LEN}")
    if len(description.strip()) > DESCRIPTION_MAX_LEN:
        _fail(f"description exceeds max length {DESCRIPTION_MAX_LEN}")
    _validate_github_repo(github, plugin_name)

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


def _normalize_repo_url(url: str) -> str | None:
    parsed = _parse_repo_url(url)
    if not parsed:
        return None
    owner, repo = parsed
    return f"https://github.com/{owner.lower()}/{repo.lower()}"


def _repo_owner_from_url(url: str) -> str | None:
    parsed = _parse_repo_url(url)
    if not parsed:
        return None
    owner, _ = parsed
    return owner


def _load_index_plugins() -> dict[str, Any]:
    if not INDEX_JSON_PATH.exists():
        return {}
    try:
        loaded = json.loads(INDEX_JSON_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"Unable to parse {INDEX_JSON_PATH.name}: {e}")
    if not isinstance(loaded, dict):
        _fail(f"{INDEX_JSON_PATH.name} must contain a JSON object")
    plugins = loaded.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    return cast(dict[str, Any], plugins)


def _indexed_plugin(plugin_name: str) -> dict[str, Any] | None:
    plugins = _load_index_plugins()
    plugin = plugins.get(plugin_name)
    return cast(dict[str, Any], plugin) if isinstance(plugin, dict) else None


def _pr_author() -> str | None:
    author = os.environ.get("PR_AUTHOR", "").strip()
    return author or None


def _warn_if_non_owner_update_or_delete(plugin_name: str, action: str) -> None:
    indexed = _indexed_plugin(plugin_name)
    if not isinstance(indexed, dict):
        return
    github = indexed.get("github")
    owner = _repo_owner_from_url(github) if isinstance(github, str) else None
    author = _pr_author()
    if not owner or not author:
        return
    if owner.lower() != author.lower():
        _warn(
            f"PR author '@{author}' is not the owner of the indexed plugin repository '@{owner}' "
            f"for plugin '{plugin_name}' during {action}"
        )


def _validate_github_repo_not_in_index(plugin_name: str, url: str) -> None:
    normalized_url = _normalize_repo_url(url)
    if not normalized_url or not INDEX_JSON_PATH.exists():
        return
    plugins = _load_index_plugins()
    for indexed_plugin_name, indexed_plugin in plugins.items():
        if indexed_plugin_name == plugin_name or not isinstance(indexed_plugin, dict):
            continue
        indexed_url = indexed_plugin.get("github")
        if not isinstance(indexed_url, str):
            continue
        normalized_indexed_url = _normalize_repo_url(indexed_url)
        if normalized_indexed_url == normalized_url:
            _fail(
                f"github repository is already present in {INDEX_JSON_PATH.name} "
                f"for plugin '{indexed_plugin_name}'"
            )


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


def _validate_remote_plugin_name(content_obj: dict[str, Any], plugin_name: str) -> None:
    if content_obj.get("encoding") != "base64" or not isinstance(content_obj.get("content"), str):
        _fail("unable to read remote plugin.yaml contents")
    try:
        encoded_content = cast(str, content_obj.get("content"))
        decoded_bytes = base64.b64decode(encoded_content, validate=False)
        decoded_text = decoded_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        _fail(f"unable to decode remote plugin.yaml contents: {e}")
    try:
        remote_yaml = yaml.safe_load(decoded_text)
    except Exception as e:
        _fail(f"remote plugin.yaml is invalid YAML: {e}")
    if not isinstance(remote_yaml, dict):
        _fail("remote plugin.yaml must be a YAML mapping/object")
    remote_name = remote_yaml.get("name")
    if not isinstance(remote_name, str) or not remote_name.strip():
        _fail("remote plugin.yaml must contain non-empty string field 'name'")
    _validate_plugin_name_format(remote_name, context="remote plugin.yaml name")
    if remote_name != plugin_name:
        _fail(f"remote plugin.yaml name must exactly match plugin folder name '{plugin_name}'")


def _validate_github_repo(url: str, plugin_name: str) -> None:
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
    _validate_remote_plugin_name(content_obj, plugin_name)


def _validate_thumbnail(plugin_name: str) -> None:
    commit = _head_sha()
    plugin_files = _git_plugin_files(commit, plugin_name)
    thumbnails = [name for name in plugin_files if Path(name).stem == "thumbnail"]
    if len(thumbnails) > 1:
        _fail("Only one thumbnail file is allowed")
    if not thumbnails:
        return
    thumbnail_name = thumbnails[0]
    thumbnail = Path(thumbnail_name)
    if thumbnail.suffix.lower() not in ALLOWED_IMAGE_EXTS:
        _fail("thumbnail must be png/jpg/jpeg/webp")
    thumbnail_bytes = _git_read_bytes(commit, f"plugins/{plugin_name}/{thumbnail_name}")
    if len(thumbnail_bytes) > THUMBNAIL_MAX_BYTES:
        _fail("thumbnail exceeds 20 KB")
    with Image.open(io.BytesIO(thumbnail_bytes)) as img:
        width, height = img.size
    if width != height:
        _fail("thumbnail must be square")


def _validate_allowed_files(plugin_name: str) -> None:
    commit = _head_sha()
    for name in _git_plugin_files(commit, plugin_name):
        path = Path(name)
        if path.name == INDEX_YAML_NAME:
            continue
        if path.stem == "thumbnail" and path.suffix.lower() in ALLOWED_IMAGE_EXTS:
            continue
        _fail(f"Unexpected file in plugin folder: {path.name}")


def main() -> int:
    entries = _changed_entries()
    paths = _all_changed_paths(entries)
    if not paths:
        _fail("No changed files detected")
    plugin_name = _submission_plugin_name(paths)
    if _is_deletion_pr(entries, plugin_name):
        if _git_path_exists(_head_sha(), f"plugins/{plugin_name}/{INDEX_YAML_NAME}"):
            _fail(f"Deletion PR must remove the plugin directory entirely: plugins/{plugin_name}")
        if _indexed_plugin(plugin_name) is None:
            _fail(f"Cannot delete plugin '{plugin_name}' because it is not present in {INDEX_JSON_PATH.name}")
        _warn_if_non_owner_update_or_delete(plugin_name, "deletion")
        print(f"Validation passed for plugin deletion: {plugin_name}")
        return 0
    if not _git_path_exists(_head_sha(), f"plugins/{plugin_name}/{INDEX_YAML_NAME}"):
        _fail(f"Plugin directory does not exist in PR head: plugins/{plugin_name}")
    if _indexed_plugin(plugin_name) is not None:
        _warn_if_non_owner_update_or_delete(plugin_name, "update")
    meta = _read_plugin_yaml(plugin_name)
    _validate_fields(meta, plugin_name)
    _validate_github_repo_not_in_index(plugin_name, cast(str, meta.get("github")))
    _validate_allowed_files(plugin_name)
    _validate_thumbnail(plugin_name)
    print(f"Validation passed for plugin: {plugin_name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidatePluginSubmissionError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
