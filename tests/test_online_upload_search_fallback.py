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
from src.video_organizer.core.telegram_bot import (
    TelegramBotService,
    parse_target_expression,
)


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


class _FlatThenFullTreeClient:
    """搜索结果只有作品级信息，按 video_id 查才带完整季/集"""

    def __init__(self, flat_results, full_tree):
        self.flat_results = flat_results
        self.full_tree = full_tree

    def get_video_tree(self, video_type=None, title=None, todb_id=None, tmdb_id=None, video_id=None):
        if video_id is not None:
            return list(self.full_tree)
        return list(self.flat_results)


class TestResolveEpisodeFromCandidates(unittest.TestCase):
    """搜索候选没有嵌套季/集时，按候选 id 拉完整目录树定位具体某一集"""

    def setUp(self):
        self.service = OnlineUploadService()
        self.service.configure({"emos": {"auth_token": "t", "base_url": "https://emos.best"}})
        self.flat = [
            {
                "title": "死有对证",
                "video_type": "tv",
                "item_type": "vl",
                "item_id": "202974",
                "seasons": [],
            }
        ]
        self.full_tree = [
            {
                "item_id": "202974",
                "title": "死有对证",
                "video_type": "tv",
                "item_type": "vl",
                "seasons": [
                    {
                        "season_number": 1,
                        "season_title": "第 1 季",
                        "item_type": "vs",
                        "item_id": "vs-202974-1",
                        "episodes": [
                            {
                                "episode_number": 19,
                                "episode_title": "第 19 集",
                                "item_type": "ve",
                                "item_id": "ve-202974-1-19",
                            }
                        ],
                    }
                ],
            }
        ]

    def test_pick_target_fails_without_nested_episodes(self):
        # 候选只有作品级信息时，pick_target 定位不到具体某一集
        self.assertIsNone(self.service.pick_target(self.flat, 1, 19, "tv", title="死有对证"))

    def test_resolve_falls_back_to_full_tree(self):
        client = _FlatThenFullTreeClient(self.flat, self.full_tree)
        with patch.object(OnlineUploadService, "get_client", lambda self: client):
            match = self.service.resolve_episode_from_candidates(
                self.flat, 1, 19, "tv", title="死有对证"
            )
        self.assertIsNotNone(match)
        self.assertEqual(match["item_id"], "ve-202974-1-19")
        self.assertEqual(match["kind"], "episode")
        self.assertEqual(match["episode_number"], 19)

    def test_resolve_returns_none_without_episode(self):
        client = _FlatThenFullTreeClient(self.flat, self.full_tree)
        with patch.object(OnlineUploadService, "get_client", lambda self: client):
            self.assertIsNone(
                self.service.resolve_episode_from_candidates(self.flat, 1, None, "tv", title="死有对证")
            )


class _LocateServiceStub:
    def __init__(self):
        self.pick_calls = 0
        self.resolve_calls = 0

    def pick_target(self, *args, **kwargs):
        self.pick_calls += 1
        return None

    def resolve_episode_from_candidates(self, candidates, season, episode, media_type="", title=None):
        self.resolve_calls += 1
        return {
            "item_type": "ve",
            "item_id": "ve-1",
            "label": "S1E19",
            "kind": "episode",
            "season_number": 1,
            "episode_number": 19,
        }


class TestTelegramLocateTarget(unittest.TestCase):
    """TG 回复修正：pick_target 失败后用完整目录树兜底定位剧集"""

    def test_falls_back_to_full_tree(self):
        service = _LocateServiceStub()
        expr = parse_target_expression("死有对证S01E19")
        match = TelegramBotService._locate_target(service, [{"item_id": "202974"}], expr, "tv")
        self.assertEqual(match["item_id"], "ve-1")
        self.assertEqual(service.pick_calls, 1)
        self.assertEqual(service.resolve_calls, 1)

    def test_no_episode_does_not_resolve(self):
        service = _LocateServiceStub()
        expr = parse_target_expression("死有对证 (2024)")
        self.assertIsNone(TelegramBotService._locate_target(service, [{"item_id": "1"}], expr, "movie"))
        self.assertEqual(service.resolve_calls, 0)


