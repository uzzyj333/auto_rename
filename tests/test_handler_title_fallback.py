# -*- coding: utf-8 -*-
"""自动上传管道「按标题搜索兜底」的回归测试

文件名识别正确、但 TMDB 匹配失败（或没有 TMDB ID）时，以前会直接判失败并推送
报错；现在会先按标题在 Emos 里搜索并定位到具体某一集，命中就直接上传。
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


class _FakeService:
    """只对「狂王」返回候选，用来验证标题变体依次搜索"""

    def __init__(self):
        self.searched = []

    def search_targets(self, video_type=None, title=None, todb_id=None):
        self.searched.append(title)
        if title == "狂王":
            return [{"title": "狂王", "item_id": "1", "item_type": "vl", "seasons": []}]
        return []

    def pick_target(
        self, candidates, season, episode, media_type, year=None, title=None
    ):
        return {"item_type": "ve", "item_id": "99", "label": "狂王 S2 E2"}

    def resolve_episode_from_candidates(
        self, candidates, season, episode, media_type, title=None
    ):
        return None


class _FakeRenamer:
    def __init__(self, metadata, tmdb_client=None):
        self._metadata = metadata
        self.tmdb_client = tmdb_client

    def extract_metadata(self, file_path):
        return dict(self._metadata)


class _RaisingEmosClient:
    """模拟 getVideoId 返回 404 的 Emos 客户端"""

    def get_video_id(self, *args, **kwargs):
        raise RuntimeError("Emos 接口返回 HTTP 404: GET /api/video/getVideoId")


class TestTitleSearchFallback(_TempDbMixin, unittest.TestCase):
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

    def tearDown(self):
        self.handler.stop_upload_queue()
        self._teardown_temp_db()

    def test_variants_tried_until_hit(self):
        service = _FakeService()
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService.instance",
            lambda: service,
        ):
            match = self.handler._search_emos_by_title(
                "狂王.Asura", "tv", 2, 2, 2026, 0
            )

        self.assertIsNotNone(match)
        self.assertEqual(match["item_id"], "99")
        self.assertIn("狂王", service.searched)

    def test_no_candidates_returns_none(self):
        service = _FakeService()
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService.instance",
            lambda: service,
        ):
            match = self.handler._search_emos_by_title("查无此剧", "tv", 1, 1, None, 0)

        self.assertIsNone(match)

    def test_missing_tmdb_uses_title_fallback(self):
        self.handler.renamer = _FakeRenamer(
            {
                "show_name": "狂王",
                "media_type": "tv",
                "season": 2,
                "episode": 2,
                "year": 2026,
            }
        )
        searched = []

        def fake_search(
            self, title, media_type, season, episode, year=None, worker_id=0
        ):
            searched.append((title, media_type, season, episode, year))
            return {"item_type": "ve", "item_id": "99", "label": "狂王 S2 E2"}

        uploaded = []
        with patch.object(
            VideoFileHandler, "_search_emos_by_title", fake_search
        ), patch.object(
            VideoFileHandler,
            "_execute_upload",
            lambda self, *a, **k: uploaded.append(a),
        ):
            result = self.handler._process_file_internal(
                "/media/狂王.Asura.S02E02.2026.mkv", 0
            )

        self.assertTrue(result)
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0][2], "99")
        self.assertIn("狂王", searched[0][0])

    def test_tmdb_lookup_error_falls_back_to_title(self):
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

        def fake_search(
            self, title, media_type, season, episode, year=None, worker_id=0
        ):
            return {"item_type": "ve", "item_id": "77", "label": "狂王 S2 E2"}

        uploaded = []
        notified = []
        with patch.object(
            VideoFileHandler, "_search_emos_by_title", fake_search
        ), patch.object(
            VideoFileHandler,
            "_execute_upload",
            lambda self, *a, **k: uploaded.append(a),
        ), patch.object(
            VideoFileHandler,
            "_notify_match_error",
            lambda self, *a, **k: notified.append(a),
        ):
            result = self.handler._process_file_internal(
                "/media/狂王.Asura.S02E02.2026.mkv", 0
            )

        self.assertTrue(result)
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0][2], "77")
        self.assertEqual(notified, [])


if __name__ == "__main__":
    unittest.main()
