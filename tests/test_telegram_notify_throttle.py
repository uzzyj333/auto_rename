"""Telegram 报错通知节流的回归测试

文件上传一直失败时，重试队列每分钟都会重新处理一次；如果不做节流，
Telegram 会被同一份报错反复刷屏。
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.telegram_bot import TelegramBotService


class TestNotifyErrorThrottle(unittest.TestCase):
    def setUp(self):
        self.service = TelegramBotService()
        self.service._token = "bot-token"
        self.service._chat_id = "1"
        self.service._enabled = True
        self.sent = []
        self.service.send_text = self._fake_send
        # 报错通知前会检查本地文件是否存在，所以用真实临时文件
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "a.mkv").write_bytes(b"a")
        (self.dir / "b.mkv").write_bytes(b"b")

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_send(self, text, reply_to=None, chat_id=None):
        self.sent.append(text)
        return len(self.sent)

    def _context(self, name="a.mkv"):
        return {
            "file_path": str(self.dir / name),
            "file_name": name,
            "title": "来！金来号！",
        }

    def test_same_error_is_throttled(self):
        context = self._context()

        self.assertTrue(self.service.notify_error(context, "Emos 上传失败"))
        self.assertFalse(self.service.notify_error(context, "Emos 上传失败"))

        self.assertEqual(len(self.sent), 1)

    def test_other_file_or_stage_still_notifies(self):
        context = self._context()
        self.assertTrue(self.service.notify_error(context, "boom"))
        self.assertTrue(
            self.service.notify_error(context, "boom", header="未找到 Emos 上传目标")
        )
        other = self._context("b.mkv")
        self.assertTrue(self.service.notify_error(other, "boom"))

        self.assertEqual(len(self.sent), 3)

    def test_notifies_again_after_window(self):
        context = self._context()
        self.assertTrue(self.service.notify_error(context, "boom"))
        key = f"{context['file_path']}|上传失败"
        self.service._error_notified_at[key] = time.time() - 301

        self.assertTrue(self.service.notify_error(context, "boom"))

        self.assertEqual(len(self.sent), 2)

    def test_failed_send_can_retry(self):
        context = self._context()
        self.service.send_text = lambda text, reply_to=None, chat_id=None: None

        self.assertFalse(self.service.notify_error(context, "boom"))

        self.service.send_text = self._fake_send
        self.assertTrue(self.service.notify_error(context, "boom"))
        self.assertEqual(len(self.sent), 1)

    def test_missing_file_is_not_notified(self):
        """本地文件已经不存在时不再推送报错"""
        context = {
            "file_path": str(self.dir / "gone.mkv"),
            "file_name": "gone.mkv",
            "title": "来！金来号！",
        }
        self.assertFalse(self.service.notify_error(context, "boom"))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
