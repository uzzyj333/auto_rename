# -*- coding: utf-8 -*-
"""在线识别上传「电视剧必须落到具体某一集」的回归测试

在「在线识别上传」里搜索剧名，结果第一行是整部作品（vl/xxx）。以前点「设为目标」
会直接把 vl 交给上传接口，Emos 的 ``GET /api/upload/video/base`` 返回 404，
用户只能看到一条没头没尾的报错。现在创建任务时会把 vl/vs 按季/集号纠正到 ve。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.online_upload import OnlineUploadService


class _NoopExecutor:
    """不真的跑上传，只保留任务记录供断言"""

    def submit(self, fn, *args, **kwargs):
        return None


class TestResolveTvEpisodeTarget(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.video = self.root / "Jade.Cause Of Death.Ep13.HDTV.1080p.H264-CNHK.ts"
        self.video.write_bytes(b"v" * 64)
        self.service = OnlineUploadService()
        self.service.configure(
            {
                "emos": {"auth_token": "t", "base_url": "https://emos.best"},
                "processing": {"delete_after_upload": False},
                "online_upload": {"video_root": str(self.root)},
            }
        )
        self.service._executor = _NoopExecutor()

    def tearDown(self):
        self._tmp.cleanup()

    def _create(self, **overrides):
        item = {
            "file_path": str(self.video),
            "item_type": "vl",
            "item_id": "202974",
            "storage": "internal",
            "title": "Jade Cause Of Death",
            "media_type": "tv",
            "season_number": 1,
            "episode_number": 13,
        }
        item.update(overrides)
        return self.service.create_task(item)

    def test_vl_target_is_resolved_to_episode(self):
        with patch.object(
            OnlineUploadService, "get_client", lambda self: object()
        ), patch.object(
            OnlineUploadService,
            "resolve_episode_from_tree",
            staticmethod(
                lambda client, vl_id, season, episode: {
                    "item_type": "ve",
                    "item_id": "888",
                    "kind": "episode",
                    "season_number": 1,
                    "episode_number": 13,
                }
            ),
        ):
            task = self._create()

        self.assertEqual(task["item_type"], "ve")
        self.assertEqual(task["item_id"], "888")
        self.assertEqual(task["episode_number"], 13)

    def test_vl_target_not_found_in_tree_is_rejected(self):
        with patch.object(
            OnlineUploadService, "get_client", lambda self: object()
        ), patch.object(
            OnlineUploadService,
            "resolve_episode_from_tree",
            staticmethod(lambda client, vl_id, season, episode: None),
        ):
            with self.assertRaises(ValueError) as ctx:
                self._create()

        self.assertIn("具体某一集", str(ctx.exception))
        self.assertEqual(self.service.list_tasks(), [])

    def test_tv_target_without_episode_is_rejected(self):
        """电视剧选了整部作品/整季又解析不出集号时，直接报错而不是去撞 404"""
        with patch.object(
            OnlineUploadService, "get_client", lambda self: object()
        ), patch.object(
            OnlineUploadService,
            "resolve_episode_from_tree",
            staticmethod(
                lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("没有集号时不应查询目录树")
                )
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                self._create(item_type="vs", item_id="555", episode_number=None)

        self.assertIn("具体某一集", str(ctx.exception))
        self.assertEqual(self.service.list_tasks(), [])

    def test_movie_keeps_vl_target(self):
        with patch.object(
            OnlineUploadService, "get_client", lambda self: object()
        ), patch.object(
            OnlineUploadService,
            "resolve_episode_from_tree",
            staticmethod(
                lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("电影不应触发剧集定位")
                )
            ),
        ):
            task = self._create(media_type="movie", season_number=None, episode_number=None)

        self.assertEqual(task["item_type"], "vl")
        self.assertEqual(task["item_id"], "202974")

    def test_episode_target_is_left_untouched(self):
        with patch.object(
            OnlineUploadService, "get_client", lambda self: object()
        ), patch.object(
            OnlineUploadService,
            "resolve_episode_from_tree",
            staticmethod(
                lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("ve 目标不应再走剧集定位")
                )
            ),
        ):
            task = self._create(item_type="ve", item_id="888")

        self.assertEqual(task["item_type"], "ve")
        self.assertEqual(task["item_id"], "888")


if __name__ == "__main__":
    unittest.main()
