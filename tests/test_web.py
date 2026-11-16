from pathlib import Path
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from viewkey_batch.web import app, downloaded_keys, pagination_info
import viewkey_batch.web as web
from viewkey_batch.models import VideoItem


client = TestClient(app)


def test_web_index_loads():
    response = client.get("/")
    assert response.status_code == 200
    assert "91Fetch" in response.text
    assert "下载所选" in response.text
    assert "no-store" in response.headers["cache-control"]
    assert "app.js?v=38" in response.text
    assert "账号安全" not in response.text
    assert "定时下载" not in response.text


def test_ui_controls_bind_before_initial_network_load():
    script = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    select_all_binding = script.index('$("#selectAll").addEventListener')
    initial_loader = script.index("void (async () =>", select_all_binding)
    first_initial_request = script.index('await api("/api/config")', initial_loader)

    assert select_all_binding < initial_loader < first_initial_request


def test_web_config_exposes_categories(tmp_path, monkeypatch):
    # 不依赖当前目录是否恰好存在 site.json，直接用仓库自带的示例配置
    config_file = tmp_path / "site.json"
    example = Path(__file__).resolve().parent.parent / "site.example.json"
    config_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(web, "CONFIG_PATH", config_file)

    response = client.get("/api/config")
    assert response.status_code == 200
    assert {"top_day", "top_month", "latest", "hot", "featured"}.issubset(response.json()["categories"])
    assert "top_week" not in response.json()["categories"]


def test_download_queue_accepts_another_batch_while_running(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc"))
    isolated.add(VideoItem("https://example.test/watch?viewkey=def", "def"))
    isolated.jobs["running"] = web.Job(id="running", kind="download", status="running", viewkeys=["abc"])
    isolated.download_status["abc"] = {"state": "downloading", "percent": 5}
    monkeypatch.setattr(web, "store", isolated)

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(web, "Thread", DeferredThread)
    job = web.queue_download(["def"], workers=2, fragments=4)

    assert job.status == "queued"
    assert job.viewkeys == ["def"]
    assert isolated.download_status["def"]["state"] == "queued"


def test_downloads_report_active_count_and_total_speed(tmp_path, monkeypatch):
    """左下角卡片要的「几个在下载 + 总速度」，由 /api/downloads 一次算好。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    for key in ("a", "b", "c"):
        isolated.add(VideoItem(f"https://example.test/watch?viewkey={key}", key, title=key))
    now = time.time()
    isolated.download_status["a"] = {"state": "downloading", "percent": 40, "speed": "100.00KiB/s", "speed_bps": 102400, "speed_at": now}
    isolated.download_status["b"] = {"state": "downloading", "percent": 20, "speed": "50.00KiB/s", "speed_bps": 51200, "speed_at": now}
    isolated.download_status["c"] = {"state": "queued", "percent": 0}
    monkeypatch.setattr(web, "store", isolated)

    active = client.get("/api/downloads").json()["active"]

    assert active["count"] == 3
    assert active["downloading"] == 2
    assert active["queued"] == 1
    assert active["speed_bps"] == 153600
    assert active["percent"] == 20.0


def test_downloads_ignore_stale_speed(tmp_path, monkeypatch):
    """卡住的下载不能一直报着上一次的速度。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=a", "a", title="a"))
    isolated.download_status["a"] = {
        "state": "downloading", "percent": 30, "speed": "100.00KiB/s",
        "speed_bps": 102400, "speed_at": time.time() - 600,
    }
    monkeypatch.setattr(web, "store", isolated)

    active = client.get("/api/downloads").json()["active"]

    assert active["downloading"] == 1
    assert active["speed_bps"] == 0


def test_download_entries_expose_size_duration_and_clean_speed(tmp_path, monkeypatch):
    """下载列表的元信息：文件大小、视频时长、干净的速度串（不带 ANSI 颜色码）。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=a", "a", title="a", duration="12:34"))
    isolated.download_status["a"] = {
        "state": "downloading",
        "percent": 50,
        # 旧版本会把 yt-dlp 的原始字符串存进来，读接口时必须重新格式化
        "speed": "\x1b[0;32m 1.24MiB/s\x1b[0m",
        "speed_bps": 1300480,
        "speed_at": time.time(),
        "total_bytes": 104857600,
        "downloaded_bytes": 52428800,
    }
    monkeypatch.setattr(web, "store", isolated)

    entries = {entry["viewkey"]: entry for entry in client.get("/api/downloads").json()["downloads"]}

    assert entries["a"]["duration"] == "12:34"
    # 还没下完时用 yt-dlp 探到的总大小，前端才能显示「已下 / 共」
    assert entries["a"]["file_size"] == 104857600
    assert entries["a"]["downloaded_bytes"] == 52428800
    assert entries["a"]["speed"] == "1.24 MB/s"
    assert "\x1b" not in entries["a"]["speed"] and "[" not in entries["a"]["speed"]


def test_format_speed_and_strip_ansi():
    assert web.format_speed(0) == ""
    assert web.format_speed(512) == "512 B/s"
    assert web.format_speed(102400) == "100 KB/s"
    assert web.format_speed(1300480) == "1.24 MB/s"
    assert web.format_speed(3 * 1024 ** 3) == "3.00 GB/s"
    assert web.strip_ansi("\x1b[0;32m 260.40KiB/s\x1b[0m") == "260.40KiB/s"


def test_pause_and_resume_downloads(tmp_path, monkeypatch):
    """暂停把在跑/排队的任务记成 paused，继续时重新入队（yt-dlp 从 .part 续传）。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    monkeypatch.setattr(web, "scheduler", web.DownloadScheduler())
    for key in ("a", "b", "c"):
        isolated.add(VideoItem(f"https://example.test/watch?viewkey={key}", key, title=key))
    isolated.download_status["a"] = {"state": "downloading", "percent": 42}
    isolated.download_status["b"] = {"state": "queued", "percent": 0}
    isolated.download_status["c"] = {"state": "completed", "percent": 100}
    job = web.Job(id="j1", kind="download", status="running", viewkeys=["a", "b"])
    isolated.jobs["j1"] = job
    monkeypatch.setattr(web, "store", isolated)

    paused = client.post("/api/downloads/pause").json()

    assert paused["paused"] == 2
    assert isolated.download_status["a"]["state"] == "paused"
    assert isolated.download_status["a"]["percent"] == 42
    assert isolated.download_status["b"]["state"] == "paused"
    assert isolated.download_status["c"]["state"] == "completed"
    assert job.status == "paused" and job.paused and job.cancelled
    assert client.get("/api/downloads").json()["counts"]["paused"] == 2

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(web, "Thread", DeferredThread)
    resumed = client.post("/api/downloads/resume").json()

    assert resumed["resumed"] == 2
    assert isolated.download_status["a"]["state"] == "queued"
    assert isolated.download_status["b"]["state"] == "queued"
    assert resumed["job"]["viewkeys"] == ["a", "b"]


def test_download_manager_loads():
    response = client.get("/api/downloads")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["counts"]) == {"queued", "downloading", "completed", "failed", "paused"}


