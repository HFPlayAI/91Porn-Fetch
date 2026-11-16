from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import Event, Lock, Thread, Timer
from time import sleep, time
from typing import Literal
from urllib.parse import parse_qs, quote, quote_plus, urljoin, urlparse, urlsplit, urlunparse
import base64
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import threading
import uuid
import webbrowser

import httpx

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from .cache import ListingCache, PrefetchQueue, cache_key, ttl_for, VIDEO_TTL
from .crawler import Crawler, fresh_listing_url, listing_page_url
from .downloader import BatchDownloader, StalledError, is_drop_error, is_stalled_error, validate_media_file
from .http import build_client, load_cookies, make_httpx_client
from .models import SiteConfig, VideoItem
from .parser import parse_listing, parse_video_page, viewkey_from_url
from .sites import (
    SiteBook,
    normalize_proxy,
    proxy_needs_socks,
    rebind_cookie_domain,
    socks_available,
)


ROOT = Path.cwd()
STATIC_DIR = Path(__file__).parent / "static"
CONFIG_PATH = ROOT / "site.json"
DATA_DIR = Path(os.getenv("VIEWKEY_DATA_DIR", str(ROOT / "downloads")))
CATALOG_PATH = DATA_DIR / "catalog.jsonl"
STATE_PATH = DATA_DIR / "state.json"
VIDEO_DIR = DATA_DIR / "videos"
SETTINGS_PATH = DATA_DIR / "settings.json"
COOKIES_PATH = DATA_DIR / "cookies.txt"
THUMB_DIR = DATA_DIR / "thumbs"

log = logging.getLogger("viewkey.web")

# 状态文件落盘锁：多个下载线程收尾时都会写 state.json，必须串行
_atomic_write_lock = Lock()


class AppSettings(BaseModel):
    download_dir: str = ""
    folder_mode: Literal["flat", "date", "category", "date_category"] = "flat"
    prefer_hd: bool = False
    workers: int = Field(2, ge=1, le=8)
    fragments: int = Field(4, ge=1, le=16)
    ui_refresh_seconds: int = Field(3, ge=1, le=60)
    proxy: str = ""


