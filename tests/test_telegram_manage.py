"""Telegram 机器人「回复删除 / 文件浏览完整名称 / 快捷配置开关」回归测试

需求背景：

1. 删除失败任务后机器人仍每 5 分钟提醒 —— 删除任务时要同步清除报错提醒，
   并支持直接回复报错消息发送「删除」来删除任务、停止提醒；
2. 文件浏览里过长的文件名被截断看不清 —— 在消息正文里按行给出完整名称；
3. 快捷配置 —— 常用布尔配置以开关呈现，点按即切换，需要输入的再提示输入。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.telegram_bot import TelegramBotService


class _FakeUploadService:
    """只记录删除调用，不触碰真实上传服务"""

    def __init__(self, roots=None, deleted=0):
        self._roots = list(roots or [])
        self._deleted = deleted
        self.deleted_paths = []

    @classmethod
    def instance(cls):
        return cls._current

    def roots(self):
        return list(self._roots)

    def resolve(self, raw):
        return Path(str(raw))

    def delete_tasks_for_file(self, file_path):
        self.deleted_paths.append(str(file_path))
        return self._deleted


class _BotHarness(unittest.TestCase):
    def setUp(self):
        self.service = TelegramBotService()
        self.service._token = "bot-token"
        self.service._chat_id = "1"
        self.service._enabled = True
        self.sent = []
        self.keyboards = []
        self.service.send_text = self._fake_send
        self.service._send_keyboard = self._fake_keyboard

    def _fake_send(self, text, reply_to=None, chat_id=None):
        self.sent.append(text)
        return len(self.sent)

    def _fake_keyboard(self, chat_id, text, rows, edit_message_id=None):
        self.keyboards.append((text, rows))


class TestReplyDelete(_BotHarness):
    def test_delete_keywords(self):
        self.assertTrue(self.service._is_delete_intent("删除"))
        self.assertTrue(self.service._is_delete_intent(" delete "))
        self.assertFalse(self.service._is_delete_intent("删除任务"))  # 不是纯关键词
        self.assertFalse(self.service._is_delete_intent("时光代理人S04E09"))

    def test_delete_reply_removes_task_and_stops_reminder(self):
        file_path = "/media/a.mkv"
        self.service._active_errors[f"{file_path}|未识别到条目"] = {"context": {}}
        fake = _FakeUploadService(deleted=2)
        _FakeUploadService._current = fake
        context = {"file_path": file_path, "file_name": "a.mkv"}
        self.service._replies["55"] = dict(context)

        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_delete_reply(context, "1", 55)

        self.assertEqual(fake.deleted_paths, [file_path])
        self.assertNotIn(f"{file_path}|未识别到条目", self.service._active_errors)
        self.assertNotIn("55", self.service._replies)
        self.assertIn("已删除该文件的任务并停止提醒", self.sent[-1])

    def test_clear_all_errors(self):
        self.service._active_errors["a|未识别到条目"] = {"context": {}}
        self.service._active_errors["b|未识别到条目"] = {"context": {}}
        self.service._error_notified_at["a|未识别到条目"] = 1.0

        removed = self.service.clear_all_errors()

        self.assertEqual(removed, 2)
        self.assertEqual(self.service._active_errors, {})
        self.assertEqual(self.service._error_notified_at, {})

    def test_delete_directory_clears_child_error_reminders(self):
        """删除目录时要清掉目录下子文件遗留的报错提醒"""
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "sub"
            sub.mkdir()
            child = sub / "a.mkv"
            child.write_bytes(b"x")
            other = Path(tmp) / "other.mkv"
            self.service._active_errors[f"{child}|未识别到条目"] = {
                "context": {"file_path": str(child)},
                "header": "未识别到条目",
                "last_sent": 0.0,
            }
            self.service._active_errors[f"{other}|上传失败"] = {
                "context": {"file_path": str(other)},
                "header": "上传失败",
                "last_sent": 0.0,
            }
            _FakeUploadService._current = _FakeUploadService(roots=[tmp])
            with patch(
                "src.video_organizer.core.online_upload.OnlineUploadService",
                _FakeUploadService,
            ):
                self.service._delete_path(str(sub), "1", None)

            self.assertFalse(sub.exists())
            self.assertNotIn(f"{child}|未识别到条目", self.service._active_errors)
            # 目录外的报错不受影响
            self.assertIn(f"{other}|上传失败", self.service._active_errors)
            self.assertIn("已删除目录", self.keyboards[-1][0])

    def test_clear_error_matches_normalized_path(self):
        """调用方传来带 .. 的等价路径时也要能清掉提醒"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.mkv"
            path.write_bytes(b"x")
            key = f"{path}|上传失败"
            self.service._active_errors[key] = {
                "context": {"file_path": str(path)},
                "header": "上传失败",
            }
            weird = os.path.join(tmp, "sub", "..", "a.mkv")
            self.service.clear_error(weird)
            self.assertNotIn(key, self.service._active_errors)


class TestBrowseFullName(_BotHarness):
    def test_browse_lists_full_file_name_in_message(self):
        long_name = "魅影神捕.Shadow.Punished.2024.2160p.WEB-DL.HEVC.DDP5.1.mkv"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / long_name).write_bytes(b"x")
            fake = _FakeUploadService(roots=[tmp])
            _FakeUploadService._current = fake
            with patch(
                "src.video_organizer.core.online_upload.OnlineUploadService",
                _FakeUploadService,
            ):
                self.service._send_browse("1", tmp)

        self.assertEqual(len(self.keyboards), 1)
        text, rows = self.keyboards[0]
        self.assertIn("本页完整名称：", text)
        self.assertIn(long_name, text)  # 正文里能看到完整文件名
        flat = [button["text"] for row in rows for button in row]
        self.assertTrue(any("🗑️" in button for button in flat))  # 有删除键


class TestQuickConfig(_BotHarness):
    def setUp(self):
        super().setUp()
        self.service._config = {
            "processing": {"delete_after_upload": True},
            "emos": {"upload_subtitles": False},
        }
        self.saved = []

        def _fake_save(config):
            self.saved.append(dict(config.get("processing", {})))
            return True, "已保存"

        self.service._save_config = _fake_save

    def test_menu_renders_toggle_and_input(self):
        self.service._config["processing"]["max_upload_workers"] = 3
        self.service._send_config_menu("1")
        text, rows = self.keyboards[-1]
        flat = [button["text"] for row in rows for button in row]
        self.assertTrue(any("上传后删除原文件：开" in button for button in flat))
        self.assertTrue(any("上传并发数 = 3" in button for button in flat))

    def test_toggle_flips_boolean_and_saves(self):
        self.service._toggle_config_bool("1", "processing", "delete_after_upload")
        self.assertIs(self.service._config["processing"]["delete_after_upload"], False)
        self.assertEqual(len(self.saved), 1)
        text, rows = self.keyboards[-1]
        flat = [button["text"] for row in rows for button in row]
        self.assertTrue(any("上传后删除原文件：关" in button for button in flat))


if __name__ == "__main__":
    unittest.main()
