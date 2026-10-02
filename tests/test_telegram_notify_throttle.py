"""Telegram 报错通知节流的回归测试

文件上传一直失败时，重试队列每分钟都会重新处理一次；如果不做节流，
Telegram 会被同一份报错反复刷屏。
"""

import os
import sys
import time
import unittest

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

    def _fake_send(self, text, reply_to=None, chat_id=None):
        self.sent.append(text)
        return len(self.sent)

    @staticmethod
    def _context(path="E:/downloads/a.mkv", name="a.mkv"):
        return {"file_path": path, "file_name": name, "title": "来！金来号！"}

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
        other = self._context("E:/downloads/b.mkv", "b.mkv")
        self.assertTrue(self.service.notify_error(other, "boom"))

        self.assertEqual(len(self.sent), 3)

    def test_notifies_again_after_window(self):
        context = self._context()
        self.assertTrue(self.service.notify_error(context, "boom"))
        key = "E:/downloads/a.mkv|上传失败"
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


if __name__ == "__main__":
    unittest.main()
