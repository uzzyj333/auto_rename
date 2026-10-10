# -*- coding: utf-8 -*-
"""「搜不到目标」的失败文件应加入每分钟自动重试队列的回归测试

此前「未找到 TMDB 匹配结果」和「Emos 中没有该条目 / 该集」只写失败记录并推送
Telegram（每 5 分钟提醒一次），但不进 FilesystemMonitor 的重试队列，用户在 Emos
补建条目后必须手动重传。现在两类失败都加入 _retry_files，由重试循环每分钟重跑
一次识别 + 上传，命中后自动完成。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.video_file_handler import VideoFileHandler


class _TempDbMixin:
    """把测试用数据库写到临时目录，避免污染项目数据"""

    def _setup_temp_db(self):
        self._old_db = os.environ.get("VIDEO_ORGANIZER_DB_PATH")
        self._db_tmp = tempfile.TemporaryDirectory()
        os.environ["VIDEO_ORGANIZER_DB_PATH"] = str(Path(self._db_tmp.name) / "test.db")

    def _teardown_temp_db(self):
        try:
            from src.video_organizer.database import session as db_session

            if db_session._engine is not None:
                db_session._engine.dispose()
            db_session._engine = None
            db_session._SessionLocal = None
        except Exception:
            pass
        if self._old_db is None:
            os.environ.pop("VIDEO_ORGANIZER_DB_PATH", None)
        else:
            os.environ["VIDEO_ORGANIZER_DB_PATH"] = self._old_db
        try:
            self._db_tmp.cleanup()
        except Exception:
            pass


class _FakeRenamer:
    def __init__(self, metadata, tmdb_client=None):
        self._metadata = metadata
        self.tmdb_client = tmdb_client

    def extract_metadata(self, file_path):
        return dict(self._metadata)


class _FakeMonitor:
    """只提供重试队列，用于验证失败文件被排进每分钟重试"""

    def __init__(self):
        self._retry_files = set()


class _RaisingEmosClient:
    """模拟 getVideoId 返回 404 的 Emos 客户端"""

    def get_video_id(self, *args, **kwargs):
        raise RuntimeError("Emos 接口返回 HTTP 404: GET /api/video/getVideoId")


class TestNotFoundAutoRetry(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._setup_temp_db()
        self.handler = VideoFileHandler(
            output_dir=self._db_tmp.name,
            supported_extensions=[".mkv"],
            emos_config={"auth_token": "t"},
            processing_config={"max_upload_workers": 1},
            config={
                "processing": {"max_upload_workers": 1},
                "emos": {"auth_token": "t"},
            },
        )
        self.handler.probe_enabled = False
        self.monitor = _FakeMonitor()
        self.handler._parent_monitor = self.monitor

    def tearDown(self):
        self.handler.stop_upload_queue()
        self._teardown_temp_db()

    def test_emos_target_missing_joined_retry_queue(self):
        """TMDB 有 ID 但 Emos 没有该剧集：记失败 + 每 1 分钟自动重试"""
        self.handler.renamer = _FakeRenamer(
            {
                "show_name": "狂王",
                "tmdb_id": "12345",
                "media_type": "tv",
                "season": 2,
                "episode": 2,
                "year": 2026,
            }
        )
        self.handler.emos_client = _RaisingEmosClient()
        notified = []
        with patch.object(
            VideoFileHandler, "_search_emos_by_title", lambda self, *a, **k: None
        ), patch.object(
            VideoFileHandler,
            "_notify_match_error",
            lambda self, *a, **k: notified.append((a, k)),
        ), patch.object(
            VideoFileHandler,
            "_execute_upload",
            lambda self, *a, **k: self.fail("未找到 Emos 目标时不应执行上传"),
        ):
            file_path = "/media/狂王.Asura.S02E02.2026.mkv"
            result = self.handler._process_file_internal(file_path, 0)
            self.assertTrue(result)
            self.assertIn(file_path, self.monitor._retry_files)
            # 模拟重试循环：处理前会先从队列移除，未命中目标时必须重新入队
            self.monitor._retry_files.discard(file_path)
            self.handler._process_file_internal(file_path, 0)

        self.assertIn(file_path, self.monitor._retry_files)
        self.assertIn(file_path, self.handler._failed_files)
        self.assertTrue(notified)
        self.assertTrue(all(kw.get("auto_retry") for _a, kw in notified))

    def test_tmdb_not_found_joined_retry_queue(self):
        """TMDB 搜不到且 Emos 也搜不到：同样加入每分钟自动重试"""
        self.handler.renamer = _FakeRenamer(
            {
                "show_name": "查无此剧",
                "media_type": "tv",
                "season": 1,
                "episode": 1,
            }
        )
        notified = []
        with patch.object(
            VideoFileHandler, "_search_emos_by_title", lambda self, *a, **k: None
        ), patch.object(
            VideoFileHandler,
            "_notify_match_error",
            lambda self, *a, **k: notified.append((a, k)),
        ):
            file_path = "/media/查无此剧.S01E01.2026.mkv"
            result = self.handler._process_file_internal(file_path, 0)

        self.assertFalse(result)
        self.assertIn(file_path, self.monitor._retry_files)
        self.assertEqual(
            self.handler._failed_files.get(file_path), "未找到 TMDB 匹配结果"
        )
        self.assertEqual(len(notified), 1)
        self.assertTrue(notified[0][1].get("auto_retry"))


if __name__ == "__main__":
    unittest.main()
