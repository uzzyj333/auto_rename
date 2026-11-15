# -*- coding: utf-8 -*-
"""片名匹配与搜索变体回归测试。

用户反馈的误上传都以同一个根因出现：搜索词退化成 ``Yi`` / ``Your`` /
``Star`` 后，旧的子串匹配把无关剧集当成同一部作品。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.guessit_parser import extract_leading_bracket_titles
from src.video_organizer.core.online_upload import OnlineUploadService
from src.video_organizer.core.telegram_bot import TelegramBotService
from src.video_organizer.core.title_matching import (
    PYPINYIN_AVAILABLE,
    title_match_score,
    title_matches,
)


class TestTitleMatching(unittest.TestCase):
    def test_short_english_fragments_do_not_match_unrelated_titles(self):
        self.assertFalse(title_matches("The Flying Nun", "Yi"))
        self.assertFalse(title_matches("JBYB (Just Bring Your Own Body)", "Your"))
        self.assertFalse(title_matches("Tonight Starring Jack Paar", "Star"))

    def test_exact_titles_still_match(self):
        self.assertTrue(title_matches("Star", "Star"))
        self.assertTrue(title_matches("Your Highness", "Your Highness"))

    def test_shared_single_word_does_not_match_longer_title(self):
        self.assertFalse(title_matches("Silent Mentor", "Silent"))
        self.assertTrue(title_matches("成吉思-VIVANT THE ORIGIN STORY-", "VIVANT"))

    def test_candidate_with_extra_subtitle_is_not_auto_matched(self):
        self.assertFalse(
            title_matches("Galactic Saga: The Next Generation", "Galactic Saga")
        )
        self.assertTrue(title_matches("Galactic Saga", "Galactic Saga"))

    def test_shorter_candidate_is_not_auto_matched_to_longer_file_title(self):
        """反方向也要保守：Star Wars 不能自动命中 Star Wars: The Clone Wars"""
        self.assertFalse(
            title_matches("Star Wars", "Star Wars The Clone Wars")
        )
        self.assertFalse(title_matches("The Office", "The Office US"))

    def test_uppercase_acronym_does_not_match_spinoff_prefix(self):
        """NCIS / FBI 只有自身完整标题或真正缩写展开时才算命中"""
        self.assertFalse(title_matches("NCIS: Sydney", "NCIS"))
        self.assertFalse(title_matches("FBI: Most Wanted", "FBI"))

    def test_single_pronoun_titles_still_match_after_removing_episode_tags(self):
        """You / It / Her 是完整片名，不能因为属于代词就被当成空标题"""
        self.assertTrue(title_matches("You", "You S01E01 1080p"))
        self.assertTrue(title_matches("It", "It 2017 1080p"))

    def test_chinese_short_title_can_match_title_with_english_subtitle(self):
        self.assertTrue(title_matches("狂王", "狂王 Asura"))

    @unittest.skipUnless(PYPINYIN_AVAILABLE, "需要 pypinyin")
    def test_romanized_chinese_matches_hanzi_title(self):
        self.assertGreater(title_match_score("一路前行", "Yi Lu Qian Xing"), 0)

    def test_english_title_matches_with_punctuation(self):
        self.assertGreater(
            title_match_score("Star Detective Precure!", "Star Detective Precure"),
            0,
        )

    def test_uppercase_acronym_expands_to_full_title(self):
        """Law and Order SVU / FBI 这类缩写要能匹配完整英文名"""
        self.assertGreater(
            title_match_score("Law & Order: Special Victims Unit", "Law and Order SVU"),
            0,
        )
        self.assertGreater(
            title_match_score("Federal Bureau of Investigation", "FBI"), 0
        )
        self.assertFalse(
            title_matches("Law & Order: Organized Crime", "Law and Order SVU")
        )


class TestSearchTitleVariants(unittest.TestCase):
    def test_filename_first_segment_is_not_used_for_english_titles(self):
        for title in (
            "Your.Highness.2022.S01E01.1080p.WEB-DL.mkv",
            "Yi.Lu.Qian.Xing.2023.S01E11.2160p.WEB-DL.mkv",
            "Star.Detective.Precure.S01E41.2026.1080p.WEB-DL.mkv",
        ):
            variants = TelegramBotService._title_variants(title)
            self.assertNotIn("Your", variants)
            self.assertNotIn("Yi", variants)
            self.assertNotIn("Star", variants)

    def test_chinese_first_segment_is_still_kept(self):
        variants = TelegramBotService._title_variants("狂王.Asura.S02E04")
        self.assertIn("狂王", variants)

    def test_uppercase_acronym_is_kept_as_title(self):
        """NCIS / FBI 这类短英文缩写不能被当成无效搜索词过滤掉"""
        self.assertIn(
            "NCIS", TelegramBotService._title_variants("NCIS.S01E01.1080p.mkv")
        )
        self.assertIn("FBI", TelegramBotService._title_variants("FBI.S01E01.1080p.mkv"))

    def test_complete_short_english_titles_are_kept(self):
        """Lost / Dark / 24 是完整片名，不能因为短就被当成无效片段"""
        self.assertIn(
            "Lost", TelegramBotService._title_variants("Lost.S01E01.1080p.mkv")
        )
        self.assertIn(
            "Dark", TelegramBotService._title_variants("Dark.S01E01.1080p.mkv")
        )
        self.assertIn("24", TelegramBotService._title_variants("24.S01E01.1080p.mkv"))

    def test_bracket_alias_is_extracted_without_season_suffix(self):
        self.assertEqual(
            extract_leading_bracket_titles(
                "[一路前行 第一季].Yi.Lu.Qian.Xing.2023.S01E11.mkv"
            ),
            ["一路前行"],
        )
        self.assertEqual(
            extract_leading_bracket_titles("[痞子殿下].Your.Highness.2022.S01E01.mkv"),
            ["痞子殿下"],
        )


class TestEmosTargetValidation(unittest.TestCase):
    def test_unrelated_emos_title_is_rejected_even_with_matching_episode(self):
        payload = {
            "title": "Tonight Starring Jack Paar",
            "video_type": "tv",
            "item_id": "vl-wrong",
            "episode_info": {
                "item_type": "ve",
                "item_id": "ve-wrong",
                "season_number": 1,
                "episode_number": 41,
            },
        }
        match = OnlineUploadService._pick_match(
            payload,
            "tv",
            season=1,
            episode=41,
            expected_titles=["Star Detective Precure"],
        )
        self.assertIsNone(match)

    def test_correct_emos_title_is_kept(self):
        payload = {
            "title": "Star Detective Precure!",
            "video_type": "tv",
            "item_id": "vl-ok",
            "episode_info": {
                "item_type": "ve",
                "item_id": "ve-ok",
                "season_number": 1,
                "episode_number": 41,
            },
        }
        match = OnlineUploadService._pick_match(
            payload,
            "tv",
            season=1,
            episode=41,
            expected_titles=["Star Detective Precure"],
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["item_id"], "ve-ok")


class _FakeRenamer:
    def __init__(self, metadata):
        self.metadata = metadata

    def extract_metadata(self, file_path):
        return dict(self.metadata)


class _WrongFallbackClient:
    """getVideoId 指向错误作品，目录树里也有同季同集，但标题不相关"""

    def __init__(self):
        self.tree_calls = 0

    def get_video_id(self, *args, **kwargs):
        return {
            "title": "Unrelated Program",
            "video_type": "tv",
            "item_id": "vl-wrong",
        }

    def get_video_base(self, *args, **kwargs):
        return {"title": "Unrelated Program"}

    def get_video_tree(self, *args, **kwargs):
        self.tree_calls += 1
        return [
            {
                "title": "Unrelated Program",
                "item_id": "vl-wrong",
                "seasons": [
                    {
                        "season_number": 1,
                        "episodes": [
                            {
                                "episode_number": 3,
                                "item_id": "ve-wrong",
                                "item_type": "ve",
                            }
                        ],
                    }
                ],
            }
        ]


class _TitlelessWrongFallbackClient:
    """getVideoId / videoBase 都不返回标题，目录树里却是无关作品"""

    def __init__(self):
        self.tree_calls = 0

    def get_video_id(self, *args, **kwargs):
        return {"video_type": "tv", "item_id": "vl-wrong"}

    def get_video_base(self, *args, **kwargs):
        return {}

    def get_video_tree(self, *args, **kwargs):
        self.tree_calls += 1
        return [
            {
                "title": "Unrelated Program",
                "item_id": "vl-wrong",
                "seasons": [
                    {
                        "season_number": 1,
                        "episodes": [
                            {
                                "episode_number": 3,
                                "item_id": "ve-wrong",
                                "item_type": "ve",
                            }
                        ],
                    }
                ],
            }
        ]


class TestRecognizeFinalTitleGuard(unittest.TestCase):
    """在线识别返回前必须再次核对最终目标，不能信任上游的 title_matched"""

    def test_unrelated_auto_target_is_rejected_before_returning(self):
        metadata = {
            "title": "Fictional Harbor",
            "show_name": "Fictional Harbor",
            "media_type": "tv",
            "season": 1,
            "episode": 3,
            "year": 2026,
        }
        candidate = {
            "title": "Fictional Harbor",
            "video_type": "tv",
            "item_type": "vl",
            "item_id": "vl-ok",
            "seasons": [],
        }
        wrong_match = {
            "item_type": "ve",
            "item_id": "ve-wrong",
            "label": "第 3 集",
            "title_matched": True,
            "video_title": "Unrelated Program",
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "[虚构港].Fictional.Harbor.2026.S01E03.1080p.mkv"
            path.write_bytes(b"0")
            service = OnlineUploadService()
            service.configure(
                {
                    "emos": {"auth_token": "t", "base_url": "https://emos.best"},
                    "online_upload": {"video_root": tmp},
                }
            )
            service.get_renamer = lambda: _FakeRenamer(metadata)
            with patch.object(
                service, "search_targets", lambda **kwargs: [candidate]
            ), patch.object(
                OnlineUploadService,
                "_pick_from_candidates",
                staticmethod(lambda *args, **kwargs: dict(wrong_match)),
            ), patch(
                "src.video_organizer.core.mapping_store.TargetMappingStore.resolve",
                lambda **kwargs: None,
            ):
                result = service.recognize(str(path))

        self.assertIsNone(result["match"])
        self.assertIn("Unrelated Program", result["error"])
        self.assertIn("不一致", result["error"])

    def test_tree_fallback_cannot_bypass_title_guard(self):
        metadata = {
            "title": "Fictional Harbor",
            "show_name": "Fictional Harbor",
            "tmdb_id": "42",
            "media_type": "tv",
            "season": 1,
            "episode": 3,
            "year": 2026,
        }
        client = _WrongFallbackClient()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Fictional.Harbor.2026.S01E03.1080p.mkv"
            path.write_bytes(b"0")
            service = OnlineUploadService()
            service.configure(
                {
                    "emos": {"auth_token": "t", "base_url": "https://emos.best"},
                    "online_upload": {"video_root": tmp},
                }
            )
            service.get_renamer = lambda: _FakeRenamer(metadata)
            service.get_client = lambda: client
            with patch.object(service, "search_targets", lambda **kwargs: []), patch(
                "src.video_organizer.core.mapping_store.TargetMappingStore.resolve",
                lambda **kwargs: None,
            ):
                result = service.recognize(str(path))

        self.assertIsNone(result["match"])
        self.assertEqual(client.tree_calls, 0)

    def test_tree_fallback_checks_tree_title_when_api_returns_no_title(self):
        """getVideoId / videoBase 都没标题时，仍不能按同季同集盲选目录树"""
        client = _TitlelessWrongFallbackClient()
        match = OnlineUploadService.resolve_episode_from_tree(
            client,
            "vl-wrong",
            1,
            3,
            expected_titles=["Fictional Harbor"],
        )
        self.assertIsNone(match)
        self.assertEqual(client.tree_calls, 1)


if __name__ == "__main__":
    unittest.main()
