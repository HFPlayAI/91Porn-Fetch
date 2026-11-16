from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from functools import lru_cache
import logging
from collections.abc import Callable
import re
import shutil
import subprocess
import time

from yt_dlp import YoutubeDL

from .models import VideoItem


log = logging.getLogger(__name__)
# 低于该阈值的文件视为 CDN 空响应壳。正常视频即使很短也很少小于 64KB，
# 之前的 256KB 阈值会误杀合法的小体积视频。
MIN_MEDIA_BYTES = 64 * 1024

# MPEG-TS 的包长与同步字节：HLS 分片常见形态，分片文件常被命名成 .jpg/.mp4
TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """找一个可用的 ffmpeg：PATH 里的优先，其次是 imageio-ffmpeg 自带的那个。

    HLS 站点（91pinse 系）拿到的是 m3u8，分片是 MPEG-TS。没有 ffmpeg 时
    yt-dlp 只能把分片直接拼成 ``.mp4`` 外壳，播放器打开会报错/花屏；
    有 ffmpeg 就能无损 remux 成真正的 MP4。
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # 没装或装坏了都按「没有 ffmpeg」处理
        return ""


def is_mpeg_ts_head(head: bytes) -> bool:
    if not head:
        return False
    if head[0] != TS_SYNC_BYTE:
        return False
    if len(head) < TS_PACKET_SIZE * 2 + 1:
        return True
    return head[TS_PACKET_SIZE] == TS_SYNC_BYTE and head[TS_PACKET_SIZE * 2] == TS_SYNC_BYTE


def is_mpeg_ts(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return is_mpeg_ts_head(handle.read(TS_PACKET_SIZE * 3))
    except OSError:
        return False


def finalize_media(path: Path) -> Path:
    """收尾 HLS 产物：``.mp4`` 外壳里其实是 MPEG-TS 时，改回真实格式。

    有 ffmpeg 就 remux 成真 MP4（``-c copy`` 不重编码，几秒完事）；
    没有就只把后缀改成 ``.ts``——内容本来就是 TS，VLC / PotPlayer / mpv 都能直接播，
    总比留下一个播放器打不开的假 MP4、或者被校验逻辑当成坏文件删掉要好。
    """
    if path.suffix.lower() != ".mp4" or not is_mpeg_ts(path):
        return path
    target = path.with_suffix(".ts")
    try:
        path.replace(target)
    except OSError:
        return path
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        log.info("HLS 分片已按原始 MPEG-TS 保存为 %s（装 ffmpeg 可自动转成 MP4）", target.name)
        return target
    try:
        result = subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(target),
                "-c", "copy", "-movflags", "+faststart",
                str(path),
            ],
            capture_output=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return target
    if result.returncode == 0 and path.exists() and path.stat().st_size > 0:
        target.unlink(missing_ok=True)
        return path
    return target


def validate_media_file(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
        if size < MIN_MEDIA_BYTES:
            return False, f"媒体文件只有 {size} B，疑似 CDN 空响应"
        with path.open("rb") as handle:
            head = handle.read(1024)
        lowered = head.lstrip().lower()
        if lowered.startswith((b"<!doctype", b"<html", b"<?xml")):
            return False, "CDN 返回了 HTML/XML 错误页"
        # HLS 分片拼出来的流是 MPEG-TS，别拿 MP4 的 ftyp 去要求它
        if is_mpeg_ts_head(head):
            return True, ""
        if path.suffix.lower() == ".mp4" and b"ftyp" not in head[:64]:
            return False, "MP4 文件头无效"
        return True, ""
    except OSError as exc:
        return False, f"无法校验媒体文件：{exc}"


class StalledError(RuntimeError):
    """连接被 CDN 掐到几乎不动（典型是几十 B/s 且永不超时）。

    yt-dlp 的 socket_timeout 只在完全收不到字节时触发；CDN 把速率压到 100 B/s
    时每 20 秒仍能收到约 2KB，超时永远不触发，任务会一直挂着。这里主动抛异常
    中断传输，让上层重新建连并从 .part 续传。
    """


def is_stalled_error(error: BaseException | str) -> bool:
    if isinstance(error, StalledError):
        return True
    return "连接停滞" in str(error)


# CDN 侧主动掐断连接（或读取超时）的常见报错特征。
# 这不是任务失败，重新建连并从 .part 续传即可——实测站点 CDN 每传
# 25~50 MB 就会掐一次连接，重连后还能再跑一段满速。
DROP_MARKERS = (
    "remote protocol error",
    "remoteprotocolerror",
    "connection reset",
    "connection aborted",
    "connection closed",
    "peer closed connection",
    "server disconnected",
    "incomplete read",
    "incompleteread",
    "read timed out",
    "the read operation timed out",
    "unable to download webpage",
)


def is_drop_error(error: BaseException | str) -> bool:
    if isinstance(error, StalledError):
        return False
    text = str(error).lower()
    return any(marker in text for marker in DROP_MARKERS)



class BatchDownloader:
    def __init__(
        self,
        output_dir: Path,
        workers: int = 2,
        fragments: int = 4,
        cookies: Path | None = None,
        rate_limit: int = 0,
        folder_mode: str = "flat",
        progress_callback: Callable[[VideoItem, dict], None] | None = None,
        proxy: str = "",
        stall_retries: int = 2,
        stall_backoff: float = 10.0,
        drop_retries: int = 4,
        drop_backoff: float = 2.0,
        retry_callback: Callable[[VideoItem, int, int], None] | None = None,
    ):
        self.output_dir = output_dir
        self.workers = workers
        self.fragments = fragments
        self.cookies = cookies
        self.rate_limit = rate_limit
        self.folder_mode = folder_mode
        self.progress_callback = progress_callback
        self.proxy = proxy
        self.stall_retries = max(0, stall_retries)
        self.stall_backoff = stall_backoff
        self.drop_retries = max(0, drop_retries)
        self.drop_backoff = drop_backoff
        self.retry_callback = retry_callback
        # 供上层记录「这条任务为什么慢」：尝试次数、停滞/断线重连次数、用时
        self.last_stats: dict[str, float] = {}
        output_dir.mkdir(parents=True, exist_ok=True)

    def _download_one(self, item: VideoItem) -> tuple[VideoItem, str | None]:
        """下载单个视频，分两类重连：

        - 断线（CDN 主动掐断 / 读取超时）：短退避后立刻从 .part 续传，
          这类是站点 CDN 的常态，不能当任务失败；
        - 停滞（连接活着但几乎不动）：长退避，重连后会重新握手到新的边缘节点。

        两种情况都保留 .part，不会从头重下。
        """
        stall_left = self.stall_retries
        drop_left = self.drop_retries
        stalls = drops = attempts = 0
        started = time.time()
        error = ""
        while True:
            attempts += 1
            error, kind = self._attempt(item)
            if not error:
                break
            if kind == "stall" and stall_left > 0:
                stall_left -= 1
                stalls += 1
                delay = self.stall_backoff * (stalls)
            elif kind == "drop" and drop_left > 0:
                drop_left -= 1
                drops += 1
                delay = self.drop_backoff * (drops)
            else:
                break
            if self.retry_callback:
                # (第几次尝试, 总预算)：停滞与断线共用同一个尝试预算
                self.retry_callback(item, attempts, 1 + self.stall_retries + self.drop_retries)
            log.info(
                "%s %s，%.0f 秒后从 .part 续传（%s）",
                "连接停滞" if kind == "stall" else "连接被掐断",
                f"{attempts} 次尝试", delay, item.title[:24],
            )
            time.sleep(delay)
        self.last_stats = {
            "attempts": attempts,
            "stalls": stalls,
            "drops": drops,
            "seconds": round(time.time() - started, 1),
        }
        return item, (error or None)

    def _attempt(self, item: VideoItem) -> tuple[str, str]:
        """执行一次下载尝试，返回 (错误信息, 错误类型)："stall" / "drop" / ""。"""

        url = item.stream_url or item.page_url
        target_dir = self.output_dir
        date_folder = datetime.now().strftime("%Y-%m-%d")
        category_folder = re.sub(r'[^A-Za-z0-9._-]+', "_", item.source or "uncategorized").strip("._") or "uncategorized"
        if self.folder_mode == "date":
            target_dir /= date_folder
        elif self.folder_mode == "category":
            target_dir /= category_folder
        elif self.folder_mode == "date_category":
            target_dir = target_dir / date_folder / category_folder
        target_dir.mkdir(parents=True, exist_ok=True)
        options = {
            "outtmpl": str(target_dir / f"{item.filename}.%(ext)s"),
            "continuedl": True,
            "overwrites": True,
            "concurrent_fragment_downloads": self.fragments,
            # 重试交给上层：yt-dlp 自己 retries=10 时，一次握手超时要磨 2~3 分钟，
            # 这期间并发位被占着、进度钩子也不触发，界面上就是「卡住不动」。
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 1,
            "file_access_retries": 2,
            "retry_sleep_functions": {
                # RetryManager 不同调用点传参方式不一样（有时只有 error= 关键字），
                # 这里必须收任意参数，否则会抛 TypeError 被当成下载失败
                "http": lambda *_, **__: 3,
                "fragment": lambda *_, **__: 3,
                "extractor": lambda *_, **__: 3,
                "file_access": lambda *_, **__: 2,
            },
            "http_headers": {"Referer": item.page_url},
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            # 默认 1 KB 起步，跑到几 MB/s 时每 KB 一次读 + 一次进度回调；
            # 直接给 1 MB 缓冲，在高带宽线路上少掉这些固定开销
            "buffersize": 1024 * 1024,
            # CDN 卡住时尽快让连接失败，配合上层的停滞看门狗一起用
            "socket_timeout": 10,
            # 禁用 yt-dlp 的某些站点检查（解决 Piracy 错误）
            "skip_download": False,
        }
        if self.progress_callback:
            options["progress_hooks"] = [lambda data: self.progress_callback(item, data)]
        # 这里刻意不把 ffmpeg 路径交给 yt-dlp：imageio-ffmpeg 只带 ffmpeg 不带
        # ffprobe，yt-dlp 探测不到时长会改用 ffmpeg 直接拉 HLS，实测分片只下到
        # 一半就以「成功」收尾。保持 yt-dlp 用原生 HLS 下载器（分片能完整落盘），
        # 收尾的 TS→MP4 转封装由 finalize_media 自己做。
        if self.rate_limit:
            options["ratelimit"] = self.rate_limit
        if self.cookies:
            options["cookiefile"] = str(self.cookies)
        if self.proxy:
            # yt-dlp 的 proxy 支持 http/socks，与 httpx 侧共用同一个用户配置
            options["proxy"] = self.proxy
        try:
            with YoutubeDL(options) as ydl:
                ydl.download([url])
            files = [
                path for path in target_dir.iterdir()
                if f"[{item.identity}]" in path.name
                if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".temp"}
            ]
            if not files:
                return "下载器未生成媒体文件", ""
            output = max(files, key=lambda path: path.stat().st_mtime)
            # HLS 站点会把 MPEG-TS 分片直接拼成「.mp4」，这里按真实内容纠正格式
            output = finalize_media(output)
            valid, reason = validate_media_file(output)
            if not valid:
                try:
                    output.unlink()
                except OSError:
                    pass
                return reason, ""
            return "", ""
        except Exception as exc:  # yt-dlp wraps network and ffmpeg failures.
            error = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
            if is_stalled_error(exc):
                return error, "stall"
            if is_drop_error(exc):
                return error, "drop"
            return error, ""

    def download_one(self, item: VideoItem) -> tuple[VideoItem, str | None]:
        """下载单条视频（带停滞重连）。全局调度器按并发上限逐条调用它。"""
        return self._download_one(item)

    def download(self, items: list[VideoItem]) -> list[tuple[VideoItem, str | None]]:
        results: list[tuple[VideoItem, str | None]] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._download_one, item): item for item in items}
            for future in as_completed(futures):
                item, error = future.result()
                log.error("下载失败 %s: %s", item.page_url, error) if error else log.info("完成 %s", item.filename)
                results.append((item, error))
        return results
