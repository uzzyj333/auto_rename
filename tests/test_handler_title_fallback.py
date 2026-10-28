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


class _AlternateTitleService(_FakeService):
    """只认识文件名里的英文原名，模拟 TMDB 中文译名与 Emos 不一致"""

    def search_targets(self, video_type=None, title=None, todb_id=None):
        self.searched.append(title)
        if title == "VIVANT":
            return [{"title": "VIVANT", "item_id": "1", "item_type": "vl", "seasons": []}]
        return []


class _WrongMatchService:
    """模拟 Emos 搜「Brothers」却返回 Ang Probinsyano 的同号集"""

    def search_targets(self, video_type=None, title=None, todb_id=None):
        return [
            {
                "title": "Ang Probinsyano",
                "item_id": "222",
                "item_type": "vl",
                "seasons": [],
            }
        ]

    def pick_target(self, candidates, season, episode, media_type, year=None, title=None):
        return {
            "item_type": "ve",
            "item_id": "ve-ang-7",
            "label": "第 7 集",
            "title_matched": False,
            "video_title": "Ang Probinsyano",
        }

    def resolve_episode_from_candidates(self, *args, **kwargs):
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

    def test_alternate_title_tried_when_tmdb_title_misses(self):
        service = _AlternateTitleService()
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService.instance",
            lambda: service,
        ):
            match = self.handler._search_emos_by_title(
                "成吉思-VIVANT THE ORIGIN STORY-",
                "tv",
                2,
                7,
                2026,
                0,
                alt_titles=["VIVANT"],
            )

        self.assertIsNotNone(match)
        self.assertEqual(match["item_id"], "99")
        self.assertIn("VIVANT", service.searched)

    def test_process_passes_filename_title_as_alternate(self):
        self.handler.renamer = _FakeRenamer(
            {
                "title": "成吉思-VIVANT THE ORIGIN STORY-",
                "show_name": "成吉思-VIVANT THE ORIGIN STORY-",
                "original_show_name": "VIVANT",
                "tmdb_id": "12345",
                "media_type": "tv",
                "season": 2,
                "episode": 7,
                "year": 2026,
            }
        )
        self.handler.emos_client = _RaisingEmosClient()
        captured = {}

        def fake_search(
            self, title, media_type, season, episode, year=None, worker_id=0, alt_titles=None
        ):
            captured["title"] = title
            captured["alt_titles"] = list(alt_titles or [])
            return {"item_type": "ve", "item_id": "ve-7", "label": "VIVANT S2 E7"}

        with patch.object(
            VideoFileHandler, "_search_emos_by_title", fake_search
        ), patch.object(
            VideoFileHandler, "_execute_upload", lambda self, *a, **k: None
        ):
            self.handler._process_file_internal(
                "/media/[别班 第二季].VIVANT.2026.S02E07.1080p.mkv", 0
            )

        self.assertEqual(captured["title"], "成吉思-VIVANT THE ORIGIN STORY-")
        self.assertIn("VIVANT", captured["alt_titles"])

    def test_no_candidates_returns_none(self):
        service = _FakeService()
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService.instance",
            lambda: service,
        ):
            match = self.handler._search_emos_by_title("查无此剧", "tv", 1, 1, None, 0)

        self.assertIsNone(match)

    def test_title_mismatch_is_not_auto_uploaded(self):
        """自动匹配拒绝标题不相关的候选，交给用户在 Telegram 手动选"""
        service = _WrongMatchService()
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService.instance",
            lambda: service,
        ):
            match = self.handler._search_emos_by_title("Brothers", "tv", 1, 7, None, 0)

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
            self, title, media_type, season, episode, year=None, worker_id=0, alt_titles=None
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
            self, title, media_type, season, episode, year=None, worker_id=0, alt_titles=None
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


class TestMovieMediaTypeFallback(_TempDbMixin, unittest.TestCase):
    """renamer 判不出类型（media_type=None）时电影不能按电视剧查询

    以前 media_type 为空一律归类为 tv：电影文件（如
    「梁祝.(1994).1080p.H.265.10-bit.Dolby.Digital.mkv」）会拿 tmdb_type=tv
    去查 Emos（getVideoId 404），选中作品级条目时又会被当成
    「电视剧必须选到具体某一集（ve）」，永远传不上去。
    """

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

    def test_movie_without_episode_uses_movie_type(self):
        self.handler.renamer = _FakeRenamer(
            {
                "show_name": "梁祝",
                "media_type": None,
                "year": 1994,
                "tmdb_id": "123",
            }
        )
        self.handler.emos_client = _RaisingEmosClient()
        searched = []

        def fake_search(
            self, title, media_type, season, episode, year=None, worker_id=0, alt_titles=None
        ):
            searched.append((title, media_type, season, episode))
            return {"item_type": "vl", "item_id": "vl-1", "label": "梁祝"}

        uploaded = []
        with patch.object(
            VideoFileHandler, "_search_emos_by_title", fake_search
        ), patch.object(
            VideoFileHandler,
            "_execute_upload",
            lambda self, *a, **k: uploaded.append(a),
        ):
            result = self.handler._process_file_internal(
                "/media/梁祝.(1994).1080p.H.265.10-bit.Dolby.Digital.mkv", 0
            )

        self.assertTrue(result)
        # 标题搜索兜底必须收到 movie，而不是 tv
        self.assertEqual(searched[0][1], "movie")
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0][1], "vl")
        self.assertEqual(uploaded[0][5], "movie")


if __name__ == "__main__":
    unittest.main()
