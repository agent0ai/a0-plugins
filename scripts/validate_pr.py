import os
import re
import subprocess
import sys
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


class ValidationError(Exception):
    pass


def _run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=REPO_ROOT)
    return out.decode("utf-8", errors="replace")


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


def _validate_yaml(plugin_yaml: Path) -> None:
    loaded = None
    try:
        loaded = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"Invalid YAML in {plugin_yaml.relative_to(REPO_ROOT)}: {e}")

    data = loaded

    if not isinstance(data, dict):
        _fail(f"{plugin_yaml.relative_to(REPO_ROOT)} must contain a YAML mapping/object")

    keys = set(data.keys())
    extra = keys - ALLOWED_YAML_KEYS
    missing = REQUIRED_YAML_KEYS - keys

    if extra:
        _fail(
            f"{plugin_yaml.relative_to(REPO_ROOT)} contains unsupported fields: {sorted(extra)}. "
            f"Allowed fields are: {sorted(ALLOWED_YAML_KEYS)}"
        )
    if missing:
        _fail(
            f"{plugin_yaml.relative_to(REPO_ROOT)} is missing required fields: {sorted(missing)}"
        )

    for k in REQUIRED_YAML_KEYS:
        v = data.get(k)
        if not isinstance(v, str) or not v.strip():
            _fail(f"{plugin_yaml.relative_to(REPO_ROOT)} field '{k}' must be a non-empty string")

    github = data.get("github")
    if isinstance(github, str) and not re.match(r"^https?://", github.strip()):
        _fail(
            f"{plugin_yaml.relative_to(REPO_ROOT)} field 'github' must be a valid http(s) URL"
        )

    if "tags" in data:
        tags = data.get("tags")
        if tags is None:
            _fail(f"{plugin_yaml.relative_to(REPO_ROOT)} field 'tags' must be a list of strings")
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            _fail(f"{plugin_yaml.relative_to(REPO_ROOT)} field 'tags' must be a list of strings")
        tags_list = cast(list[str], tags)
        if len(tags_list) > MAX_TAGS:
            _fail(
                f"{plugin_yaml.relative_to(REPO_ROOT)} field 'tags' must contain at most {MAX_TAGS} entries"
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

    plugin_root = REPO_ROOT / plugin_root_rel
    if not plugin_root.exists() or not plugin_root.is_dir():
        _fail(f"Plugin folder does not exist: {plugin_root_rel.as_posix()}")

    plugin_yaml = plugin_root / "plugin.yaml"
    if not plugin_yaml.exists():
        _fail(f"Missing required file: {plugin_yaml.relative_to(REPO_ROOT)}")

    thumbnails: list[Path] = []
    for child in plugin_root.iterdir():
        if child.is_dir():
            _fail(
                f"No subdirectories are allowed inside a plugin folder: {child.relative_to(REPO_ROOT)}"
            )
        if child.name == "plugin.yaml":
            continue
        if child.suffix.lower() in ALLOWED_IMAGE_EXTS:
            if child.stem.lower() != THUMBNAIL_BASENAME:
                _fail(
                    f"Thumbnail must be named '{THUMBNAIL_BASENAME}<ext>' (e.g. thumbnail.png). "
                    f"Found: {child.relative_to(REPO_ROOT)}"
                )
            thumbnails.append(child)
            continue
        _fail(
            f"Unsupported file in plugin folder: {child.relative_to(REPO_ROOT)}. "
            "Only plugin.yaml and an optional thumbnail image are allowed."
        )

    if len(thumbnails) > 1:
        _fail(
            "At most one thumbnail image is allowed. Found: "
            + ", ".join(t.relative_to(REPO_ROOT).as_posix() for t in thumbnails)
        )

    _validate_yaml(plugin_yaml)

    if thumbnails:
        _validate_thumbnail(thumbnails[0])

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
