from pathlib import Path
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import viewkey_batch.web as web
from viewkey_batch.cache import ListingCache, cache_key
from viewkey_batch.models import SiteConfig, VideoItem
from viewkey_batch.parser import parse_card_fields, strip_site_suffix

client = TestClient(web.app)

EXAMPLE = Path(__file__).resolve().parent.parent / "site.example.json"

# 结构与真实站点一致：well 容器 + 作者/热度标签 + 页码链接
LISTING_HTML = """
<html><body>
<div class="well well-sm">
  <a href="/view_video.php?viewkey=aaa111bbb222">
    <img class="img-responsive" src="https://cdn.test/thumb/1.jpg">
    <span class="duration">00:01:40</span>
    <span class="video-title">KTV 测试标题</span>
  </a>
  <span class="info">作者:</span> 牛来牛来 <span class="info">热度:</span> 438
</div>
<a href="?search_id=KTV&amp;page=2">2</a>
</body></html>
"""

VIDEO_HTML = """
<html><head><title>演示视频 - 91porn</title></head>
<body><a href="uprofile.php?UID=u1"><span class="title">作者甲</span></a></body></html>
"""

INDEX_RE = r"https://91porn\.com/index\.php.*"
SEARCH_RE = r"https://91porn\.com/search_result\.php.*"
BROWSE_RE = r"https://91porn\.com/v\.php.*"
VIDEO_RE = r"https://91porn\.com/view_video\.php.*"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """独立配置 + 独立数据目录，关掉限速和预取，避免测试真的等几秒。"""
    config = tmp_path / "site.json"
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw.update({"request_interval": 0, "request_jitter": 0, "max_retries": 1, "backoff_base": 0})
    config.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(web, "CONFIG_PATH", config)
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    monkeypatch.setattr(web, "PRELOAD_ENABLED", False)
    monkeypatch.setattr(web.store, "videos", {})
    return config


def _warmup():
    return respx.get(url__regex=INDEX_RE).mock(return_value=httpx.Response(200, text="ok"))


# ---- 全站搜索 ----

def test_extract_viewkey_accepts_id_and_url():
    assert web.extract_viewkey("aaa111bbb222") == "aaa111bbb222"
    assert web.extract_viewkey("https://91porn.com/view_video.php?viewkey=abc123def456") == "abc123def456"
    assert web.extract_viewkey("KTV") == ""
    assert web.extract_viewkey("") == ""


@respx.mock
def test_search_queries_site_search_page(env):
    _warmup()
    route = respx.get(url__regex=SEARCH_RE).mock(return_value=httpx.Response(200, text=LISTING_HTML))

    payload = client.get("/api/search", params={"q": "KTV", "page": 1}).json()

    assert payload["mode"] == "search"
    assert payload["query"] == "KTV"
    assert payload["cached"] is False
    assert payload["pagination"]["has_next"] is True
    video = payload["videos"][0]
    assert video["viewkey"] == "aaa111bbb222"
    assert video["title"] == "KTV 测试标题"
    # 作者不能把后面的「热度」一起吞进去
    assert video["author"] == "牛来牛来"
    assert video["views"] == "438"
    assert route.called


@respx.mock
def test_search_repeat_visit_hits_cache_and_refresh_bypasses(env):
    _warmup()
    route = respx.get(url__regex=SEARCH_RE).mock(return_value=httpx.Response(200, text=LISTING_HTML))

    first = client.get("/api/search", params={"q": "KTV"}).json()
    second = client.get("/api/search", params={"q": "KTV"}).json()
    forced = client.get("/api/search", params={"q": "KTV", "refresh": True}).json()

    assert first["cached"] is False
    assert second["cached"] is True
    assert forced["cached"] is False
    assert route.call_count == 2
    assert second["videos"][0]["viewkey"] == first["videos"][0]["viewkey"]


@respx.mock
def test_search_by_viewkey_opens_video_page(env):
    _warmup()
    route = respx.get(url__regex=VIDEO_RE).mock(return_value=httpx.Response(200, text=VIDEO_HTML))

    payload = client.get("/api/search", params={"q": "aaa111bbb222"}).json()

    assert payload["matched_by"] == "viewkey"
    assert [video["viewkey"] for video in payload["videos"]] == ["aaa111bbb222"]
    # <title> 里的站名尾巴要去掉
    assert payload["videos"][0]["title"] == "演示视频"
    assert payload["videos"][0]["author"] == "作者甲"
    assert payload["pagination"]["has_next"] is False
    assert route.called