class SettingsStore:
    def __init__(self) -> None:
        self.lock = Lock()
        self.value = AppSettings(
            download_dir=os.getenv("VIEWKEY_DEFAULT_DOWNLOAD_DIR", ""),
            workers=int(os.getenv("VIEWKEY_WORKERS", "2")),
            fragments=int(os.getenv("VIEWKEY_FRAGMENTS", "4")),
            proxy=os.getenv("VIEWKEY_PROXY", ""),
        )
        self.load()

    def load(self) -> None:
        if not SETTINGS_PATH.exists():
            return
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            self.value = AppSettings(**{**self.value.model_dump(), **raw})
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock:
            content = json.dumps(self.value.model_dump(), ensure_ascii=False, indent=2)
        temporary = SETTINGS_PATH.with_suffix(".json.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(SETTINGS_PATH)

    def snapshot(self) -> AppSettings:
        with self.lock:
            return AppSettings(**self.value.model_dump())


settings_store = SettingsStore()


def sites_file() -> Path:
    # 每次按当前 DATA_DIR 解析，测试里替换 DATA_DIR 后无需重新构造
    return DATA_DIR / "sites.json"


def site_book() -> SiteBook:
    return SiteBook(sites_file(), CONFIG_PATH)


def app_proxy() -> str:
    """当前生效的代理地址；空串表示走系统/环境变量代理。"""
    return settings_store.snapshot().proxy.strip()


# 停滞看门狗：窗口内的平均速率低于阈值就认定连接被 CDN 掐死，中断重连。
# 站点把单连接压到 100 B/s 时，yt-dlp 的 socket_timeout 永远不触发，
# 任务会一直挂在「正在下载」却下不动。
STALL_WINDOW_SECONDS = 40
STALL_MIN_BPS = 8 * 1024

# 直链签名（st=/e= 或 secure=...,<时间戳>）有效期约 3 小时，留 5 分钟余量复用
STREAM_URL_MARGIN_SECONDS = 300


def stream_url_fresh(url: str) -> bool:
    """判断直链签名是否还没过期，未过期就不必重新解析视频页。

    站点给的临时直链带过期时间戳，排队的几十个任务如果每条都重新解析，
    一串密集请求很容易再次触发 Cloudflare 的 IP 封禁（error 1106）。
    """
    if not url or "://" not in url:
        return False
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return False
    stamp = ""
    if query.get("e"):
        stamp = query["e"][0]
    elif query.get("secure"):
        # cdn77 形如 secure=<token>,<expire>
        stamp = query["secure"][0].rsplit(",", 1)[-1]
    if not stamp.isdigit():
        return False
    return int(stamp) - time() > STREAM_URL_MARGIN_SECONDS


# 连接层失败（RST / TLS 握手超时 / 读超时）说明这条签名直链已经作废了：可能是出口 IP
# 被 CDN 拒了，也可能是签名被判无效。只要签名里的 e= 还没到期，item_stream_fresh()
# 就会一直说「新鲜」，于是每一轮重试都在打同一个死链接——用户看到的就是
# 「重新下载无数次，永远卡在 0%」。这里按「主机+路径」记住失败过的直链，
# 一旦连接层失败就把它清掉，下一次必须重新去视频页换一条新链接。
_failed_streams: dict[str, tuple[str, int]] = {}


def stream_host_path(url: str) -> str:
    """直链的「主机+路径」，忽略每次都变的签名参数。"""
    parts = urlsplit(url or "")
    host = (parts.netloc or "").lower()
    return f"{host}{parts.path}" if host else ""


def mark_stream_dead(key: str, item: VideoItem) -> int:
    """作废这条任务的直链，返回同一个主机连续失败的次数。"""
    host_path = stream_host_path(item.stream_url)
    times = 0
    if host_path:
        previous, times = _failed_streams.get(key, ("", 0))
        times = times + 1 if previous == host_path else 1
        _failed_streams[key] = (host_path, times)
    with store.lock:
        stored = store.videos.get(key)
        if stored is not None:
            stored.stream_url = ""
            stored.stream_expires = 0.0
    store.save()
    return times


def item_stream_fresh(item: VideoItem) -> bool:
    """这条任务手里的直链还能不能直接用。

    页面内联的直链可以从签名里算到期时间；播放接口给的 HLS 地址没有签名，
    解析时按站点的 stream_ttl 记了 ``stream_expires``，两者都认。
    """
    if not item.stream_url:
        return False
    failed = _failed_streams.get(item.identity)
    if failed and failed[0] and failed[0] == stream_host_path(item.stream_url):
        # 这条链接在连接层已经失败过，别再拿它去撞墙
        return False
    if item.stream_expires and item.stream_expires - time() > STREAM_URL_MARGIN_SECONDS:
        return True
    return stream_url_fresh(item.stream_url)


def site_hosts() -> set[str]:
    """站点列表里出现过的所有域名，用于判断某个 URL 是否属于「站点自身」。"""
    hosts: set[str] = set()
    for entry in site_book().snapshot():
        host = urlparse(entry.base_url).netloc.lower()
        if host:
            hosts.add(host)
    return hosts


def align_to_site(url: str, config: SiteConfig) -> str:
    """切换镜像站点后，把旧域名下的页面地址对齐到当前站点。

    只替换主机名，路径和查询串保持不变。
    """
    if not url or "://" not in url:
        return url
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    old_host = parts.netloc.lower()
    if not old_host or old_host == config.host.lower():
        return url
    base = urlparse(config.base_url)
    return urlunparse(parts._replace(scheme=base.scheme or "https", netloc=config.host))


def align_media_url(url: str, config: SiteConfig, hosts: set[str]) -> str:
    """缩略图等资源只在「确实指向旧站点」时才改写，避免误伤 CDN 域名。"""
    if not url or "://" not in url:
        return url
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return url
    if host not in hosts:
        return url
    return align_to_site(url, config)


def get_video_dir() -> Path:
    configured = settings_store.snapshot().download_dir.strip()
    if not configured:
        return VIDEO_DIR
    path = Path(configured).expanduser()
    return path if path.is_absolute() else DATA_DIR / path


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    current: int = 0
    total: int = 0
    message: str = "等待开始"
    error: str = ""
    started_at: float = field(default_factory=time)
    viewkeys: list[str] = field(default_factory=list)
    cancelled: bool = False
    skipped: int = 0
    # 暂停时也走 cancelled 中断在跑的 yt-dlp，但状态记成 paused 而不是失败
    paused: bool = False


class CrawlRequest(BaseModel):
    mode: Literal["category", "author", "url"] = "category"
    category: str = "latest"
    author: str = ""
    url: str = ""
    pages: int = Field(1, ge=0, le=1000)


class DownloadRequest(BaseModel):
    viewkeys: list[str] = Field(min_length=1)
    workers: int = Field(2, ge=1, le=8)
    fragments: int = Field(4, ge=1, le=16)


class RemoveRequest(BaseModel):
    viewkeys: list[str]


class CookieRequest(BaseModel):
    content: str


class LoginRequest(BaseModel):
    sid: str
    username: str
    password: str
    captcha: str


# 浏览器内直接登录站点时使用的临时会话（绑定验证码图片），10 分钟过期
_login_sessions: dict[str, tuple[float, httpx.Client]] = {}
_login_sessions_lock = Lock()
LOGIN_SESSION_TTL_SECONDS = 600.0


def _expire_login_sessions() -> None:
    now = time()
    expired = [sid for sid, (created, _) in _login_sessions.items() if now - created > LOGIN_SESSION_TTL_SECONDS]
    for sid in expired:
        _, client = _login_sessions.pop(sid)
        client.close()


def app_cookies() -> dict[str, str]:
    """网页模式使用的会话 Cookie；用户在设置中心保存后所有请求自动携带。"""
    if not COOKIES_PATH.exists():
        return {}
    try:
        return load_cookies(COOKIES_PATH)
    except (OSError, json.JSONDecodeError, KeyError, IndexError):
        return {}


def parse_cookie_text(content: str) -> dict[str, str]:
    """解析粘贴的 Cookie，支持 Netscape 文件内容和 Cookie 请求头两种格式。"""
    content = content.strip()
    if not content:
        return {}
    if content.startswith("# Netscape"):
        temporary = DATA_DIR / ".cookie-import.tmp"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary.write_text(content, encoding="utf-8")
        try:
            return load_cookies(temporary)
        finally:
            temporary.unlink(missing_ok=True)
    cookies: dict[str, str] = {}
    for pair in content.replace("\n", ";").split(";"):
        name, _, value = pair.strip().partition("=")
        if name and value:
            cookies[name.strip()] = value.strip()
    return cookies


class Store:
    def __init__(self) -> None:
        self.lock = Lock()
        self.videos: dict[str, VideoItem] = {}
        self.jobs: dict[str, Job] = {}
        self.download_status: dict[str, dict] = {}
        self.dismissed_downloads: set[str] = set()
        self._load()

    def _load(self) -> None:
        if CATALOG_PATH.exists():
            for line in CATALOG_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = VideoItem.from_json(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                self.videos[item.identity] = item
        if STATE_PATH.exists():
            try:
                raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                self.download_status = raw.get("download_status", {})
                self.dismissed_downloads = set(raw.get("dismissed_downloads", []))
                for status in self.download_status.values():
                    if status.get("state") in {"queued", "downloading"}:
                        # 上次运行时被强杀的，记成「已暂停」而不是失败：
                        # 它会出现在已暂停里，用户手动点「继续下载」就能从 .part 续传
                        status.update(state="paused", error="", speed="", speed_bps=0.0, speed_at=0.0)
            except (json.JSONDecodeError, OSError):
                self.download_status = {}
                self.dismissed_downloads = set()

    def save(self) -> None:
        with self.lock:
            content = "".join(item.to_json() + "\n" for item in self.videos.values())
        self._atomic_write(CATALOG_PATH, content)

    def save_state(self) -> None:
        with self.lock:
            content = json.dumps(
                {
                    "download_status": self.download_status,
                    "dismissed_downloads": sorted(self.dismissed_downloads),
                },
                ensure_ascii=False,
                indent=2,
            )
        self._atomic_write(STATE_PATH, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 多个下载线程会同时收尾并写状态文件：临时文件名按线程区分并串行化落盘，
        # 否则两个线程会抢同一个 .tmp，Windows 上直接 PermissionError（WinError 32）
        temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        with _atomic_write_lock:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)

    def add(self, item: VideoItem) -> None:
        with self.lock:
            previous = self.videos.get(item.identity)
            if previous:
                for attribute in ("title", "author", "stream_url", "thumbnail_url", "duration", "views"):
                    if not getattr(item, attribute):
                        setattr(item, attribute, getattr(previous, attribute))
                item.sources = list(dict.fromkeys([*previous.sources, *item.sources]))
                item.listing_urls = {**previous.listing_urls, **item.listing_urls}
            self.videos[item.identity] = item

    def replace_source_membership(self, source: str, identities: set[str]) -> None:
        with self.lock:
            for identity, item in list(self.videos.items()):
                if identity in identities or source not in item.sources:
                    continue
                item.sources.remove(source)
                item.listing_urls.pop(source, None)
                if item.source == source:
                    item.source = item.sources[-1] if item.sources else ""
                    item.listing_url = item.listing_urls.get(item.source, "")
                if not item.sources and identity not in self.download_status:
                    self.videos.pop(identity)


store = Store()
app = FastAPI(title="91Fetch")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def disable_local_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


def pagination_info(html: str, current_page: int) -> dict[str, int | bool]:
    soup = BeautifulSoup(html, "html.parser")
    linked_pages: set[int] = set()
    for anchor in soup.select("a[href]"):
        value = parse_qs(urlparse(anchor.get("href", "")).query).get("page", [""])[0]
        if str(value).isdigit():
            linked_pages.add(int(value))
    return {
        "page": current_page,
        "has_previous": current_page > 1,
        "has_next": any(page > current_page for page in linked_pages),
        "last_visible_page": max(linked_pages | {current_page}),
    }


_downloaded_keys_cache: dict[str, tuple[float, frozenset[str]]] = {}
_downloaded_keys_lock = Lock()
DOWNLOADED_KEYS_TTL_SECONDS = 5.0


def downloaded_keys() -> set[str]:
    directory = get_video_dir()
    cache_key = str(directory)
    now = time()
    with _downloaded_keys_lock:
        cached = _downloaded_keys_cache.get(cache_key)
        if cached and now - cached[0] < DOWNLOADED_KEYS_TTL_SECONDS:
            return set(cached[1])
    if not directory.exists():
        return set()
    keys: set[str] = set()
    for path in directory.rglob("*"):
        if not path.is_file() or path.name == ".downloaded.txt":
            continue
        if path.suffix.lower() in {".part", ".ytdl", ".temp"}:
            continue
        match = re.search(r"\[([^\]]+)\]\.[^.]+$", path.name)
        if not match:
            continue
        valid, _ = validate_media_file(path)
        if valid:
            keys.add(match.group(1))
    with _downloaded_keys_lock:
        _downloaded_keys_cache[cache_key] = (now, frozenset(keys))
    return keys


# ---- 全局下载队列 ----
# 所有下载入口（下载所选 / 下载全部未完成 / 继续下载 / 单个重试）都只往同一个队列里塞任务，
# 由 DownloadScheduler 统一按设置里的「并行下载数」起线程：真正同时在下的视频数永远不超过
# 这个值，一个下完才补下一个。旧实现是「一次点击 = 一个批次任务」，批次之间各跑各的，
# 多点几下就会同时开出十几路连接，正是站点限速和 403 error 1106 的触发源。

LISTING_SNAPSHOT_TTL_SECONDS = 600.0


def _set_status(key: str, *, state: str, error: str = "", **fields) -> None:
    """更新单条下载状态；条目已被用户删除时什么都不做。"""
    with store.lock:
        current = store.download_status.get(key)
        if current is None:
            return
        current = dict(current)
        current.update(state=state, error=error)
        current.update(fields)
        store.download_status[key] = current


class DownloadScheduler:
    """全局下载队列：同一个队列、同一个并发上限，完成一个才补下一个。"""

    def __init__(self) -> None:
        self.lock = Lock()
        self.queue: list[str] = []
        self.running: dict[str, Thread] = {}
        # 停滞任务在这里等冷却（key -> 到点时间），到点后回到队列尾
        self.deferred: dict[str, float] = {}
        # 熔断冷却截止时间：到点前不启动新任务
        self.cooldown_until = 0.0
        self.cancelled: set[str] = set()
        self.paused = False
        self.wake = Event()
        self._thread: Thread | None = None
        self._started = False

    # ---- 对外接口 ----

    @staticmethod
    def limit() -> int:
        """并发上限始终取设置中心的「并行下载数」，改完设置下一个任务就生效。"""
        return max(1, min(8, int(settings_store.snapshot().workers)))

    def ensure_started(self) -> None:
        with self.lock:
            if self._started:
                return
            self._started = True
            self._thread = Thread(target=self._loop, daemon=True, name="viewkey-download-scheduler")
            self._thread.start()

    def enqueue(self, keys: list[str]) -> int:
        self.ensure_started()
        added = 0
        with self.lock:
            self.paused = False
            known = set(self.queue) | set(self.running) | set(self.deferred)
            for key in keys:
                if key in known:
                    continue
                self.queue.append(key)
                known.add(key)
                added += 1
        self.wake.set()
        return added

    def pause(self) -> None:
        with self.lock:
            self.paused = True
            self.cancelled.update(self.running)
            self.queue.clear()
            self.deferred.clear()
        self.wake.set()

    def cancel(self, keys) -> None:
        removed = set(keys)
        with self.lock:
            self.queue[:] = [key for key in self.queue if key not in removed]
            for key in removed:
                self.deferred.pop(key, None)
            self.cancelled.update(removed & set(self.running))
        self.wake.set()

    def cool_down(self, seconds: float) -> None:
        """整体冷却：到点前 _fill 不启动任何新任务。"""
        with self.lock:
            self.cooldown_until = max(self.cooldown_until, time() + seconds)
        self.wake.set()

    def requeue(self, key: str, delay: float) -> None:
        """让任务先冷却一会儿再回到队列尾：把并发位腾给别人先下。"""
        with self.lock:
            self.deferred[key] = time() + max(delay, 0.0)
        self.wake.set()

    def should_stop(self, key: str) -> bool:
        return self.paused or key in self.cancelled

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "queued": list(self.queue),
                "running": list(self.running),
                "waiting": sorted(self.deferred),
            }

    # ---- 调度循环 ----

    def _loop(self) -> None:
        while True:
            try:
                self._fill()
            except Exception:  # 调度线程不能死，否则整个下载队列停摆
                log.exception("下载调度异常")
            self.wake.wait(0.5)
            self.wake.clear()

    def _fill(self) -> None:
        with self.lock:
            if self.paused:
                return
            limit = self.limit()
            if time() < self.cooldown_until:
                return
            # 冷却到点的任务回到队列尾
            now = time()
            for key, due in list(self.deferred.items()):
                if due > now:
                    continue
                self.deferred.pop(key, None)
                if key not in self.queue and key not in self.running:
                    self.queue.append(key)
            # 队列里可能混进了被删除、已下完或被暂停的条目，先清掉再补位
            self.queue[:] = [
                key for key in self.queue
                if key in store.videos and store.download_status.get(key, {}).get("state") == "queued"
            ]
            while self.queue and len(self.running) < limit:
                key = self.queue.pop(0)
                thread = Thread(target=self._run, args=(key,), daemon=True, name=f"viewkey-download-{key}")
                self.running[key] = thread
                thread.start()

    def _run(self, key: str) -> None:
        try:
            with store.lock:
                item = store.videos.get(key)
            if item is not None:
                download_one(key, item)
        except Exception as exc:
            log.exception("下载任务异常 %s", key)
            _set_status(key, state="failed", error=str(exc))
            # 异常路径同样要落盘，否则界面刷新后这条任务又变回「等待中」
            store.save_state()
        finally:
            with self.lock:
                self.running.pop(key, None)
                self.cancelled.discard(key)
            self.wake.set()


scheduler = DownloadScheduler()


def queue_download(viewkeys: list[str], workers: int, fragments: int, kind: str = "download") -> Job:
    """把任务放进全局队列；workers/fragments 只为兼容旧调用，并发一律以设置中心为准。"""
    requested = list(dict.fromkeys(viewkeys))
    with store.lock:
        active_keys = {
            key for key, status in store.download_status.items()
            if status.get("state") in {"queued", "downloading"}
        }
        # 已经在队列或正在下载的条目直接跳过，不让一个重复项把整批请求顶回去：
        # 之前只要有一条在跑就整批 409，用户看到的只是「服务器返回异常（HTTP 409）」
        valid = [key for key in requested if key in store.videos and key not in active_keys]
        skipped = len(requested) - len(valid)
        if not valid:
            if not any(key in store.videos for key in requested):
                raise HTTPException(409, "这些视频不在当前采集结果里，请重新采集后再下载")
            raise HTTPException(
                409,
                f"所选 {len(requested)} 个任务已在下载队列中，等它们下完再试",
            )
        for key in valid:
            store.dismissed_downloads.discard(key)
            for kind in ("stall", "drop"):
                _yield_counts.pop((key, kind), None)
            # 手动重试要重新给机会：用户可能刚换了代理节点，别拿上一轮的失败次数卡死
            _failed_streams.pop(key, None)
            current = store.download_status.get(key) or {}
            store.download_status[key] = {
                "state": "queued",
                # 续传时保留原进度，进度条不会莫名其妙归零
                "percent": current.get("percent", 0) if current.get("state") == "paused" else 0,
                "error": "",
            }
    store.save_state()
    job = new_job(kind)
    job.total = len(valid)
    job.skipped = skipped
    job.viewkeys = valid
    job.message = f"排队等待 0/{len(valid)}"
    scheduler.enqueue(valid)
    return job


def reconcile_download_state() -> None:
    """启动时只做状态收尾，绝不自动开始下载——下载必须由用户手动点。

    旧版本会在启动时把「上次被中断」的任务直接重新入队，程序一开就对着站点
    连开好几路请求，很容易刚启动就被限速。现在只把磁盘上已经完整的文件标成已完成，
    其余保持「已暂停」，等用户自己点「继续下载」或「下载全部未完成」。
    """
    existing = downloaded_keys()
    if not existing:
        return
    with store.lock:
        for key in existing:
            status = store.download_status.get(key)
            if status and status.get("state") != "completed":
                store.download_status[key] = {"state": "completed", "percent": 100, "error": ""}
    store.save_state()



def new_job(kind: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind)
    with store.lock:
        store.jobs[job.id] = job
    return job


def crawl_worker(job: Job, request: CrawlRequest) -> None:
    try:
        job.status = "running"
        config = SiteConfig.load(CONFIG_PATH)
        if request.mode == "author":
            # quote 同时完成 URL 转义和花括号转义，避免作者输入破坏 format 模板
            start_url = config.author_url.format(author=quote(request.author, safe=""))
        elif request.mode == "url":
            start_url = align_to_site(request.url, config)
        else:
            if request.category not in config.category_urls:
                raise RuntimeError(
                    f"当前站点没有「{request.category}」这个栏目，换一个分类或切换回原站点再试"
                )
            start_url = config.category_urls[request.category]
        start_url = urljoin(config.base_url, start_url)
        source = request.author or request.category or "custom"
        crawled: set[str] = set()
        with build_client(config, app_cookies(), app_proxy()) as client:
            crawler = Crawler(client, config)
            for item in crawler.crawl(start_url, request.pages):
                item.add_source(source, start_url)
                store.add(item)
                crawled.add(item.identity)
                job.current += 1
                job.message = f"已采集 {job.current} 个视频"
        store.replace_source_membership(source, crawled)
        store.save()
        job.status = "completed"
        job.message = f"采集完成，共 {job.current} 个视频"
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.message = "采集失败"


def check_stall(
    watch: dict[str, tuple[float, int]], key: str, downloaded: int, now: float
) -> float | None:
    """停滞看门狗：返回窗口内平均速率（B/s）表示判定为停滞，否则 None。

    只看 yt-dlp 的 speed 不够准（它自己做了平滑），这里用「窗口内
    downloaded_bytes 的净增长 / 实际经过时间」，能直接抓到 100 B/s 这种半死连接。
    """
    mark = watch.get(key)
    if mark is None:
        watch[key] = (now, downloaded)
        return None
    elapsed = now - mark[0]
    if elapsed < STALL_WINDOW_SECONDS:
        return None
    rate = (downloaded - mark[1]) / elapsed
    if rate < STALL_MIN_BPS:
        watch.pop(key, None)
        return rate
    watch[key] = (now, downloaded)
    return None


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: object) -> str:
    """去掉 ANSI 颜色码。

    yt-dlp 内部的速度字符串是 `\\x1b[0;32m 260.40KiB/s\\x1b[0m`，直接塞进网页
    会露出 `[0;32m` 这种乱码，所以凡是给前端看的字符串都要先过一遍这里。
    """
    return ANSI_ESCAPE.sub("", str(text or "")).strip()


