# -*- coding: utf-8 -*-
"""路径映射（path_mappings）回归测试

Web 端「监控配置 / 下载器配置」里的路径映射是文本框，保存时写的是字符串，
而且 ``StateManager.get_config()`` 是浅拷贝，字符串会直接写进监控器共享的
配置字典。结果 aria2 / qBittorrent 每次上报下载完成都会在
``path_mappings.items()`` 处抛 ``'str' object has no attribute 'items'``，
表现就是「下载完了却一直不识别 / 不自动上传」。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.config_loader import (
    format_path_mappings,
    load_config,
    normalize_path_mappings,
    update_config,
)
from src.video_organizer.core.filesystem_monitor import FileSystemMonitor
from src.video_organizer.core.video_file_handler import VideoFileHandler


class TestNormalizePathMappings(unittest.TestCase):
    def test_dict_passthrough(self):
        self.assertEqual(
            normalize_path_mappings({"/media": "/downloads"}),
            {"/media": "/downloads"},
        )

    def test_legacy_ini_string(self):
        self.assertEqual(
            normalize_path_mappings("/downloads:F:/Downloads,/data:/mnt/data"),
            {"/downloads": "F:/Downloads", "/data": "/mnt/data"},
        )

    def test_json_string(self):
        self.assertEqual(
            normalize_path_mappings('{"/media": "/downloads"}'),
            {"/media": "/downloads"},
        )

    def test_windows_drive_letter_key(self):
        self.assertEqual(
            normalize_path_mappings(
                r"F:\XunLeiDownLoad\media:F:\XunLeiDownLoad\media"
            ),
            {r"F:\XunLeiDownLoad\media": r"F:\XunLeiDownLoad\media"},
        )

    def test_list_and_empty(self):
        self.assertEqual(
            normalize_path_mappings(["/a:/b", "/c:/d"]), {"/a": "/b", "/c": "/d"}
        )
        self.assertEqual(normalize_path_mappings(None), {})
        self.assertEqual(normalize_path_mappings(""), {})
        self.assertEqual(normalize_path_mappings("not-a-mapping"), {})

    def test_format_round_trip(self):
        text = format_path_mappings({"/media": "/downloads"})
        self.assertEqual(text, "/media:/downloads")
        self.assertEqual(normalize_path_mappings(text), {"/media": "/downloads"})


class TestStringPathMappingsDoesNotCrash(unittest.TestCase):
    """回归：配置里是字符串时，路径映射不能抛异常"""

    def test_monitor_apply_path_mapping_with_string_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = FileSystemMonitor(
                watch_path=str(Path(tmp) / "watch"),
                processed_path=str(Path(tmp) / "out"),
                tmdb_api_key="",
                supported_extensions=[".mkv"],
                downloader_configs=[],
                config={"monitoring": {"path_mappings": "/media:/downloads"}},
            )
            try:
                mapped = monitor._apply_path_mapping("/media/Show.S01E01.mkv")
            finally:
                monitor.stop()
        self.assertEqual(
            os.path.normpath(mapped), os.path.normpath("/downloads/Show.S01E01.mkv")
        )

    def test_handler_reverse_apply_with_string_path_mappings(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = VideoFileHandler(
                output_dir=str(Path(tmp) / "out"),
                supported_extensions=[".mkv"],
                tmdb_config={"api_key": ""},
                path_mappings="/downloads:/media",
            )
            try:
                reversed_path = handler._reverse_apply_path_mapping(
                    "/media/Show.S01E01.mkv"
                )
            finally:
                handler.stop_upload_queue()
        self.assertTrue(reversed_path.endswith("Show.S01E01.mkv"))
        self.assertTrue(reversed_path.startswith("/downloads"))


class TestPathMappingsConfigRoundTrip(unittest.TestCase):
    """update_config 写盘后 load_config 必须还能读回字典"""

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "config.ini")
            config = {"monitoring": {"path_mappings": {"/media": "/downloads"}}}
            update_config(config, path)
            loaded = load_config(path)
            self.assertEqual(
                loaded["monitoring"]["path_mappings"], {"/media": "/downloads"}
            )


if __name__ == "__main__":
    unittest.main()
