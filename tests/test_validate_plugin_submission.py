"""Starter unit suite for scripts/validate_plugin_submission.py.

Hermetic by design: git operations run against a throwaway repo fixture,
and the GitHub API / screenshot network calls are stubbed. Covers the
validator's existing rules only — no new rules are introduced here.

Run from the repo root:  python -m pytest tests/ -q
"""

import base64
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from PIL import Image

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import validate_plugin_submission as v  # noqa: E402


# ---------------------------------------------------------------- fixtures ---

def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _commit(root: Path, msg: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def repo_root(tmp_path, monkeypatch):
    """Throwaway git repo standing in for the index repo.

    Module constants and env are patched so the validator reads from this
    repo instead of the real one."""
    root = tmp_path / "index-repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "index.json").write_text(json.dumps({"plugins": {}}))
    base = _commit(root, "init")

    monkeypatch.setattr(v, "REPO_ROOT", root)
    monkeypatch.setattr(v, "PLUGINS_DIR", root / "plugins")
    monkeypatch.setattr(v, "INDEX_JSON_PATH", root / "index.json")
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.delenv("HEAD_SHA", raising=False)
    monkeypatch.delenv("PR_AUTHOR", raising=False)
    return root


def _set_head(monkeypatch, root: Path, msg: str = "pr") -> str:
    head = _commit(root, msg)
    monkeypatch.setenv("HEAD_SHA", head)
    return head


def _write_plugin(root: Path, name: str, **overrides) -> Path:
    d = root / "plugins" / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": "Some plugin",
        "description": "Does something useful.",
        "github": f"https://github.com/owner/{name}-repo",
        **overrides,
    }
    (d / "index.yaml").write_text(yaml.safe_dump(meta))
    return d


def _write_index_json(root: Path, plugins: dict) -> None:
    (root / "index.json").write_text(json.dumps({"plugins": plugins}))


def _commit_index_entry(root: Path, monkeypatch, plugins: dict) -> None:
    """Commit an index.json state into the base (it is not part of the PR)."""
    _write_index_json(root, plugins)
    base = _commit(root, "index entry")
    monkeypatch.setenv("BASE_SHA", base)


def _stub_github(monkeypatch, plugin_name: str):
    """Fake the two GitHub API calls _validate_github_repo makes."""

    def fake_request_json(url: str):
        if url.endswith("/contents/plugin.yaml"):
            content = base64.b64encode(f"name: {plugin_name}\n".encode()).decode()
            return {"type": "file", "encoding": "base64", "content": content}
        return {"full_name": f"owner/{plugin_name}-repo"}

    monkeypatch.setattr(v, "_request_json", fake_request_json)


def _stub_screenshots(monkeypatch, seen: list | None = None):
    def fake_validate_screenshot_url(url: str):
        if seen is not None:
            seen.append(url)

    monkeypatch.setattr(v, "_validate_screenshot_url", fake_validate_screenshot_url)


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _expect_fail(fn, *args, match: str = "", **kwargs):
    with pytest.raises(v.ValidatePluginSubmissionError) as exc:
        fn(*args, **kwargs)
    if match:
        assert match in str(exc.value)


# ------------------------------------------------------- changed entries ------

def test_changed_entries_parses_statuses(repo_root, monkeypatch):
    _write_plugin(repo_root, "alpha")
    _write_plugin(repo_root, "beta")
    base = _commit(repo_root, "add alpha + beta")
    monkeypatch.setenv("BASE_SHA", base)

    (repo_root / "plugins" / "beta" / "index.yaml").write_text(
        yaml.safe_dump({"title": "B2", "description": "y",
                        "github": "https://github.com/owner/beta-repo"}))
    _git(repo_root, "mv", "plugins/alpha/index.yaml", "plugins/alpha/flow.yaml")
    _write_plugin(repo_root, "gamma")
    _set_head(monkeypatch, repo_root, "modify + rename + add")

    entries = v._changed_entries()
    statuses = [s for s, _ in entries]
    assert any(s.startswith("M") for s in statuses)
    assert "A" in statuses
    renames = [p for s, p in entries if s.startswith("R")]
    assert renames and len(renames[0]) == 2  # old + new path


def test_submission_name_accepts_create_update_delete():
    assert v._submission_plugin_name(["plugins/my_plugin/index.yaml"]) == "my_plugin"
    assert v._submission_plugin_name(["plugins/my_plugin/thumbnail.png"]) == "my_plugin"


