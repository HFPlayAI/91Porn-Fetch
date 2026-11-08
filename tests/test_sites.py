from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import viewkey_batch.web as web
from viewkey_batch.models import SiteConfig
from viewkey_batch.sites import (
    SiteBook,
    normalize_base_url,
    normalize_proxy,
    rebind_cookie_domain,
)

client = TestClient(web.app)

EXAMPLE = Path(__file__).resolve().parent.parent / "site.example.json"


@pytest.fixture
def site_env(tmp_path, monkeypatch):
    config_path = tmp_path / "site.json"
    config_path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(web, "CONFIG_PATH", config_path)
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    return config_path


# ---- 地址规范化 ----

def test_normalize_proxy_adds_default_scheme():
    assert normalize_proxy("127.0.0.1:7897") == "http://127.0.0.1:7897"
    assert normalize_proxy(" http://127.0.0.1:7897/ ") == "http://127.0.0.1:7897"
    assert normalize_proxy("") == ""


def test_normalize_proxy_keeps_auth_and_socks():
    assert normalize_proxy("socks5://user:pass@10.0.0.1:1080") == "socks5://user:pass@10.0.0.1:1080"


@pytest.mark.parametrize("value", ["127.0.0.1", "ftp://127.0.0.1:21", "http://:8080"])
def test_normalize_proxy_rejects_bad_input(value):
    with pytest.raises(ValueError):
        normalize_proxy(value)


def test_normalize_base_url_drops_path_and_decorations():
    assert normalize_base_url(" 91porn.com ") == "https://91porn.com"
    assert normalize_base_url("https://a.example.com/some/path?x=1") == "https://a.example.com"
    assert normalize_base_url("http://A.example.com:8080/") == "http://a.example.com:8080"


@pytest.mark.parametrize("value", ["", "ftp://a.com", "https://"])
def test_normalize_base_url_rejects_bad_input(value):
    with pytest.raises(ValueError):
        normalize_base_url(value)


# ---- 站点列表 ----

def test_site_book_seeds_current_site_from_config(site_env):
    book = SiteBook(site_env.parent / "sites.json", site_env)

    assert [entry.base_url for entry in book.snapshot()] == ["https://91porn.com"]
    assert book.active().base_url == "https://91porn.com"


def test_site_book_add_and_switch_rewrites_config_base_url(site_env):
    book = SiteBook(site_env.parent / "sites.json", site_env)
    entry = book.add("备用站", "backup.example.com")

    book.set_active(entry.id)

    assert SiteConfig.load(site_env).base_url == "https://backup.example.com"
    # 其它解析规则原样保留
    assert SiteConfig.load(site_env).category_urls["latest"] == "/v.php?category=mr&viewtype=basic"
    # 重新读取后仍是同一个生效站点
    reloaded = SiteBook(site_env.parent / "sites.json", site_env)
    assert reloaded.active().base_url == "https://backup.example.com"


def test_site_book_rejects_duplicate_host(site_env):
    book = SiteBook(site_env.parent / "sites.json", site_env)
    first = book.add("A", "https://dup.example.com")
    second = book.add("B", "https://dup.example.com/other")

    assert first.id == second.id
    assert len(book.snapshot()) == 2


def test_site_book_cannot_remove_last_site(site_env):
    book = SiteBook(site_env.parent / "sites.json", site_env)

    with pytest.raises(ValueError, match="至少保留一个站点"):
        book.remove(book.active_id)


def test_site_book_remove_active_falls_back(site_env):
    book = SiteBook(site_env.parent / "sites.json", site_env)
    backup = book.add("备用", "https://backup.example.com")
    book.set_active(backup.id)

    book.remove(backup.id)

    assert book.active().base_url == "https://91porn.com"
    assert SiteConfig.load(site_env).base_url == "https://91porn.com"


def test_removed_site_keeps_membership_when_host_still_present(site_env):
    """手工把 site.json 换成新域名后，旧域名仍作为站点保留。"""
    raw = site_env.read_text(encoding="utf-8").replace("https://91porn.com", "https://mirror.example.com")
    site_env.write_text(raw, encoding="utf-8")

    reloaded = SiteBook(site_env.parent / "sites.json", site_env)

    urls = [entry.base_url for entry in reloaded.snapshot()]
    assert "https://mirror.example.com" in urls
    assert reloaded.active().base_url == "https://mirror.example.com"


# ---- API ----

def test_api_sites_crud(site_env):
    created = client.post("/api/sites", json={"name": "镜像 B", "base_url": "b.example.com"})
    assert created.status_code == 200
    site = created.json()["site"]
    assert site["base_url"] == "https://b.example.com"

    switched = client.post("/api/sites/active", json={"site_id": site["id"]})
    assert switched.status_code == 200
    assert switched.json()["active_id"] == site["id"]
    assert SiteConfig.load(site_env).base_url == "https://b.example.com"

    listed = client.get("/api/sites").json()
    assert listed["active_id"] == site["id"]
    assert len(listed["sites"]) == 2

    removed = client.delete(f"/api/sites/{site['id']}")
    assert removed.status_code == 200
    assert removed.json()["active_id"] != site["id"]


def test_api_sites_rejects_bad_url(site_env):
    response = client.post("/api/sites", json={"name": "", "base_url": "ftp://bad.example.com"})
    assert response.status_code == 400


def test_api_config_exposes_sites(site_env):
    payload = client.get("/api/config").json()

    assert payload["base_url"] == "https://91porn.com"
    assert payload["site_id"]
    assert payload["sites"][0]["base_url"] == "https://91porn.com"