def format_speed(bps: float) -> str:
    """把字节/秒格式化成 1.24 MB/s，代替 yt-dlp 带颜色码的字符串。"""
    if not bps or bps <= 0:
        return ""
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    value = float(bps)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.0f} {units[index]}" if index < 2 else f"{value:.2f} {units[index]}"


SITE_BLOCKED_HINT = (
    "站点临时拒绝访问（HTTP 403 / Cloudflare error 1106）：当前代理节点的出口 IP "
    "被站点判为异常流量。请等 1–2 分钟后再试，或换一个代理节点"
)
STREAM_FAIL_LIMIT = 3
SITE_COOLDOWN_SECONDS = 60
# 已完成的任务发现文件不见了时用的提示语。以它为前缀的失败记录会被当成
# 「用户自己删了文件」，不再在下载列表里显示成失败任务
MISSING_FILE_MARK = "本地文件不存在"


def _resolve_with_cooldown(crawler: Crawler, item: VideoItem, prefer_hd: bool) -> VideoItem:
    """解析视频页；碰到 403（Cloudflare 按 IP 封禁）先冷却再试一次。

    实测 1106 封禁是临时的：停手 1–2 分钟后再请求就恢复 200。
    """
    for attempt in range(2):
        try:
            return crawler.resolve(item, prefer_hd=prefer_hd)
        except httpx.HTTPStatusError as exc:
            blocked = exc.response.status_code == 403
            if not blocked or attempt:
                if blocked:
                    raise RuntimeError(SITE_BLOCKED_HINT) from exc
                raise
            log.info("站点临时限制当前 IP，冷却 %s 秒后重试", SITE_COOLDOWN_SECONDS)
            sleep(SITE_COOLDOWN_SECONDS)
    raise RuntimeError(SITE_BLOCKED_HINT)


def describe_site_error(exc: Exception) -> str:
    """把 httpx 的网络异常翻译成用户能照做的提示。"""
    if isinstance(exc, httpx.ProxyError):
        return "代理拒绝连接，请检查设置中心里的代理地址与端口是否正确"
    if isinstance(exc, httpx.ConnectTimeout):
        return "连接站点超时：代理节点可能已失效，请切换节点或重启代理后重试"
    if isinstance(exc, httpx.ReadTimeout):
        return "站点响应超时：代理不稳或当前出口 IP 被限流，稍后再试或换节点"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接到站点：代理没有启动或节点不可用"
    return str(exc) or exc.__class__.__name__


_listing_snapshots: dict[tuple[str, int], tuple[float, dict[str, VideoItem]]] = {}
_listing_snapshot_locks: dict[tuple[str, int], Lock] = {}
_listing_snapshot_guard = Lock()