def test_submission_name_rejects_multiple_folders():
    _expect_fail(
        v._submission_plugin_name,
        ["plugins/foo/index.yaml", "plugins/bar/index.yaml"],
        match="exactly one plugin folder",
    )


def test_submission_name_rejects_non_plugins_path():
    _expect_fail(
        v._submission_plugin_name, ["scripts/foo.py"],
        match="Only files under plugins/",
    )


def test_submission_name_rejects_underscore_prefix():
    _expect_fail(
        v._submission_plugin_name, ["plugins/_draft/index.yaml"],
        match="reserved",
    )


def test_submission_name_rejects_dashes():
    _expect_fail(
        v._submission_plugin_name, ["plugins/my-plugin/index.yaml"],
        match="dashes are not allowed",
    )


def test_rename_within_folder_keeps_single_plugin():
    # git rename entries carry (status, [old_path, new_path])
    name = v._submission_plugin_name(
        ["plugins/my_plugin/thumbnail.png", "plugins/my_plugin/thumbnail.jpg"]
    )
    assert name == "my_plugin"


def test_is_deletion_pr():
    entries = [("D", ["plugins/gone/index.yaml"])]
    assert v._is_deletion_pr(entries, "gone") is True
    mixed = [("D", ["plugins/gone/index.yaml"]), ("M", ["plugins/gone/other"])]
    assert v._is_deletion_pr(mixed, "gone") is False
    assert v._is_deletion_pr([], "gone") is False
    wrong_folder = [("D", ["plugins/other/index.yaml"])]
    assert v._is_deletion_pr(wrong_folder, "gone") is False


# ----------------------------------------------------------------- fields ---

def test_fields_accept_valid(monkeypatch):
    monkeypatch.setattr(v, "_validate_github_repo", lambda url, name: None)
    seen: list = []
    _stub_screenshots(monkeypatch, seen)
    v._validate_fields(
        {
            "title": "T",
            "description": "D",
            "github": "https://github.com/owner/repo",
            "tags": ["a", "b"],
            "screenshots": ["https://example.com/shot.png"],
        },
        "my_plugin",
    )
    assert seen == ["https://example.com/shot.png"]


def test_fields_reject_unknown_field(monkeypatch):
    monkeypatch.setattr(v, "_validate_github_repo", lambda url, name: None)
    _expect_fail(
        v._validate_fields,
        {"title": "T", "description": "D", "github": "https://github.com/o/r",
         "bogus": 1},
        "my_plugin",
        match="unsupported fields",
    )


def test_fields_reject_missing_required(monkeypatch):
    monkeypatch.setattr(v, "_validate_github_repo", lambda url, name: None)
    _expect_fail(
        v._validate_fields,
        {"title": "T", "github": "https://github.com/o/r"},
        "my_plugin",
        match="missing required",
    )
    _expect_fail(
        v._validate_fields,
        {"title": "T", "description": "  ", "github": "https://github.com/o/r"},
        "my_plugin",
        match="missing required",
    )


def test_fields_reject_overlong_title_and_description(monkeypatch):
    monkeypatch.setattr(v, "_validate_github_repo", lambda url, name: None)
    _expect_fail(
        v._validate_fields,
        {"title": "x" * 51, "description": "D", "github": "https://github.com/o/r"},
        "my_plugin",
        match="title exceeds",
    )
    _expect_fail(
        v._validate_fields,
        {"title": "T", "description": "x" * 501, "github": "https://github.com/o/r"},
        "my_plugin",
        match="description exceeds",
    )


def test_fields_reject_bad_tags(monkeypatch):
    monkeypatch.setattr(v, "_validate_github_repo", lambda url, name: None)
    base = {"title": "T", "description": "D", "github": "https://github.com/o/r"}
    _expect_fail(v._validate_fields, {**base, "tags": "nope"}, "p",
                 match="tags must be a list")
    _expect_fail(v._validate_fields, {**base, "tags": ["ok", ""]}, "p",
                 match="tags must be a list")
    _expect_fail(v._validate_fields, {**base, "tags": [f"t{i}" for i in range(6)]},
                 "p", match="at most 5")


# -------------------------------------------------------------- github url ---

def test_parse_repo_url_failures():
    assert v._parse_repo_url("not a url") is None
    assert v._parse_repo_url("https://gitlab.com/owner/repo") is None
    assert v._parse_repo_url("https://github.com/onlyowner") is None
    assert v._parse_repo_url("") is None