def test_pagination_info_reads_site_page_links():
    html = '<a href="/v.php?page=2&category=hot">2</a><a href="/v.php?page=3">下一页</a>'
    assert pagination_info(html, 2) == {
        "page": 2,
        "has_previous": True,
        "has_next": True,
        "last_visible_page": 3,
    }


def test_download_state_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    first = web.Store()
    first.download_status["abc"] = {"state": "downloading", "percent": 42}
    first.dismissed_downloads.add("hidden")
    first.save_state()

    restored = web.Store()
    # 上次被强杀的任务记成「已暂停」而不是失败：程序启动不会自己重下，
    # 用户点「继续下载」才从 .part 续传
    assert restored.download_status["abc"]["state"] == "paused"
    assert restored.download_status["abc"]["percent"] == 42
    assert restored.download_status["abc"]["error"] == ""
    assert restored.dismissed_downloads == {"hidden"}


def test_store_merges_category_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    catalog = web.Store()
    latest = VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo")
    latest.add_source("latest", "https://example.test/v.php?next=watch")
    hot = VideoItem("https://example.test/watch?viewkey=abc", "abc", thumbnail_url="thumb.jpg")
    hot.add_source("hot", "https://example.test/v.php?category=hot")

    catalog.add(latest)
    catalog.add(hot)

    merged = catalog.videos["abc"]
    assert merged.sources == ["latest", "hot"]
    assert merged.listing_urls == {
        "latest": "https://example.test/v.php?next=watch",
        "hot": "https://example.test/v.php?category=hot",
    }
    assert merged.title == "Demo"
    assert merged.thumbnail_url == "thumb.jpg"


