# -*- coding: utf-8 -*-
"""Telegram 原生命令菜单（setMyCommands / 「菜单」按钮）的回归测试

启动 / 绑定 / 测试消息时都会注册 Telegram 原生的「菜单」按钮和输入 / 时的
命令列表；只用这套原生菜单，不再显示底部快捷键盘，绑定时顺手把旧键盘收起。
"""

import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.telegram_bot import (
    _BOT_COMMANDS,
    TelegramBotService,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"{}"
        self.status_code = 200

    def json(self):
        return self._payload


class TestTelegramCommandMenu(unittest.TestCase):
    def setUp(self):
        self.service = TelegramBotService()
        self.service._token = "123:abc"
        self.service._enabled = True
        self.service._reply_enabled = True

    def test_sync_command_menu_posts_commands(self):
        calls = []

        def fake_post(url, json=None, timeout=None, **kwargs):
            calls.append((url, json))
            return _FakeResponse({"ok": True})

        with patch(
            "src.video_organizer.core.telegram_bot.requests.post", fake_post
        ):
            self.assertTrue(self.service.sync_command_menu())

        urls = [url for url, _payload in calls]
        self.assertTrue(any(url.endswith("/setMyCommands") for url in urls))
        self.assertTrue(any(url.endswith("/setChatMenuButton") for url in urls))

        payload = next(
            body for url, body in calls if url.endswith("/setMyCommands")
        )
        commands = payload["commands"]
        self.assertEqual(commands, _BOT_COMMANDS)
        names = [item["command"] for item in commands]
        self.assertIn("upload", names)
        self.assertIn("status", names)
        for item in commands:
            # Telegram 约束：小写字母/数字/下划线，长度 1-32；描述 3-256 字符
            self.assertRegex(item["command"], re.compile(r"^[a-z0-9_]{1,32}$"))
            self.assertTrue(3 <= len(item["description"]) <= 256)

        menu_button = next(
            body["menu_button"]
            for url, body in calls
            if url.endswith("/setChatMenuButton")
        )
        self.assertEqual(menu_button, {"type": "commands"})

    def test_sync_command_menu_without_token_does_nothing(self):
        self.service._token = ""
        with patch(
            "src.video_organizer.core.telegram_bot.requests.post"
        ) as post:
            self.assertFalse(self.service.sync_command_menu())
        post.assert_not_called()

    def test_bind_registers_menu_and_hides_bottom_keyboard(self):
        self.service._chat_id = "42"
        shown = []
        hidden = []
        with patch.object(
            TelegramBotService, "sync_command_menu", lambda self: True
        ), patch.object(
            TelegramBotService,
            "send_text",
            lambda self, text, **kwargs: shown.append(text),
        ), patch.object(
            TelegramBotService,
            "hide_quick_keyboard",
            lambda self, *a, **k: hidden.append(True),
        ), patch.object(
            TelegramBotService,
            "send_quick_keyboard",
            lambda self, *a, **k: self.fail("绑定后不应再弹底部快捷键盘"),
            create=True,
        ):
            self.service._handle_command("/bind", "42", "1", [])

        self.assertTrue(shown)
        self.assertEqual(hidden, [True])


if __name__ == "__main__":
    unittest.main()