class _CorrectionServiceStub:
    """模拟：带类型搜索只给作品级候选，去掉类型才给能定位到剧集的候选"""

    def __init__(self):
        self.search_calls = []
        self.created = []

    def search_targets(self, video_type=None, title=None):
        self.search_calls.append(video_type)
        if video_type:
            return [{"item_id": "202974", "title": "死有对证", "seasons": []}]
        return [{"item_id": "202974", "title": "死有对证", "seasons": [], "full": True}]

    def pick_target(self, candidates, season, episode, media_type, year=None, title=None):
        return None

    def resolve_episode_from_candidates(self, candidates, season, episode, media_type="", title=None):
        for candidate in candidates or []:
            if candidate.get("full"):
                return {
                    "item_type": "ve",
                    "item_id": "ve-19",
                    "label": "S1E19",
                    "kind": "episode",
                    "season_number": 1,
                    "episode_number": 19,
                }
        return None

    def create_task(self, item):
        self.created.append(item)
        return {"id": "task-1"}

    def delete_after_upload_enabled(self):
        return False


class TestTelegramCorrectionRetryWithoutType(unittest.TestCase):
    def test_retries_without_type_then_creates_task(self):
        service = TelegramBotService()
        service._token = "t"
        service._chat_id = "1"
        service._enabled = True
        service._reply_enabled = True
        sent = []
        service.send_text = lambda text, reply_to=None, chat_id=None: sent.append(text) or 42
        stub = _CorrectionServiceStub()
        context = {"file_path": "E:/downloads/a.mkv", "title": "死有对证", "media_type": "tv"}
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService.instance",
            return_value=stub,
        ):
            service._handle_correction(context, "死有对证S01E19", "1", 99)

        self.assertEqual(stub.search_calls, ["tv", None])
        self.assertEqual(len(stub.created), 1)
        self.assertEqual(stub.created[0]["item_id"], "ve-19")
        self.assertEqual(stub.created[0]["media_type"], "tv")
        self.assertTrue(any("已按修正目标重新上传" in text for text in sent))


class _EndpointOnlyClient:
    """目录树里没有嵌套的集，只有季 / 集接口能查到（模拟 Emos 手动新增的集）"""

    def __init__(self, seasons, episodes):
        self._seasons = seasons
        self._episodes = episodes
        self.episode_calls = []

    def get_video_tree(
        self,
        video_type=None,
        title=None,
        todb_id=None,
        tmdb_id=None,
        video_id=None,
    ):
        return [
            {
                "item_id": str(video_id),
                "title": "狂王",
                "video_type": "tv",
                "item_type": "vl",
                "seasons": [],
            }
        ]

    def get_seasons(self, video_id):
        return list(self._seasons)

    def get_episodes(self, video_id, season_number=None):
        self.episode_calls.append(season_number)
        return list(self._episodes.get(season_number, []))


class TestEpisodeEndpointFallback(unittest.TestCase):
    """目录树里没有手动新增的集时，用 Emos 季 / 集接口兜底定位"""

    def test_resolve_uses_episode_endpoint(self):
        client = _EndpointOnlyClient(
            seasons=[{"season_number": 1, "item_id": "vs1"}],
            episodes={
                1: [
                    {
                        "episode_number": 5,
                        "episode_title": "第五集",
                        "item_type": "ve",
                        "item_id": "ve15",
                    }
                ]
            },
        )
        match = OnlineUploadService.resolve_episode_from_tree(
            client, "vl1", 1, 5
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["item_id"], "ve15")
        self.assertEqual(match["episode_number"], 5)

    def test_resolve_returns_none_when_endpoint_has_no_episode(self):
        client = _EndpointOnlyClient(
            seasons=[{"season_number": 1, "item_id": "vs1"}],
            episodes={1: []},
        )
        self.assertIsNone(
            OnlineUploadService.resolve_episode_from_tree(client, "vl1", 1, 5)
        )

    def test_resolve_prefers_requested_season_then_avoids_ambiguity(self):
        client = _EndpointOnlyClient(
            seasons=[{"season_number": 1}, {"season_number": 2}],
            episodes={
                1: [{"episode_number": 5, "item_id": "ve-a"}],
                2: [{"episode_number": 5, "item_id": "ve-b"}],
            },
        )
        match = OnlineUploadService.resolve_episode_from_tree(
            client, "vl1", 2, 5
        )
        self.assertEqual(match["item_id"], "ve-b")
        # 没给季号且多季同号时不猜，避免传错集
        self.assertIsNone(
            OnlineUploadService.resolve_episode_from_tree(
                client, "vl1", None, 5
            )
        )


