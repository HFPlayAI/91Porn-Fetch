from viewkey_batch import downloader
from viewkey_batch.downloader import (
    MIN_MEDIA_BYTES,
    finalize_media,
    is_mpeg_ts,
    is_mpeg_ts_head,
    validate_media_file,
)


def test_rejects_tiny_mp4_shell(tmp_path):
    path = tmp_path / "empty.mp4"
    path.write_bytes(b"\x00\x00\x00 ftypisom\x00\x00\x00\x00mdat")

    valid, reason = validate_media_file(path)

    assert not valid
    assert "CDN 空响应" in reason


def test_accepts_sized_mp4_with_valid_header(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * MIN_MEDIA_BYTES)

    valid, reason = validate_media_file(path)

    assert valid
    assert reason == ""


def ts_bytes(packets: int = 400) -> bytes:
    """构造一段带 TS 同步字节的假数据，够触发格式判定。"""
    packet = b"\x47" + b"\x00" * 187
    return packet * packets


def test_detects_mpeg_ts_payload():
    head = ts_bytes(3)
    assert is_mpeg_ts_head(head)
    assert not is_mpeg_ts_head(b"\x00\x00\x00 ftypisom")
    assert not is_mpeg_ts_head(b"")


def test_accepts_mpeg_ts_even_when_named_mp4(tmp_path):
    """HLS 分片直接拼出来的流是 MPEG-TS，不能被当成坏文件删掉。"""
    path = tmp_path / "hls.mp4"
    path.write_bytes(ts_bytes(500))

    assert is_mpeg_ts(path)
    valid, reason = validate_media_file(path)
    assert valid, reason


def test_finalize_renames_ts_payload_to_ts_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_exe", lambda: "")
    path = tmp_path / "hls.mp4"
    path.write_bytes(ts_bytes(500))

    result = finalize_media(path)

    assert result.suffix == ".ts"
    assert result.exists()
    assert not path.exists()


def test_finalize_leaves_real_mp4_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_exe", lambda: "")
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * MIN_MEDIA_BYTES)

    assert finalize_media(path) == path
    assert path.exists()
