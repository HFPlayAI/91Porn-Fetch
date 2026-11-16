"""两套站点规则（classic / pinse）的单元测试。

这些用例锁住的是「同一个工具能跑两种页面结构」这件事：
91porn 系把视频 ID 放在查询串、直链内联在页面里；
91pinse 系放在路径里、直链要另调一次播放接口。任何一边的解析退化都会先在这里报错。
"""

from pathlib import Path

import httpx
import pytest
import respx

import viewkey_batch.web as web
from viewkey_batch.crawler import Crawler
from viewkey_batch.http import RateLimitedClient
from viewkey_batch.models import SiteConfig, VideoItem
from viewkey_batch.parser import parse_listing, parse_video_page, viewkey_from_url
from viewkey_batch.profiles import (
    DEFAULT_PROFILE,
    PROFILE_CLASSIC,
    PROFILE_PINSE,
    detect_profile,
    normalize_profile,
    profile_label,
    rules_for,
)
from viewkey_batch.sites import SiteBook

EXAMPLE = Path(__file__).resolve().parent.parent / "site.example.json"

PINSE_LISTING_HTML = """
<html><body>
<article class="video-card group/card">
    <a href="/v/475058" aria-label="标题甲">
        <div class="video-thumb" id="preview_475058">
            <img src="https://cdn-image.example.test/images/1260078.webp" alt="标题甲" class="video-thumb-img">
            <span class="video-badge-today">今日</span>
            <span class="video-duration">0:00:24</span>
        </div>
    </a>
    <div class="video-card-body">
        <a href="/v/475058" class="video-card-title" title="标题甲">标题甲</a>
        <div class="video-card-meta">
            <a href="/v/author/sansanjiu" class="video-card-author" title="sansanjiu"><span class="truncate">sansanjiu</span></a>
            <span>727</span>
            <span>7小时前</span>
        </div>
    </div>
</article>
<article class="video-card group/card">
    <a href="/v/475068"><img src="https://cdn-image.example.test/images/1260093.webp" alt="标题乙" class="video-thumb-img"></a>
    <div class="video-card-body">
        <a href="/v/475068" class="video-card-title" title="标题乙">标题乙</a>
        <div class="video-card-meta">
            <a href="/v/author/CDCdingji" class="video-card-author"><span class="truncate">CDCdingji</span></a>
            <span>2.1万</span>
            <span>3天前</span>
        </div>
    </div>
</article>
</body></html>
"""


def pinse_config() -> SiteConfig:
    rules = rules_for(PROFILE_PINSE)
    overrides = {
        "profile": PROFILE_PINSE,
        "request_interval": 0,
        "request_jitter": 0,
        "backoff_base": 0,
        "max_retries": 0,
    }
    merged = {key: value for key, value in rules.items() if key not in overrides}
    return SiteConfig(base_url="https://pinse.example.test", **merged, **overrides)


# ---- 模板 ----

def test_detect_profile_matches_known_hosts():
    assert detect_profile("https://91pinse.com") == PROFILE_PINSE
    assert detect_profile("https://91porn.com") == PROFILE_CLASSIC
    assert detect_profile("") == DEFAULT_PROFILE


def test_normalize_profile_falls_back_to_classic():
    assert normalize_profile("PINSE ") == PROFILE_PINSE
    assert normalize_profile("nope") == DEFAULT_PROFILE
    assert profile_label(PROFILE_PINSE).startswith("91pinse")


def test_rules_for_returns_independent_copies():
    first = rules_for(PROFILE_PINSE)
    first["category_urls"]["latest"] = "/broken"
    assert rules_for(PROFILE_PINSE)["category_urls"]["latest"] == "/v/"


def test_classic_rules_match_shipped_site_example():
    """模板和仓库自带的 site.example.json 必须一致，否则切站点会改坏老配置。"""
    import json

    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    classic = rules_for(PROFILE_CLASSIC)
    for key, value in example.items():
        if key == "base_url":
            continue
        assert classic[key] == value, f"{key} 与 site.example.json 不一致"


# ---- 解析 ----

