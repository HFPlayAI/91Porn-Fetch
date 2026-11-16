from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse
import hashlib
import json
import logging
import re


log = logging.getLogger(__name__)

# Windows 保留设备名不能直接作为文件名使用
RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
MAX_FILENAME_TITLE = 80


@dataclass(slots=True)
class VideoItem:
    page_url: str
    viewkey: str
    title: str = ""
    author: str = ""
    stream_url: str = ""
    # 直链有效期（Unix 时间戳）。页面内联的直链可以从签名里算出来，
    # 走播放接口拿到的 HLS 地址没有签名，按站点的 stream_ttl 记一个到期时间，
    # 重试/续传时就不必再回源请求页面。
    stream_expires: float = 0.0
    thumbnail_url: str = ""
    duration: str = ""
    views: str = ""
    source: str = ""
    listing_url: str = ""
    listing_page: int = 0
    sources: list[str] = field(default_factory=list)
    listing_urls: dict[str, str] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.viewkey or hashlib.sha256(self.page_url.encode()).hexdigest()[:16]

    @property
    def filename(self) -> str:
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", self.title).strip(" ._")
        title = title[:MAX_FILENAME_TITLE].strip(" ._")
        if not title:
            title = "video"
        # Windows 会拒绝 CON、NUL 等保留名组成的文件路径
        if title.upper() in RESERVED_WINDOWS_NAMES:
            title = f"_{title}"
        return f"{title} [{self.identity}]"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def add_source(self, source: str, listing_url: str = "") -> None:
        if source and source not in self.sources:
            self.sources.append(source)
        if source and listing_url:
            self.listing_urls[source] = listing_url
        if source:
            self.source = source
        if listing_url:
            self.listing_url = listing_url

    @classmethod
    def from_json(cls, line: str) -> "VideoItem":
        raw = json.loads(line)
        source = raw.get("source", "")
        listing_url = raw.get("listing_url", "")
        if not raw.get("sources") and source:
            raw["sources"] = [source]
        if not raw.get("listing_urls") and source and listing_url:
            raw["listing_urls"] = {source: listing_url}
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})


@dataclass(slots=True)
class SiteConfig:
    base_url: str
    category_urls: dict[str, str]
    author_url: str
    page_param: str = "page"
    first_page: int = 1
    # 全站搜索页：{query} 会被替换成关键词。站点改版时只改这里，不用动代码
    search_url: str = "/search_result.php?search_type=search_videos&search_id={query}&min_duration="
    # 单条视频页：只拿到 viewkey 时用它拼地址
    video_url: str = "/view_video.php?viewkey={viewkey}"
    video_link_selector: str = 'a[href*="viewkey="]'
    title_selectors: tuple[str, ...] = ("h1", "h2.title", "title")
    author_selectors: tuple[str, ...] = ('.author a', 'a[href*="UID="]', 'a[href*="author="]')
    timeout: float = 30.0
    request_interval: float = 1.5
    request_jitter: float = 1.0
    max_retries: int = 5
    backoff_base: float = 2.0
    stop_on_challenge: bool = True
    # ---- 多站点差异化的部分 ----
    # 规则模板名（classic / pinse），只用于展示和迁移，真正生效的是下面这些字段
    profile: str = "classic"
    # 视频 ID 的取法：先按查询参数（91porn 的 ?viewkey=），取不到再按路径正则
    viewkey_param: str = "viewkey"
    viewkey_path_regex: str = ""
    # 列表卡片：容器，以及标题/作者/时长/播放量/封面各自的选择器
    card_selector: str = ".well"
    card_title_selector: str = ".video-title"
    card_author_selector: str = ""
    card_duration_selector: str = ".duration"
    card_views_selector: str = ""
    card_image_selector: str = "img[src]"
    # 取流方式：html = 地址内联在页面里；api = 需要再调一次 JSON 播放接口
    stream_mode: str = "html"
    playback_url: str = ""
    playback_method: str = "POST"
    playback_headers: dict = field(default_factory=dict)
    playback_url_keys: tuple[str, ...] = ("url", "fallback_url")
    # 播放接口返回的地址通常没有签名，按这个时长复用，避免每次重试都回源
    stream_ttl: int = 1800
    # 预热页：老站点是 /index.php，镜像站点没有这个路径
    warmup_path: str = "/index.php"

    @classmethod
    def load(cls, path: Path) -> "SiteConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["title_selectors"] = tuple(raw.get("title_selectors", ("h1", "h2.title", "title")))
        raw["author_selectors"] = tuple(
            raw.get("author_selectors", ('.author a', 'a[href*="UID="]', 'a[href*="author="]'))
        )
        raw["playback_url_keys"] = tuple(raw.get("playback_url_keys", ("url", "fallback_url")))
        if not isinstance(raw.get("playback_headers"), dict):
            raw["playback_headers"] = {}
        known = {item.name for item in fields(cls)}
        unknown = [key for key in raw if key not in known]
        for key in unknown:
            log.warning("site.json 中存在已废弃或未知的配置项 %r，已忽略", key)
        return cls(**{key: value for key, value in raw.items() if key in known})

    def rules(self) -> dict:
        """导出除 base_url 之外的全部解析规则，用于写进站点列表。"""
        data = asdict(self)
        data.pop("base_url", None)
        return data

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc
