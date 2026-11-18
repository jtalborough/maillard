import re
from hashlib import sha256

from fastapi.testclient import TestClient

from maillard.core.config import MaillardConfig
from maillard.mcp.state import ApplicationState
from maillard.web import app as app_module


def dashboard_client():
    return TestClient(app_module.create_app(ApplicationState(config=MaillardConfig())))


def asset_url(content: bytes, path: str) -> str:
    return f"/assets/{sha256(content).hexdigest()[:16]}/{path}"


def test_dashboard_serves_fingerprinted_scripts_from_both_entry_points():
    client = dashboard_client()
    root = client.get("/")
    index = client.get("/index.html")
    assert root.status_code == index.status_code == 200
    assert root.text == index.text
    assert client.head("/index.html").headers["cache-control"] == "no-store"
    assert root.headers["cache-control"] == index.headers["cache-control"] == "no-store"
    assert 'src="https://cdn.jsdelivr.net/npm/chart.js@4"' in root.text

    paths = re.findall(
        r'<script src="(js/[^"]+\.js)"', (app_module._STATIC_DIR / "index.html").read_text("utf-8")
    )
    assert {"js/history.js", "js/roast.js"}.issubset(paths)
    for path in paths:
        content = (app_module._STATIC_DIR / path).read_bytes()
        url = asset_url(content, path)
        assert f'src="{url}"' in root.text
        assert f'src="{path}"' not in root.text
        asset = client.get(url)
        assert asset.status_code == 200
        assert asset.content == content
        assert asset.headers["content-type"].startswith("text/javascript")
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_rebuilt_dashboard_uses_new_urls_for_changed_js_and_css(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_STATIC_DIR", tmp_path)
    (tmp_path / "index.html").write_text(
        """<link rel="stylesheet" href='/dashboard.css'>"""
        """<script src="dashboard.js"></script>""",
        encoding="utf-8",
    )
    paths = {
        "dashboard.js": (b"let build = 1", b"let build = 2"),
        "dashboard.css": (b"body{color:red}", b"body{color:blue}"),
    }
    for path, (old, _) in paths.items():
        (tmp_path / path).write_bytes(old)
    old_build = dashboard_client()
    old_index = old_build.get("/index.html").text

    for path, (_, new) in paths.items():
        (tmp_path / path).write_bytes(new)
    new_build = dashboard_client()
    new_index = new_build.get("/").text

    for path, (old, new) in paths.items():
        old_url = asset_url(old, path)
        new_url = asset_url(new, path)
        assert old_url in old_index and new_url in new_index
        assert old_url not in new_index
        assert old_build.get(old_url).content == old
        stale = new_build.get(old_url)
        assert stale.status_code == 404
        assert stale.headers["cache-control"] == "no-store"
        assert new_build.get(new_url).content == new
    assert (
        new_build.get(asset_url(paths["dashboard.css"][1], "dashboard.css"))
        .headers["content-type"]
        .startswith("text/css")
    )
