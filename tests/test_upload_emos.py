"""Emos 上传器分片直传的回归测试

重点覆盖两类曾经导致「分片 1/N 上传失败: HTTP 400」的问题：

1. 直传预签名 URL 时误用带 Emos ``Authorization`` 头的会话（对象存储会拒绝）；
2. 失败时只报状态码，不带响应体，无法定位真实原因。

另外覆盖分片并发上传与 Telegram 进度文本格式。
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.emos_client import EmosApiError, EmosClient
from src.video_organizer.upload.upload_emos import (
    RobustEmosVideoUploader,
    _recall_tg_message,
    find_subtitle_files,
    format_time,
    progress_bar,
)


class _FakeResponse:
    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = body
        self.closed = False

    def close(self):
        self.closed = True


class _RecordingSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def put(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class _BarrierSession:
    """用栅栏验证多个分片确实是并发上传的（串行会超时）"""

    def __init__(self, barrier):
        self.barrier = barrier
        self.urls = []
        self._lock = threading.Lock()
        self.closed = False

    def put(self, url, **kwargs):
        with self._lock:
            self.urls.append(url)
        self.barrier.wait(timeout=10)
        return _FakeResponse(200, {"ETag": '"etag-1"'})

    def close(self):
        self.closed = True


class _RecordingTusSession:
    """记录 tusd 的 POST / PATCH 请求"""

    def __init__(self, location="/files/abc"):
        self.calls = []
        self.location = location
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _FakeResponse(201, {"Location": self.location})

    def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        data = kwargs.get("data") or b""
        offset = int(kwargs["headers"].get("Upload-Offset") or 0)
        return _FakeResponse(204, {"Upload-Offset": str(offset + len(data))})

    def close(self):
        self.closed = True


class _SlashAwareTusSession:
    """模拟 Emos 的 tusd：``/files`` 返回 404，``/files/`` 才是真正的 base path"""

    def __init__(self, location="https://file.emos.best/files/abc"):
        self.calls = []
        self.location = location
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/"):
            return _FakeResponse(201, {"Location": self.location})
        return _FakeResponse(404, {}, b"")

    def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        data = kwargs.get("data") or b""
        offset = int(kwargs["headers"].get("Upload-Offset") or 0)
        return _FakeResponse(204, {"Upload-Offset": str(offset + len(data))})

    def close(self):
        self.closed = True


class _FailingTusSession:
    """创建上传直接失败（用于验证错误提示）"""

    def __init__(self, status=403):
        self.status = status
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _FakeResponse(self.status, {}, b"forbidden")

    def patch(self, url, **kwargs):
        raise AssertionError("创建失败时不应发送 PATCH")

    def close(self):
        pass


class TestUploadEmosDirectPut(unittest.TestCase):
    def _uploader(self, max_retries=1):
        return RobustEmosVideoUploader(
            auth_token="token-123",
            base_url="https://emos.best",
            max_retries=max_retries,
        )

    def test_storage_session_has_no_emos_auth_headers(self):
        """直传用的会话必须是干净的，不能继承 Emos 的鉴权头"""
        uploader = self._uploader()
        self.assertEqual(
            uploader.session.headers.get("authorization"), "Bearer token-123"
        )
        self.assertIsNot(uploader.storage_session, uploader.session)
        self.assertNotIn("Authorization", uploader.storage_session.headers)
        self.assertNotIn(
            "origin", {k.lower() for k in uploader.storage_session.headers}
        )
        self.assertNotIn(
            "referer", {k.lower() for k in uploader.storage_session.headers}
        )

    def test_put_part_success_returns_etag(self):
        response = _FakeResponse(200, {"ETag": '"etag-1"'})
        session = _RecordingSession(response)
        uploader = self._uploader()
        uploader.storage_session = session

        etag = uploader._put_part(
            "https://r2.example.com/part-1", b"abcd", 4, 1, 2, "video/x-matroska"
        )

        self.assertEqual(etag, "etag-1")
        _, kwargs = session.calls[0]
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["headers"]["Content-Type"], "video/x-matroska")
        self.assertEqual(kwargs["headers"]["Content-Length"], "4")
        self.assertTrue(response.closed)

    def test_put_part_error_includes_response_body(self):
        body = (
            b"<Error><Code>InvalidArgument</Code>"
            b"<Message>Only one auth mechanism allowed</Message></Error>"
        )
        session = _RecordingSession(_FakeResponse(400, {}, body))
        uploader = self._uploader()
        uploader.storage_session = session

        with self.assertRaises(EmosApiError) as ctx:
            uploader._put_part(
                "https://r2.example.com/part-1", b"abcd", 4, 1, 2, "video/x-matroska"
            )

        message = str(ctx.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("Only one auth mechanism allowed", message)
        self.assertEqual(len(session.calls), 1)  # 400 不重试

    def test_multipart_uses_clean_session_and_file_mime(self):
        handle, path = tempfile.mkstemp(suffix=".mkv")
        os.write(handle, b"x" * 16)
        os.close(handle)
        try:
            uploader = self._uploader()
            uploader.client.multipart_presign = MagicMock(
                return_value=[{"number": 1, "upload_url": "https://r2.example.com/p1"}]
            )
            uploader.client.multipart_complete = MagicMock(return_value={})
            session = _RecordingSession(_FakeResponse(200, {"ETag": '"e1"'}))
            uploader.storage_session = session

            uploader._upload_multipart(
                Path(path),
                {"file_id": "f1", "data": {"multipart_size": {"min": 0, "max": 0}}},
                16,
            )

            _, kwargs = session.calls[0]
            self.assertNotIn("Authorization", kwargs["headers"])
            self.assertEqual(kwargs["headers"]["Content-Type"], "video/x-matroska")
            uploader.client.multipart_complete.assert_called_once()
        finally:
            os.remove(path)

    def test_close_releases_sessions(self):
        uploader = self._uploader()
        session = _RecordingSession(_FakeResponse(200))
        uploader.storage_session = session
        uploader.close()
        self.assertTrue(session.closed)

    def test_upload_concurrency_is_clamped(self):
        self.assertEqual(self._uploader().upload_concurrency, 10)
        self.assertEqual(
            RobustEmosVideoUploader(
                auth_token="t", upload_concurrency=0
            ).upload_concurrency,
            1,
        )
        self.assertEqual(
            RobustEmosVideoUploader(
                auth_token="t", upload_concurrency=999
            ).upload_concurrency,
            32,
        )
        self.assertEqual(
            RobustEmosVideoUploader(
                auth_token="t", upload_concurrency="8"
            ).upload_concurrency,
            8,
        )

    def test_storage_session_pool_covers_concurrency(self):
        """连接池必须跟得上并发数，否则连接被反复丢弃会拖慢上传"""
        uploader = RobustEmosVideoUploader(auth_token="t", upload_concurrency=16)
        adapter = uploader.storage_session.get_adapter("https://r2.example.com/p1")
        self.assertGreaterEqual(adapter._pool_maxsize, 16)
        self.assertGreaterEqual(adapter._pool_connections, 16)

    def test_multipart_uploads_parts_concurrently(self):
        size = 4 * 1024 * 1024
        handle, path = tempfile.mkstemp(suffix=".mkv")
        os.write(handle, b"x" * size)
        os.close(handle)
        try:
            uploader = self._uploader()
            uploader.chunk_size_mb = 1  # 1MB/片 -> 4 片
            uploader.upload_concurrency = 4
            uploader.client.multipart_presign = MagicMock(
                return_value=[
                    {"number": i, "upload_url": f"https://r2.example.com/p{i}"}
                    for i in range(1, 5)
                ]
            )
            uploader.client.multipart_complete = MagicMock(return_value={})
            session = _BarrierSession(threading.Barrier(4))
            uploader.storage_session = session

            uploader._upload_multipart(
                Path(path),
                {"file_id": "f1", "data": {"multipart_size": {"min": 0, "max": 0}}},
                size,
            )

            self.assertEqual(len(session.urls), 4)
            self.assertEqual(uploader._progress_parts_done, 4)
            self.assertEqual(uploader._progress_total_parts, 4)
            _, parts = uploader.client.multipart_complete.call_args[0]
            self.assertEqual([part["number"] for part in parts], [1, 2, 3, 4])
            self.assertTrue(all(part["etag"] == "etag-1" for part in parts))
        finally:
            os.remove(path)

    def test_progress_reporting_does_not_block_other_parts(self):
        """进度上报（含 Telegram）在锁外执行：多个分片的上报可以同时进行"""
        size = 4 * 1024 * 1024
        handle, path = tempfile.mkstemp(suffix=".mkv")
        os.write(handle, b"x" * size)
        os.close(handle)
        try:
            uploader = self._uploader()
            uploader.chunk_size_mb = 1  # 1MB/片 -> 4 片
            uploader.upload_concurrency = 4
            uploader.client.multipart_presign = MagicMock(
                return_value=[
                    {"number": i, "upload_url": f"https://r2.example.com/p{i}"}
                    for i in range(1, 5)
                ]
            )
            uploader.client.multipart_complete = MagicMock(return_value={})
            uploader.storage_session = _BarrierSession(threading.Barrier(4))

            overlap = threading.Barrier(2)
            broken = []

            def on_progress(progress, uploaded, total, status):
                try:
                    overlap.wait(timeout=5)
                except Exception as exc:  # 上报被串行化时栅栏会超时
                    broken.append(exc)

            uploader.progress_callback = on_progress

            uploader._upload_multipart(
                Path(path),
                {"file_id": "f1", "data": {"multipart_size": {"min": 0, "max": 0}}},
                size,
            )

            self.assertEqual(broken, [])
            self.assertEqual(uploader._progress_parts_done, 4)
        finally:
            os.remove(path)

    def test_telegram_progress_update_does_not_wait_for_network(self):
        """Telegram 进度消息由后台线程发送，不能卡住上传线程"""
        uploader = self._uploader()
        uploader.tg_bot_token = "bot-token"
        uploader.tg_chat_id = "123"
        uploader._progress_started_at = time.time()
        uploader._progress_total_parts = 3
        sent = []
        started = threading.Event()
        release = threading.Event()

        def slow_send(text):
            sent.append(text)
            started.set()
            release.wait(timeout=10)

        uploader._tg_send = slow_send

        begin = time.time()
        uploader._tg_update("a.mkv", 40.0, 40, 100, "", "uploading")
        elapsed = time.time() - begin

        self.assertLess(elapsed, 1.0)
        self.assertTrue(started.wait(timeout=5))
        self.assertIn("📤 *上传进度*", sent[0])
        release.set()

    def test_finish_appends_result_instead_of_replacing_progress(self):
        """上传结束后不覆盖进度消息，只在末尾追加「上传完成」"""
        uploader = self._uploader()
        uploader.tg_bot_token = "bot-token"
        uploader.tg_chat_id = "123"
        uploader._progress_started_at = time.time() - 5
        uploader._progress_total_parts = 3
        uploader._tg_base_text = uploader._progress_text(
            "a.mkv", 97.0, 97 * 1024 * 1024, 100 * 1024 * 1024
        )
        sent = []
        uploader._tg_send = lambda text: sent.append(text)

        uploader._tg_finish("a.mkv", "来！金来号！ S01 E10")

        self.assertEqual(len(sent), 1)
        text = sent[0]
        self.assertTrue(text.startswith("📤 *上传进度*"))
        self.assertIn("平均速度: ", text)  # 进度信息保留
        self.assertTrue(text.rstrip().endswith("标题: 来！金来号！ S01 E10"))
        self.assertIn("✅ 上传完成", text)

    def test_failure_appends_reason_to_progress(self):
        uploader = self._uploader()
        uploader.tg_bot_token = "bot-token"
        uploader.tg_chat_id = "123"
        uploader._tg_base_text = "📤 *上传进度*\n\n文件: `a.mkv`"
        sent = []
        uploader._tg_send = lambda text: sent.append(text)

        uploader._tg_finish("a.mkv", "", error="分片 3/100 上传失败: HTTP 500")

        self.assertTrue(sent[0].startswith("📤 *上传进度*"))
        self.assertIn("❌ Emos 上传失败", sent[0])
        self.assertIn("分片 3/100 上传失败: HTTP 500", sent[0])

    def test_retry_reuses_same_telegram_message(self):
        """同一文件重试时编辑原消息，而不是每分钟新建一条通知"""
        from unittest.mock import patch

        class _TgResponse:
            status_code = 200
            content = b"{}"

            @staticmethod
            def json():
                return {"ok": True, "result": {"message_id": 4321}}

        key = "123:retry-reuse.mkv"
        first = self._uploader()
        first.tg_bot_token = "bot-token"
        first.tg_chat_id = "123"
        first._tg_message_key = key
        with patch(
            "src.video_organizer.upload.upload_emos.requests.post",
            return_value=_TgResponse(),
        ) as post:
            first._tg_send("进度 1")
            self.assertIn("sendMessage", post.call_args[0][0])
            self.assertEqual(first._tg_message_id, 4321)

            # 重试会新建上传器实例，但应复用上一条消息
            second = self._uploader()
            second.tg_bot_token = "bot-token"
            second.tg_chat_id = "123"
            second._tg_message_key = key
            second._tg_message_id = _recall_tg_message(key)
            post.reset_mock()
            second._tg_send("进度 2")
            self.assertIn("editMessageText", post.call_args[0][0])
            self.assertEqual(post.call_args[1]["json"]["message_id"], 4321)

    def test_progress_text_uses_rich_format(self):
        uploader = self._uploader()
        uploader._progress_started_at = time.time() - 4.0
        uploader._progress_parts_done = 1
        uploader._progress_total_parts = 3

        text = uploader._progress_text(
            "a.mkv", 33.3, 100 * 1024 * 1024, 300 * 1024 * 1024
        )

        self.assertIn("📤 *上传进度*", text)
        self.assertIn("文件: `a.mkv`", text)
        self.assertIn("进度: 33.3%", text)
        self.assertIn("分片: 1/3", text)
        self.assertIn("已上传: 100.00 MB", text)
        self.assertIn("平均速度: ", text)
        self.assertIn("已用时间: ", text)
        self.assertIn("剩余时间: ", text)
        bar_line = [line for line in text.splitlines() if line.startswith("[")][0]
        self.assertEqual(len(bar_line), 22)  # [ + 20 格 + ]
        self.assertEqual(bar_line.count("█"), 7)

    def test_progress_text_omits_part_line_for_single_stream(self):
        uploader = self._uploader()
        text = uploader._progress_text("a.mkv", 50.0, 50, 100)
        self.assertNotIn("分片:", text)

    def test_format_time_and_progress_bar(self):
        self.assertEqual(format_time(3.9), "3.9秒")
        self.assertEqual(format_time(90), "1分30.0秒")
        self.assertEqual(format_time(3661), "1时1分1.0秒")
        self.assertEqual(progress_bar(0), "░" * 20)
        self.assertEqual(progress_bar(100), "█" * 20)


class TestUploadEmosAlreadyUploaded(unittest.TestCase):
    """Emos 对同一资源限制一周内不能重复上传（HTTP 422）"""

    _BODY = '{"message": "\\u6b64\\u8d44\\u6e90\\u60a8\\u4e00\\u5468\\u5185\\u4e0a\\u4f20\\u8fc7"}'

    def test_is_already_uploaded_matches_weekly_limit_message(self):
        self.assertTrue(
            EmosClient.is_already_uploaded({"message": "此资源您一周内上传过"})
        )
        self.assertTrue(EmosClient.is_already_uploaded({"message": "该资源之前上传过"}))
        self.assertTrue(EmosClient.is_already_uploaded({"message": "已上传过"}))
        self.assertFalse(EmosClient.is_already_uploaded({"message": "文件大小超限"}))

    def test_get_upload_token_treats_422_as_existed(self):
        client = EmosClient(base_url="https://emos.best", auth_token="t")
        client._json = MagicMock(side_effect=EmosApiError("HTTP 422", 422, self._BODY))

        token = client.get_upload_token("a.mkv", 1024)

        self.assertTrue(token.get("existed"))
        self.assertEqual(token.get("message"), "此资源您一周内上传过")

    def test_upload_video_skips_when_episode_already_has_the_file(self):
        handle, path = tempfile.mkstemp(suffix=".mkv")
        os.write(handle, b"x" * 16)
        os.close(handle)
        try:
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            uploader.client.get_video_base = MagicMock(
                return_value={
                    "title": "来！金来号！",
                    "video_medias": [
                        {"media_name": os.path.basename(path), "media_file_size": 16}
                    ],
                }
            )
            uploader.client.get_upload_token = MagicMock(
                return_value={"existed": True, "message": "此资源您一周内上传过"}
            )
            uploader.client.save_video = MagicMock(return_value={"media_id": "m1"})
            uploader.client.multipart_presign = MagicMock(
                side_effect=AssertionError("命中已上传过时不应请求分片凭证")
            )
            uploader.tg_bot_token = "bot-token"
            uploader.tg_chat_id = "123"
            sent_texts = []
            uploader._tg_finish = lambda *args, **kwargs: sent_texts.append(
                kwargs.get("text")
            )

            result = uploader.upload_video(path, "ve", "3039064")

            self.assertIsNotNone(result)
            self.assertTrue(result.get("skipped"))
            self.assertTrue(result.get("existed"))
            self.assertFalse(result.get("deferred"))
            self.assertEqual(result.get("file_id"), "")
            uploader.client.save_video.assert_not_called()
            self.assertEqual(uploader.last_error, "")
            self.assertEqual(uploader._skip_reason, "此资源您一周内上传过")
            self.assertIn("♻️ Emos 已存在该资源，跳过上传", sent_texts[-1])
        finally:
            os.remove(path)

    def test_upload_video_defers_when_episode_has_no_such_file(self):
        """Emos 说「一周内上传过」但目标条目下没有该文件：不能算成功，也不该重试"""
        handle, path = tempfile.mkstemp(suffix=".mkv")
        os.write(handle, b"x" * 16)
        os.close(handle)
        try:
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            uploader.client.get_video_base = MagicMock(
                return_value={"title": "来！金来号！", "video_medias": []}
            )
            uploader.client.get_upload_token = MagicMock(
                return_value={"existed": True, "message": "此资源您一周内上传过"}
            )
            uploader.client.save_video = MagicMock()
            uploader.client.multipart_presign = MagicMock(
                side_effect=AssertionError("被拒绝时不应请求分片凭证")
            )

            result = uploader.upload_video(path, "ve", "3039064")

            self.assertIsNotNone(result)
            self.assertTrue(result.get("deferred"))
            self.assertFalse(result.get("skipped"))
            self.assertIn("Emos 限制", str(result.get("reason")))
            self.assertIn("目标条目下没有找到该文件", str(result.get("reason")))
            uploader.client.save_video.assert_not_called()
            self.assertEqual(uploader.last_error, str(result.get("reason")))
        finally:
            os.remove(path)

    def test_episode_has_media_requires_same_name_when_present(self):
        """有文件名时只认同名，不能仅凭大小把别的文件当成「已上传」"""
        uploader = RobustEmosVideoUploader(auth_token="t", base_url="https://emos.best")
        uploader.client.get_video_base = MagicMock(
            return_value={
                "video_medias": [{"media_name": "b.mkv", "media_file_size": 42}]
            }
        )
        self.assertTrue(uploader._episode_has_media("ve", 1, "b.mkv", 7))
        self.assertFalse(uploader._episode_has_media("ve", 1, "a.mkv", 42))
        self.assertFalse(uploader._episode_has_media("ve", 1, "a.mkv", 7))

        # 媒体项没有文件名时才退回按大小判断
        uploader.client.get_video_base = MagicMock(
            return_value={"video_medias": [{"media_file_size": 42}]}
        )
        self.assertTrue(uploader._episode_has_media("ve", 1, "a.mkv", 42))
        self.assertFalse(uploader._episode_has_media("ve", 1, "a.mkv", 7))

        uploader.client.get_video_base = MagicMock(side_effect=RuntimeError("boom"))
        self.assertFalse(uploader._episode_has_media("ve", 1, "a.mkv", 7))


class TestUploadEmosSubtitles(unittest.TestCase):
    """视频上传成功后顺带上传同目录同名的外挂字幕（/api/upload/subtitle/save）"""

    def test_find_subtitle_files_matches_same_stem_and_language_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "Show.S01E01.mkv"
            video.write_bytes(b"v")
            (base / "Show.S01E01.srt").write_bytes(b"s")
            (base / "Show.S01E01.chs.ass").write_bytes(b"s")
            (base / "Show.S01E01.zh-Hans.vtt").write_bytes(b"s")
            (base / "Show.S01E01.mkv.srt").write_bytes(b"s")
            (base / "Show.S01E02.srt").write_bytes(b"s")
            (base / "Other.srt").write_bytes(b"s")
            (base / "Show.S01E01.txt").write_bytes(b"s")

            found = {p.name for p in find_subtitle_files(video)}

            self.assertEqual(
                found,
                {
                    "Show.S01E01.srt",
                    "Show.S01E01.chs.ass",
                    "Show.S01E01.zh-Hans.vtt",
                    "Show.S01E01.mkv.srt",
                },
            )

    def test_find_subtitle_files_skips_subtitles_of_sibling_videos(self):
        """同目录同一集有多个格式时，别把 A.ts 的字幕挂到 A.m2ts 上"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ts_video = base / "Show.S01E01.ts"
            ts_video.write_bytes(b"v")
            m2ts_video = base / "Show.S01E01.m2ts"
            m2ts_video.write_bytes(b"v")
            (base / "Show.S01E01.ts.ass").write_bytes(b"s")
            (base / "Show.S01E01.m2ts.ass").write_bytes(b"s")

            self.assertEqual(
                [p.name for p in find_subtitle_files(m2ts_video)],
                ["Show.S01E01.m2ts.ass"],
            )
            self.assertEqual(
                [p.name for p in find_subtitle_files(ts_video)],
                ["Show.S01E01.ts.ass"],
            )

    def test_find_subtitle_files_handles_missing_directory(self):
        self.assertEqual(find_subtitle_files(Path("Z:/not-exists/a.mkv")), [])
        self.assertEqual(find_subtitle_files(Path("")), [])

    def test_find_subtitle_files_matches_underscore_track_suffix(self):
        """下划线/短横线分隔的轨道、语言标签也要能匹配（..._track9_chi.ass）"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            name = (
                "迪迦奥特曼.Ultraman.Tiga.EP01.光的继承者.1996.BluRay.1080p.x264.LPCM."
                "国粤日台多音轨.内封多字幕.FFans@星星"
            )
            video = base / f"{name}.mkv"
            video.write_bytes(b"v")
            (base / f"{name}_track9_chi.ass").write_bytes(b"s")
            (base / f"{name}-chi.srt").write_bytes(b"s")
            (base / f"{name} chi.srt").write_bytes(b"s")

            found = {p.name for p in find_subtitle_files(video)}

            self.assertEqual(
                found,
                {
                    f"{name}_track9_chi.ass",
                    f"{name}-chi.srt",
                    f"{name} chi.srt",
                },
            )

    def test_find_subtitle_files_keeps_subtitles_of_other_videos(self):
        """A_2.srt 属于 A_2.mkv，不能挂到 A.mkv 上"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "A.mkv").write_bytes(b"v")
            (base / "A_2.mkv").write_bytes(b"v")
            (base / "A_2.srt").write_bytes(b"s")
            (base / "A.ass").write_bytes(b"s")

            self.assertEqual(
                [p.name for p in find_subtitle_files(base / "A.mkv")], ["A.ass"]
            )
            self.assertEqual(
                [p.name for p in find_subtitle_files(base / "A_2.mkv")], ["A_2.srt"]
            )

    def test_upload_subtitle_binds_standalone_subtitle(self):
        """视频不在本地时也要能单独补传字幕"""
        handle, path = tempfile.mkstemp(suffix=".ass")
        os.write(handle, b"[Script Info]\n")
        os.close(handle)
        try:
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            uploader.client.get_upload_token = MagicMock(
                return_value={
                    "file_id": "f1",
                    "type": "r2",
                    "data": {"upload_url": "https://r2.example/x"},
                }
            )
            uploader.client.save_subtitle = MagicMock(
                return_value={"subtitle_id": "sub9"}
            )
            uploader._upload_google_drive = MagicMock()

            result = uploader.upload_subtitle(Path(path), "ve", "42", "internal")

            self.assertEqual(result.get("subtitle_id"), "sub9")
            self.assertEqual(result.get("kind"), "subtitle")
            uploader.client.save_subtitle.assert_called_once_with("ve", "42", "f1")
        finally:
            os.remove(path)

    def test_upload_subtitle_rejects_unsupported_file(self):
        handle, path = tempfile.mkstemp(suffix=".mkv")
        os.write(handle, b"v")
        os.close(handle)
        try:
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            with self.assertRaises(EmosApiError):
                uploader.upload_subtitle(Path(path), "ve", "1")
            with self.assertRaises(EmosApiError):
                uploader.upload_subtitle(Path(path).with_suffix(".srt"), "ve", "1")
        finally:
            os.remove(path)

    def test_save_subtitle_posts_to_subtitle_endpoint(self):
        client = EmosClient(base_url="https://emos.best", auth_token="t")
        captured = {}

        class _Resp:
            status_code = 200
            text = '{"subtitle_id": "s1", "carrot": 0}'

            def close(self):
                pass

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["payload"] = json.loads(kwargs["data"].decode("utf-8"))
            return _Resp()

        client.session.request = fake_request

        result = client.save_subtitle("ve", 12, "f1")

        self.assertEqual(result.get("subtitle_id"), "s1")
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/api/upload/subtitle/save"))
        self.assertEqual(
            captured["payload"], {"item_type": "ve", "item_id": 12, "file_id": "f1"}
        )

    def test_upload_subtitle_file_uses_subtitle_resource_type(self):
        handle, path = tempfile.mkstemp(suffix=".srt")
        os.write(handle, b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        os.close(handle)
        try:
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            uploader.client.get_upload_token = MagicMock(
                return_value={
                    "file_id": "f1",
                    "type": "r2",
                    "data": {"upload_url": "https://r2.example/x"},
                }
            )
            uploader.client.save_subtitle = MagicMock(
                return_value={"subtitle_id": "sub1"}
            )
            uploader._upload_google_drive = MagicMock()

            subtitle_id = uploader._upload_subtitle_file(
                Path(path), "ve", "1", "internal"
            )

            self.assertEqual(subtitle_id, "sub1")
            kwargs = uploader.client.get_upload_token.call_args.kwargs
            self.assertEqual(kwargs["resource_type"], "subtitle")
            self.assertEqual(kwargs["file_name"], os.path.basename(path))
            self.assertEqual(kwargs["file_storage"], "internal")
            uploader._upload_google_drive.assert_called_once()
            uploader.client.save_subtitle.assert_called_once_with("ve", "1", "f1")
        finally:
            os.remove(path)

    def test_subtitle_upload_does_not_report_video_progress(self):
        """字幕直传不能上报进度，否则会覆盖视频任务/Telegram 进度"""
        handle, path = tempfile.mkstemp(suffix=".srt")
        os.write(handle, b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        os.close(handle)
        try:
            reports = []
            uploader = RobustEmosVideoUploader(
                auth_token="t",
                base_url="https://emos.best",
                progress_callback=lambda *args: reports.append(args),
            )
            uploader.storage_session = _RecordingSession(
                _FakeResponse(200, {"ETag": '"e1"'})
            )
            uploader.client.get_upload_token = MagicMock(
                return_value={
                    "file_id": "f1",
                    "type": "r2",
                    "data": {"upload_url": "https://r2.example/x"},
                }
            )
            uploader.client.save_subtitle = MagicMock(
                return_value={"subtitle_id": "s1"}
            )

            uploader._upload_subtitle_file(Path(path), "ve", "1", "internal")

            self.assertEqual(reports, [])
            self.assertEqual(len(uploader.storage_session.calls), 1)
        finally:
            os.remove(path)

    def test_upload_tus_creates_then_patches(self):
        """tusd 必须先 POST 创建拿 Location，再按 Upload-Offset 逐段 PATCH"""
        handle, path = tempfile.mkstemp(suffix=".srt")
        os.write(handle, b"x" * 20)
        os.close(handle)
        try:
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            uploader._tus_chunk_size = lambda size: 8
            session = _RecordingTusSession(location="/files/abc")
            uploader.storage_session = session
            uploader.client.get_user_base = MagicMock(
                return_value={"user_id": "u1", "username": "tester"}
            )
            token = {
                "file_id": "f1",
                "type": "tusd",
                "data": {"upload_url": "https://emos.best/tus/"},
            }

            uploader._upload_tus(Path(path), token, 20, report=False)

            self.assertEqual(
                [call[0] for call in session.calls],
                ["POST", "PATCH", "PATCH", "PATCH"],
            )
            _, url, kwargs = session.calls[0]
            self.assertEqual(url, "https://emos.best/tus/")
            self.assertEqual(kwargs["headers"]["Tus-Resumable"], "1.0.0")
            self.assertEqual(kwargs["headers"]["Upload-Length"], "20")
            # tusd 的 pre-create 钩子要求同时带 user_id + file_id，否则 422
            self.assertEqual(
                kwargs["headers"]["Upload-Metadata"],
                "file_id "
                + base64.b64encode(b"f1").decode("ascii")
                + ",user_id "
                + base64.b64encode(b"u1").decode("ascii"),
            )
            # Location 是绝对路径（tusd 的 /files/<id>）时替换 endpoint 的路径部分
            self.assertEqual(session.calls[1][1], "https://emos.best/files/abc")
            self.assertEqual(
                [int(call[2]["headers"]["Upload-Offset"]) for call in session.calls[1:]],
                [0, 8, 16],
            )
            self.assertEqual(
                session.calls[1][2]["headers"]["Content-Type"],
                "application/offset+octet-stream",
            )
        finally:
            os.remove(path)

    def test_upload_tus_create_failure_is_reported(self):
        handle, path = tempfile.mkstemp(suffix=".srt")
        os.write(handle, b"x" * 20)
        os.close(handle)
        try:
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            session = _FailingTusSession(403)
            uploader.storage_session = session
            token = {
                "file_id": "f1",
                "type": "tusd",
                "data": {"upload_url": "https://emos.best/tus/"},
            }

            with self.assertRaises(EmosApiError) as ctx:
                uploader._upload_tus(Path(path), token, 20)

            self.assertIn("tusd 创建上传失败", str(ctx.exception))
            self.assertIn("403", str(ctx.exception))
        finally:
            os.remove(path)

    def test_upload_tus_metadata_user_id_from_token_data(self):
        """token.data 里带 user_id 时优先使用，不再请求 /api/user/base"""
        uploader = RobustEmosVideoUploader(
            auth_token="t", base_url="https://emos.best"
        )
        uploader.client.get_user_base = MagicMock(
            side_effect=AssertionError("不应调用 get_user_base")
        )
        token = {
            "file_id": "f1",
            "type": "tusd",
            "data": {"upload_url": "https://emos.best/tus/", "user_id": "u9"},
        }

        metadata = uploader._tus_metadata(token)

        self.assertEqual(
            metadata,
            "file_id "
            + base64.b64encode(b"f1").decode("ascii")
            + ",user_id "
            + base64.b64encode(b"u9").decode("ascii"),
        )

    def test_upload_tus_metadata_without_user_id_falls_back_to_file_id(self):
        """拿不到用户 ID 时只带 file_id（避免完全不上传）"""
        uploader = RobustEmosVideoUploader(
            auth_token="t", base_url="https://emos.best"
        )
        uploader.client.get_user_base = MagicMock(return_value={})
        token = {"file_id": "f1", "type": "tusd", "data": {}}

        self.assertEqual(
            uploader._tus_metadata(token),
            "file_id " + base64.b64encode(b"f1").decode("ascii"),
        )

    def test_upload_tus_create_retries_other_slash_form(self):
        """Emos 的 upload_url 少了结尾斜杠时（/files 404）自动补斜杠重试"""
        handle, path = tempfile.mkstemp(suffix=".srt")
        os.write(handle, b"x" * 20)
        os.close(handle)
        try:
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            uploader._tus_chunk_size = lambda size: 8
            uploader.client.get_user_base = MagicMock(return_value={"user_id": "u1"})
            session = _SlashAwareTusSession()
            uploader.storage_session = session
            token = {
                "file_id": "f1",
                "type": "tusd",
                "data": {"upload_url": "https://file.emos.best/files"},
            }

            uploader._upload_tus(Path(path), token, 20, report=False)

            posts = [call[1] for call in session.calls if call[0] == "POST"]
            self.assertEqual(
                posts,
                [
                    "https://file.emos.best/files",
                    "https://file.emos.best/files/",
                ],
            )
            patches = [call[1] for call in session.calls if call[0] == "PATCH"]
            self.assertTrue(patches)
            self.assertEqual(patches[0], "https://file.emos.best/files/abc")
        finally:
            os.remove(path)

    def test_upload_subtitle_file_uses_tus_when_token_type_is_tusd(self):
        """字幕走 tusd 时也要能上传（官方已对字幕暂停 r2）"""
        handle, path = tempfile.mkstemp(suffix=".srt")
        os.write(handle, b"x" * 20)
        os.close(handle)
        try:
            reports = []
            uploader = RobustEmosVideoUploader(
                auth_token="t",
                base_url="https://emos.best",
                progress_callback=lambda *args: reports.append(args),
            )
            uploader.client.get_upload_token = MagicMock(
                return_value={
                    "file_id": "f1",
                    "type": "tusd",
                    "data": {"upload_url": "https://emos.best/tus/"},
                }
            )
            uploader.client.save_subtitle = MagicMock(
                return_value={"subtitle_id": "s1"}
            )
            session = _RecordingTusSession()
            uploader.storage_session = session
            uploader.client.get_user_base = MagicMock(return_value={"user_id": "u1"})

            subtitle_id = uploader._upload_subtitle_file(
                Path(path), "ve", "1", "internal"
            )

            self.assertEqual(subtitle_id, "s1")
            self.assertEqual([call[0] for call in session.calls], ["POST", "PATCH"])
            self.assertEqual(reports, [])
            uploader.client.save_subtitle.assert_called_once_with("ve", "1", "f1")
        finally:
            os.remove(path)

    def test_subtitle_multipart_uses_subtitle_mime_and_no_progress(self):
        handle, path = tempfile.mkstemp(suffix=".srt")
        os.write(handle, b"x" * 16)
        os.close(handle)
        try:
            reports = []
            uploader = RobustEmosVideoUploader(
                auth_token="t",
                base_url="https://emos.best",
                progress_callback=lambda *args: reports.append(args),
            )
            uploader.client.get_upload_token = MagicMock(
                return_value={
                    "file_id": "f1",
                    "type": "multipart",
                    "data": {"multipart_size": {"min": 0, "max": 0}},
                }
            )
            uploader.client.multipart_presign = MagicMock(
                return_value=[{"number": 1, "upload_url": "https://r2.example.com/p1"}]
            )
            uploader.client.multipart_complete = MagicMock(return_value={})
            uploader.client.save_subtitle = MagicMock(
                return_value={"subtitle_id": "s1"}
            )
            session = _RecordingSession(_FakeResponse(200, {"ETag": '"e1"'}))
            uploader.storage_session = session

            subtitle_id = uploader._upload_subtitle_file(
                Path(path), "ve", "1", "internal"
            )

            self.assertEqual(subtitle_id, "s1")
            _, kwargs = session.calls[0]
            self.assertEqual(kwargs["headers"]["Content-Type"], "application/x-subrip")
            self.assertEqual(reports, [])
            uploader.client.multipart_complete.assert_called_once()
            uploader.client.save_subtitle.assert_called_once_with("ve", "1", "f1")
        finally:
            os.remove(path)

    def test_upload_subtitle_files_continues_after_one_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "A.mkv"
            video.write_bytes(b"v")
            (base / "A.srt").write_bytes(b"s")
            (base / "A.ass").write_bytes(b"s")
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )

            def fake_upload(path, item_type, item_id, file_storage):
                if path.suffix == ".ass":
                    raise EmosApiError("Emos 接口返回 HTTP 422")
                return "sub-1"

            uploader._upload_subtitle_file = fake_upload

            summary = uploader.upload_subtitle_files(str(video), "ve", "1")

            self.assertEqual(summary["found"], 2)
            self.assertEqual(summary["uploaded"], ["A.srt"])
            self.assertEqual(len(summary["failed"]), 1)
            self.assertEqual(summary["failed"][0]["name"], "A.ass")
            self.assertEqual(summary["paths"], [str(base / "A.srt")])

    def test_upload_video_uploads_sibling_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "A.mkv"
            video.write_bytes(b"x" * 16)
            (base / "A.chs.srt").write_bytes(b"1\n")
            uploader = RobustEmosVideoUploader(
                auth_token="t", base_url="https://emos.best"
            )
            uploader.client.get_video_base = MagicMock(return_value={"title": "T"})
            uploader.client.get_upload_token = MagicMock(
                return_value={
                    "file_id": "f1",
                    "type": "r2",
                    "data": {"upload_url": "https://r2.example/x"},
                }
            )
            uploader.client.save_video = MagicMock(return_value={"media_id": "m1"})
            uploader.client.save_subtitle = MagicMock(
                return_value={"subtitle_id": "s1"}
            )
            uploader._upload_google_drive = MagicMock()

            result = uploader.upload_video(str(video), "ve", "1")

            self.assertIsNotNone(result)
            self.assertEqual(result["subtitles"]["found"], 1)
            self.assertEqual(result["subtitles"]["uploaded"], ["A.chs.srt"])
            self.assertEqual(result["subtitle_paths"], [str(base / "A.chs.srt")])
            uploader.client.save_video.assert_called_once()
            uploader.client.save_subtitle.assert_called_once_with("ve", "1", "f1")
            self.assertIn("📎 字幕", uploader._tg_result_extra)

    def test_upload_video_can_skip_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "A.mkv"
            video.write_bytes(b"x" * 16)
            (base / "A.srt").write_bytes(b"1\n")
            uploader = RobustEmosVideoUploader(
                auth_token="t",
                base_url="https://emos.best",
                upload_subtitles=False,
            )
            uploader.client.get_video_base = MagicMock(return_value={"title": "T"})
            uploader.client.get_upload_token = MagicMock(
                return_value={
                    "file_id": "f1",
                    "type": "r2",
                    "data": {"upload_url": "https://r2.example/x"},
                }
            )
            uploader.client.save_video = MagicMock(return_value={"media_id": "m1"})
            uploader.client.save_subtitle = MagicMock()
            uploader._upload_google_drive = MagicMock()

            result = uploader.upload_video(str(video), "ve", "1")

            self.assertIsNotNone(result)
            self.assertNotIn("subtitles", result)
            uploader.client.save_subtitle.assert_not_called()

    def test_subtitle_summary_text_reports_failures(self):
        text = RobustEmosVideoUploader._subtitle_summary_text(
            {
                "found": 2,
                "uploaded": ["a.srt"],
                "failed": [{"name": "b.ass", "error": "HTTP 422"}],
            }
        )

        self.assertIn("1/2", text)
        self.assertIn("b.ass", text)
        self.assertIn("HTTP 422", text)
        self.assertEqual(
            RobustEmosVideoUploader._subtitle_summary_text({"found": 0}), ""
        )

    def test_completed_telegram_text_includes_subtitles(self):
        uploader = RobustEmosVideoUploader(
            auth_token="t", base_url="https://emos.best"
        )
        uploader.tg_bot_token = "bot"
        uploader.tg_chat_id = "1"
        sent = []
        uploader._tg_send = lambda text: sent.append(text)
        uploader._tg_result_extra = "📎 字幕: 1/1 已上传"

        uploader._tg_update("A.mkv", 100, 16, 16, "", "completed")

        self.assertTrue(sent)
        self.assertIn("📎 字幕: 1/1 已上传", sent[-1])


if __name__ == "__main__":
    unittest.main()