def listing_reference(item: VideoItem, config: SiteConfig) -> tuple[str, int] | None:
    """任务来自哪个列表页（含页码），用于回源校正详情页地址。"""
    listing = (
        item.listing_url
        or item.listing_urls.get(item.source)
        or next(iter(item.listing_urls.values()), "")
        or config.category_urls.get(item.source)
    )
    if not listing:
        return None
    # 清单里记录的可能是上一个镜像站点的地址，统一切到当前站点
    listing = align_to_site(listing, config)
    # 采集时记录的列表页码优先；视频详情页 URL 本身不带 page 参数，
    # 旧清单回退到从 URL 解析，再不行按第 1 页处理
    page = item.listing_page
    if page <= 0:
        value = parse_qs(urlparse(item.page_url).query).get("page", ["1"])[0]
        page = int(value) if str(value).isdigit() else 1
    return (urljoin(config.base_url, listing), page)


def listing_snapshot(client: httpx.Client, config: SiteConfig, key: tuple[str, int]) -> dict[str, VideoItem]:
    """同一列表页只回源一次，几十个任务共用一份快照，不会每下一条就抓一遍列表。"""
    now = time()
    with _listing_snapshot_guard:
        cached = _listing_snapshots.get(key)
        if cached and now - cached[0] < LISTING_SNAPSHOT_TTL_SECONDS:
            return cached[1]
        guard = _listing_snapshot_locks.setdefault(key, Lock())
    with guard:
        with _listing_snapshot_guard:
            cached = _listing_snapshots.get(key)
            if cached and time() - cached[0] < LISTING_SNAPSHOT_TTL_SECONDS:
                return cached[1]
        listing_url, page = key
        response = client.get(fresh_listing_url(listing_page_url(listing_url, config.page_param, page, config.first_page)))
        response.raise_for_status()
        snapshot = {item.identity: item for item in parse_listing(response.text, str(response.url), config)}
        with _listing_snapshot_guard:
            _listing_snapshots[key] = (time(), snapshot)
        return snapshot


def refresh_page_url(item: VideoItem, config: SiteConfig, client: httpx.Client) -> VideoItem:
    """用最新列表页校正详情页地址；列表页取不到就退回旧地址，不因此中断下载。"""
    key = listing_reference(item, config)
    if key:
        try:
            fresh_link = listing_snapshot(client, config, key).get(item.identity)
        except (httpx.HTTPError, RuntimeError, ValueError):
            fresh_link = None
        if fresh_link:
            return replace(item, page_url=fresh_link.page_url)
    return replace(item, page_url=align_to_site(item.page_url, config))


# 卡住/连不上时，任务让出并发位回到队尾：先给队列里别的任务机会，自己冷却后再来。
# 站点 CDN 同一个出口 IP 往往只喂一条快连接，两条并发时被冷落的那条会掉到几十 B/s，
# 以前它原地重连（15/30 秒退避）等于把并发位和带宽一起浪费掉。
YIELD_LIMITS = {"stall": 6, "drop": 4}
YIELD_BASE_SECONDS = {"stall": 20, "drop": 30}
YIELD_MAX_SECONDS = 180
_yield_counts: dict[tuple[str, str], int] = {}
# 连续断线熔断：短时间内多条任务都连不上，说明是出口 IP 被站点掐了，
# 继续重试只会延长封禁，统一歇一会儿
BREAKER_WINDOW_SECONDS = 90
BREAKER_COOLDOWN_SECONDS = 60
_breaker_events: list[float] = []
_breaker_lock = Lock()


def _yield_task(key: str, error: str, kind: str) -> bool:
    """把卡住（stall）或连不上（drop）的任务退回队尾，返回是否已让位。"""
    limit = YIELD_LIMITS[kind]
    with store.lock:
        count = _yield_counts.get((key, kind), 0) + 1
        if count > limit:
            _yield_counts.pop((key, kind), None)
            return False
        _yield_counts[(key, kind)] = count
        current = store.download_status.get(key)
        if current is None:
            return False
        current = dict(current)
        current.update(state="queued", error="", speed="", speed_bps=0.0, speed_at=0.0)
        store.download_status[key] = current
    delay = min(YIELD_BASE_SECONDS[kind] * count, YIELD_MAX_SECONDS)
    scheduler.requeue(key, delay)
    log.info(
        "%s（%s），让出并发位，%s 秒后回到队列尾（第 %s 次）",
        "连接被掐断" if kind == "drop" else "任务卡住",
        error.strip()[:60], delay, count,
    )
    if kind == "drop":
        # 连接层断了就把这条直链作废：下一轮重新去视频页换链接，
        # 否则签名没过期的死链会被反复复用，重试多少次都是 0%
        with store.lock:
            dead = store.videos.get(key)
        if dead is not None:
            mark_stream_dead(key, dead)
        _note_drop()
    return True


def _note_drop() -> None:
    """连续两条任务都连不上就整体歇 1 分钟：此时重试只会让封禁更久。"""
    now = time()
    with _breaker_lock:
        _breaker_events[:] = [ts for ts in _breaker_events if now - ts < BREAKER_WINDOW_SECONDS]
        _breaker_events.append(now)
        if len(_breaker_events) < 2:
            return
        _breaker_events.clear()
        # 这是整条出口被掐，不是单条任务的锅：清掉断线计数，冷却后大家重新开始
        for key, kind in [item for item in _yield_counts if item[1] == "drop"]:
            _yield_counts.pop(key, None)
    scheduler.cool_down(BREAKER_COOLDOWN_SECONDS)
    log.warning("连续连接失败，判定出口 IP 被站点限制，暂停 %s 秒再试", BREAKER_COOLDOWN_SECONDS)


def download_one(key: str, item: VideoItem) -> None:
    """下载单个视频：解析直链 → 交给 yt-dlp。由调度器按并发上限逐条调用。"""
    config = SiteConfig.load(CONFIG_PATH)
    settings = settings_store.snapshot()
    if item_stream_fresh(item):
        # 直链签名还没过期：一个页面请求都不发，任务立刻开始跑字节。
        # 之前无论直链是否有效都先抓一次列表页，每条任务白等 2~4 秒，
        # 两个并发位轮流空转，排一批任务时前半分钟几乎没速度。
        store.add(replace(item, page_url=align_to_site(item.page_url, config)))
    else:
        with build_client(config, app_cookies(), app_proxy()) as client:
            crawler = Crawler(client, config)
            item = refresh_page_url(item, config, client)
            if scheduler.should_stop(key):
                _set_status(key, state="paused")
                return
            try:
                fresh = _resolve_with_cooldown(crawler, item, settings.prefer_hd)
            except (httpx.HTTPError, RuntimeError) as exc:
                # 页面都打不开时不要把一句「timed out」甩给用户：区分代理没通和站点限流
                raise RuntimeError(describe_site_error(exc)) from exc
            thumb_match = re.search(r"/thumb/(?:\d+_)?(\d+)\.jpg", fresh.thumbnail_url, re.I)
            media_match = re.search(r"/mp4\d*/(\d+)\.mp4", fresh.stream_url, re.I)
            if thumb_match and media_match and thumb_match.group(1) != media_match.group(1):
                raise RuntimeError(f"{fresh.viewkey} 的列表会话已过期，媒体源与封面不匹配，请重新采集后重试")
            host_path = stream_host_path(fresh.stream_url)
            previous, times = _failed_streams.get(key, ("", 0))
            if host_path and previous == host_path and times >= STREAM_FAIL_LIMIT:
                # 换了新签名还是同一个 CDN 主机连不上，再重试也只是继续撞墙
                host = host_path.split("/")[0]
                raise RuntimeError(
                    f"CDN 主机 {host} 连续 {times} 次拒绝连接，当前出口 IP 大概率被站点限制；"
                    "请切换代理节点（或换一个 Clash 节点）后再点「继续下载」"
                )
            item = fresh
            store.add(fresh)
            store.save()

    if scheduler.should_stop(key):
        _set_status(key, state="paused")
        return

    watch: dict[str, tuple[float, int]] = {}

    def on_progress(data: dict) -> None:
        if scheduler.should_stop(key):
            # 抛异常打断 yt-dlp，已下载部分留在 .part 里，继续时续传
            raise RuntimeError("任务已暂停或删除")
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        percent = round(downloaded * 100 / total, 1) if total else 0
        state = "completed" if data.get("status") == "finished" else "downloading"
        # 停滞看门狗：CDN 把速率压到几十 B/s 时 socket_timeout 不触发，
        # 连接会永远挂着；这里用「窗口内实际增长量」判断，主动断开重连。
        if state != "completed":
            stalled_rate = check_stall(watch, key, downloaded, time())
            if stalled_rate is not None:
                raise StalledError(f"连接停滞（{stalled_rate / 1024:.1f} KB/s）")
        with store.lock:
            if key not in store.download_status:
                return
            speed_bps = float(data.get("speed") or 0)
            store.download_status[key] = {
                "state": state,
                "percent": percent,
                # 速度自己格式化：yt-dlp 的 _speed_str 带 ANSI 颜色码
                "speed": format_speed(speed_bps),
                # 汇总总速度要用数值，_speed_str 是给人看的
                "speed_bps": speed_bps,
                "speed_at": time(),
                # 文件总大小与已下载量：任务还没结束时前端据此显示「已下 / 共」，
                # 下载完成后以磁盘上的真实文件大小为准
                "total_bytes": total,
                "downloaded_bytes": downloaded,
            }

    def on_retry(attempt: int, budget: int) -> None:
        # 具体是「停滞」还是「断线」由 downloader 自己记一行，这里不重复写原因
        log.debug("第 %s/%s 次尝试（%s）", attempt, budget, item.title[:24])

    # 真正开始下载了，状态从「等待下载」切到「正在下载」；yt-dlp 在连不上时
    # 可能十几秒都不触发进度钩子，不切状态的话界面上会一直显示「等待中」
    _set_status(key, state="downloading")
    downloader = BatchDownloader(
        get_video_dir(),
        1,
        settings.fragments,
        cookies=COOKIES_PATH if COOKIES_PATH.exists() else None,
        folder_mode=settings.folder_mode,
        progress_callback=lambda _item, data: on_progress(data),
        proxy=app_proxy(),
        retry_callback=lambda _item, attempt, attempts: on_retry(attempt, attempts),
        # 停滞不在这里硬重试：交给 _yield_stalled_task 让出并发位、排队尾再来
        stall_retries=0,
    )
    _item, error = downloader.download_one(item)
    stats = getattr(downloader, "last_stats", None) or {}
    if error:
        # 停滞 → 让位冷却；连不上（握手超时/一直掉线）→ 同样让位，别占着位子空转
        if is_stalled_error(error) and _yield_task(key, error, "stall"):
            return
        if is_drop_error(error) and _yield_task(key, error, "drop"):
            return
    title = item.title[:24]
    if error:
        log.info(
            "任务失败 %s：%s（尝试 %s 次，断线重连 %s 次，用时 %ss）",
            title, error.strip()[:90], stats.get("attempts", 1), stats.get("drops", 0), stats.get("seconds", 0),
        )
    else:
        for kind in ("stall", "drop"):
            _yield_counts.pop((key, kind), None)
        _failed_streams.pop(key, None)
        log.info(
            "任务完成 %s（尝试 %s 次，断线重连 %s 次，用时 %ss）",
            title, stats.get("attempts", 1), stats.get("drops", 0), stats.get("seconds", 0),
        )
    finish_download(key, error)


