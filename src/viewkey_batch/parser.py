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


def viewkey_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query).get("viewkey", [""])[0]


def parse_listing(html: str, page_url: str, config: SiteConfig) -> list[VideoItem]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[VideoItem] = []
    for anchor in soup.select(config.video_link_selector):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(page_url, href)
        key = viewkey_from_url(url)
        if not key:
            continue
        container = anchor.find_parent(class_="well") or anchor.parent
        title_node = anchor.select_one(".video-title")
        image = anchor.select_one("img[src]")
        duration_node = anchor.select_one(".duration")
        title = (
            anchor.get("title")
            or (title_node.get_text(" ", strip=True) if title_node else "")
            or (image.get("alt", "") if image else "")
            or anchor.get_text(" ", strip=True)
        )
        fields = parse_card_fields(container.get_text(" ", strip=True) if container else "")
        candidates.append(
            VideoItem(
                page_url=url,
                viewkey=key,
                title=title,
                author=fields.get("author", ""),
                thumbnail_url=urljoin(page_url, image.get("src")) if image and image.get("src") else "",
                duration=duration_node.get_text(strip=True) if duration_node else fields.get("duration", ""),
                views=fields.get("views", ""),
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
