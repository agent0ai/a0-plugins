import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn, cast

import yaml
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

ALLOWED_YAML_KEYS = {"title", "description", "github", "tags"}
REQUIRED_YAML_KEYS = {"title", "description", "github"}
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_BYTES = 20 * 1024
MAX_TAGS = 5
THUMBNAIL_BASENAME = "thumbnail"
MAX_TITLE_LENGTH = 50
MAX_DESCRIPTION_LENGTH = 500


class ValidationError(Exception):
    pass


def _run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=REPO_ROOT)
    return out.decode("utf-8", errors="replace")


def _run_bytes(cmd: list[str]) -> bytes:
    return subprocess.check_output(cmd, cwd=REPO_ROOT)


def _git_ls_tree_names(commit: str, path: str) -> list[str]:
    raw = _run(["git", "ls-tree", "-r", "--name-only", commit, "--", path])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _git_show_bytes(commit: str, file_path: str) -> bytes:
    try:
        return _run_bytes(["git", "show", f"{commit}:{file_path}"])
    except subprocess.CalledProcessError as e:
        _fail(f"Unable to read {file_path} at {commit}: {e}")


def _get_changed_files(base_sha: str, head_sha: str) -> list[tuple[str, str]]:
    # Use name-status so we can detect deleted/renamed files.
    raw = _run(["git", "diff", "--name-status", f"{base_sha}..{head_sha}"])
    changes: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        # For renames/copies, the format is: R100\told\tnew
        # For others: A|M|D\tpath
        path = parts[-1]
        changes.append((status, path))
    return changes


def _fail(msg: str) -> NoReturn:
    raise ValidationError(msg)


def _validate_yaml_bytes(plugin_yaml_path: str, content: bytes) -> None:
    loaded = None
    try:
        loaded = yaml.safe_load(content.decode("utf-8", errors="strict"))
    except Exception as e:
        _fail(f"Invalid YAML in {plugin_yaml_path}: {e}")

    data = loaded

    if not isinstance(data, dict):
        _fail(f"{plugin_yaml_path} must contain a YAML mapping/object")

    keys = set(data.keys())
    extra = keys - ALLOWED_YAML_KEYS
    missing = REQUIRED_YAML_KEYS - keys

    if extra:
        _fail(
            f"{plugin_yaml_path} contains unsupported fields: {sorted(extra)}. "
            f"Allowed fields are: {sorted(ALLOWED_YAML_KEYS)}"
        )
    if missing:
        _fail(
            f"{plugin_yaml_path} is missing required fields: {sorted(missing)}"
        )

    for k in REQUIRED_YAML_KEYS:
        v = data.get(k)
        if not isinstance(v, str) or not v.strip():
            _fail(f"{plugin_yaml_path} field '{k}' must be a non-empty string")

    title = data.get("title")
    if isinstance(title, str) and len(title) > MAX_TITLE_LENGTH:
        _fail(
            f"{plugin_yaml_path} field 'title' must be at most {MAX_TITLE_LENGTH} characters"
        )

    description = data.get("description")
    if isinstance(description, str) and len(description) > MAX_DESCRIPTION_LENGTH:
        _fail(
            f"{plugin_yaml_path} field 'description' must be at most {MAX_DESCRIPTION_LENGTH} characters"
        )

    github = data.get("github")
    if isinstance(github, str) and not re.match(r"^https?://", github.strip()):
        _fail(
            f"{plugin_yaml_path} field 'github' must be a valid http(s) URL"
        )

    if isinstance(github, str):
        _validate_github_repo(github.strip())

    if "tags" in data:
        tags = data.get("tags")
        if tags is None:
            _fail(f"{plugin_yaml_path} field 'tags' must be a list of strings")
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            _fail(f"{plugin_yaml_path} field 'tags' must be a list of strings")
        tags_list = cast(list[str], tags)
        if len(tags_list) > MAX_TAGS:
            _fail(
                f"{plugin_yaml_path} field 'tags' must contain at most {MAX_TAGS} entries"
            )


def _validate_thumbnail(image_path: Path) -> None:
    if image_path.suffix.lower() not in ALLOWED_IMAGE_EXTS:
        _fail(
            f"Thumbnail must be one of {sorted(ALLOWED_IMAGE_EXTS)}: {image_path.relative_to(REPO_ROOT)}"
        )

    size = image_path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        _fail(
            f"Thumbnail is too large ({size} bytes). Max is {MAX_IMAGE_BYTES} bytes: {image_path.relative_to(REPO_ROOT)}"
        )

    try:
        with Image.open(image_path) as im:
            w, h = im.size
    except Exception as e:
        _fail(f"Thumbnail image could not be opened: {image_path.relative_to(REPO_ROOT)}: {e}")

    if w != h:
        _fail(
            f"Thumbnail must be square (width == height). Got {w}x{h}: {image_path.relative_to(REPO_ROOT)}"
        )