def describe_download_error(error: str) -> str:
    """yt-dlp 的原始报错太技术化，失败行前面补一句能照做的说明。"""
    if is_drop_error(error):
        return (
            "CDN 连接中断（代理节点可能已失效，或当前出口 IP 被站点限制）"
            f"；原始错误：{error.strip()[:140]}"
        )
    return error


def finish_download(key: str, error: str) -> None:
    """收尾：暂停/删除中断的记 paused，失败的记 failed，成功的记 completed。"""
    with store.lock:
        current = store.download_status.get(key)
        if current is None:
            return  # 任务已被用户删除
        current = dict(current)
    if scheduler.should_stop(key) or "任务已暂停或删除" in (error or ""):
        current.update(state="paused", error="", speed="", speed_bps=0.0, speed_at=0.0)
    elif error:
        current.update(state="failed", error=describe_download_error(error), speed="", speed_bps=0.0, speed_at=0.0)
    else:
        current.update(state="completed", percent=100, error="", speed="", speed_bps=0.0, speed_at=0.0)
    with store.lock:
        store.download_status[key] = current
    store.save_state()


def refresh_job(job: Job) -> Job:
    """一批点击对应一个 job，进度直接由条目状态推出来，不需要额外的看护线程。"""
    if job.kind != "download" or not job.viewkeys or job.cancelled:
        return job
    with store.lock:
        # 条目被删掉时状态会消失（missing）：同样算「已结束」，批次不会永远卡在运行中
        states = [store.download_status.get(key, {}).get("state", "missing") for key in job.viewkeys]
    total = len(states)
    finished = sum(state in {"completed", "failed", "missing"} for state in states)
    failed = sum(state == "failed" for state in states)
    paused = sum(state == "paused" for state in states)
    job.total = total
    job.current = finished
    if job.paused:
        job.status = "paused"
        job.message = f"已暂停 {paused}/{total}"
        return job
    if finished + paused >= total:
        if paused:
            job.status = "paused"
            job.message = "已暂停，点击继续下载"
        elif failed:
            job.status = "failed"
            job.error = f"{failed} 个任务失败"
            job.message = f"完成 {finished - failed}/{total}，{failed} 个失败"
        else:
            job.status = "completed"
            job.error = ""
            job.message = f"下载完成 {total}/{total}"
        return job
    job.error = ""
    if any(state == "downloading" for state in states):
        job.status = "running"
        job.message = f"正在下载 {job.current}/{total}"
    else:
        job.status = "queued"
        job.message = f"排队等待 {job.current}/{total}"
    return job


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _item_payload(item: VideoItem, config: SiteConfig | None = None, hosts: set[str] | None = None) -> dict:
    # identity 是 viewkey 为空时的去重主键，前端用它做选择/下载的键
    payload = asdict(item)
    payload["identity"] = item.identity
    if config is not None:
        # 切站点后，历史条目里的旧域名地址要对齐到当前站点，否则缩略图会裂开
        # 该条目自己的页面域名也算「站点自身」，即使它在列表里已被删掉
        allowed = set(hosts or ())
        try:
            page_host = urlparse(payload["page_url"]).netloc.lower()
        except ValueError:
            page_host = ""
        if page_host:
            allowed.add(page_host)
        payload["page_url"] = align_to_site(payload["page_url"], config)
        payload["thumbnail_url"] = align_media_url(payload["thumbnail_url"], config, allowed)
    return payload


@app.get("/api/catalog")
def catalog(category: str | None = None) -> dict:
    with store.lock:
        items = list(store.videos.values())
    config = SiteConfig.load(CONFIG_PATH)
    hosts = site_hosts()
    with store.lock:
        videos = [
            _item_payload(item, config, hosts) for item in reversed(items)
            if not category or category in item.sources or category == item.source
        ]
        statuses = dict(store.download_status)
    return {"videos": videos, "download_status": statuses}


# ---- 列表缓存 / 全站搜索 / 预取 ----

DEFAULT_SEARCH_URL = "/search_result.php?search_type=search_videos&search_id={query}&min_duration="
DEFAULT_VIDEO_URL = "/view_video.php?viewkey={viewkey}"
VIEWKEY_PATTERN = re.compile(r"[0-9a-f]{8,40}", re.I)
MISSING_VIDEO_MARKERS = ("视频不存在", "视频已被删除", "页面不存在", "video not found")

# 关掉后不预取下一页（测试里方便控制）
PRELOAD_ENABLED = True

_listing_cache: ListingCache | None = None
_listing_cache_lock = Lock()


def listing_cache() -> ListingCache:
    global _listing_cache
    directory = DATA_DIR / "cache" / "listings"
    with _listing_cache_lock:
        if _listing_cache is None or _listing_cache.directory != directory:
            _listing_cache = ListingCache(directory)
        return _listing_cache


def search_url_for(config: SiteConfig, query: str) -> str:
    template = (config.search_url or "").strip() or DEFAULT_SEARCH_URL
    return urljoin(config.base_url, template.replace("{query}", quote_plus(query)))


def video_url_for(config: SiteConfig, viewkey: str) -> str:
    template = (config.video_url or "").strip() or DEFAULT_VIDEO_URL
    return urljoin(config.base_url, template.replace("{viewkey}", viewkey))


def listing_source(config: SiteConfig, kind: str, ident: str) -> str:
    if kind == "search":
        return search_url_for(config, ident)
    return urljoin(config.base_url, config.category_urls[ident])


def extract_viewkey(query: str, config: SiteConfig | None = None) -> str:
    """用户可能直接粘视频地址，或输入纯 ID。

    两种站点的 ID 形态不同：91porn 是 16 位十六进制，91pinse 是纯数字
    （``/v/475058``）。粘地址时按当前站点的规则取，纯 ID 两种都认。
    """
    raw = (query or "").strip()
    if not raw:
        return ""
    if "://" in raw or raw.startswith("/"):
        if config is not None:
            return viewkey_from_url(raw, config)
        return parse_qs(urlparse(raw).query).get("viewkey", [""])[0].strip()
    if VIEWKEY_PATTERN.fullmatch(raw):
        return raw.lower()
    if config is not None and re.fullmatch(r"\d{1,12}", raw):
        return raw
    return ""


def _single_pagination() -> dict:
    return {"page": 1, "has_previous": False, "has_next": False, "last_visible_page": 1}


def fetch_listing_page(
    config: SiteConfig,
    kind: str,
    ident: str,
    page: int,
    *,
    refresh: bool = False,
    warmup: bool = True,
) -> dict:
    """取一页列表：命中缓存直接返回，否则回源并写回缓存。"""
    base = listing_source(config, kind, ident)
    key = cache_key(config.host, kind, ident, page)
    cache = listing_cache()
    if not refresh:
        cached = cache.get(key, ttl_for(page))
        if cached is not None:
            return {
                "items": cached.items(),
                "pagination": cached.pagination,
                "url": cached.url or base,
                "cached": True,
                "cache_age": cached.age,
            }
    page_url = fresh_listing_url(listing_page_url(base, config.page_param, page, config.first_page))
    try:
        with build_client(config, app_cookies(), app_proxy(), warmup=warmup) as client:
            response = client.get(page_url)
            response.raise_for_status()
    except (httpx.HTTPError, RuntimeError) as exc:
        detail = str(exc)[:160] or type(exc).__name__
        raise HTTPException(502, f"列表页获取失败：{detail}") from exc
    items = parse_listing(response.text, str(response.url), config)
    pagination = pagination_info(response.text, page)
    for item in items:
        item.listing_page = page
    cache.put(key, base, page, items, pagination)
    return {"items": items, "pagination": pagination, "url": base, "cached": False, "cache_age": 0.0}


