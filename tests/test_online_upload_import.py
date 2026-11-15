# -*- coding: utf-8 -*-
"""浏览器本地上传 / 在线 URL 导入的落盘与安全测试"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException

from src.video_organizer.core.online_upload import OnlineUploadService
from src.video_organizer.web.routers import online_upload as online_upload_router


class _FakeResponse:
    def __init__(self, chunks, headers=None, status_code=200):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size=1024 * 1024):
        yield from self._chunks

    def close(self):
        self.closed = True


class _FakeUpload:
    def __init__(self, filename, chunks):
        self.filename = filename
        self._chunks = list(chunks)
        self.closed = False

    async def read(self, size=-1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    async def close(self):
        self.closed = True


class TestUploadStorage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = OnlineUploadService()
        self.service.configure({"online_upload": {"video_root": str(self.root)}})

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_upload_dir_is_under_video_root(self):
        directory = self.service.uploads_dir()
        self.assertEqual(directory, self.root / ".web_uploads")
        self.assertTrue(directory.is_dir())

    def test_alloc_path_keeps_safe_original_name(self):
        path = self.service.alloc_upload_path("../../Show.S01E01.mkv")
        self.assertEqual(path.name, "Show.S01E01.mkv")
        self.assertEqual(path.parent.parent, self.root / ".web_uploads")
        self.assertTrue(path.parent.is_dir())

    def test_rejects_unsupported_extension(self):
        with self.assertRaises(ValueError):
            self.service.alloc_upload_path("payload.exe")

    def test_configured_upload_dir_cannot_escape_video_root(self):
        self.service.configure(
            {
                "online_upload": {
                    "video_root": str(self.root),
                    "upload_dir": str(self.root.parent / "outside"),
                }
            }
        )
        with self.assertRaises(PermissionError):
            self.service.uploads_dir()

    def test_local_upload_honours_size_limit(self):
        self.service.configure(
            {
                "online_upload": {
                    "video_root": str(self.root),
                    "max_upload_mb": 0.000001,
                }
            }
        )
        upload = _FakeUpload("Show.S01E01.mkv", [b"x" * 10])
        with patch.object(online_upload_router, "_service", lambda: self.service):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(online_upload_router.upload_local([upload]))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("max_upload_mb", str(ctx.exception.detail))
        self.assertTrue(upload.closed)
        leftovers = list((self.root / ".web_uploads").rglob("*"))
        self.assertEqual([item for item in leftovers if item.is_file()], [])


class TestUrlImport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = OnlineUploadService()
        self.service.configure(
            {"online_upload": {"video_root": str(self.root), "timeout": 5}}
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_import_url_uses_content_disposition_name(self):
        response = _FakeResponse(
            [b"abc", b"def"],
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''Show.S01E02.mkv",
                "Content-Length": "6",
            },
        )
        with patch(
            "src.video_organizer.core.online_upload.requests.get", return_value=response
        ):
            info = self.service.import_url("https://example.com/download/123")

        path = Path(info["path"])
        self.assertEqual(path.name, "Show.S01E02.mkv")
        self.assertEqual(path.read_bytes(), b"abcdef")
        self.assertTrue(response.closed)

    def test_import_url_rejects_non_http_scheme(self):
        with self.assertRaises(ValueError):
            self.service.import_url("file:///etc/passwd")

    def test_import_url_honours_size_limit(self):
        self.service.configure(
            {
                "online_upload": {
                    "video_root": str(self.root),
                    "max_upload_mb": 0.000001,
                }
            }
        )
        response = _FakeResponse([b"x" * 10], headers={"Content-Length": "10"})
        with patch(
            "src.video_organizer.core.online_upload.requests.get", return_value=response
        ):
            with self.assertRaises(ValueError):
                self.service.import_url("https://example.com/a.mkv")
        leftovers = list((self.root / ".web_uploads").rglob("a.mkv"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
