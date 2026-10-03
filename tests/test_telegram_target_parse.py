# -*- coding: utf-8 -*-
"""Telegram 回复文本解析的回归测试

用户常直接粘贴文件名（如 ``大王饶命.S03E03.mkv``）回复报错消息，标题里的
ASCII 点 / 视频后缀不能残留，否则会拿「大王饶命.」去搜 Emos 导致误报未找到。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.telegram_bot import parse_target_expression


class TestParseTargetExpression(unittest.TestCase):
    def test_dot_separator_is_stripped_from_title(self):
        expr = parse_target_expression("大王饶命.S03E03")
        self.assertEqual(expr.title, "大王饶命")
        self.assertEqual(expr.season, 3)
        self.assertEqual(expr.episode, 3)

    def test_media_extension_is_stripped(self):
        expr = parse_target_expression("大王饶命.S03E03.mkv")
        self.assertEqual(expr.title, "大王饶命")
        self.assertEqual(expr.season, 3)
        self.assertEqual(expr.episode, 3)

    def test_space_and_dash_separators(self):
        for raw in ("大王饶命 S03E03", "大王饶命-S03E03", "大王饶命_S03E03"):
            expr = parse_target_expression(raw)
            self.assertEqual(expr.title, "大王饶命", raw)
            self.assertEqual(expr.season, 3, raw)
            self.assertEqual(expr.episode, 3, raw)

    def test_chinese_season_episode(self):
        expr = parse_target_expression("时光代理人 第4季第9集")
        self.assertEqual(expr.title, "时光代理人")
        self.assertEqual(expr.season, 4)
        self.assertEqual(expr.episode, 9)

    def test_movie_year(self):
        expr = parse_target_expression("大王饶命 (2024)")
        self.assertEqual(expr.title, "大王饶命")
        self.assertEqual(expr.year, 2024)

    def test_internal_period_title_is_kept(self):
        expr = parse_target_expression("Mr. Robot S01E01")
        self.assertEqual(expr.title, "Mr. Robot")
        self.assertEqual(expr.season, 1)
        self.assertEqual(expr.episode, 1)


if __name__ == "__main__":
    unittest.main()
