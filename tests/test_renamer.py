import os
import shutil
import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

# 添加项目根目录到Python路径
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.renamer import VideoRenamer


class TestVideoRenamer(unittest.TestCase):

    def setUp(self):
        """
        设置测试环境
        """
        # 创建临时目录
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir)

        # 输出目录
        self.output_dir = os.path.join(self.temp_dir, "output")
        os.makedirs(self.output_dir)

        # 测试API密钥
        self.tmdb_api_key = "test_api_key_12345"

        # 自定义命名规则
        self.naming_rules = {
            "movie": "Movies/{movie_name} ({year})/{movie_name} ({year})",
            "tv": "TV/{show_name}/Season {season:02d}/{show_name} - S{season:02d}E{episode:02d}",
        }

        # 模拟TMDB客户端
        self.mock_tmdb_client = Mock()

        # 创建重命名器实例
        self.renamer = VideoRenamer(
            tmdb_api_key=self.tmdb_api_key,
            ai_service_url=None,
            watch_path=Path(self.temp_dir),
            naming_rules=self.naming_rules,
        )

        # 替换tmdb_client属性以进行测试
        # 直接替换私有属性可能需要通过反射或在初始化时注入
        # 这里假设VideoRenamer类有tmdb_client属性
        self.renamer.tmdb_client = self.mock_tmdb_client

    def test_constructor(self):
        """
        测试构造函数
        """
        self.assertEqual(self.renamer.tmdb_client, self.mock_tmdb_client)
        self.assertEqual(self.renamer.naming_rules, self.naming_rules)
        self.assertEqual(self.renamer.watch_path, Path(self.temp_dir))
        self.assertIsNone(self.renamer.ai_service_url)

    def test_extract_metadata_basic(self):
        """
        测试基本的元数据提取
        """
        # 创建临时测试文件
        test_file = os.path.join(self.temp_dir, "test_file.mp4")
        with open(test_file, "w") as f:
            f.write("dummy content")

        # 提取元数据
        metadata = self.renamer.extract_metadata(Path(test_file))

        # 验证基本元数据
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.get("extension"), ".mp4")

    def test_single_letter_roman_in_title_is_not_a_season(self):
        """
        片名里的单字母 X / I 不能当成季号（Doctor X → S10E04 回归）

        「Doctor.X.Mafia.in.White.S01E04」曾把片名里的 X 当成罗马数字第 10 季，
        变成 S10E04 去 Emos 找上传目标，导致上传到错误条目 / 一直报错。
        """
        cases = {
            "Doctor.X.Mafia.in.White.S01E04.1080p.NF.WEB-DL.DUAL.AAC2.0.H.264-RAWR.mkv": (
                "01",
                "04",
            ),
            "Spy.x.Family.S01E04.1080p.mkv": ("01", "04"),
            "Hunter.x.Hunter.S01E04.1080p.mkv": ("01", "04"),
        }
        for name, (season, episode) in cases.items():
            metadata = self.renamer._extract_with_regex(name)
            self.assertEqual(metadata.get("season"), season, name)
            self.assertEqual(metadata.get("episode"), episode, name)

    def test_single_letter_roman_followed_by_episode_is_a_season(self):
        """紧跟集号的单字母罗马数字（X_01）仍然当季号"""
        metadata = self.renamer._extract_with_regex("Show.Name.X_01.1080p.mkv")
        self.assertEqual(metadata.get("season"), "10")
        self.assertEqual(metadata.get("episode"), "01")

    def test_title_with_x_without_episode_is_not_tv_season(self):
        """「Doctor.X.Mafia.in.White.1080p」不能变成「第 10 季剧集」"""
        metadata = self.renamer._extract_with_regex(
            "Doctor.X.Mafia.in.White.1080p.BluRay.x264.mkv"
        )
        self.assertIsNone(metadata.get("season"))

    def test_h265_codec_is_not_an_episode_number(self):
        """
        「H.265」不能被当成第 265 集（梁祝.(1994).1080p.H.265... 回归）

        清理后的名字是「梁祝 H 265 10-bit ...」，会被「剧名 + 空格 + 数字」的
        集号模式匹配成 S1E265，文件于是被当成电视剧去查 Emos，报
        「电视剧必须选到具体某一集（ve）」。
        """
        metadata = self.renamer._extract_with_regex(
            "梁祝.(1994).1080p.H.265.10-bit.Dolby Digital.mkv"
        )
        self.assertIsNone(metadata.get("season"))
        self.assertIsNone(metadata.get("episode"))
        self.assertEqual(metadata.get("show_name"), "梁祝")
        self.assertEqual(metadata.get("year"), "1994")

    def test_movie_uses_filename_not_parent_dir_as_title(self):
        """「片名.(年份).技术信息」的电影用文件名当片名，而不是退回父目录名"""
        movie_dir = Path(self.temp_dir) / "media"
        movie_dir.mkdir(exist_ok=True)
        file_name = "梁祝.(1994).1080p.H.265.10-bit.Dolby Digital.mkv"
        test_file = movie_dir / file_name
        test_file.write_text("dummy content")

        metadata = self.renamer.extract_metadata(test_file)

        self.assertEqual(metadata.get("show_name"), "梁祝")
        self.assertIsNone(metadata.get("season"))
        self.assertIsNone(metadata.get("episode"))

    def test_extract_tv_show_pattern(self):
        """
        测试从文件名提取电视剧信息的模式匹配
        """
        # 创建临时测试文件（使用匹配正则表达式的格式）
        test_file = os.path.join(self.temp_dir, "Friends.S01E01.mp4")
        with open(test_file, "w") as f:
            f.write("dummy content")

        # 提取元数据
        metadata = self.renamer.extract_metadata(Path(test_file))

        # 验证基本电视剧信息提取
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.get("show_name"), "Friends")
        self.assertEqual(int(metadata.get("season")), 1)
        self.assertEqual(int(metadata.get("episode")), 1)

    def test_extract_movie_pattern(self):
        """
        测试从文件名提取电影信息的基本功能
        """
        # 创建临时测试文件
        test_file = os.path.join(self.temp_dir, "inception_2010.mp4")
        with open(test_file, "w") as f:
            f.write("dummy content")

        # 提取元数据
        metadata = self.renamer.extract_metadata(Path(test_file), "movie")

        # 验证基本电影信息提取
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.get("extension"), ".mp4")
        self.assertEqual(metadata.get("original_filename"), "inception_2010.mp4")

    def test_sanitize_filename(self):
        """
        测试清理文件名方法
        """
        # 测试清理特殊字符
        dirty_filename = 'file?*<>:"|name.mp4'
        clean_filename = self.renamer._sanitize_filename(dirty_filename)
        # 实际实现可能只替换部分特殊字符
        self.assertNotIn("?", clean_filename)
        self.assertNotIn("*", clean_filename)
        self.assertNotIn("<", clean_filename)
        self.assertNotIn(">", clean_filename)
        self.assertNotIn("|", clean_filename)

        # 测试清理空字符
        empty_filename = ""
        clean_filename = self.renamer._sanitize_filename(empty_filename)
        self.assertEqual(clean_filename, "")

        # 测试保留合法字符
        valid_filename = "Valid_Filename.123.mp4"
        clean_filename = self.renamer._sanitize_filename(valid_filename)
        self.assertEqual(clean_filename, valid_filename)

    def test_set_naming_rules(self):
        """
        测试设置命名规则方法
        """
        # 检查命名规则字典存在
        self.assertTrue(hasattr(self.renamer, "naming_rules"))
        self.assertIsInstance(self.renamer.naming_rules, dict)

        # 设置自定义命名规则
        custom_rules = {"movie": "Custom/Movies/{movie_name}"}
        self.renamer.set_naming_rules(custom_rules)

        # 验证自定义命名规则被设置
        self.assertEqual(
            self.renamer.naming_rules.get("movie"), "Custom/Movies/{movie_name}"
        )