def test_old_catalog_source_is_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    old = VideoItem("https://example.test/watch?viewkey=abc", "abc", source="latest", listing_url="/latest")
    raw = old.to_json().replace(', "sources": ["latest"], "listing_urls": {"latest": "/latest"}', "")
    (tmp_path / "catalog.jsonl").write_text(raw + "\n", encoding="utf-8")

    restored = web.Store()
    assert restored.videos["abc"].sources == ["latest"]
    assert restored.videos["abc"].listing_urls == {"latest": "/latest"}


def test_replacing_category_snapshot_removes_only_stale_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    catalog = web.Store()
    shared = VideoItem("u1", "shared")
    shared.add_source("latest", "/latest")
    shared.add_source("hot", "/hot")
    stale = VideoItem("u2", "stale")
    stale.add_source("hot", "/hot")
    catalog.add(shared)
    catalog.add(stale)

    catalog.replace_source_membership("hot", {"shared"})

    assert catalog.videos["shared"].sources == ["latest", "hot"]
    assert "stale" not in catalog.videos


def test_deleted_completed_file_leaves_download_list(tmp_path, monkeypatch):
    """看完自己删掉的视频不该再变成失败任务冒出来。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    monkeypatch.setattr(web, "get_video_dir", lambda: tmp_path / "videos")
    (tmp_path / "videos").mkdir()
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo"))
    isolated.download_status["abc"] = {"state": "completed", "percent": 100, "error": ""}
    monkeypatch.setattr(web, "store", isolated)

    payload = client.get("/api/downloads").json()

    assert payload["downloads"] == []
    assert payload["counts"]["failed"] == 0
    assert "abc" not in isolated.download_status


def test_legacy_missing_file_failure_is_cleaned_up(tmp_path, monkeypatch):
    """1.1.15 之前写下的「本地文件不存在」失败记录要被收掉，而不是一直挂着。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    monkeypatch.setattr(web, "get_video_dir", lambda: tmp_path / "videos")
    (tmp_path / "videos").mkdir()
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo"))
    isolated.download_status["abc"] = {
        "state": "failed",
        "percent": 0,
        "error": "本地文件不存在，点击重新下载，点击重新下载",
    }
    monkeypatch.setattr(web, "store", isolated)

    payload = client.get("/api/downloads").json()

    assert payload["downloads"] == []
    assert "abc" not in isolated.download_status


def test_failed_download_without_file_stays_visible(tmp_path, monkeypatch):
    """真正失败的下载（从没落过文件）仍然要留在列表里，方便重试。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    monkeypatch.setattr(web, "get_video_dir", lambda: tmp_path / "videos")
    (tmp_path / "videos").mkdir()
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo"))
    isolated.download_status["abc"] = {"state": "failed", "percent": 0, "error": "连接被重置"}
    monkeypatch.setattr(web, "store", isolated)

    payload = client.get("/api/downloads").json()

    assert [entry["state"] for entry in payload["downloads"]] == ["failed"]
    assert "abc" in isolated.download_status


def test_completed_file_restored_from_trash_returns_to_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    monkeypatch.setattr(web, "get_video_dir", lambda: tmp_path / "videos")
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "Demo [abc].mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * (70 * 1024))
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo"))
    isolated.download_status["abc"] = {"state": "failed", "percent": 0, "error": "本地文件不存在，点击重新下载"}
    monkeypatch.setattr(web, "store", isolated)

    payload = client.get("/api/downloads").json()

    assert [entry["state"] for entry in payload["downloads"]] == ["completed"]


def test_unfinished_only_covers_download_manager_tasks(tmp_path, monkeypatch):
    """「下载全部未完成」只看下载管理自己的任务，不把整份采集结果拉回来。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    for key in ("failed_one", "never_queued", "dismissed_one"):
        isolated.add(VideoItem(f"https://example.test/watch?viewkey={key}", key, title=key))
    isolated.download_status["failed_one"] = {"state": "failed", "percent": 0, "error": "network"}
    isolated.download_status["dismissed_one"] = {"state": "failed", "percent": 0, "error": "network"}
    isolated.dismissed_downloads.add("dismissed_one")
    monkeypatch.setattr(web, "store", isolated)

    payload = client.get("/api/downloads/unfinished").json()

    assert payload == {
        "viewkeys": ["failed_one"],
        "count": 1,
        "active": [],
        "active_count": 0,
        "total_count": 1,
    }
    assert client.get("/api/downloads").json()["counts"]["failed"] == 1