def _github_api_get_json(url: str) -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "a0-plugins-validator",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"GitHub API request failed ({e.code}) for {url}: {msg}")
    except Exception as e:
        _fail(f"GitHub API request failed for {url}: {e}")

    try:
        return cast(dict, __import__("json").loads(payload))
    except Exception as e:
        _fail(f"GitHub API returned invalid JSON for {url}: {e}")


def _parse_github_repo_url(repo_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(repo_url)
    if parsed.netloc.lower() != "github.com":
        _fail(f"github field must point to github.com: {repo_url}")

    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        _fail(f"github field must be a GitHub repository URL like https://github.com/<owner>/<repo>: {repo_url}")

    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    return owner, repo


def _validate_github_repo(repo_url: str) -> None:
    owner, repo = _parse_github_repo_url(repo_url)

    repo_info = _github_api_get_json(f"https://api.github.com/repos/{owner}/{repo}")
    default_branch = repo_info.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        _fail(f"Unable to determine default branch for GitHub repo: {repo_url}")

    _github_api_get_json(
        f"https://api.github.com/repos/{owner}/{repo}/contents/plugin.yaml?ref={urllib.parse.quote(default_branch)}"
    )


def main() -> int:
    base_sha = os.environ.get("BASE_SHA")
    head_sha = os.environ.get("HEAD_SHA")
    if not base_sha or not head_sha:
        _fail("BASE_SHA and HEAD_SHA environment variables are required")

    base_sha = cast(str, base_sha)
    head_sha = cast(str, head_sha)

    changes = _get_changed_files(base_sha, head_sha)
    if not changes:
        _fail("No changed files detected")

    changed_paths: list[Path] = []
    for _, p in changes:
        # Normalize to posix-like path fragments.
        changed_paths.append(Path(p))

    # Only allow modifications within exactly one plugin folder.
    plugin_roots: set[Path] = set()
    for p in changed_paths:
        parts = p.parts
        if len(parts) < 3 or parts[0] != "plugins":
            _fail(
                "PRs must only change files under plugins/<plugin-name>/. "
                f"Found change outside plugins/: {p.as_posix()}"
            )
        plugin_roots.add(Path(parts[0]) / parts[1])

    if len(plugin_roots) != 1:
        _fail(
            f"PR must submit exactly one plugin folder. Found: {sorted(pr.as_posix() for pr in plugin_roots)}"
        )

    plugin_root_rel = next(iter(plugin_roots))
    plugin_name = plugin_root_rel.parts[1]

    if plugin_name.startswith("_"):
        _fail(
            f"Plugin folder '{plugin_name}' starts with '_' which is reserved and not visible in Agent Zero"
        )

    plugin_root_path = plugin_root_rel.as_posix()
    plugin_files = _git_ls_tree_names(head_sha, plugin_root_path)
    if not plugin_files:
        _fail(f"Plugin folder does not exist in PR head: {plugin_root_path}")

    plugin_yaml_path = f"{plugin_root_path}/plugin.yaml"
    if plugin_yaml_path not in plugin_files:
        _fail(f"Missing required file in PR head: {plugin_yaml_path}")

    # Validate no extra files and optional thumbnail naming.
    thumbnails: list[str] = []
    for f in plugin_files:
        name = f.split("/")[-1]
        if name == "plugin.yaml":
            continue
        suffix = Path(name).suffix.lower()
        if suffix in ALLOWED_IMAGE_EXTS:
            stem = Path(name).stem.lower()
            if stem != THUMBNAIL_BASENAME:
                _fail(
                    f"Thumbnail must be named '{THUMBNAIL_BASENAME}<ext>' (e.g. thumbnail.png). Found: {f}"
                )
            thumbnails.append(f)
            continue
        _fail(
            f"Unsupported file in plugin folder: {f}. Only plugin.yaml and an optional thumbnail image are allowed."
        )

    if len(thumbnails) > 1:
        _fail("At most one thumbnail image is allowed. Found: " + ", ".join(thumbnails))

    plugin_yaml_bytes = _git_show_bytes(head_sha, plugin_yaml_path)
    _validate_yaml_bytes(plugin_yaml_path, plugin_yaml_bytes)

    if thumbnails:
        thumb_path = thumbnails[0]
        thumb_bytes = _git_show_bytes(head_sha, thumb_path)
        with tempfile.NamedTemporaryFile(suffix=Path(thumb_path).suffix, delete=False) as tmp:
            tmp.write(thumb_bytes)
            tmp_path = Path(tmp.name)
        try:
            _validate_thumbnail(tmp_path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # Ensure the PR didn't delete required files.
    deleted = [p for status, p in changes if status.startswith("D")]
    if deleted:
        _fail(f"PR must not delete files. Deleted: {deleted}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as e:
        print(f"Validation failed: {e}")
        raise SystemExit(1)
