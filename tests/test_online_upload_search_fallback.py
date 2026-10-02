# -*- coding: utf-8 -*-
"""在线识别上传「按类型搜索无结果时去掉类型重试」的回归测试

Emos 的 ``/api/video/tree`` 带上 ``type=tv`` 时可能什么都搜不到，而 Web 搜索
（不带 type）却能搜到。以前在线识别与 Telegram 回复修正都会带上 type，于是
明明存在的条目被判成「未在 Emos 中找到匹配条目」。现在带类型搜不到时去掉
类型再搜一次兜底。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.online_upload import OnlineUploadService


class _FakeClient:
    """按 video_type 是否为空返回不同结果的假 Emos 客户端"""

    def __init__(self, tree_without_type, tree_with_type=None):
        self.tree_without_type = tree_without_type
        self.tree_with_type = tree_with_type
        self.calls = []

    def get_video_tree(self, video_type=None, title=None, todb_id=None):
        self.calls.append(video_type)
        if video_type:
            return list(self.tree_with_type or [])
        return list(self.tree_without_type)


def _video(item_id="202974", title="死有对证"):
    return {
        "title": title,
        "video_type": "tv",
        "item_type": "vl",
        "item_id": item_id,
        "tmdb_id": "33912",
        "date_air": "2026-01-01",
        "seasons": [],
    }


class TestSearchTargetsTypeFallback(unittest.TestCase):
    def setUp(self):
        self.service = OnlineUploadService()
        self.service.configure({"emos": {"auth_token": "t", "base_url": "https://emos.best"}})

    def test_type_filter_empty_falls_back_to_no_type(self):
        client = _FakeClient(tree_without_type=[_video()])
        with patch.object(OnlineUploadService, "get_client", lambda self: client):
            results = self.service.search_targets(video_type="tv", title="死有对证")

        self.assertEqual([r["item_id"] for r in results], ["202974"])
        self.assertEqual(client.calls, ["tv", None])

    def test_type_filter_hit_does_not_retry(self):
        client = _FakeClient(tree_without_type=[_video(item_id="999")], tree_with_type=[_video()])
        with patch.object(OnlineUploadService, "get_client", lambda self: client):
            results = self.service.search_targets(video_type="tv", title="死有对证")

        self.assertEqual([r["item_id"] for r in results], ["202974"])
        self.assertEqual(client.calls, ["tv"])

    def test_no_type_still_searches_once(self):
        client = _FakeClient(tree_without_type=[_video()])
        with patch.object(OnlineUploadService, "get_client", lambda self: client):
            results = self.service.search_targets(title="死有对证")

        self.assertEqual([r["item_id"] for r in results], ["202974"])
        self.assertEqual(client.calls, [None])

    def test_empty_title_and_todb_returns_empty_without_query(self):
        client = _FakeClient(tree_without_type=[_video()])
        with patch.object(OnlineUploadService, "get_client", lambda self: client):
            self.assertEqual(self.service.search_targets(title=None), [])
        self.assertEqual(client.calls, [])


def _tv(item_id, title, episode_id, season=1, episode=19):
    return {
        "title": title,
        "video_type": "tv",
        "item_type": "vl",
        "item_id": item_id,
        "seasons": [
            {
                "season_number": season,
                "season_title": "第 1 季",
                "item_type": "vs",
                "item_id": "vs" + str(item_id),
                "episodes": [
                    {
                        "episode_number": episode,
                        "episode_title": "第 %d 集" % episode,
                        "item_type": "ve",
                        "item_id": episode_id,
                    }
                ],
            }
        ],
    }


class TestPickTargetTitlePreference(unittest.TestCase):
    """去掉类型兜底搜索可能一次返回多个作品，优先在标题相关的候选里选目标"""

    def setUp(self):
        self.service = OnlineUploadService()
        self.service.configure({"emos": {"auth_token": "t", "base_url": "https://emos.best"}})

    def test_prefers_title_matching_candidate(self):
        wrong = _tv("111", "另一部剧", "ve-wrong")
        right = _tv("202974", "死有对证", "ve-right")
        match = self.service.pick_target([wrong, right], 1, 19, "tv", title="死有对证")
        self.assertEqual(match["item_id"], "ve-right")

    def test_falls_back_when_no_title_matches(self):
        first = _tv("111", "另一部剧", "ve-first")
        second = _tv("202974", "死有对证", "ve-second")
        match = self.service.pick_target([first, second], 1, 19, "tv", title="完全不相干的标题")
        self.assertEqual(match["item_id"], "ve-first")

    def test_unmatched_english_query_keeps_original_order(self):
        first = _tv("111", "死有对证", "ve-first")
        second = _tv("202974", "Another Show", "ve-second")
        match = self.service.pick_target([first, second], 1, 19, "tv", title="Jade Cause Of Death")
        self.assertEqual(match["item_id"], "ve-first")


if __name__ == "__main__":
    unittest.main()
