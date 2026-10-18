# -*- coding: utf-8 -*-
"""回归测试：电影文件名不能因为「有 show_name」就被当成电视剧

「梁祝.(1994).1080p.H.265.10-bit.Dolby.Digital.mkv」这类电影文件名既没有
SxxExx，也没有 WEB-DL / BluRay / x264 这类片源标签，renamer 判定不出
media_type（None）。识别逻辑以前用 ``show_name`` 是否存在来判断，而电影格式
里 show_name 几乎总是有值，于是电影被判成 tv：

* 拿 ``tmdb_type=tv`` 去调 Emos ``getVideoId`` → HTTP 404；
* Telegram 报错回复里选中作品级条目时，又会被当成「电视剧必须选到具体某一集（ve）」
  卡住，永远传不上去。

现在改成按「有没有季 / 集号」判断，上面的电影才算电影。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.emos_client import EmosApiError
from src.video_organizer.core.mapping_store import TargetMappingStore
from src.video_organizer.core.online_upload import OnlineUploadService


class _FakeRenamer:
    """直接返回固定 metadata 的假 renamer"""

    def __init__(self, metadata):
        self.metadata = metadata

    def extract_metadata(self, file_path):
        return dict(self.metadata)


class _FakeClient:
    """记录 tmdb_type，并模拟电影 / 电视剧各自的返回结果"""

    def __init__(self, movie_payload=None, tv_payload=None):
        self.movie_payload = movie_payload
        self.tv_payload = tv_payload
        self.get_video_id_calls = []
        self.tree_calls = []

    def get_video_id(
        self,
        video_id,
        id_type="tmdb",
        tmdb_type="tv",
        season_number=None,
        episode_number=None,
    ):
        self.get_video_id_calls.append(
            {
                "video_id": video_id,
                "id_type": id_type,
                "tmdb_type": tmdb_type,
                "season_number": season_number,
                "episode_number": episode_number,
            }
        )
        if tmdb_type == "movie":
            return dict(self.movie_payload or {})
        return dict(self.tv_payload or {})

    def get_video_tree(self, video_type=None, title=None, todb_id=None):
        self.tree_calls.append(video_type)
        return []


def _video_payload(title):
    return {
        "title": title,
        "video_type": "movie",
        "item_type": "vl",
        "item_id": "vl-1",
        "tmdb_id": "123",
    }


def _episode_payload(title):
    return {
        "title": title,
        "video_type": "tv",
        "item_type": "vl",
        "item_id": "vl-2",
        "tmdb_id": "456",
        "episode_info": {
            "item_type": "ve",
            "item_id": "ve-1",
            "episode_title": "第 4 集",
            "season_number": 1,
            "episode_number": 4,
        },
    }


class TestRecognizeMediaTypeFallback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _service(self, file_name, metadata, client):
        video_root = Path(self._tmp.name)
        path = video_root / file_name
        path.write_bytes(b"0")
        service = OnlineUploadService()
        service.configure(
            {
                "emos": {"auth_token": "t", "base_url": "https://emos.best"},
                "online_upload": {"video_root": str(video_root)},
            }
        )
        service.get_renamer = lambda: _FakeRenamer(metadata)
        patcher = patch.object(OnlineUploadService, "get_client", lambda self: client)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 目标映射表用的是真实数据库，测试里屏蔽掉，避免受本机已有映射影响
        mapping_patcher = patch.object(
            TargetMappingStore, "resolve", lambda **kwargs: None
        )
        mapping_patcher.start()
        self.addCleanup(mapping_patcher.stop)
        return service, str(path)

    def test_movie_without_episode_is_not_treated_as_tv(self):
        # 电影文件名：无季 / 集号，但 show_name 有值（旧逻辑据此判成 tv）
        metadata = {
            "show_name": "梁祝",
            "media_type": None,
            "year": "1994",
            "tmdb_id": "123",
        }
        client = _FakeClient(movie_payload=_video_payload("梁祝"))
        service, file_path = self._service(
            "梁祝.(1994).1080p.H.265.10-bit.Dolby.Digital.mkv", metadata, client
        )

        result = service.recognize(file_path)

        self.assertEqual(result["metadata"]["media_type"], "movie")
        self.assertEqual(result["error"], "")
        self.assertIsNotNone(result["match"])
        self.assertEqual(result["match"]["item_id"], "vl-1")
        self.assertEqual(result["match"]["kind"], "movie")
        # 关键点：用 movie 查 getVideoId，不能是 tv（否则 Emos 404）
        self.assertEqual(
            [call["tmdb_type"] for call in client.get_video_id_calls], ["movie"]
        )

    def test_tv_with_episode_still_uses_tv_type(self):
        metadata = {
            "show_name": "死有对证",
            "media_type": None,
            "season": 1,
            "episode": 4,
            "tmdb_id": "456",
        }
        client = _FakeClient(tv_payload=_episode_payload("死有对证"))
        service, file_path = self._service(
            "死有对证.S01E04.1080p.mkv", metadata, client
        )

        result = service.recognize(file_path)

        self.assertEqual(result["metadata"]["media_type"], "tv")
        self.assertEqual(result["match"]["item_type"], "ve")
        self.assertEqual(
            [call["tmdb_type"] for call in client.get_video_id_calls], ["tv"]
        )

    def test_movie_still_errors_when_emos_has_no_match(self):
        # Emos 里确实没有这部片时，报错应是「查不到」，而不是「电视剧必须选到具体某一集」
        metadata = {
            "show_name": "梁祝",
            "media_type": None,
            "year": "1994",
            "tmdb_id": "123",
        }

        class _EmptyClient(_FakeClient):
            def get_video_id(self, *args, **kwargs):
                raise EmosApiError("Emos 接口返回 HTTP 404: GET /api/video/getVideoId")

        client = _EmptyClient()
        service, file_path = self._service(
            "梁祝.(1994).1080p.H.265.10-bit.Dolby.Digital.mkv", metadata, client
        )

        result = service.recognize(file_path)

        self.assertEqual(result["metadata"]["media_type"], "movie")
        self.assertIsNone(result["match"])
        self.assertIn("HTTP 404", result["error"])
        self.assertNotIn("具体某一集", result["error"])


if __name__ == "__main__":
    unittest.main()
