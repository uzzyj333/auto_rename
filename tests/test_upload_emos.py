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

from src.video_organizer.core.emos_client import EmosApiError
from src.video_organizer.upload.upload_emos import (
    RobustEmosVideoUploader,
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
        self.assertEqual(self._uploader().upload_concurrency, 4)
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
            16,
        )
        self.assertEqual(
            RobustEmosVideoUploader(
                auth_token="t", upload_concurrency="8"
            ).upload_concurrency,
            8,
        )

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


if __name__ == "__main__":
    unittest.main()
