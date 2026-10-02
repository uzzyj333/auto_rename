"""Emos 上传器分片直传的回归测试

重点覆盖两类曾经导致「分片 1/N 上传失败: HTTP 400」的问题：

1. 直传预签名 URL 时误用带 Emos ``Authorization`` 头的会话（对象存储会拒绝）；
2. 失败时只报状态码，不带响应体，无法定位真实原因。

另外覆盖分片并发上传与 Telegram 进度文本格式。
"""

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

    def test_episode_has_media_matches_name_or_size(self):
        uploader = RobustEmosVideoUploader(auth_token="t", base_url="https://emos.best")
        uploader.client.get_video_base = MagicMock(
            return_value={
                "video_medias": [{"media_name": "b.mkv", "media_file_size": 42}]
            }
        )
        self.assertTrue(uploader._episode_has_media("ve", 1, "b.mkv", 7))
        self.assertTrue(uploader._episode_has_media("ve", 1, "a.mkv", 42))
        self.assertFalse(uploader._episode_has_media("ve", 1, "a.mkv", 7))

        uploader.client.get_video_base = MagicMock(side_effect=RuntimeError("boom"))
        self.assertFalse(uploader._episode_has_media("ve", 1, "a.mkv", 7))


if __name__ == "__main__":
    unittest.main()