def _prefetch_fetch(kind: str, ident: str, page: int) -> None:
    config = SiteConfig.load(CONFIG_PATH)
    cache = listing_cache()
    key = cache_key(config.host, kind, ident, page)
    if cache.is_fresh(key, ttl_for(page)):
        return
    fetch_listing_page(config, kind, ident, page, warmup=False)


prefetcher = PrefetchQueue(_prefetch_fetch)


def schedule_preload(kind: str, ident: str, page: int, pagination: dict) -> None:
    """把下一页悄悄抓下来，前端滚到底时直接命中缓存。"""
    if not PRELOAD_ENABLED or not pagination.get("has_next"):
        return
    if page >= 10000:
        return
    prefetcher.submit(kind, ident, page + 1)


def _remember(items: list[VideoItem]) -> None:
    """写回本地目录，只有内容真的变了才落盘。"""
    dirty = False
    for item in items:
        with store.lock:
            previous = store.videos.get(item.identity)
            if previous is None or previous.title != item.title or previous.thumbnail_url != item.thumbnail_url:
                dirty = True
        store.add(item)
    if dirty:
        store.save()


def _listing_response(
    config: SiteConfig,
    result: dict,
    *,
    kind: str,
    ident: str,
    source: str,
    query: str = "",
    matched_by: str = "",
) -> dict:
    items = result.get("items", [])
    listing_url = result.get("url", "")
    for item in items:
        # 单条视频没有列表页，留空可省掉下载前多一次回源
        item.add_source(source, "" if kind == "video" else listing_url)
    _remember(items)
    with store.lock:
        statuses = dict(store.download_status)
    return {
        "videos": [_item_payload(item) for item in items],
        "download_status": statuses,
        "downloaded_keys": sorted(downloaded_keys().intersection(item.identity for item in items)),
        "pagination": result.get("pagination") or _single_pagination(),
        "cached": bool(result.get("cached")),
        "cache_age": round(float(result.get("cache_age") or 0.0), 1),
        "mode": "search" if kind in {"search", "video"} else "category",
        "category": ident if kind == "category" else "",
        "query": query or (ident if kind == "search" else ""),
        "matched_by": matched_by,
        "source_url": listing_url,
    }


def probe_video(config: SiteConfig, viewkey: str) -> dict | None:
    """按 viewkey 直接打开视频页；视频不存在时返回 None，让上层退回关键词搜索。"""
    key = cache_key(config.host, "video", viewkey, 1)
    cache = listing_cache()
    cached = cache.get(key, VIDEO_TTL)
    if cached is not None:
        items = cached.items()
        if not items:
            return None
        return {
            "items": items,
            "pagination": cached.pagination or _single_pagination(),
            "url": cached.url,
            "cached": True,
            "cache_age": cached.age,
        }
    url = video_url_for(config, viewkey)
    try:
        with build_client(config, app_cookies(), app_proxy()) as client:
            response = client.get(url)
    except (httpx.HTTPError, RuntimeError):
        return None
    if response.status_code >= 400:
        return None
    item = VideoItem(page_url=str(response.url), viewkey=viewkey)
    parsed = parse_video_page(response.text, str(response.url), item, config)
    if not parsed.title or any(marker in response.text for marker in MISSING_VIDEO_MARKERS):
        # 记一个空结果，同一个错 ID 不会再反复打站点
        cache.put(key, url, 1, [], _single_pagination())
        return None
    parsed.listing_page = 1
    cache.put(key, url, 1, [parsed], _single_pagination())
    return {"items": [parsed], "pagination": _single_pagination(), "url": url, "cached": False, "cache_age": 0.0}


def _check_page(page: int) -> None:
    if page < 1 or page > 10000:
        raise HTTPException(400, "页码必须在 1 到 10000 之间")


@app.get("/api/browse")
def browse(category: str = "latest", page: int = 1, refresh: bool = False) -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    if category not in config.category_urls:
        raise HTTPException(400, "未知分类")
    _check_page(page)
    if category == "top_day" and page != 1:
        raise HTTPException(400, "每日排行只有一页")
    result = fetch_listing_page(config, "category", category, page, refresh=refresh)
    schedule_preload("category", category, page, result["pagination"])
    return _listing_response(config, result, kind="category", ident=category, source=category)


@app.get("/api/search")
def search(q: str = "", page: int = 1, refresh: bool = False) -> dict:
    """全站搜索：标题、用户名走站点搜索页；纯 viewkey / 视频地址直接开视频页。"""
    query = " ".join((q or "").split())
    if not query:
        raise HTTPException(400, "请输入搜索关键词")
    if len(query) > 80:
        raise HTTPException(400, "搜索关键词最多 80 个字符")
    _check_page(page)
    config = SiteConfig.load(CONFIG_PATH)
    if page == 1 and not refresh:
        viewkey = extract_viewkey(query, config)
        if viewkey:
            hit = probe_video(config, viewkey)
            if hit is not None:
                return _listing_response(
                    config, hit, kind="video", ident=query, source="viewkey",
                    query=query, matched_by="viewkey",
                )
    result = fetch_listing_page(config, "search", query, page, refresh=refresh)
    schedule_preload("search", query, page, result["pagination"])
    return _listing_response(config, result, kind="search", ident=query, source=f"search:{query}")


@app.get("/api/cache")
def cache_stats() -> dict:
    stats = listing_cache().stats()
    stats["ttl_first_page"] = ttl_for(1)
    stats["ttl_deep_page"] = ttl_for(2)
    stats["preload_enabled"] = bool(PRELOAD_ENABLED)
    return stats


@app.post("/api/cache/clear")
def cache_clear() -> dict:
    removed = listing_cache().clear()
    return {"removed": removed}


THUMB_EXTENSIONS = ("jpg", "png", "gif", "webp", "avif")
THUMB_MEDIA_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/avif": "avif"}


def _thumb_cache_path(url: str) -> Path | None:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    for ext in THUMB_EXTENSIONS:
        candidate = THUMB_DIR / f"{digest}.{ext}"
        if candidate.exists() and candidate.stat().st_size:
            return candidate
    return None


@app.get("/api/thumb")
def thumbnail(u: str) -> Response:
    """缩略图也走设置里的代理。

    CDN 图片原本由浏览器直接请求，程序里的代理对它不生效，所以这里由后端代取一次，
    顺带落到本地缓存，翻页回来不用再回源。
    """
    target = (u or "").strip()
    if not target.startswith(("http://", "https://")):
        raise HTTPException(400, "缩略图地址无效")
    cached = _thumb_cache_path(target)
    if cached:
        return FileResponse(cached, headers={"Cache-Control": "public, max-age=604800"})
    config = SiteConfig.load(CONFIG_PATH)
    try:
        with make_httpx_client(config, {}, app_proxy()) as client:
            response = client.get(target, headers={"Referer": f"{config.base_url}/"}, timeout=15.0)
            response.raise_for_status()
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, f"缩略图获取失败：{type(exc).__name__}") from exc
    media_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not media_type.startswith("image/"):
        raise HTTPException(502, "缩略图返回的不是图片")
    ext = THUMB_MEDIA_EXT.get(media_type, "jpg")
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(target.encode("utf-8")).hexdigest()
        temporary = THUMB_DIR / f"{digest}.{ext}.part"
        temporary.write_bytes(response.content)
        temporary.replace(THUMB_DIR / f"{digest}.{ext}")
    except OSError:
        pass
    return Response(
        content=response.content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=604800"},
    )


