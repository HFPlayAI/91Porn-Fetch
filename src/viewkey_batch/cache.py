"""列表页缓存与后台预取。

同一个分类/搜索页在 TTL 内重复访问直接命中缓存，不必每次都回源；预取则把下一页
提前抓下来，前端滚到底部时立刻就有内容，形成「无缝翻页」。

缓存键里带站点域名，配合站点列表切换时不会串数据。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread
from typing import Any
import hashlib
import json
import logging
import time

from .models import VideoItem


log = logging.getLogger(__name__)

# 第一页会不断冒出新视频，TTL 短一些；深页几乎不变，可以放久一点
FIRST_PAGE_TTL = 300.0
DEEP_PAGE_TTL = 1800.0
# 单条视频（按 viewkey 直接搜）变化更慢
VIDEO_TTL = 3600.0
MAX_MEMORY_ENTRIES = 400


def ttl_for(page: int) -> float:
    return FIRST_PAGE_TTL if page <= 1 else DEEP_PAGE_TTL


def cache_key(host: str, kind: str, ident: str, page: int) -> str:
    """``host`` 隔离站点，``kind`` 区分分类/搜索/单条视频。"""
    return f"{host}|{kind}|{ident.lower()}|{page}"


@dataclass(slots=True)
class CachedPage:
    url: str
    page: int
    saved_at: float
    raw_items: list[dict]
    pagination: dict

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.saved_at)

    def items(self) -> list[VideoItem]:
        """每次返回全新对象，避免缓存和本地库互相改同一个实例。"""
        items: list[VideoItem] = []
        for raw in self.raw_items:
            try:
                items.append(VideoItem.from_json(json.dumps(raw, ensure_ascii=False)))
            except (TypeError, ValueError):
                continue
        return items


class ListingCache:
    """磁盘 + 内存两层缓存，重启后仍然命中。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = Lock()
        self._memory: dict[str, CachedPage] = {}

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, key: str, ttl: float | None = None) -> CachedPage | None:
        with self.lock:
            entry = self._memory.get(key)
        if entry is None:
            entry = self._read(key)
        if entry is None:
            return None
        if ttl is not None and entry.age > ttl:
            return None
        return entry

    def is_fresh(self, key: str, ttl: float) -> bool:
        return self.get(key, ttl) is not None

    def put(self, key: str, url: str, page: int, items: list[VideoItem], pagination: dict) -> CachedPage:
        entry = CachedPage(
            url=url,
            page=page,
            saved_at=time.time(),
            raw_items=[json.loads(item.to_json()) for item in items],
            pagination=dict(pagination or {}),
        )
        with self.lock:
            self._memory[key] = entry
            self._prune()
        self._write(key, entry)
        return entry

    def clear(self) -> int:
        """清空缓存，返回删掉的文件数。"""
        with self.lock:
            self._memory.clear()
        removed = 0
        if self.directory.exists():
            for path in self.directory.glob("*.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def stats(self) -> dict:
        files = list(self.directory.glob("*.json")) if self.directory.exists() else []
        size = 0
        for path in files:
            try:
                size += path.stat().st_size
            except OSError:
                continue
        with self.lock:
            memory = len(self._memory)
        return {"entries": len(files), "bytes": size, "memory_entries": memory}

    def _prune(self) -> None:
        if len(self._memory) <= MAX_MEMORY_ENTRIES:
            return
        oldest = sorted(self._memory.items(), key=lambda pair: pair[1].saved_at)
        for key, _ in oldest[: len(self._memory) - MAX_MEMORY_ENTRIES]:
            self._memory.pop(key, None)

    def _read(self, key: str) -> CachedPage | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = CachedPage(
                url=str(raw.get("url", "")),
                page=int(raw.get("page", 1)),
                saved_at=float(raw.get("saved_at", 0)),
                raw_items=list(raw.get("items", [])),
                pagination=dict(raw.get("pagination", {})),
            )
        except (OSError, ValueError, TypeError):
            return None
        with self.lock:
            self._memory[key] = entry
        return entry

    def _write(self, key: str, entry: CachedPage) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            content = json.dumps(
                {
                    "url": entry.url,
                    "page": entry.page,
                    "saved_at": entry.saved_at,
                    "items": entry.raw_items,
                    "pagination": entry.pagination,
                },
                ensure_ascii=False,
            )
            temporary = self._path(key).with_suffix(".json.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(self._path(key))
        except OSError:
            # 缓存写不进去不影响功能，下一次回源就是
            log.debug("列表缓存写入失败 %s", key, exc_info=True)


class PrefetchQueue:
    """单线程预取队列：串行抓取，避免和前台请求一起把站点打崩。"""

    def __init__(self, fetch: Callable[..., Any], *, delay: float = 0.6, maxsize: int = 48) -> None:
        self._fetch = fetch
        self._delay = delay
        self._queue: Queue = Queue(maxsize=maxsize)
        self._pending: set[tuple] = set()
        self._lock = Lock()
        self._thread: Thread | None = None
        self.enabled = True

    def submit(self, *task: Any) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if task in self._pending:
                return False
            self._pending.add(task)
            if self._thread is None or not self._thread.is_alive():
                self._thread = Thread(target=self._run, name="viewkey-prefetch", daemon=True)
                self._thread.start()
        try:
            self._queue.put_nowait(task)
        except Full:
            with self._lock:
                self._pending.discard(task)
            return False
        return True

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if self._delay:
                    time.sleep(self._delay)
                self._fetch(*task)
            except Exception:  # 预取失败不能影响前台
                log.debug("预取失败 %s", task, exc_info=True)
            finally:
                with self._lock:
                    self._pending.discard(task)
                self._queue.task_done()