def test_unfinished_keeps_active_out_of_retry_list(tmp_path, monkeypatch):
    """排队中/下载中的任务不算「待续传」，否则一点按钮就是 409。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    for key in ("queued_one", "downloading_one", "failed_one"):
        isolated.add(VideoItem(f"https://example.test/watch?viewkey={key}", key, title=key))
    isolated.download_status["queued_one"] = {"state": "queued", "percent": 0}
    isolated.download_status["downloading_one"] = {"state": "downloading", "percent": 30}
    isolated.download_status["failed_one"] = {"state": "failed", "percent": 0, "error": "timeout"}
    monkeypatch.setattr(web, "store", isolated)

    payload = client.get("/api/downloads/unfinished").json()

    assert payload["viewkeys"] == ["failed_one"]
    assert payload["count"] == 1
    assert payload["active_count"] == 2
    assert set(payload["active"]) == {"queued_one", "downloading_one"}
    assert payload["total_count"] == 3


def test_queue_download_skips_already_active_items(tmp_path, monkeypatch):
    """重复提交只跳过在跑的那几条，不该把整批请求 409 掉。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    isolated = web.Store()
    for key in ("abc", "def"):
        isolated.add(VideoItem(f"https://example.test/watch?viewkey={key}", key))
    isolated.download_status["abc"] = {"state": "downloading", "percent": 40}
    monkeypatch.setattr(web, "store", isolated)

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(web, "Thread", DeferredThread)

    job = web.queue_download(["abc", "def"], workers=2, fragments=4)

    assert job.viewkeys == ["def"]
    assert job.total == 1
    assert job.skipped == 1
    assert isolated.download_status["abc"]["state"] == "downloading"
    assert isolated.download_status["def"]["state"] == "queued"


def test_queue_download_reports_all_active_as_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc"))
    isolated.download_status["abc"] = {"state": "queued", "percent": 0}
    monkeypatch.setattr(web, "store", isolated)

    with pytest.raises(HTTPException) as excinfo:
        web.queue_download(["abc"], workers=2, fragments=4)

    assert excinfo.value.status_code == 409
    assert "已在下载队列中" in excinfo.value.detail


class RecordingThread:
    """只记录被调度器起了哪些线程，不真的执行下载。"""

    started: list[str] = []

    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")
        self.target = kwargs.get("target")
        self.args = kwargs.get("args", ())

    def start(self):
        RecordingThread.started.append(self.name)


def _queued_store(tmp_path, monkeypatch, keys):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    isolated = web.Store()
    for key in keys:
        isolated.add(VideoItem(f"https://example.test/watch?viewkey={key}", key, title=key))
        isolated.download_status[key] = {"state": "queued", "percent": 0}
    monkeypatch.setattr(web, "store", isolated)
    return isolated


def _pin_workers(monkeypatch, workers: int) -> None:
    base = web.settings_store.snapshot()
    monkeypatch.setattr(web.settings_store, "snapshot", lambda: base.model_copy(update={"workers": workers}))


def test_scheduler_keeps_parallel_downloads_at_setting(tmp_path, monkeypatch):
    """点一次「下载全部未完成」也只同时开 N 路，其余排队。"""
    isolated = _queued_store(tmp_path, monkeypatch, ["a", "b", "c", "d", "e"])
    _pin_workers(monkeypatch, 2)
    scheduler = web.DownloadScheduler()
    monkeypatch.setattr(web, "scheduler", scheduler)
    RecordingThread.started = []
    monkeypatch.setattr(web, "Thread", RecordingThread)

    scheduler.enqueue(["a", "b", "c", "d", "e"])
    scheduler._fill()

    assert sorted(scheduler.running) == ["a", "b"]
    assert scheduler.queue == ["c", "d", "e"]
    assert sorted(name for name in RecordingThread.started if name.startswith("viewkey-download-") and "scheduler" not in name) == [
        "viewkey-download-a",
        "viewkey-download-b",
    ]
    assert isolated.download_status["c"]["state"] == "queued"


def test_scheduler_refills_slot_when_one_finishes(tmp_path, monkeypatch):
    """前一个下完才补下一个，而不是等整批结束。"""
    _queued_store(tmp_path, monkeypatch, ["a", "b", "c"])
    _pin_workers(monkeypatch, 2)
    scheduler = web.DownloadScheduler()
    monkeypatch.setattr(web, "scheduler", scheduler)
    RecordingThread.started = []
    monkeypatch.setattr(web, "Thread", RecordingThread)

    scheduler.enqueue(["a", "b", "c"])
    scheduler._fill()
    assert sorted(scheduler.running) == ["a", "b"]

    # 模拟 a 下完：调度器把线程从 running 摘掉后再补位
    scheduler.running.pop("a")
    scheduler._fill()

    assert sorted(scheduler.running) == ["b", "c"]
    assert scheduler.queue == []


