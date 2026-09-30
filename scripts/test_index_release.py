"""Offline regression check: python scripts/test_index_release.py."""

import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import download_index_release as download
import publish_index_release as publish


OLD = b'{"version":1,"plugins":{"kept":{},"removed":{}}}'
NEW = b'{"version":1,"plugins":{"kept":{}}}'


def check(initial: dict[str, bytes], fault: str = "", content: bytes = NEW) -> None:
    assets = {}
    contents = {}
    faults = []
    mutations = []
    serial = 0

    def add(name: str, data: bytes) -> dict:
        nonlocal serial
        serial += 1
        asset = {"id": serial, "name": name, "state": "uploaded", "size": len(data)}
        assets[serial] = asset
        contents[serial] = data
        return asset

    for name, data in initial.items():
        add(name, data)

    def error(request, code: int) -> None:
        raise urllib.error.HTTPError(request.full_url, code, "Injected failure", {}, io.BytesIO(b'{}'))

    def request(request, timeout):
        url = urllib.parse.urlparse(request.full_url)
        method = request.get_method()
        if method != "GET":
            mutations.append(method)
        if url.hostname == "download.test":
            if not url.query:
                return io.BytesIO(OLD)
            return io.BytesIO(contents[int(url.path.strip("/"))])
        if "/releases/assets/" in url.path:
            asset_id = int(url.path.rsplit("/", 1)[1])
            if asset_id not in assets:
                error(request, 404)
            asset = assets[asset_id]
            if method == "DELETE":
                assert asset["name"] != "index.json", "The current index must never be deleted"
                del assets[asset_id]
                del contents[asset_id]
                if fault == "delete_502" and not faults:
                    faults.append(fault)
                    error(request, 502)
                return io.BytesIO()
            if method == "PATCH":
                name = json.loads(request.data)["name"]
                if fault == "promotion_failure" and asset["name"] == "index.json.next":
                    faults.append(fault)
                    error(request, 502)
                assert all(a["id"] == asset_id or a["name"] != name for a in assets.values())
                asset["name"] = name
                if fault == "rename_502" and not faults:
                    faults.append(fault)
                    error(request, 502)
            return io.BytesIO(json.dumps(asset).encode())
        if method == "POST" and url.hostname == "uploads.github.com":
            assert "index.json" not in urllib.parse.parse_qs(url.query)["name"]
            if fault == "upload_failure":
                faults.append(fault)
                error(request, 502)
            asset = add(urllib.parse.parse_qs(url.query)["name"][0], request.data)
            if fault == "incomplete_upload":
                faults.append(fault)
                asset["state"] = "starter"
            return io.BytesIO(json.dumps(asset).encode())
        assert method == "GET", (method, url.path)
        payload = {
            "id": 1,
            "upload_url": "https://uploads.github.com/repos/test/repo/releases/1/assets{?name,label}",
            "assets": [dict(a, browser_download_url=f'https://download.test/{a["id"]}') for a in assets.values()],
        }
        return io.BytesIO(json.dumps(payload).encode())

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        index = root / "index.json"
        with (
            patch.dict(os.environ, {"GITHUB_REPOSITORY": "test/repo", "GITHUB_TOKEN": "test"}, clear=True),
            patch.object(publish, "INDEX_PATH", index),
            patch.object(download, "INDEX_PATH", index),
            patch.object(download, "REPO_ROOT", root),
            patch("urllib.request.urlopen", request),
            patch("time.sleep"),
            redirect_stdout(io.StringIO()),
        ):
            if initial:
                assert download.main() == 0
                expected = next(initial[name] for name in ("index.json", "index.json.next", "index.json.previous") if name in initial)
                assert index.read_bytes() == expected
            index.write_bytes(content)
            failed = False
            try:
                assert publish.main() == 0
            except publish.PublishReleaseError:
                failed = True
            assert failed == (fault not in ("", "rename_502", "delete_502")), fault
            current = next(a for a in assets.values() if a["name"] == "index.json")
            assert contents[current["id"]] == (OLD if failed else content), fault
            if fault == "invalid_index":
                assert not mutations
            elif fault:
                assert faults, f"Fault was not exercised: {fault}"
            if not failed:
                assert download.main() == 0
                assert index.read_bytes() == content


def check_rebuild() -> None:
    import yaml

    repo = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((repo / ".github/workflows/generate-plugin-state.yml").read_text())
    script = next(step["run"] for step in workflow["jobs"]["refresh"]["steps"] if step["name"] == "Sync plugin state")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "plugins").symlink_to(repo / "plugins", target_is_directory=True)
        (root / "scripts").mkdir()
        (root / "scripts/sync_plugin_state.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(repo / 'scripts')!r})\n"
            "import sync_plugin_state as sync\n"
            "sync.INDEX_JSON_PATH = Path('index.json')\n"
            "sync._get_repo_and_category = lambda *args: ('repo', 'category')\n"
            "sync._sync_existing_plugin = lambda *args: ('updated', 'https://example.com/discussion')\n"
            "sync._sync_deleted_plugin = lambda *args: 'missing'\n"
            "raise SystemExit(sync.main())\n"
        )
        (root / "index.json").write_text('{"plugins":{"removed_plugin":{}}}')
        env = {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "REBUILD_INDEX": "true",
            "GITHUB_REPOSITORY": "test/repo",
            "GITHUB_SHA": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
            "PLUGIN_NAMES": "does_not_exist",
            "BEFORE_SHA": "invalid-sha",
            "AFTER_SHA": "invalid-sha",
            "START_FROM": "9999",
            "MAX_PLUGINS": "1000",
        }
        result = subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        rebuilt = json.loads((root / "index.json").read_text())["plugins"]
        assert "removed_plugin" not in rebuilt
        assert rebuilt
        print(result.stdout.splitlines()[-1])


def main() -> None:
    check({"index.json": OLD})
    check({"index.json": OLD}, "rename_502")
    check({"index.json": OLD}, "upload_failure")
    check({"index.json": OLD}, "incomplete_upload")
    check({"index.json": OLD}, "promotion_failure")
    check({"index.json": OLD, "index.json.previous": OLD}, "delete_502")
    check({"index.json.next": NEW, "index.json.previous": OLD})
    check({"index.json.previous": OLD})
    check({})
    check({"index.json": OLD}, "invalid_index", b'{"error":"Not Found"}')
    check({"index.json": OLD}, "invalid_index", b'{"plugins":{}}')
    check_rebuild()
    print("Index publication and recovery checks passed.")


if __name__ == "__main__":
    main()