@respx.mock
def test_missing_viewkey_falls_back_to_keyword_search(env):
    _warmup()
    respx.get(url__regex=VIDEO_RE).mock(
        return_value=httpx.Response(200, text="<html><head><title>91porn</title></head><body>视频不存在</body></html>")
    )
    search = respx.get(url__regex=SEARCH_RE).mock(return_value=httpx.Response(200, text=LISTING_HTML))

    payload = client.get("/api/search", params={"q": "aaa111bbb222"}).json()

    assert payload["matched_by"] == ""
    assert payload["query"] == "aaa111bbb222"
    assert len(payload["videos"]) == 1
    assert search.called


def test_search_rejects_empty_keyword(env):
    assert client.get("/api/search", params={"q": "  "}).status_code == 400
    assert client.get("/api/search", params={"q": "x" * 81}).status_code == 400


# ---- 分类缓存 ----

@respx.mock
def test_browse_serves_repeat_visit_from_cache(env):
    _warmup()
    route = respx.get(url__regex=BROWSE_RE).mock(return_value=httpx.Response(200, text=LISTING_HTML))

    first = client.get("/api/browse", params={"category": "latest"}).json()
    second = client.get("/api/browse", params={"category": "latest"}).json()

    assert first["cached"] is False
    assert second["cached"] is True
    assert route.call_count == 1
    assert second["videos"][0]["viewkey"] == "aaa111bbb222"


@respx.mock
def test_browse_pages_are_cached_separately(env):
    _warmup()
    route = respx.get(url__regex=BROWSE_RE).mock(return_value=httpx.Response(200, text=LISTING_HTML))

    client.get("/api/browse", params={"category": "latest", "page": 1}).json()
    client.get("/api/browse", params={"category": "latest", "page": 2}).json()
    client.get("/api/browse", params={"category": "hot", "page": 1}).json()

    assert route.call_count == 3


def test_cache_key_isolates_site_and_normalizes_query():
    assert cache_key("a.com", "category", "latest", 1) != cache_key("b.com", "category", "latest", 1)
    assert cache_key("a.com", "search", "KTV", 1) == cache_key("a.com", "search", "ktv", 1)


def test_listing_cache_returns_independent_items(tmp_path):
    cache = ListingCache(tmp_path / "listings")
    item = VideoItem("https://x.test/watch?viewkey=abc", "abc", title="Demo")
    cache.put("k", "https://x.test/v", 1, [item], {"page": 1})

    served = cache.get("k").items()
    served[0].title = "changed"

    assert cache.get("k").items()[0].title == "Demo"
    assert cache.get("k", ttl=-1.0) is None
    assert cache.get("k", ttl=60) is not None


def test_cache_survives_new_instance(tmp_path):
    first = ListingCache(tmp_path / "listings")
    first.put("k", "https://x.test/v", 2, [VideoItem("u", "abc")], {"page": 2})

    restored = ListingCache(tmp_path / "listings").get("k")

    assert restored is not None
    assert restored.page == 2
    assert restored.items()[0].viewkey == "abc"


def test_cache_stats_and_clear(env):
    cache = web.listing_cache()
    cache.put("k", "https://x.test/v", 1, [], {"page": 1, "has_next": False})

    stats = client.get("/api/cache").json()
    assert stats["entries"] == 1
    assert stats["preload_enabled"] is False

    assert client.post("/api/cache/clear").json()["removed"] == 1
    assert client.get("/api/cache").json()["entries"] == 0


# ---- 预取 ----

def test_preload_only_schedules_when_next_page_exists(monkeypatch):
    calls = []
    monkeypatch.setattr(web.prefetcher, "submit", lambda *task: calls.append(task) or True)
    monkeypatch.setattr(web, "PRELOAD_ENABLED", True)

    web.schedule_preload("category", "latest", 1, {"has_next": True})
    web.schedule_preload("search", "KTV", 3, {"has_next": True})
    web.schedule_preload("category", "latest", 9, {"has_next": False})

    assert calls == [("category", "latest", 2), ("search", "KTV", 4)]


def test_prefetch_queue_skips_duplicate_tasks(monkeypatch):
    from viewkey_batch.cache import PrefetchQueue

    done = []
    queue = PrefetchQueue(lambda *task: done.append(task), delay=0)
    monkeypatch.setattr(queue, "_run", lambda: None)  # 不真的起线程

    assert queue.submit("category", "latest", 2) is True
    assert queue.submit("category", "latest", 2) is False


# ---- 解析细节 ----

def test_parse_card_fields_splits_labels():
    fields = parse_card_fields("添加时间: 1 小时 前 作者: 牛来牛来 热度: 438 收藏: 1 留言: 0 积分: 0")
    assert fields["author"] == "牛来牛来"
    assert fields["views"] == "438"
    assert fields["favorites"] == "1"


def test_strip_site_suffix_removes_brand_tail():
    config = SiteConfig(base_url="https://91porn.com", category_urls={}, author_url="")
    assert strip_site_suffix("演示视频 - 91porn", config) == "演示视频"
    assert strip_site_suffix("96 - 91porn", config) == "96"