def test_viewkey_from_query_and_path():
    classic = SiteConfig(base_url="https://a.test", category_urls={}, author_url="/a/{author}")
    assert viewkey_from_url("https://a.test/view_video.php?viewkey=abc123", classic) == "abc123"

    pinse = pinse_config()
    assert viewkey_from_url("https://pinse.example.test/v/475058", pinse) == "475058"
    assert viewkey_from_url("https://pinse.example.test/v/475058/", pinse) == "475058"
    # 没传配置时也要能认出路径型地址
    assert viewkey_from_url("https://pinse.example.test/v/475058") == "475058"


def test_pinse_listing_extracts_every_card_field():
    items = parse_listing(PINSE_LISTING_HTML, "https://pinse.example.test/v/", pinse_config())

    assert [item.viewkey for item in items] == ["475058", "475068"]
    first, second = items
    assert first.page_url == "https://pinse.example.test/v/475058"
    assert first.title == "标题甲"
    assert first.author == "sansanjiu"
    assert first.duration == "0:00:24"
    assert first.views == "727"
    assert first.thumbnail_url.endswith("/images/1260078.webp")
    # 播放量别把作者名里的数字或「3天前」当成数字
    assert second.author == "CDCdingji"
    assert second.views == "21000"


def test_pinse_listing_deduplicates_thumb_and_title_links():
    """卡片里封面和标题各自是一个链接，同一视频只能留一条。"""
    items = parse_listing(PINSE_LISTING_HTML, "https://pinse.example.test/v/", pinse_config())
    assert len({item.identity for item in items}) == len(items)


def test_pinse_video_page_leaves_stream_to_playback_api():
    html = "<html><body><h1 class='watch-title'>标题甲</h1><a class='watch-author' href='#'><span class='truncate'>sansanjiu</span></a></body></html>"
    item = VideoItem(page_url="https://pinse.example.test/v/475058", viewkey="475058")

    resolved = parse_video_page(html, item.page_url, item, pinse_config())

    assert resolved.title == "标题甲"
    assert resolved.author == "sansanjiu"
    assert resolved.stream_url == ""


# ---- 播放接口取流 ----

PLAYBACK_URL = "https://pinse.example.test/api/videos/475058/playback"


@respx.mock
def test_resolve_stream_reads_playback_api():
    route = respx.post(PLAYBACK_URL).mock(
        return_value=httpx.Response(200, json={"url": "https://cdn.test/x/pl.m3u8", "fallback_url": ""})
    )
    config = pinse_config()
    item = VideoItem(page_url="https://pinse.example.test/v/475058", viewkey="475058")

    with RateLimitedClient(config, {}) as client:
        resolved = Crawler(client, config).resolve_stream(item)

    assert route.called
    request = route.calls[0].request
    assert request.headers["x-requested-with"] == "XMLHttpRequest"
    assert request.headers["referer"] == item.page_url
    assert resolved.stream_url == "https://cdn.test/x/pl.m3u8"
    assert resolved.stream_expires > 0


@respx.mock
def test_resolve_stream_falls_back_to_fallback_url():
    respx.post(PLAYBACK_URL).mock(
        return_value=httpx.Response(200, json={"url": "", "fallback_url": "https://cdn.test/y/pl.m3u8"})
    )
    config = pinse_config()
    item = VideoItem(page_url="https://pinse.example.test/v/475058", viewkey="475058")

    with RateLimitedClient(config, {}) as client:
        resolved = Crawler(client, config).resolve_stream(item)

    assert resolved.stream_url == "https://cdn.test/y/pl.m3u8"


@respx.mock
def test_resolve_stream_rejects_html_answer():
    respx.post(PLAYBACK_URL).mock(return_value=httpx.Response(200, text="<html>blocked</html>"))
    config = pinse_config()
    item = VideoItem(page_url="https://pinse.example.test/v/475058", viewkey="475058")

    with RateLimitedClient(config, {}) as client:
        with pytest.raises(RuntimeError, match="不是 JSON"):
            Crawler(client, config).resolve_stream(item)


@respx.mock
def test_resolve_stream_reports_missing_url():
    respx.post(PLAYBACK_URL).mock(return_value=httpx.Response(200, json={"url": "", "detail": "需要登录"}))
    config = pinse_config()
    item = VideoItem(page_url="https://pinse.example.test/v/475058", viewkey="475058")

    with RateLimitedClient(config, {}) as client:
        with pytest.raises(RuntimeError, match="需要登录"):
            Crawler(client, config).resolve_stream(item)