def test_scheduler_limit_follows_settings_and_pause_clears_queue(tmp_path, monkeypatch):
    _queued_store(tmp_path, monkeypatch, ["a", "b", "c", "d"])
    _pin_workers(monkeypatch, 1)
    scheduler = web.DownloadScheduler()
    monkeypatch.setattr(web, "scheduler", scheduler)
    monkeypatch.setattr(web, "Thread", RecordingThread)

    scheduler.enqueue(["a", "b", "c", "d"])
    scheduler._fill()
    assert sorted(scheduler.running) == ["a"]
    assert scheduler.queue == ["b", "c", "d"]

    scheduler.pause()
    assert scheduler.queue == [] and scheduler.paused
    scheduler._fill()  # 暂停后不再补位
    assert sorted(scheduler.running) == ["a"]


def test_startup_never_starts_downloads_on_its_own(tmp_path, monkeypatch):
    """启动只收尾状态：被中断的任务转「已暂停」，不自动开下载。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    (tmp_path / "catalog.jsonl").write_text(
        VideoItem("https://example.test/watch?viewkey=abc", "abc").to_json() + "\n", encoding="utf-8"
    )
    (tmp_path / "state.json").write_text(
        json.dumps({"download_status": {"abc": {"state": "downloading", "percent": 33}}, "dismissed_downloads": []}),
        encoding="utf-8",
    )
    isolated = web.Store()
    monkeypatch.setattr(web, "store", isolated)
    scheduler = web.DownloadScheduler()
    monkeypatch.setattr(web, "scheduler", scheduler)
    RecordingThread.started = []
    monkeypatch.setattr(web, "Thread", RecordingThread)

    web.reconcile_download_state()

    assert isolated.download_status["abc"]["state"] == "paused"
    assert isolated.download_status["abc"]["percent"] == 33
    assert scheduler.queue == [] and not scheduler.running
    assert RecordingThread.started == []
    assert not hasattr(web, "resume_interrupted_downloads")


def test_job_status_is_derived_from_item_states(tmp_path, monkeypatch):
    """一批点击的进度按条目状态推导：全部结束就结束，不会永远卡在 running。"""
    isolated = _queued_store(tmp_path, monkeypatch, ["a", "b"])
    job = web.Job(id="job-x", kind="download", viewkeys=["a", "b"])
    isolated.jobs[job.id] = job
    isolated.download_status["a"] = {"state": "downloading", "percent": 40}

    payload = client.get("/api/jobs/job-x").json()
    assert payload["status"] == "running"
    assert payload["total"] == 2 and payload["current"] == 0

    isolated.download_status["a"] = {"state": "completed", "percent": 100}
    isolated.download_status["b"] = {"state": "failed", "percent": 0, "error": "boom"}

    payload = client.get("/api/jobs/job-x").json()
    assert payload["status"] == "failed"
    assert payload["current"] == 2
    assert payload["error"] == "1 个任务失败"


class _NullClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        raise AssertionError("直链还有效时不该再请求列表页")


def test_download_one_reuses_fresh_stream_url(tmp_path, monkeypatch):
    """直链签名没过期时一个页面请求都不发：不建客户端、不抓列表页，直接开下。"""
    isolated = _queued_store(tmp_path, monkeypatch, ["a", "b"])
    monkeypatch.setattr(web, "scheduler", web.DownloadScheduler())
    page_clients: list[int] = []
    monkeypatch.setattr(web, "build_client", lambda *a, **k: page_clients.append(1) or _NullClient())
    monkeypatch.setattr(web, "get_video_dir", lambda: tmp_path / "videos")
    expiry = int(time.time()) + 7200
    isolated.videos["a"].stream_url = f"https://cdn.test/a.mp4?st=t&e={expiry}"

    class StubBatch:
        calls: list = []

        def __init__(self, *args, **kwargs):
            StubBatch.calls.append({"workers": args[1], "progress": kwargs.get("progress_callback")})

        def download_one(self, item):
            return item, None

    monkeypatch.setattr(web, "BatchDownloader", StubBatch)

    web.download_one("a", isolated.videos["a"])

    assert isolated.download_status["a"]["state"] == "completed"
    # 单个任务只占 1 路并发，总并发由调度器控制
    assert StubBatch.calls[0]["workers"] == 1
    assert page_clients == [], "直链还有效时不该再建页面客户端（启动延迟就出在这里）"


def test_downloader_retries_dropped_connection(tmp_path, monkeypatch):
    """CDN 掐断连接是常态：短退避后从 .part 续传，不能直接判失败。"""
    from viewkey_batch import downloader as dl

    calls: list[int] = []
    item = VideoItem("https://example.test/watch?viewkey=abc", "abc", title="断线测试")

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionResetError(104, "Connection reset by peer")
            (tmp_path / f"{item.filename}.mp4").write_bytes(
                b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 70000
            )

    monkeypatch.setattr(dl, "YoutubeDL", FakeYDL)
    batch = dl.BatchDownloader(
        tmp_path, workers=1, stall_retries=0, drop_retries=2, drop_backoff=0
    )
    result = batch.download([item])

    assert len(calls) == 2
    assert result[0][1] is None
    assert batch.last_stats["drops"] == 1


def test_stalled_task_yields_slot_then_fails(tmp_path, monkeypatch):
    """卡住的任务让出并发位排队尾，连续 4 次都不行才判失败。"""
    isolated = _queued_store(tmp_path, monkeypatch, ["a"])
    scheduler = web.DownloadScheduler()
    monkeypatch.setattr(web, "scheduler", scheduler)
    monkeypatch.setattr(web, "build_client", lambda *a, **k: _NullClient())
    monkeypatch.setattr(web, "get_video_dir", lambda: tmp_path / "videos")
    web._yield_counts.clear()
    isolated.videos["a"].stream_url = f"https://cdn.test/a.mp4?st=t&e={int(time.time()) + 7200}"

    class StubBatch:
        def __init__(self, *args, **kwargs):
            self.last_stats = {"attempts": 1, "drops": 0, "stalls": 0, "seconds": 3}

        def download_one(self, item):
            return item, "连接停滞（5.0 KB/s）"

    monkeypatch.setattr(web, "BatchDownloader", StubBatch)

    web.download_one("a", isolated.videos["a"])

    assert isolated.download_status["a"]["state"] == "queued"
    assert scheduler.snapshot()["waiting"] == ["a"]

    for _ in range(web.YIELD_LIMITS['stall']):
        web.download_one("a", isolated.videos["a"])

    assert isolated.download_status["a"]["state"] == "failed"
    assert "停滞" in isolated.download_status["a"]["error"]


def test_two_drops_trip_global_breaker(tmp_path, monkeypatch):
    """短时间内两条任务都连不上，说明出口 IP 被掐，整体停一会儿别再硬顶。"""
    _queued_store(tmp_path, monkeypatch, ["a", "b"])
    scheduler = web.DownloadScheduler()
    monkeypatch.setattr(web, "scheduler", scheduler)
    web._yield_counts.clear()
    web._breaker_events.clear()

    assert web._yield_task("a", "Connection reset by peer", "drop") is True
    assert scheduler.cooldown_until == 0.0, "单条失败还不该熔断"

    assert web._yield_task("b", "Connection reset by peer", "drop") is True
    assert scheduler.cooldown_until > time.time()

    # 熔断期间调度器不启动新任务
    monkeypatch.setattr(web, "Thread", RecordingThread)
    RecordingThread.started = []
    scheduler._fill()
    assert RecordingThread.started == []


def test_two_drops_trip_global_breaker(tmp_path, monkeypatch):
    """短时间内两条任务都连不上，说明出口 IP 被掐，整体停一会儿别再硬顶。"""
    _queued_store(tmp_path, monkeypatch, ["a", "b"])
    scheduler = web.DownloadScheduler()
    monkeypatch.setattr(web, "scheduler", scheduler)
    web._yield_counts.clear()
    web._breaker_events.clear()

    assert web._yield_task("a", "Connection reset by peer", "drop") is True
    assert scheduler.cooldown_until == 0.0, "单条失败还不该熔断"

    assert web._yield_task("b", "Connection reset by peer", "drop") is True
    assert scheduler.cooldown_until > time.time()

    # 熔断期间调度器不启动新任务
    monkeypatch.setattr(web, "Thread", RecordingThread)
    RecordingThread.started = []
    scheduler._fill()
    assert RecordingThread.started == []


def test_drop_clears_dead_signed_stream(tmp_path, monkeypatch):
    """连接层断了就作废这条直链：签名没过期也不能再拿去重试。"""
    isolated = _queued_store(tmp_path, monkeypatch, ["a"])
    monkeypatch.setattr(web, "scheduler", web.DownloadScheduler())
    web._failed_streams.clear()
    expiry = int(time.time()) + 7200
    isolated.videos["a"].stream_url = f"https://cdn.test/mp43/a.mp4?st=t&e={expiry}"
    assert web.item_stream_fresh(isolated.videos["a"]) is True

    assert web._yield_task("a", "Connection reset by peer", "drop") is True

    assert isolated.videos["a"].stream_url == ""
    assert web.item_stream_fresh(isolated.videos["a"]) is False
    assert web._failed_streams["a"][1] == 1
    web._failed_streams.clear()


def test_same_host_keeps_failing_stops_retrying(tmp_path, monkeypatch):
    """换了个签名还是同一个 CDN 主机连不上，就该停下并说清原因，而不是无限重试。"""
    isolated = _queued_store(tmp_path, monkeypatch, ["a"])
    monkeypatch.setattr(web, "scheduler", web.DownloadScheduler())
    monkeypatch.setattr(web, "build_client", lambda *a, **k: _NullClient())
    monkeypatch.setattr(web, "get_video_dir", lambda: tmp_path / "videos")
    expiry = int(time.time()) + 7200
    fresh = VideoItem(
        "https://example.test/watch?viewkey=a",
        "a",
        title="a",
        stream_url=f"https://cdn.test/mp43/a.mp4?st=other&e={expiry}",
    )

    class StubCrawler:
        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, item, prefer_hd=False):
            return fresh

    monkeypatch.setattr(web, "Crawler", StubCrawler)
    scheduler = web.DownloadScheduler()
    monkeypatch.setattr(web, "scheduler", scheduler)
    web._failed_streams["a"] = ("cdn.test/mp43/a.mp4", web.STREAM_FAIL_LIMIT)
    try:
        scheduler._run("a")
    finally:
        web._failed_streams.clear()

    status = isolated.download_status["a"]
    assert status["state"] == "failed"
    assert "cdn.test" in status["error"]
    assert "代理节点" in status["error"]


def test_download_one_records_failure_reason(tmp_path, monkeypatch):
    isolated = _queued_store(tmp_path, monkeypatch, ["a"])
    monkeypatch.setattr(web, "scheduler", web.DownloadScheduler())
    monkeypatch.setattr(web, "build_client", lambda *a, **k: _NullClient())
    monkeypatch.setattr(web, "get_video_dir", lambda: tmp_path / "videos")
    expiry = int(time.time()) + 7200
    isolated.videos["a"].stream_url = f"https://cdn.test/a.mp4?st=t&e={expiry}"

    class StubBatch:
        def __init__(self, *args, **kwargs):
            pass

        def download_one(self, item):
            return item, "CDN 空响应"

    monkeypatch.setattr(web, "BatchDownloader", StubBatch)

    web.download_one("a", isolated.videos["a"])

    assert isolated.download_status["a"]["state"] == "failed"
    assert isolated.download_status["a"]["error"] == "CDN 空响应"


def test_removed_download_stays_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo"))
    isolated.download_status["abc"] = {"state": "failed", "percent": 0, "error": "network"}
    monkeypatch.setattr(web, "store", isolated)

    response = client.post("/api/downloads/remove", json={"viewkeys": ["abc"]})

    assert response.status_code == 200
    assert client.get("/api/downloads").json()["downloads"] == []
    restored = web.Store()
    assert restored.dismissed_downloads == {"abc"}


def test_removing_download_cancels_only_that_task(tmp_path, monkeypatch):
    """删一条任务不能连带把同批的其他任务判死，它们该继续排队下完。"""
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "scheduler", web.DownloadScheduler())
    isolated = web.Store()
    isolated.download_status.update({"abc": {"state": "downloading", "percent": 12}, "def": {"state": "queued", "percent": 0}})
    job = web.Job(id="job1", kind="download", status="running", viewkeys=["abc", "def"])
    isolated.jobs[job.id] = job
    monkeypatch.setattr(web, "store", isolated)

    response = client.post("/api/downloads/remove", json={"viewkeys": ["abc"]})

    assert response.status_code == 200
    assert job.cancelled is False
    assert job.viewkeys == ["def"]
    assert "abc" not in isolated.download_status
    assert isolated.download_status["def"]["state"] == "queued"


def test_downloaded_filter_ignores_tiny_media_shell(tmp_path, monkeypatch):
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    (video_dir / "broken [bad].mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 36)
    (video_dir / "valid [good].mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * (256 * 1024))
    monkeypatch.setattr(web, "get_video_dir", lambda: video_dir)

    assert downloaded_keys() == {"good"}


def test_account_cookie_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web, "COOKIES_PATH", tmp_path / "cookies.txt")

    # 粘贴 Cookie 请求头格式
    response = client.post("/api/account/cookies", json={"content": "ya0uid=abc; yao_c=sessionvalue"})
    assert response.status_code == 200
    assert response.json()["cookie_count"] == 2

    status = client.get("/api/account").json()
    assert status["has_cookies"] is True
    assert status["cookie_count"] == 2

    assert web.app_cookies() == {"ya0uid": "abc", "yao_c": "sessionvalue"}

    response = client.delete("/api/account/cookies")
    assert response.status_code == 200
    assert client.get("/api/account").json()["has_cookies"] is False


def test_account_cookies_rejects_unparsable(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web, "COOKIES_PATH", tmp_path / "cookies.txt")

    response = client.post("/api/account/cookies", json={"content": "没有等号的无效内容"})
    assert response.status_code == 400


def test_parse_cookie_text_supports_netscape(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    content = "# Netscape HTTP Cookie File\n.example.test\tTRUE\t/\tFALSE\t0\tsession\tTOKEN\n"
    assert web.parse_cookie_text(content) == {"session": "TOKEN"}
    assert web.parse_cookie_text("a=1;b=2") == {"a": "1", "b": "2"}
    assert web.parse_cookie_text("") == {}


def test_stream_url_fresh_uses_signature_expiry():
    now = int(time.time())
    assert web.stream_url_fresh(f"https://la.btc620.com/mp43/a.mp4?st=t&e={now + 7200}")
    assert not web.stream_url_fresh(f"https://la.btc620.com/mp43/a.mp4?st=t&e={now - 60}")
    assert web.stream_url_fresh(f"https://cdn.test/a.mp4?secure=tok,{now + 7200}")
    assert not web.stream_url_fresh("https://cdn.test/a.mp4")
    assert not web.stream_url_fresh("")


def test_check_stall_detects_trickle_connection():
    watch: dict[str, tuple[float, int]] = {}
    assert web.check_stall(watch, "v1", 1000, 1000.0) is None  # 建立基准
    # 40 秒只涨了 4KB，约 100 B/s，判定停滞
    assert web.check_stall(watch, "v1", 5096, 1000.0 + web.STALL_WINDOW_SECONDS) == pytest.approx(102.4)
    # 窗口未满时不判定
    watch2: dict[str, tuple[float, int]] = {}
    web.check_stall(watch2, "v2", 0, 0.0)
    assert web.check_stall(watch2, "v2", 10, 10.0) is None
    # 正常速度（20 KB/s）不判定，并把窗口前移
    watch3: dict[str, tuple[float, int]] = {}
    web.check_stall(watch3, "v3", 0, 0.0)
    grown = int(20 * 1024 * web.STALL_WINDOW_SECONDS)
    assert web.check_stall(watch3, "v3", grown, float(web.STALL_WINDOW_SECONDS)) is None
    assert watch3["v3"] == (float(web.STALL_WINDOW_SECONDS), grown)


def test_downloader_retries_stalled_attempt(tmp_path, monkeypatch):
    from viewkey_batch import downloader as dl

    calls: list[int] = []
    retries: list[tuple[int, int]] = []
    item = VideoItem("https://example.test/watch?viewkey=abc", "abc", title="停滞测试")

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):
            calls.append(1)
            if len(calls) == 1:
                raise dl.StalledError("连接停滞（0.1 KB/s）")
            (tmp_path / f"{item.filename}.mp4").write_bytes(
                b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 70000
            )

    monkeypatch.setattr(dl, "YoutubeDL", FakeYDL)
    result = dl.BatchDownloader(
        tmp_path,
        workers=1,
        stall_retries=1,
        stall_backoff=0,
        retry_callback=lambda it, attempt, attempts: retries.append((attempt, attempts)),
    ).download([item])

    assert len(calls) == 2, "第一次停滞应触发一次重连"
    # 第一个参数是已经尝试的次数，第二个是总尝试预算（1 次初始 + 停滞 + 断线）
    assert retries == [(1, 6)]
    assert result[0][1] is None