class TestTmdbNameCacheYearIsolation(unittest.TestCase):
    """同名不同版本（如 1996 / 2001 版《笑傲江湖》）不应命中同一个 TMDB 缓存"""

    def setUp(self):
        self.renamer = VideoRenamer(tmdb_api_key="test_api_key_12345")

    def _remember(self, year, tmdb_id):
        self.renamer._save_to_tmdb_cache(
            {
                "show_name": "笑傲江湖",
                "title": "笑傲江湖",
                "year": year,
                "tmdb_id": tmdb_id,
                "media_type": "tv",
            }
        )

    def test_same_name_different_year_is_not_reused(self):
        """先识别 1996 版，再识别 2001 版时不能复用 1996 版的 TMDB ID"""
        self._remember("1996", 11111)

        # 同年份命中
        self.assertEqual(
            self.renamer._lookup_tmdb_id_by_name("笑傲江湖", "1996"), 11111
        )
        # 年份不同不能复用，否则 2001 版会被识别成 1996 版
        self.assertIsNone(self.renamer._lookup_tmdb_id_by_name("笑傲江湖", "2001"))
        # 文件名没有年份时也不能借用带年份的缓存
        self.assertIsNone(self.renamer._lookup_tmdb_id_by_name("笑傲江湖", ""))

    def test_yearless_cache_still_reused(self):
        """文件名不带年份时仍可复用（保持原来的缓存加速效果）"""
        self._remember("", 22222)
        self.assertEqual(self.renamer._lookup_tmdb_id_by_name("笑傲江湖", ""), 22222)

    def test_different_years_cached_side_by_side(self):
        """不同年份各自缓存，互不覆盖"""
        self._remember("1996", 11111)
        self._remember("2001", 33333)
        self.assertEqual(
            self.renamer._lookup_tmdb_id_by_name("笑傲江湖", "1996"), 11111
        )
        self.assertEqual(
            self.renamer._lookup_tmdb_id_by_name("笑傲江湖", "2001"), 33333
        )


if __name__ == "__main__":
    unittest.main()