def test_parse_repo_url_accepts_variants():
    assert v._parse_repo_url("https://github.com/Owner/Repo.git") == ("Owner", "Repo")
    assert v._parse_repo_url("http://github.com/o/r/") == ("o", "r")
    # Host must already be lowercase github.com; owner/repo are normalized.
    assert v._parse_repo_url("https://GitHub.com/Owner/Repo") is None
    assert v._normalize_repo_url("https://github.com/Owner/Repo") == \
        "https://github.com/owner/repo"


def test_validate_github_repo_rejects_non_github_url():
    _expect_fail(
        v._validate_github_repo, "https://example.com/owner/repo", "my_plugin",
        match="must be a valid GitHub repository URL",
    )


def test_validate_github_repo_inaccessible(monkeypatch):
    def boom(url: str):
        raise v.ValidatePluginSubmissionError(
            "GitHub API request failed (404) GET " + url + ": Not Found"
        )

    monkeypatch.setattr(v, "_request_json", boom)
    _expect_fail(
        v._validate_github_repo, "https://github.com/owner/missing", "my_plugin",
        match="404",
    )


def test_validate_github_repo_name_must_match_folder(monkeypatch):
    def fake_request_json(url: str):
        if url.endswith("/contents/plugin.yaml"):
            content = base64.b64encode(b"name: other_name\n").decode()
            return {"type": "file", "encoding": "base64", "content": content}
        return {"full_name": "owner/repo"}

    monkeypatch.setattr(v, "_request_json", fake_request_json)
    _expect_fail(
        v._validate_github_repo, "https://github.com/owner/repo", "my_plugin",
        match="must exactly match plugin folder name",
    )


# -------------------------------------------------------------- screenshots --

def test_screenshot_urls_list_rules(monkeypatch):
    _stub_screenshots(monkeypatch)  # network stub; rules below are pre-network
    _expect_fail(v._validate_screenshot_urls, "https://x/y.png",
                 match="must be a list")
    _expect_fail(v._validate_screenshot_urls,
                 [f"https://x/{i}.png" for i in range(6)], match="at most 5")
    _expect_fail(v._validate_screenshot_urls, ["https://x/y.png", 42],
                 match="non-empty strings")

    seen: list = []
    _stub_screenshots(monkeypatch, seen)
    v._validate_screenshot_urls(["https://x/a.png", "https://x/b.jpg"])
    assert seen == ["https://x/a.png", "https://x/b.jpg"]


def test_screenshot_url_rejects_bad_scheme_and_extension():
    # Fails before any network access.
    _expect_fail(v._validate_screenshot_url, "ftp://x/y.png",
                 match="full http/https URLs")
    _expect_fail(v._validate_screenshot_url, "https://x/y.gif",
                 match="png/jpg/jpeg/webp")


# --------------------------------------------------------------- thumbnails --

def _commit_thumbnail(repo_root, monkeypatch, name: str, data: bytes,
                      filename: str = "thumbnail.png") -> None:
    d = _write_plugin(repo_root, name)
    (d / filename).write_bytes(data)
    _set_head(monkeypatch, repo_root, "add thumbnail")


def test_thumbnail_absent_ok(repo_root, monkeypatch):
    _write_plugin(repo_root, "no_thumb")
    _set_head(monkeypatch, repo_root)
    v._validate_thumbnail("no_thumb")  # no thumbnail is allowed


def test_thumbnail_square_ok(repo_root, monkeypatch):
    _commit_thumbnail(repo_root, monkeypatch, "sq", _png(64, 64))
    v._validate_thumbnail("sq")


def test_thumbnail_rejects_non_square(repo_root, monkeypatch):
    _commit_thumbnail(repo_root, monkeypatch, "rect", _png(64, 32))
    _expect_fail(v._validate_thumbnail, "rect", match="must be square")


def test_thumbnail_rejects_oversize(repo_root, monkeypatch):
    big = _png(64, 64) + b"\x00" * (v.THUMBNAIL_MAX_BYTES + 1)
    _commit_thumbnail(repo_root, monkeypatch, "big", big)
    _expect_fail(v._validate_thumbnail, "big", match="exceeds 20 KB")


def test_thumbnail_rejects_bad_extension(repo_root, monkeypatch):
    _commit_thumbnail(repo_root, monkeypatch, "gif", _png(64, 64),
                      filename="thumbnail.gif")
    _expect_fail(v._validate_thumbnail, "gif", match="png/jpg/jpeg/webp")


def test_thumbnail_rejects_multiple(repo_root, monkeypatch):
    d = _write_plugin(repo_root, "two")
    (d / "thumbnail.png").write_bytes(_png(64, 64))
    (d / "thumbnail.jpg").write_bytes(_png(64, 64))
    _set_head(monkeypatch, repo_root)
    _expect_fail(v._validate_thumbnail, "two", match="Only one thumbnail")