def collect_downloads() -> dict:
    """汇总下载管理的任务列表，供 /api/downloads 与批量续传共用。"""
    video_dir = get_video_dir()
    try:
        files = list(video_dir.rglob("*")) if video_dir.exists() else []
    except OSError:
        files = []
    media_files = [
        path for path in files
        if _is_media_candidate(path)
    ]
    files_by_key: dict[str, Path] = {}
    for path in media_files:
        match = re.search(r"\[([^\]]+)\]", path.name)
        if match and (match.group(1) not in files_by_key or _file_mtime(path) > _file_mtime(files_by_key[match.group(1)])):
            files_by_key[match.group(1)] = path
    entries: list[dict] = []
    # 已完成、但磁盘上文件已经不在了：按「用户看完删掉了」处理，从下载管理里收掉。
    # 之前这种情况被改判成 failed 写回 state.json，结果删一次文件就多一批失败任务。
    removed: set[str] = set()
    dir_ready = video_dir.exists()
    with store.lock:
        videos = list(store.videos.values())
        statuses = dict(store.download_status)
        dismissed = set(store.dismissed_downloads)
    config = SiteConfig.load(CONFIG_PATH)
    hosts = site_hosts()
    for item in reversed(videos):
        if item.identity in dismissed:
            continue
        matched = files_by_key.get(item.identity)
        status = statuses.get(item.identity)
        if not matched and not status:
            continue
        state = status.get("state", "completed") if status else "completed"
        prior_error = str((status or {}).get("error") or "")
        media_valid = False
        validation_error = MISSING_FILE_MARK
        if matched:
            media_valid, validation_error = validate_media_file(matched)
        if matched is None and dir_ready:
            if state == "completed":
                removed.add(item.identity)
                continue
            # 1.1.15 及更早的版本会把「已完成但文件被删」写成这种失败，一并收掉
            if state == "failed" and prior_error.startswith(MISSING_FILE_MARK):
                removed.add(item.identity)
                continue
        if matched and media_valid and state == "failed" and prior_error.startswith(MISSING_FILE_MARK):
            # 文件被恢复（从回收站找回 / 重新拷回来），状态跟着回到已完成
            state = "completed"
            status = {**status, "state": "completed", "percent": 100, "error": ""}
        if state == "completed" and not media_valid and dir_ready:
            state = "failed"
            status = {
                **(status or {}),
                "state": "failed",
                "percent": 0,
                "error": f"{validation_error}，点击重新下载",
            }
        file_size = _file_size(matched)
        total_bytes = int((status or {}).get("total_bytes") or 0)
        entries.append(
            {
                "viewkey": item.identity,
                "title": item.title,
                "thumbnail_url": align_media_url(item.thumbnail_url, config, hosts),
                "state": state,
                "percent": status.get("percent", 100 if matched else 0) if status else 100,
                # 速度统一由后端格式化，避免 yt-dlp 的 ANSI 颜色码漏到页面上
                "speed": format_speed(float((status or {}).get("speed_bps") or 0)),
                "speed_bps": float(status.get("speed_bps") or 0) if status else 0.0,
                "speed_at": float(status.get("speed_at") or 0) if status else 0.0,
                "error": status.get("error", "") if status else "",
                "file_name": matched.name if matched else "",
                # 文件大小以磁盘上的真实文件为准；任务还没结束时用 yt-dlp 探到的总大小
                "file_size": file_size or total_bytes,
                "downloaded_bytes": int(status.get("downloaded_bytes") or 0) if status else 0,
                # 时长来自采集时的列表数据（如 12:34），给下载列表当参考信息
                "duration": item.duration,
                "modified": _file_mtime(matched),
            }
        )
    if removed:
        with store.lock:
            for key in removed:
                store.download_status.pop(key, None)
        for key in sorted(removed):
            log.info("已完成的任务文件已不存在，按已删除处理，移出下载列表：%s", key)
        store.save_state()
    counts = {name: sum(entry["state"] == name for entry in entries) for name in ("queued", "downloading", "completed", "failed", "paused")}
    # 左下角任务卡片要显示的「当前几个任务在下载 + 总体速度」在这里一次算好，
    # 前端只需按刷新频率读 /api/downloads，不必自己拼
    active_entries = [entry for entry in entries if entry["state"] in {"queued", "downloading"}]
    now = time()
    live = [
        entry for entry in active_entries
        # 超过 20 秒没更新速度说明这条卡住了，别把上一秒的数字一直挂在那儿
        if entry["state"] == "downloading" and now - (entry["speed_at"] or 0) <= 20
    ]
    active = {
        "count": len(active_entries),
        "downloading": sum(entry["state"] == "downloading" for entry in active_entries),
        "queued": sum(entry["state"] == "queued" for entry in active_entries),
        "speed_bps": round(sum(entry["speed_bps"] for entry in live), 1),
        "percent": round(sum(entry["percent"] or 0 for entry in active_entries) / len(active_entries), 1) if active_entries else 0,
    }
    return {"downloads": entries, "counts": counts, "active": active, "total_size": sum(entry["file_size"] for entry in entries)}


@app.get("/api/downloads")
def downloads() -> dict:
    return collect_downloads()


@app.get("/api/downloads/unfinished")
def unfinished_downloads() -> dict:
    """下载管理里「待续传」的任务清单。

    只认下载管理自己的列表：已从管理里删除的任务、以及采集到但从没进过下载队列的
    视频都不会被算进来。已经在排队/下载中的条目单独作为 active 返回，
    不混进待续传清单——它们本来就在跑，再提交一次只会被后端当成重复项拒掉。
    """
    entries = collect_downloads()["downloads"]
    pending: list[str] = []
    active: list[str] = []
    for entry in entries:
        if entry["state"] == "completed":
            continue
        target = active if entry["state"] in {"queued", "downloading"} else pending
        target.append(entry["viewkey"])
    return {
        "viewkeys": pending,
        "count": len(pending),
        "active": active,
        "active_count": len(active),
        "total_count": len(pending) + len(active),
    }


def _is_media_candidate(path: Path) -> bool:
    try:
        return path.is_file() and path.name != ".downloaded.txt" and path.suffix.lower() not in {".part", ".ytdl", ".temp"}
    except OSError:
        return False


def _file_size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


def _file_mtime(path: Path | None) -> float:
    try:
        return path.stat().st_mtime if path else 0
    except OSError:
        return 0


@app.post("/api/crawl")
def start_crawl(request: CrawlRequest) -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    if request.mode == "category" and request.category not in config.category_urls:
        raise HTTPException(400, "未知分类")
    if request.mode == "author" and not request.author.strip():
        raise HTTPException(400, "请输入作者 UID")
    if request.mode == "url" and not request.url.strip():
        raise HTTPException(400, "请输入列表 URL")
    with store.lock:
        if any(job.kind == "crawl" and job.status in {"queued", "running"} for job in store.jobs.values()):
            raise HTTPException(409, "已有采集任务正在运行")
    job = new_job("crawl")
    Thread(target=crawl_worker, args=(job, request), daemon=True).start()
    return asdict(job)


@app.post("/api/downloads")
def start_download(request: DownloadRequest) -> dict:
    job = queue_download(request.viewkeys, request.workers, request.fragments)
    return asdict(job)


@app.post("/api/downloads/pause")
def pause_downloads() -> dict:
    """暂停全部下载：中断在跑的 yt-dlp（.part 保留），排队中的直接记为已暂停。"""
    # 先停调度器：清空待下载队列并标记在跑的任务中断，避免刚暂停就补位开新的
    scheduler.pause()
    paused = 0
    with store.lock:
        for job in store.jobs.values():
            if job.kind == "download" and job.status in {"queued", "running"} and not job.cancelled:
                job.cancelled = True
                job.paused = True
                job.status = "paused"
                job.message = "已暂停，点击继续下载"
        for key, status in store.download_status.items():
            if status.get("state") in {"queued", "downloading"}:
                store.download_status[key] = {
                    "state": "paused",
                    "percent": status.get("percent", 0),
                    "error": "",
                    "speed": "",
                    "speed_bps": 0.0,
                    "speed_at": 0.0,
                }
                paused += 1
    store.save_state()
    return {"paused": paused}


@app.post("/api/downloads/resume")
def resume_downloads() -> dict:
    """继续下载：把已暂停的任务重新入队，yt-dlp 接着 .part 续传而不是重下。"""
    with store.lock:
        keys = [
            key for key, status in store.download_status.items()
            if status.get("state") == "paused" and key in store.videos
        ]
    if not keys:
        return {"resumed": 0, "job": None}
    current = settings_store.snapshot()
    job = queue_download(keys, current.workers, current.fragments)
    return {"resumed": job.total, "job": asdict(job)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    with store.lock:
        job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return asdict(refresh_job(job))


@app.post("/api/catalog/remove")
def remove_items(request: RemoveRequest) -> dict:
    with store.lock:
        for key in request.viewkeys:
            store.videos.pop(key, None)
            store.download_status.pop(key, None)
            store.dismissed_downloads.discard(key)
    _detach_from_jobs(set(request.viewkeys))
    store.save()
    store.save_state()
    return {"removed": len(request.viewkeys)}


def _detach_from_jobs(removed: set[str]) -> None:
    """把删掉的任务从批次任务里剪掉，避免批次永远停在「运行中」。"""
    if not removed:
        return
    scheduler.cancel(removed)
    with store.lock:
        for job in store.jobs.values():
            if job.kind != "download" or not removed.intersection(job.viewkeys):
                continue
            # 同批任务只剪掉被删的那条，剩下的继续按队列下完
            job.viewkeys = [key for key in job.viewkeys if key not in removed]
            if not job.viewkeys and job.status in {"queued", "running"}:
                job.cancelled = True
                job.status = "failed"
                job.error = "任务已删除"


@app.post("/api/downloads/remove")
def remove_downloads(request: RemoveRequest) -> dict:
    removed = set(request.viewkeys)
    _detach_from_jobs(removed)
    with store.lock:
        for key in removed:
            store.download_status.pop(key, None)
            store.dismissed_downloads.add(key)
    store.save_state()
    return {"removed": len(request.viewkeys)}


@app.get("/api/config")
def config_info() -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    book = site_book()
    active = book.active()
    return {
        "base_url": config.base_url,
        "categories": list(config.category_urls),
        "profile": config.profile,
        "site_id": active.id if active else "",
        "sites": [entry.payload() for entry in book.snapshot()],
    }


def settings_payload() -> dict:
    current = settings_store.snapshot().model_dump()
    current["download_dir"] = str(get_video_dir().resolve())
    current["proxy_ready"] = socks_available()
    return current


@app.get("/api/settings")
def get_settings() -> dict:
    return settings_payload()


@app.put("/api/settings")
def update_settings(request: AppSettings) -> dict:
    directory = Path(request.download_dir).expanduser()
    if not directory.is_absolute():
        directory = DATA_DIR / directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".viewkey-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise HTTPException(400, f"下载目录不可写：{exc}") from exc
    try:
        request.proxy = normalize_proxy(request.proxy)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if request.proxy and proxy_needs_socks(request.proxy) and not socks_available():
        raise HTTPException(400, "SOCKS 代理缺少 socks 依赖，请改用 HTTP 代理（如 127.0.0.1:7897）")
    with settings_store.lock:
        settings_store.value = request
    settings_store.save()
    return settings_payload()


class SiteRequest(BaseModel):
    name: str = ""
    base_url: str
    # 留空则按域名自动识别（含 pinse 关键字的走 91pinse 模板）
    profile: str = ""


class SiteSwitchRequest(BaseModel):
    site_id: str


class ProxyTestRequest(BaseModel):
    proxy: str = ""


def _rebind_cookies(base_url: str) -> None:
    try:
        rebind_cookie_domain(COOKIES_PATH, base_url)
    except OSError:
        pass


@app.post("/api/proxy/test")
def test_proxy(request: ProxyTestRequest) -> dict:
    """用当前站点首页做一次真实请求，验证代理是否可用。"""
    try:
        proxy = normalize_proxy(request.proxy)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if proxy and proxy_needs_socks(proxy) and not socks_available():
        return {"ok": False, "message": "SOCKS 代理缺少 socks 依赖，请改用 HTTP 代理"}
    config = SiteConfig.load(CONFIG_PATH)
    label = proxy or "系统代理/直连"
    started = time()
    try:
        with make_httpx_client(config, {}, proxy) as client:
            response = client.get(urljoin(config.base_url, "/"), timeout=httpx.Timeout(15.0, connect=6.0))
        elapsed = (time() - started) * 1000
        if response.status_code >= 400:
            return {"ok": False, "message": f"{label} 可用，但站点返回 HTTP {response.status_code}"}
        return {"ok": True, "message": f"{label} 连接正常（{elapsed:.0f} ms，HTTP {response.status_code}）"}
    except ImportError:
        return {"ok": False, "message": "SOCKS 代理缺少 socks 依赖，请改用 HTTP 代理"}
    except httpx.ProxyError:
        return {"ok": False, "message": f"{label} 无法建立隧道，请确认代理是否允许访问该站点"}
    except httpx.ConnectError:
        return {"ok": False, "message": f"{label} 连接不上，请确认代理软件已启动、端口填写正确"}
    except httpx.TimeoutException:
        return {"ok": False, "message": f"{label} 连接超时，请检查代理或网络"}
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"{label} 请求失败：{type(exc).__name__}"}
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc)}