def test_resolve_stream_is_noop_for_classic_sites():
    classic = SiteConfig(base_url="https://a.test", category_urls={}, author_url="/a/{author}")
    item = VideoItem(page_url="https://a.test/view_video.php?viewkey=abc", viewkey="abc")

    class NoCallClient:
        def get(self, *args, **kwargs):
            raise AssertionError("classic 站点不该请求播放接口")

        post = get

    resolved = Crawler(NoCallClient(), classic).resolve_stream(item)

    assert resolved.stream_url == ""


# ---- 站点列表带规则 ----

@pytest.fixture
def site_env(tmp_path, monkeypatch):
    config_path = tmp_path / "site.json"
    config_path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(web, "CONFIG_PATH", config_path)
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    return config_path


def test_site_book_stores_rules_per_site(site_env):
    book = SiteBook(site_env.parent / "sites.json", site_env)
    entry = book.add("91pinse", "91pinse.com")

    assert entry.profile == PROFILE_PINSE
    assert entry.rules["playback_url"] == "/api/videos/{viewkey}/playback"


def test_switching_site_swaps_the_whole_rule_set(site_env):
    book = SiteBook(site_env.parent / "sites.json", site_env)
    entry = book.add("91pinse", "91pinse.com")

    book.set_active(entry.id)
    pinse = SiteConfig.load(site_env)
    assert pinse.base_url == "https://91pinse.com"
    assert pinse.profile == PROFILE_PINSE
    assert pinse.category_urls["latest"] == "/v/"
    assert pinse.stream_mode == "api"
    assert pinse.video_link_selector == "a.video-card-title"

    # 切回去要恢复成 91porn 那套规则，而不是留下一半 pinse 配置
    classic_id = next(item.id for item in book.snapshot() if item.profile == PROFILE_CLASSIC)
    book.set_active(classic_id)
    classic = SiteConfig.load(site_env)
    assert classic.base_url == "https://91porn.com"
    assert classic.profile == PROFILE_CLASSIC
    assert classic.category_urls["latest"] == "/v.php?category=mr&viewtype=basic"
    assert classic.stream_mode == "html"


def test_legacy_sites_json_without_rules_gets_migrated(site_env, tmp_path):
    """老版本 sites.json 只有域名，读取时要能补上规则。"""
    sites_path = tmp_path / "sites.json"
    sites_path.write_text(
        '{"active_id": "91porn-com", "sites": [{"id": "91porn-com", "name": "91porn.com", "base_url": "https://91porn.com"}]}',
        encoding="utf-8",
    )

    book = SiteBook(sites_path, site_env)

    entry = book.active()
    assert entry.profile == PROFILE_CLASSIC
    # 当前生效站点的规则取自 site.json，不覆盖用户手改过的选择器
    assert entry.rules["category_urls"]["latest"] == "/v.php?category=mr&viewtype=basic"
    assert entry.rules["video_link_selector"] == SiteConfig.load(site_env).video_link_selector


def test_api_add_site_accepts_profile(site_env):
    from fastapi.testclient import TestClient

    client = TestClient(web.app)
    response = client.post("/api/sites", json={"name": "91pinse", "base_url": "91pinse.com", "profile": "pinse"})

    assert response.status_code == 200
    assert response.json()["site"]["profile"] == PROFILE_PINSE
    assert response.json()["site"]["profile_label"].startswith("91pinse")


def test_extract_viewkey_accepts_both_id_shapes():
    """搜索框里粘地址或直接输 ID 时，两种站点都要认。"""
    pinse = pinse_config()
    classic = SiteConfig(base_url="https://a.test", category_urls={}, author_url="/a/{author}")

    assert web.extract_viewkey("https://pinse.example.test/v/475058", pinse) == "475058"
    assert web.extract_viewkey("475058", pinse) == "475058"
    assert web.extract_viewkey("https://a.test/view_video.php?viewkey=a3ab28f91d8d17f1", classic) == "a3ab28f91d8d17f1"
    assert web.extract_viewkey("a3ab28f91d8d17f1") == "a3ab28f91d8d17f1"
    # 普通关键词不能被当成 ID
    assert web.extract_viewkey("自拍", pinse) == ""


def test_api_config_exposes_profile(site_env):
    from fastapi.testclient import TestClient

    client = TestClient(web.app)
    payload = client.get("/api/config").json()

    assert payload["profile"] == PROFILE_CLASSIC
    assert "latest" in payload["categories"]