# ------------------------------------------------------- duplicate repos ------

def test_duplicate_repo_rejected(repo_root):
    _write_index_json(repo_root, {
        "other_plugin": {"github": "https://github.com/Owner/Repo"},
    })
    _expect_fail(
        v._validate_github_repo_not_in_index,
        "my_plugin", "https://github.com/owner/repo.git",
        match="already present",
    )


def test_same_plugin_update_allowed(repo_root):
    _write_index_json(repo_root, {
        "my_plugin": {"github": "https://github.com/owner/repo"},
    })
    v._validate_github_repo_not_in_index(
        "my_plugin", "https://github.com/owner/repo")  # no raise


def test_distinct_repo_allowed(repo_root):
    _write_index_json(repo_root, {
        "other_plugin": {"github": "https://github.com/owner/other"},
    })
    v._validate_github_repo_not_in_index(
        "my_plugin", "https://github.com/owner/repo")  # no raise


# ------------------------------------------------------------ head handling ---

def test_main_requires_base_and_head_sha(repo_root, monkeypatch):
    monkeypatch.delenv("BASE_SHA")
    with pytest.raises(v.ValidatePluginSubmissionError,
                       match="BASE_SHA and HEAD_SHA are required"):
        v.main()


def test_main_reads_from_head_not_worktree(repo_root, monkeypatch):
    """Untrusted-head handling: a dirty working tree must not affect the
    verdict — everything is read from HEAD_SHA."""
    _write_plugin(repo_root, "my_plugin")
    _set_head(monkeypatch, repo_root)
    _stub_github(monkeypatch, "my_plugin")
    _stub_screenshots(monkeypatch)

    # Tamper with the working tree after the head commit.
    (repo_root / "plugins" / "my_plugin" / "index.yaml").write_text(
        yaml.safe_dump({"title": "x" * 999}))

    assert v.main() == 0


def test_main_create_passes(repo_root, monkeypatch, capsys):
    _write_plugin(repo_root, "my_plugin")
    _set_head(monkeypatch, repo_root)
    _stub_github(monkeypatch, "my_plugin")
    _stub_screenshots(monkeypatch)

    assert v.main() == 0
    assert "Validation passed for plugin: my_plugin" in capsys.readouterr().out


def test_main_update_passes(repo_root, monkeypatch, capsys):
    _write_plugin(repo_root, "my_plugin")
    _set_head(monkeypatch, repo_root, "create")
    _commit_index_entry(repo_root, monkeypatch, {
        "my_plugin": {"github": "https://github.com/owner/my_plugin-repo"},
    })
    (repo_root / "plugins" / "my_plugin" / "index.yaml").write_text(
        yaml.safe_dump({
            "title": "Updated title",
            "description": "Does something useful.",
            "github": "https://github.com/owner/my_plugin-repo",
        }))
    _set_head(monkeypatch, repo_root, "update")
    _stub_github(monkeypatch, "my_plugin")
    _stub_screenshots(monkeypatch)

    assert v.main() == 0
    assert "Validation passed for plugin: my_plugin" in capsys.readouterr().out


def test_main_delete_passes(repo_root, monkeypatch, capsys):
    _write_plugin(repo_root, "my_plugin")
    _set_head(monkeypatch, repo_root, "create")
    _commit_index_entry(repo_root, monkeypatch, {
        "my_plugin": {"github": "https://github.com/owner/my_plugin-repo"},
    })

    _git(repo_root, "rm", "-qr", "plugins/my_plugin")
    _set_head(monkeypatch, repo_root, "delete")

    assert v.main() == 0
    assert "Validation passed for plugin deletion: my_plugin" in capsys.readouterr().out


def test_main_delete_rejects_partial_removal(repo_root, monkeypatch):
    _write_plugin(repo_root, "my_plugin")
    _set_head(monkeypatch, repo_root, "create")
    _commit_index_entry(repo_root, monkeypatch, {
        "my_plugin": {"github": "https://github.com/owner/my_plugin-repo"},
    })

    # index.yaml still in head -> not a clean deletion; the stray file trips
    # the allowed-files rule instead.
    (repo_root / "plugins" / "my_plugin" / "extra.txt").write_text("x")
    _set_head(monkeypatch, repo_root, "not a deletion")
    _stub_github(monkeypatch, "my_plugin")
    _stub_screenshots(monkeypatch)

    _expect_fail(v.main, match="Unexpected file in plugin folder")