@app.get("/api/sites")
def list_sites() -> dict:
    book = site_book()
    active = book.active()
    return {
        "sites": [entry.payload() for entry in book.snapshot()],
        "active_id": active.id if active else "",
    }


@app.post("/api/sites")
def add_site(request: SiteRequest) -> dict:
    book = site_book()
    try:
        entry = book.add(request.name, request.base_url, request.profile)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"site": entry.payload(), "sites": [item.payload() for item in book.snapshot()], "active_id": book.active_id}


@app.delete("/api/sites/{site_id}")
def delete_site(site_id: str) -> dict:
    book = site_book()
    try:
        book.remove(site_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    active = book.active()
    if active:
        _rebind_cookies(active.base_url)
    return {"sites": [item.payload() for item in book.snapshot()], "active_id": book.active_id}


@app.post("/api/sites/active")
def switch_site(request: SiteSwitchRequest) -> dict:
    book = site_book()
    try:
        entry = book.set_active(request.site_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _rebind_cookies(entry.base_url)
    # 切站点等于换了一整套页面结构：列表页快照必须清掉，否则新站点会拿着
    # 旧站点的卡片去匹配详情页地址
    _listing_snapshots.clear()
    _listing_snapshot_locks.clear()
    return {
        "site": entry.payload(),
        "sites": [item.payload() for item in book.snapshot()],
        "active_id": book.active_id,
        "base_url": entry.base_url,
    }


def save_session_cookies(session: httpx.Client) -> int:
    """把登录会话的 Cookie 以 Netscape 格式落盘，供采集/下载使用。"""
    # session 可能是包着 httpx.Client 的 RateLimitedClient
    jar = getattr(session, "_client", session).cookies.jar
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in jar:
        domain = cookie.domain or ""
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            cookie.path or "/",
            "TRUE" if cookie.secure else "FALSE",
            str(int(cookie.expires or 2147483647)),
            cookie.name,
            cookie.value or "",
        ]))
    temporary = COOKIES_PATH.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(COOKIES_PATH)
    return len(lines) - 1


@app.get("/api/account")
def account_status() -> dict:
    cookies = app_cookies()
    return {"has_cookies": bool(cookies), "cookie_count": len(cookies)}


@app.get("/api/account/captcha")
def account_captcha() -> dict:
    """新建登录会话并返回绑定该会话的验证码图片。"""
    config = SiteConfig.load(CONFIG_PATH)
    # 复用带限速和重试的会话客户端：站点时常瞬断 TLS 连接，裸 httpx 单次请求会直接失败
    session = build_client(config, {}, app_proxy())
    try:
        session.get(urljoin(config.base_url, "/login.php"))
        image = session.get(urljoin(config.base_url, "/captcha.php"))
        image.raise_for_status()
    except (httpx.HTTPError, RuntimeError):
        session.close()
        raise HTTPException(502, "获取验证码失败，请稍后重试")
    sid = uuid.uuid4().hex[:12]
    with _login_sessions_lock:
        _expire_login_sessions()
        _login_sessions[sid] = (time(), session)
    mime = image.headers.get("content-type", "image/png").split(";")[0]
    return {"sid": sid, "image": f"data:{mime};base64,{base64.b64encode(image.content).decode()}"}


@app.post("/api/account/login")
def account_login(request: LoginRequest) -> dict:
    with _login_sessions_lock:
        entry = _login_sessions.pop(request.sid, None)
    if entry is None:
        raise HTTPException(400, "验证码会话已过期，请刷新验证码后重试")
    session = entry[1]
    config = SiteConfig.load(CONFIG_PATH)
    fingerprint = uuid.uuid4().hex
    try:
        response = session.post(
            urljoin(config.base_url, "/login.php"),
            data={
                "username": request.username,
                "password": request.password,
                "fingerprint": fingerprint,
                "fingerprint2": fingerprint,
                "captcha_input": request.captcha,
                "action_login": "Log In",
                "submit": "提交",
            },
            headers={"Referer": urljoin(config.base_url, "/login.php")},
        )
    except (httpx.HTTPError, RuntimeError):
        session.close()
        return {"ok": False, "message": "网络请求失败，请重试"}
    html = response.text
    lowered = html.lower()
    # BUG FIX (#8): Cookie must be saved BEFORE closing session
    # If we close first, save_session_cookies() can't read the cookie jar from RateLimitedClient
    if "logout" in lowered or "退出" in html:
        count = save_session_cookies(session)
        session.close()
        return {"ok": True, "message": "登录成功", "cookie_count": count}
    if "验证码" in html or "captcha" in lowered:
        message = "登录失败：验证码错误"
    elif "密码" in html or "password" in lowered:
        message = "登录失败：用户名或密码错误"
    else:
        message = "登录失败，请检查账号信息后重试"
    session.close()
    return {"ok": False, "message": message}


@app.post("/api/account/cookies")
def save_cookies(request: CookieRequest) -> dict:
    cookies = parse_cookie_text(request.content)
    if not cookies:
        raise HTTPException(400, "无法解析 Cookie，请粘贴 Cookie 请求头或 Netscape 文件内容")
    # 统一转成 Netscape 格式落盘，解析器和 yt-dlp 都能直接使用
    config = SiteConfig.load(CONFIG_PATH)
    host = urlparse(config.base_url).netloc.split(":")[0]
    domain = host if host.startswith(".") else f".{host}"
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookies.items():
        lines.append("\t".join([domain, "TRUE", "/", "FALSE", "2147483647", name, value]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = COOKIES_PATH.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(COOKIES_PATH)
    return {"ok": True, "cookie_count": len(cookies)}


@app.delete("/api/account/cookies")
def clear_cookies() -> dict:
    COOKIES_PATH.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/account/verify")
def verify_login() -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    cookies = app_cookies()
    if not cookies:
        return {"logged_in": False, "has_cookies": False, "cookie_count": 0, "message": "尚未登录"}
    with build_client(config, cookies, app_proxy()) as client:
        response = client.get(urljoin(config.base_url, "/"))
        response.raise_for_status()
    html = response.text
    logged_in = "logout" in html.lower() or "退出" in html
    message = "已登录" if logged_in else "Cookie 无效或已过期，请重新登录"
    return {"logged_in": logged_in, "has_cookies": True, "cookie_count": len(cookies), "message": message}


def available_port(start: int = 8765) -> int:
    for port in range(start, start + 100):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("没有可用的本地端口")


def setup_logging() -> None:
    """把下载日志同时写进 downloads/app.log：掉速时能翻到「哪条任务、为什么重连」。"""
    root = logging.getLogger()
    if getattr(root, "_viewkey_configured", False):
        return
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            DATA_DIR / "app.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    root._viewkey_configured = True  # type: ignore[attr-defined]


def main() -> None:
    setup_logging()
    port = int(os.getenv("VIEWKEY_PORT", "8765"))
    host = os.getenv("VIEWKEY_HOST", "127.0.0.1")
    if host == "127.0.0.1" and not os.getenv("VIEWKEY_PORT"):
        port = available_port()
    url = f"http://127.0.0.1:{port}"
    if os.getenv("VIEWKEY_NO_BROWSER", "0").lower() not in {"1", "true", "yes"}:
        Timer(1.2, lambda: webbrowser.open(url)).start()
    # 只做状态收尾，不自动开始任何下载：下载必须由用户在界面上手动触发
    Thread(target=reconcile_download_state, daemon=True, name="viewkey-download-reconcile").start()
    print(f"91Fetch: {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