class _EndpointTreeClient:
    """目录树里没有嵌套季 / 集，只有季 / 集接口能查到（模拟 Emos 手动新增的集）"""

    def __init__(self):
        self.season_calls = []
        self.episode_calls = []

    def get_video_tree(self, video_type=None, title=None, todb_id=None):
        return [
            {
                "title": "狂王",
                "video_type": "tv",
                "item_type": "vl",
                "item_id": "1001",
                "seasons": [],
            }
        ]

    def get_seasons(self, video_id):
        self.season_calls.append(video_id)
        return [
            {
                "season_number": 1,
                "season_title": "第 1 季",
                "item_type": "vs",
                "item_id": "2001",
            }
        ]

    def get_episodes(self, video_id, season_number=None):
        self.episode_calls.append((video_id, season_number))
        return [
            {
                "episode_number": 5,
                "episode_title": "第五集",
                "item_type": "ve",
                "item_id": "ve15",
            }
        ]


class TestSearchTargetsSeasonFallback(unittest.TestCase):
    """搜索结果也用季 / 集接口补齐，网页端才能选到具体某一集（ve）"""

    def setUp(self):
        self.service = OnlineUploadService()
        self.service.configure({"emos": {"auth_token": "t", "base_url": "https://emos.best"}})

    def test_fills_seasons_from_endpoints(self):
        client = _EndpointTreeClient()
        with patch.object(OnlineUploadService, "get_client", lambda self: client):
            results = self.service.search_targets(title="狂王")

        self.assertEqual(len(results), 1)
        seasons = results[0]["seasons"]
        self.assertEqual([season["season_number"] for season in seasons], [1])
        self.assertEqual(seasons[0]["episodes"][0]["item_id"], "ve15")
        self.assertEqual(client.season_calls, ["1001"])
        self.assertEqual(client.episode_calls, [("1001", 1)])

    def test_keeps_tree_seasons_without_extra_calls(self):
        client = _FakeClient(tree_without_type=[_tv("202974", "死有对证", "ve-19")])
        with patch.object(OnlineUploadService, "get_client", lambda self: client):
            results = self.service.search_targets(title="死有对证")

        self.assertEqual(results[0]["seasons"][0]["episodes"][0]["item_id"], "ve-19")

    def test_skips_movie_items(self):
        calls = []

        class _MovieClient:
            def get_video_tree(self, video_type=None, title=None, todb_id=None):
                return [
                    {
                        "title": "测试电影",
                        "video_type": "movie",
                        "item_type": "vl",
                        "item_id": "9001",
                        "seasons": [],
                    }
                ]

            def get_seasons(self, video_id):
                calls.append(video_id)
                return []

        with patch.object(
            OnlineUploadService, "get_client", lambda self: _MovieClient()
        ):
            results = self.service.search_targets(title="测试电影")

        self.assertEqual(results[0]["seasons"], [])
        self.assertEqual(calls, [])

    def test_endpoint_errors_do_not_break_search(self):
        class _BrokenClient:
            def get_video_tree(self, video_type=None, title=None, todb_id=None):
                return [
                    {
                        "title": "狂王",
                        "video_type": "tv",
                        "item_type": "vl",
                        "item_id": "1001",
                        "seasons": [],
                    }
                ]

            def get_seasons(self, video_id):
                raise RuntimeError("Emos 接口返回 HTTP 500")

        with patch.object(
            OnlineUploadService, "get_client", lambda self: _BrokenClient()
        ):
            results = self.service.search_targets(title="狂王")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["seasons"], [])


if __name__ == "__main__":
    unittest.main()
