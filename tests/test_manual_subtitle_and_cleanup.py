# -*- coding: utf-8 -*-
"""手动处理链路回归测试

1. 「手动处理 / --process」把 .ass/.srt 当成视频上传过（会走 Emos 的 video/save），
   字幕必须走字幕逻辑。
2. 在线手动上传成功后下载器任务残留：文件没有建立「下载器映射」时，
   ``_downloader_cleanup_state`` 以前直接返回 None，不再尝试删任务，
   结果资源已删除、aria2 / qBittorrent 里任务还在。
3. ``normalize_extensions`` 必须能处理 INI 字符串，避免把 ".mp4" 拆成单字符。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.config_loader import normalize_extensions
from src.video_organizer.core.video_file_handler import VideoFileHandler


class _FakeDownloader:
    """记录 remove_download 调用的假下载器"""

    def __init__(self, matched=True):
        self.matched = matched
        self.calls = []

    def remove_download(self, file_path):
        self.calls.append(file_path)
        return self.matched


def _make_handler(tmp_dir, supported=(".mp4", ".mkv", ".srt", ".ass")):
    return VideoFileHandler(
        output_dir=str(Path(tmp_dir) / "out"),
        supported_extensions=list(supported),
        tmdb_config={"api_key": ""},
    )


class TestNormalizeExtensions(unittest.TestCase):
    def test_ini_string_is_split(self):
        self.assertEqual(
            normalize_extensions(".mp4,.mkv, .TS "),
            [".mp4", ".mkv", ".ts"],
        )

    def test_string_is_not_exploded_into_characters(self):
        result = normalize_extensions(".mp4")
        self.assertEqual(result, [".mp4"])

    def test_list_and_missing_dot(self):
        self.assertEqual(
            normalize_extensions(["mp4", ".MKV", "", None, "srt"]),
            [".mp4", ".mkv", ".srt"],
        )

    def test_empty(self):
        self.assertEqual(normalize_extensions(None), [])
        self.assertEqual(normalize_extensions(""), [])


class TestSubtitleNotTreatedAsVideo(unittest.TestCase):
    """手动处理字幕文件时不能走视频上传链路"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.video = base / "Show.S01E01.mkv"
        self.video.write_bytes(b"v" * 32)
        self.subtitle = base / "Show.S01E01.ass"
        self.subtitle.write_bytes(b"s" * 16)
        self.handler = _make_handler(self.tmp.name)
        self.uploaded = []
        self.handler._process_file_internal = lambda path, worker_id=0: (
            self.uploaded.append(path) or True
        )

    def tearDown(self):
        try:
            self.handler.stop_upload_queue()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_force_process_routes_subtitle_to_subtitle_handler(self):
        called = []
        self.handler._process_subtitle_file = lambda path: called.append(path) or True

        result = self.handler.force_process_file(str(self.subtitle))

        self.assertTrue(result)
        self.assertEqual(called, [str(self.subtitle)])
        self.assertEqual(self.uploaded, [])

    def test_force_process_still_queues_video(self):
        self.assertTrue(self.handler.force_process_file(str(self.video)))
        # 视频走队列（或直接处理），一定不是字幕分支
        self.assertTrue(str(self.video) in self.handler._queued_files or self.uploaded)


class TestDownloaderCleanupFallback(unittest.TestCase):
    """没有建立下载器映射时也要尽力删除下载器任务"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = Path(self.tmp.name) / "Show.S01E01.mkv"
        self.video.write_bytes(b"v" * 16)
        self.handler = _make_handler(self.tmp.name)

    def tearDown(self):
        try:
            self.handler.stop_upload_queue()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_untracked_file_still_tries_to_remove_task(self):
        downloader = _FakeDownloader()
        self.handler.downloaders = [downloader]
        self.assertEqual(self.handler._file_downloader_map, {})

        state = self.handler._downloader_cleanup_state(str(self.video))

        # 返回 None 表示「与下载器无关，可以删文件」，但任务已经尽力删掉了
        self.assertIsNone(state)
        self.assertTrue(downloader.calls)

    def test_untracked_file_without_downloaders(self):
        self.handler.downloaders = []
        self.assertIsNone(self.handler._downloader_cleanup_state(str(self.video)))


if __name__ == "__main__":
    unittest.main()
