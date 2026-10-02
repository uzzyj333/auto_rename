# -*- coding: utf-8 -*-
"""TS / M2TS / ISO 视频支持的回归测试

此前各处默认扩展名列表里没有 ``.ts`` / ``.m2ts`` / ``.iso``，导致下载器监控、
目录监控、字幕匹配都不会处理这些文件（在线识别上传本身已支持）。
这里保证默认列表保持一致，并锁定 ISO 的 MIME 映射。
"""

import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.config_loader import DEFAULT_CONFIG
from src.video_organizer.core.downloader_monitor import (
    Aria2Monitor,
    DownloaderMonitorFactory,
    QBittorrentMonitor,
)
from src.video_organizer.core.emos_client import detect_video_mime
from src.video_organizer.core.online_upload import VIDEO_EXTENSIONS
from src.video_organizer.core.subtitle_handler import SubtitleHandler
from src.video_organizer.upload.upload_emos import find_subtitle_files

# 相对「常规视频扩展名」额外支持的类型：TS 流 + 原盘镜像
EXTRA_EXTENSIONS = (".ts", ".m2ts", ".iso")


class TestExtraVideoFormats(unittest.TestCase):
    def test_default_supported_extensions_include_extra_formats(self):
        extensions = DEFAULT_CONFIG["monitoring"]["supported_extensions"]
        for ext in EXTRA_EXTENSIONS:
            self.assertIn(ext, extensions)

    def test_online_upload_accepts_extra_formats(self):
        for ext in EXTRA_EXTENSIONS:
            self.assertIn(ext, VIDEO_EXTENSIONS)

    def test_downloader_monitor_defaults_include_extra_formats(self):
        for cls in (Aria2Monitor, QBittorrentMonitor):
            default = inspect.signature(cls.__init__).parameters[
                "supported_extensions"
            ].default
            for ext in EXTRA_EXTENSIONS:
                self.assertIn(ext, default)

    def test_downloader_factory_default_includes_extra_formats(self):
        monitor = DownloaderMonitorFactory.create_monitor(
            "qbittorrent", lambda path: None, {}
        )

        self.assertIsNotNone(monitor)
        for ext in EXTRA_EXTENSIONS:
            self.assertIn(ext, monitor.supported_extensions)

    def test_subtitle_handler_matches_extra_video_formats(self):
        handler = SubtitleHandler()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for ext in EXTRA_EXTENSIONS:
                video = base / f"Show.S01E01{ext}"
                video.write_bytes(b"v")
                subtitle = base / f"Show.S01E01{ext}.srt"
                subtitle.write_bytes(b"s")

                match = handler.find_matching_video(subtitle)

                self.assertIsNotNone(match, ext)
                self.assertEqual(match.name, video.name)

    def test_find_subtitle_files_matches_extra_video_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for ext in EXTRA_EXTENSIONS:
                video = base / f"Show.S01E01{ext}"
                video.write_bytes(b"v")
                (base / f"Show.S01E01{ext}.ass").write_bytes(b"s")

                found = [p.name for p in find_subtitle_files(video)]

                self.assertEqual(found, [f"Show.S01E01{ext}.ass"], ext)

    def test_iso_mime_type(self):
        self.assertEqual(detect_video_mime("movie.iso"), "application/x-iso9660-image")
        self.assertEqual(detect_video_mime("movie.ISO"), "application/x-iso9660-image")
        self.assertEqual(detect_video_mime("movie.m2ts"), "video/mp2t")


if __name__ == "__main__":
    unittest.main()