def test_catalog_payload_rebases_old_domain_after_switch(site_env, tmp_path, monkeypatch):
    """切站点后，历史条目里的旧域名页面/封面要对齐到新站点，CDN 域名保持不动。"""
    from viewkey_batch.models import VideoItem

    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    isolated = web.Store()
    isolated.add(
        VideoItem(
            "https://91porn.com/view_video.php?viewkey=abc",
            "abc",
            title="Demo",
            thumbnail_url="https://91porn.com/thumb/abc.jpg",
        )
    )
    cdn = VideoItem(
        "https://91porn.com/view_video.php?viewkey=def",
        "def",
        thumbnail_url="https://cdn.other.test/t/def.jpg",
    )
    isolated.add(cdn)
    monkeypatch.setattr(web, "store", isolated)

    config_file = site_env
    config_file.write_text(
        config_file.read_text(encoding="utf-8").replace("https://91porn.com", "https://backup.example.com"),
        encoding="utf-8",
    )

    videos = {item["viewkey"]: item for item in client.get("/api/catalog").json()["videos"]}

    assert videos["abc"]["page_url"].startswith("https://backup.example.com/view_video.php")
    assert videos["abc"]["thumbnail_url"].startswith("https://backup.example.com/thumb/")
    # 非站点域名（CDN）不应该被改写
    assert videos["def"]["thumbnail_url"] == "https://cdn.other.test/t/def.jpg"


# ---- 代理设置 ----

def test_settings_persist_proxy(site_env, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(web.settings_store, "value", web.AppSettings())

    response = client.put(
        "/api/settings",
        json={
            "download_dir": str(tmp_path / "dl"),
            "folder_mode": "flat",
            "prefer_hd": False,
            "workers": 2,
            "fragments": 4,
            "ui_refresh_seconds": 3,
            "proxy": "127.0.0.1:7897",
        },
    )

    assert response.status_code == 200
    assert response.json()["proxy"] == "http://127.0.0.1:7897"
    assert web.app_proxy() == "http://127.0.0.1:7897"


def test_settings_rejects_bad_proxy(site_env, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(web.settings_store, "value", web.AppSettings())

    response = client.put(
        "/api/settings",
        json={
            "download_dir": str(tmp_path / "dl"),
            "folder_mode": "flat",
            "prefer_hd": False,
            "workers": 2,
            "fragments": 4,
            "ui_refresh_seconds": 3,
            "proxy": "127.0.0.1",
        },
    )

    assert response.status_code == 400
    assert "端口" in response.json()["detail"]


@respx.mock
def test_proxy_test_reports_success(site_env):
    route = respx.get("https://91porn.com/").mock(return_value=httpx.Response(200, text="ok"))

    payload = client.post("/api/proxy/test", json={"proxy": "127.0.0.1:7897"}).json()

    assert payload["ok"] is True
    assert route.called


@respx.mock
def test_proxy_test_reports_network_failure(site_env):
    respx.get("https://91porn.com/").mock(side_effect=httpx.ConnectError("boom"))

    payload = client.post("/api/proxy/test", json={"proxy": "127.0.0.1:7897"}).json()

    assert payload["ok"] is False
    assert "连接不上" in payload["message"]


def test_proxy_test_rejects_bad_address(site_env):
    response = client.post("/api/proxy/test", json={"proxy": "not-a-proxy"})
    assert response.status_code == 400


# ---- Cookie 域名迁移 ----

def test_rebind_cookie_domain_follows_new_site(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".91porn.com\tTRUE\t/\tFALSE\t2147483647\tsession\tTOKEN\n",
        encoding="utf-8",
    )

    assert rebind_cookie_domain(path, "https://backup.example.com") is True
    assert ".backup.example.com\tTRUE" in path.read_text(encoding="utf-8")


def test_rebind_cookie_domain_noop_when_unchanged(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".91porn.com\tTRUE\t/\tFALSE\t2147483647\tsession\tTOKEN\n",
        encoding="utf-8",
    )

    assert rebind_cookie_domain(path, "https://91porn.com") is False

# ---- 缩略图代取 ----

def test_api_thumb_fetches_through_backend_and_caches(site_env, tmp_path, monkeypatch):
    """缩略图由后端代取，浏览器不再直连 CDN，因此也会走设置里的代理。"""
    monkeypatch.setattr(web, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(web.settings_store, "value", web.settings_store.snapshot().model_copy(update={"proxy": ""}))
    url = "https://cdn.example.com/thumb/1.jpg"
    payload = b"\xff\xd8\xff" + b"0" * 32

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url).mock(
            return_value=httpx.Response(200, content=payload, headers={"content-type": "image/jpeg"})
        )
        first = client.get("/api/thumb", params={"u": url})
        assert first.status_code == 200
        assert first.headers["content-type"] == "image/jpeg"
        assert first.content == payload
        assert route.call_count == 1

        # 第二次命中本地缓存，不再回源
        second = client.get("/api/thumb", params={"u": url})
        assert second.status_code == 200
        assert second.content == payload
        assert route.call_count == 1

    assert list((tmp_path / "thumbs").iterdir())


@pytest.mark.parametrize("value", ["file:///etc/passwd", "javascript:alert(1)", ""])
def test_api_thumb_rejects_invalid_urls(site_env, value):
    response = client.get("/api/thumb", params={"u": value})
    assert response.status_code == 400
