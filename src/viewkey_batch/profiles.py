"""站点规则模板。

不同「91 系」站点的页面结构可以差得很远，光换域名没用：

- **classic**：91porn 那一套。列表链接形如 ``/view_video.php?viewkey=xxx``，
  视频 ID 在查询串里；直链以内联脚本 ``strencode2(...)`` 的形式写在详情页里。
- **pinse**：91pinse 那一套。列表链接形如 ``/v/475058``，视频 ID 在路径里；
  详情页 HTML 里没有任何媒体地址，必须先 ``POST /api/videos/<id>/playback``
  拿到一个 m3u8，再由 yt-dlp 拉 HLS 分片。

这里把两套差异集中成数据，``SiteConfig`` 只管照着字段读；新增一个站点形态
时只要再加一个模板，不用改解析代码。
"""

from __future__ import annotations

from urllib.parse import urlsplit

PROFILE_CLASSIC = "classic"
PROFILE_PINSE = "pinse"
DEFAULT_PROFILE = PROFILE_CLASSIC

PROFILE_LABELS = {
    PROFILE_CLASSIC: "91porn 系（viewkey 查询串 + 页面内联直链）",
    PROFILE_PINSE: "91pinse 系（/v/<id> 路径 + 播放接口取 m3u8）",
}

# 域名里带这些关键字就自动选对应模板，省得用户自己判断
HOST_HINTS: tuple[tuple[str, str], ...] = (
    ("pinse", PROFILE_PINSE),
)

CLASSIC_RULES: dict = {
    "profile": PROFILE_CLASSIC,
    "category_urls": {
        "latest": "/v.php?category=mr&viewtype=basic",
        "hot": "/v.php?category=hot&viewtype=basic",
        "featured": "/v.php?category=rf&viewtype=basic",
        "top_day": "/index.php",
        "top_month": "/v.php?category=top&viewtype=basic",
        "most_favorited": "/v.php?category=mf&viewtype=basic",
        "most_discussed": "/v.php?category=md&viewtype=basic",
        "hd": "/v.php?category=hd&viewtype=basic",
        "original": "/v.php?category=ori&viewtype=basic",
    },
    "author_url": "/uvideos.php?UID={author}&type=public",
    "page_param": "page",
    "first_page": 1,
    "search_url": "/search_result.php?search_type=search_videos&search_id={query}&min_duration=",
    "video_url": "/view_video.php?viewkey={viewkey}",
    "video_link_selector": "a[href*='view_video.php?viewkey='], a[href*='view_video_hd.php?viewkey=']",
    "title_selectors": ["h1", "h2", "title"],
    "author_selectors": [".author a", "a[href*='uvideos.php?UID=']", "a[href*='UID=']"],
    "viewkey_param": "viewkey",
    "viewkey_path_regex": "",
    "card_selector": ".well",
    "card_title_selector": ".video-title",
    "card_author_selector": "",
    "card_duration_selector": ".duration",
    "card_views_selector": "",
    "card_image_selector": "img[src]",
    "stream_mode": "html",
    "playback_url": "",
    "playback_method": "POST",
    "playback_headers": {},
    "playback_url_keys": ["url", "fallback_url"],
    "stream_ttl": 0,
    "warmup_path": "/index.php",
    "timeout": 30,
    "request_interval": 1.5,
    "request_jitter": 1.0,
    "max_retries": 5,
    "backoff_base": 2.0,
    "stop_on_challenge": True,
}

PINSE_RULES: dict = {
    "profile": PROFILE_PINSE,
    # 站点只有下面这几类真实可用的列表页；hd / original 没有对应栏目，
    # 前端会把它们置灰，不让点进去抓一页空列表
    "category_urls": {
        "latest": "/v/",
        "hot": "/v/hot/",
        "featured": "/rank/recently-featured",
        "top_day": "/rank/current-hot",
        "top_month": "/rank/month-hot",
        "most_favorited": "/rank/month-favorite",
        "most_discussed": "/rank/popular-now",
    },
    "author_url": "/v/author/{author}",
    "page_param": "page",
    "first_page": 1,
    "search_url": "/v/search?keyword={query}",
    "video_url": "/v/{viewkey}",
    "video_link_selector": "a.video-card-title",
    "title_selectors": [".watch-title", "h1", "title"],
    "author_selectors": ["a.watch-author .truncate", "a.watch-author", ".watch-meta span"],
    "viewkey_param": "",
    "viewkey_path_regex": r"/v/(\d+)/?$",
    "card_selector": "article.video-card",
    "card_title_selector": "a.video-card-title",
    "card_author_selector": "a.video-card-author",
    "card_duration_selector": ".video-duration",
    "card_views_selector": ".video-card-meta > span",
    "card_image_selector": "img.video-thumb-img",
    "stream_mode": "api",
    "playback_url": "/api/videos/{viewkey}/playback",
    "playback_method": "POST",
    # 浏览器端就是这么发的：只带 X-Requested-With，靠同源 Cookie 认会话
    "playback_headers": {"X-Requested-With": "XMLHttpRequest"},
    "playback_url_keys": ["url", "fallback_url"],
    "stream_ttl": 1800,
    "warmup_path": "/v/",
    "timeout": 30,
    "request_interval": 1.2,
    "request_jitter": 0.8,
    "max_retries": 4,
    "backoff_base": 2.0,
    "stop_on_challenge": True,
}

PROFILES: dict[str, dict] = {
    PROFILE_CLASSIC: CLASSIC_RULES,
    PROFILE_PINSE: PINSE_RULES,
}


def normalize_profile(value: str) -> str:
    """未知模板名一律退回 classic，避免坏配置把整个采集打挂。"""
    name = (value or "").strip().lower()
    return name if name in PROFILES else DEFAULT_PROFILE


def detect_profile(base_url: str) -> str:
    """按域名猜模板；猜不出来就用 classic。"""
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return DEFAULT_PROFILE
    for keyword, profile in HOST_HINTS:
        if keyword in host:
            return profile
    return DEFAULT_PROFILE


def rules_for(profile: str) -> dict:
    """取某个模板的完整规则（深拷贝，调用方随便改）。"""
    name = normalize_profile(profile)
    return _copy(PROFILES[name])


def profile_label(profile: str) -> str:
    return PROFILE_LABELS.get(normalize_profile(profile), profile)


def _copy(value):
    """只处理 dict / list / 标量的深拷贝，够用且不引入 copy 的语义负担。"""
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy(item) for item in value]
    return value
