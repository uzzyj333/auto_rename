# -*- coding: utf-8 -*-
"""GuessIt「中文名.英文名.SxxExx」解析的回归测试

文件名形如 狂王.Asura.S02E02... 时 GuessIt 只保留英文名 Asura，
把中文名整个丢掉，导致后续拿 Asura 去搜 TMDB / Emos 全部匹配失败。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.guessit_parser import GUESSIT_AVAILABLE, GuessItParser


@unittest.skipUnless(GUESSIT_AVAILABLE, "未安装 guessit")
class TestChineseTitleWithEnglishName(unittest.TestCase):
    def setUp(self):
        self.parser = GuessItParser(enabled=True)

    def test_chinese_name_is_kept_as_show_name(self):
        result = self.parser.parse(
            "狂王.Asura.S02E02.2026.2160p.TX.WEB-DL.H265.DDP2.0-ADWeb.mkv"
        )
        self.assertEqual(result.get("show_name"), "狂王")
        self.assertEqual(result.get("en_title"), "Asura")
        self.assertEqual(result.get("season"), 2)
        self.assertEqual(result.get("episode"), 2)

    def test_chinese_name_with_year_token(self):
        result = self.parser.parse("狂王.2024.S01E01.mkv")
        self.assertEqual(result.get("show_name"), "狂王")
        self.assertEqual(result.get("season"), 1)
        self.assertEqual(result.get("episode"), 1)
        self.assertEqual(result.get("year"), 2024)

    def test_english_only_name_is_unchanged(self):
        result = self.parser.parse("The.Boys.S01E01.1080p.WEB-DL.mkv")
        self.assertEqual(result.get("show_name"), "The Boys")
        self.assertIsNone(result.get("en_title"))

    def test_category_tag_is_stripped_from_prefix(self):
        result = self.parser.parse("国漫.狂王.Asura.S02E02.mkv")
        self.assertEqual(result.get("show_name"), "狂王")

    def test_subtitle_group_prefix_is_skipped(self):
        result = self.parser.parse("【字幕组】狂王.Asura.S02E02.mkv")
        self.assertEqual(result.get("show_name"), "狂王")

    def test_numeric_chinese_title_is_kept(self):
        result = self.parser.parse("唐探1900.Detective.Chinatown.S01E01.mkv")
        self.assertEqual(result.get("show_name"), "唐探1900")

    def test_multi_segment_chinese_prefix_is_joined(self):
        result = self.parser.parse("我的狂王.Asura.S02E02.mkv")
        self.assertEqual(result.get("show_name"), "我的狂王")


if __name__ == "__main__":
    unittest.main()
