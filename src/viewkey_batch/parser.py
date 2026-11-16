from __future__ import annotations

from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse
import json
import re

from bs4 import BeautifulSoup

from .models import SiteConfig, VideoItem


STREAM_PATTERNS = (
    re.compile(r'''(?:file|src|video_url|videoUrl)\s*[:=]\s*["']([^"']+?\.(?:m3u8|mp4)(?:\?[^"']*)?)["']''', re.I),
    re.compile(r'''https?:\\?/\\?/[^"'\s<>]+?\.(?:m3u8|mp4)(?:\?[^"'\s<>]*)?''', re.I),
)
ENCODED_SOURCE_PATTERN = re.compile(r'''strencode2\(\s*["']([^"']+)["']''', re.I)

# 卡片上的字段标签，中英文站点都能认；顺序无关，值取到下一个标签为止
CARD_FIELD_LABELS = {
    "添加时间": "added",
    "作者": "author",
    "author": "author",
    "from": "author",
    "uploaded": "author",
    "热度": "views",
    "播放": "views",
    "views": "views",
    "收藏": "favorites",
    "favorites": "favorites",
    "留言": "comments",
    "评论": "comments",
    "comments": "comments",
    "积分": "points",
    "points": "points",
    "时长": "duration",
    "duration": "duration",
}
_CARD_FIELD_RE = re.compile(
    r"(?:" + "|".join(sorted((re.escape(key) for key in CARD_FIELD_LABELS), key=len, reverse=True)) + r")\s*[:：]",
    re.I,
)


def parse_card_fields(text: str) -> dict[str, str]:
    """把「作者: 张三 热度: 12」这类平铺文本拆成字典。

    站点把标签和值都塞在一串兄弟文本节点里，正则整体匹配会把后一个标签的值
    也吞进来（出现过「作者: 张三 热度: 12」），所以按标签切段。
    """
    if not text:
        return {}
    matches = list(_CARD_FIELD_RE.finditer(text))
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        label = match.group(0).rstrip(":：").strip().lower()
        key = CARD_FIELD_LABELS.get(label)
        if not key:
            continue
        value = text[match.end():end].strip(" \t\r\n·|")
        if value and key not in values:
            values[key] = value
    return values


def strip_site_suffix(title: str, config: SiteConfig) -> str:
    """去掉 <title> 里的「- 91porn」站名尾巴，标题直接用于卡片和文件名。"""
    brand = (urlparse(config.base_url).hostname or "").lower()
    if brand.startswith("www."):
        brand = brand[4:]
    brand = brand.split(".")[0]
    if not title or not brand:
        return title
    pattern = re.compile(rf"\s*[-|–—]\s*[^-|–—]*{re.escape(brand)}[^-|–—]*\s*$", re.I)
    stripped = pattern.sub("", title).strip()
    return stripped or title


