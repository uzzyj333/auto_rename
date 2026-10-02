"""在线识别上传「上传后清理原文件」的回归测试

手动选片 / Telegram 回复修正两条链路共用 ``core.source_cleanup``，但此前没有
把下载器清理回调注册给 OnlineUploadService，结果是上传成功后只删掉了源文件、
没有删除下载器任务 —— qBittorrent 里会留下「文件已不存在」的空种子。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.online_upload import OnlineUploadService


class _FakeHandler:
    """只保留 VideoFileHandler 里下载器清理回调的假实现"""

    def __init__(self, state):
        self._state = state
        self.calls = []

    def _downloader_cleanup_state(self, file_path):
        self.calls.append(file_path)
        return self._state


class TestOnlineUploadSourceCleanup(unittest.TestCase):
    def setUp(self):
        self.service = OnlineUploadService()
        self.service.configure({"processing": {"delete_after_upload": True}})

    @staticmethod
    def _temp_file():
        handle, path = tempfile.mkstemp(suffix=".mkv")
        os.write(handle, b"x" * 16)
        os.close(handle)
        return path

    def test_uses_registered_downloader_cleanup(self):
        path = self._temp_file()
        calls = []
        try:
            self.service.set_downloader_cleanup(lambda p: calls.append(p) or True)

            deleted, note = self.service._delete_source_if_configured(path)

            self.assertTrue(deleted)
            self.assertEqual(calls, [path])
            self.assertFalse(os.path.exists(path))
            self.assertEqual(note, "（已删除原文件）")
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_keeps_file_while_torrent_has_pending_videos(self):
        path = self._temp_file()
        try:
            self.service.set_downloader_cleanup(lambda p: False)

            deleted, note = self.service._delete_source_if_configured(path)

            self.assertFalse(deleted)
            self.assertTrue(os.path.exists(path))
            self.assertIn("下载器中仍有未完成的任务", note)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_falls_back_to_handler_from_state_manager(self):
        from src.video_organizer.web.services.state import get_state_manager

        state = get_state_manager()
        previous = state.get_video_handler()
        path = self._temp_file()
        handler = _FakeHandler(True)
        try:
            state.set_video_handler(handler)

            deleted, _ = self.service._delete_source_if_configured(path)

            self.assertTrue(deleted)
            self.assertEqual(handler.calls, [path])
        finally:
            state.set_video_handler(previous)
            if os.path.exists(path):
                os.remove(path)

    def test_delete_disabled_keeps_file(self):
        path = self._temp_file()
        try:
            self.service.configure({"processing": {"delete_after_upload": False}})
            self.service.set_downloader_cleanup(lambda p: True)

            deleted, note = self.service._delete_source_if_configured(path)

            self.assertFalse(deleted)
            self.assertEqual(note, "")
            self.assertTrue(os.path.exists(path))
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