def viewkey_from_url(url: str, config: SiteConfig | None = None) -> str:
    """从视频页地址里取出视频 ID。

    91porn 系把它放在查询串（``?viewkey=xxx``），91pinse 系放在路径
    （``/v/475058``）。先按站点配置取，再退回两种通用写法，
    这样即使站点配置没写全也不会整页条目全丢。
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return ""
    query = parse_qs(parts.query)
    if config is not None and config.viewkey_param:
        value = query.get(config.viewkey_param, [""])[0]
        if value:
            return value
    if config is not None and config.viewkey_path_regex:
        match = re.search(config.viewkey_path_regex, parts.path)
        if match and match.lastindex:
            return match.group(1)
    value = query.get("viewkey", [""])[0]
    if value:
        return value
    # 路径型地址兜底：/v/475058、/v/475058/、/video/475058
    match = re.search(r"/(?:v|video|view)/(\d+)/?$", parts.path)
    return match.group(1) if match else ""


def _node_text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


# 「7小时前」「3天前」这类相对时间也在卡片 meta 里，别当成播放量
_TIME_LABEL_RE = re.compile(r"(前|小时|分钟|秒|天|周|月|年)\s*$")


def _first_number(text: str) -> str:
    """卡片上的播放量是裸数字（``<span>2086</span>``），取第一段数字即可。"""
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([万wWkK])?", text or "")
    if not match:
        return ""
    value = match.group(1).replace(",", "")
    unit = match.group(2) or ""
    if not unit:
        return value
    try:
        scale = 10000 if unit == "万" else 1000
        return str(int(float(value) * scale))
    except ValueError:
        return value


def _card_views(container, selector: str) -> str:
    """播放量：跳过「7小时前」这种相对时间，取第一个真正的数字。"""
    if container is None or not selector:
        return ""
    try:
        nodes = container.select(selector)
    except Exception:
        return ""
    for node in nodes:
        text = node.get_text(" ", strip=True)
        if not text or _TIME_LABEL_RE.search(text):
            continue
        number = _first_number(text)
        if number:
            return number
    return ""


def _select(node, selector: str):
    """选择器写错时不要让整页采集失败。"""
    if node is None or not selector:
        return None
    try:
        return node.select_one(selector)
    except Exception:  # soupsieve 对非法选择器抛的异常类型不统一
        return None


def _card_anchors(soup: BeautifulSoup, config: SiteConfig) -> tuple[list, dict[int, object]]:
    """返回「所有视频链接」和「链接 → 所属卡片」的映射。

    classic 的卡片是 ``div.well``，同一个卡片里有详情链接和封面链接；
    pinse 的卡片是 ``article.video-card``，标题链接和封面链接分开。
    先按卡片把链接归类，卡片外的链接退回用父节点当容器。
    """
    try:
        anchors = soup.select(config.video_link_selector)
    except Exception:
        return [], {}
    card_of: dict[int, object] = {}
    if config.card_selector:
        try:
            cards = soup.select(config.card_selector)
        except Exception:
            cards = []
        for card in cards:
            try:
                inner = card.select(config.video_link_selector)
            except Exception:
                continue
            for anchor in inner:
                card_of[id(anchor)] = card
    return anchors, card_of


def parse_listing(html: str, page_url: str, config: SiteConfig) -> list[VideoItem]:
    soup = BeautifulSoup(html, "html.parser")
    anchors, card_of = _card_anchors(soup, config)
    candidates: list[VideoItem] = []
    for anchor in anchors:
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(page_url, href)
        key = viewkey_from_url(url, config)
        if not key:
            continue
        container = card_of.get(id(anchor)) or anchor.parent
        title_node = _select(container, config.card_title_selector) or _select(anchor, config.card_title_selector)
        image = _select(anchor, config.card_image_selector) or _select(container, config.card_image_selector)
        duration_node = _select(container, config.card_duration_selector)
        author_node = _select(container, config.card_author_selector)
        title = (
            anchor.get("title")
            or _node_text(title_node)
            or (image.get("alt", "") if image else "")
            or anchor.get_text(" ", strip=True)
        )
        fields = parse_card_fields(container.get_text(" ", strip=True) if container else "")
        candidates.append(
            VideoItem(
                page_url=url,
                viewkey=key,
                title=title,
                author=_node_text(author_node) or fields.get("author", ""),
                thumbnail_url=urljoin(page_url, image.get("src")) if image and image.get("src") else "",
                duration=_node_text(duration_node) or fields.get("duration", ""),
                views=_card_views(container, config.card_views_selector) or fields.get("views", ""),
            )
        )
    all_tokens = {
        parse_qs(urlparse(item.page_url).query).get("c", [""])[0]
        for item in candidates
    }
    found: list[VideoItem] = []
    seen: set[str] = set()
    for item in candidates:
        token = parse_qs(urlparse(item.page_url).query).get("c", [""])[0]
        # Hidden cards use the current page token prefixed with "a" and may
        # carry another card's key, title, and thumbnail.
        if token.startswith("a") and token[1:] in all_tokens:
            continue
        if item.identity in seen:
            continue
        seen.add(item.identity)
        found.append(item)
    return found


def _clean_stream_url(value: str, page_url: str) -> str:
    value = unescape(value).replace("\\/", "/").replace("\\u0026", "&")
    try:
        if value.startswith('"'):
            value = json.loads(value)
    except json.JSONDecodeError:
        pass
    return urljoin(page_url, unquote(value.strip('"\'')))


def parse_video_page(html: str, page_url: str, item: VideoItem, config: SiteConfig) -> VideoItem:
    soup = BeautifulSoup(html, "html.parser")
    if not item.thumbnail_url:
        for selector in ('meta[property="og:image"]', 'meta[name="twitter:image"]', "video[poster]"):
            node = soup.select_one(selector)
            value = ((node.get("content") or node.get("poster") or "") if node else "").strip()
            if value:
                item.thumbnail_url = urljoin(page_url, value)
                break
    for selector in config.title_selectors:
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            title = node.get_text(" ", strip=True)
            # <title> 带站名尾巴（「标题 - 91porn」），会一路带进文件名
            item.title = strip_site_suffix(title, config) if node.name == "title" else title
            break
    for selector in config.author_selectors:
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            item.author = re.split(r"\s*[(（]", node.get_text(" ", strip=True))[0].strip()
            break
    else:
        # 结构变了也别丢作者：退回到「作者: 名字」这种标签文本
        for span in soup.select("span.info"):
            if not span.get_text(strip=True).startswith(("作者", "Author", "From")):
                continue
            value = span.parent.get_text(" ", strip=True) if span.parent else ""
            author = parse_card_fields(value).get("author", "")
            if author:
                item.author = re.split(r"\s*[(（]", author)[0].strip()
            break
    # 走播放接口的站点（91pinse 系）页面里只有 hls.js 的加载逻辑，
    # 真实地址在 /api/videos/<id>/playback，这里再去正则抠容易抠到广告位里的
    # 无关 mp4，所以直接跳过，交给 crawler 调接口。
    if config.stream_mode == "api" and config.playback_url:
        return item
    # The first plain <source> is shared placeholder media. The per-video
    # signed URL is percent-encoded inside strencode2(...).
    for encoded in ENCODED_SOURCE_PATTERN.findall(html):
        decoded = unquote(unescape(encoded))
        for pattern in STREAM_PATTERNS:
            match = pattern.search(decoded)
            if match:
                value = match.group(1) if match.lastindex else match.group(0)
                item.stream_url = _clean_stream_url(value, page_url)
                return item
    for node in soup.select("video[src], video source[src]"):
        src = node.get("src")
        if src:
            item.stream_url = _clean_stream_url(src, page_url)
            return item
    for pattern in STREAM_PATTERNS:
        match = pattern.search(html)
        if match:
            value = match.group(1) if match.lastindex else match.group(0)
            item.stream_url = _clean_stream_url(value, page_url)
            return item
    return item
