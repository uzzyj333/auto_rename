# -*- coding: utf-8 -*-
"""Telegram「直接发链接给 aria2 下载」的回归测试

覆盖：消息里链接的提取与清洗（中文标点 / 磁力链）、`/dl` 指令、
普通消息里的链接自动识别，以及推送给 aria2 的请求内容与结果回执。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core import downloader_monitor as dm
from src.video_organizer.core.telegram_bot import TelegramBotService, _extract_urls


class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise dm.requests.HTTPError(f"HTTP {self.status_code}")


class TestExtractUrls(unittest.TestCase):
    def test_http_and_magnet(self):
        urls = _extract_urls(
            "下这个 https://example.com/a.mkv 和 magnet:?xt=urn:btih:abcdef"
        )
        self.assertEqual(
            urls,
            ["https://example.com/a.mkv", "magnet:?xt=urn:btih:abcdef"],
        )

    def test_trailing_chinese_punctuation_is_stripped(self):
        urls = _extract_urls("看看这个（https://example.com/b.mkv）。")
        self.assertEqual(urls, ["https://example.com/b.mkv"])

    def test_duplicates_are_removed_and_limit_applied(self):
        text = " ".join(f"https://example.com/{i}.mkv" for i in range(8))
        self.assertEqual(len(_extract_urls(text)), 5)
        self.assertEqual(
            _extract_urls("https://a/x.mkv https://a/x.mkv"), ["https://a/x.mkv"]
        )

    def test_plain_text_has_no_url(self):
        self.assertEqual(_extract_urls("时光代理人S04E09"), [])


class TestTelegramUrlDownload(unittest.TestCase):
    def setUp(self):
        self.service = TelegramBotService()
        self.service._chat_id = "1"
        self.service._config = {
            "telegram": {
                "url_download_enabled": True,
                "url_download_save_dir": "/downloads",
            },
            "downloaders": [
                {"id": "aria2", "type": "aria2", "rpc_url": "http://a:6800/jsonrpc"}
            ],
        }
        self.sent = []

    def _patch_send_text(self):
        sent = self.sent
        return patch.object(
            TelegramBotService,
            "send_text",
            lambda _self, text, **kw: sent.append(text),
        )

    def test_push_urls_reports_gid_and_save_dir(self):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            calls.append((url, json))
            return _FakeResponse({"result": "gid-7"})

        with patch.object(dm.requests, "post", fake_post), self._patch_send_text():
            self.service._push_urls(["https://example.com/a.mkv"], "1")

        payload = calls[0][1]
        self.assertEqual(payload["method"], "aria2.addUri")
        self.assertEqual(payload["params"][0], ["https://example.com/a.mkv"])
        self.assertEqual(payload["params"][1], {"dir": "/downloads"})
        self.assertIn("gid-7", self.sent[0])
        self.assertIn("/downloads", self.sent[0])

    def test_push_urls_reports_failure_reason(self):
        with patch.object(
            dm.requests,
            "post",
            lambda *a, **k: _FakeResponse({"error": {"message": "Unauthorized"}}),
        ), self._patch_send_text():
            self.service._push_urls(["https://example.com/a.mkv"], "1")
        self.assertIn("Unauthorized", self.sent[0])

    def test_start_url_download_uses_background_thread(self):
        started = []
        with patch(
            "src.video_organizer.core.telegram_bot.threading.Thread"
        ) as thread_cls, self._patch_send_text():
            self.service._start_url_download(["https://a/x.mkv"], "1")
        started.append(thread_cls.call_args)
        self.assertTrue(started)
        self.assertEqual(thread_cls.call_args.kwargs["target"], self.service._push_urls)
        self.assertIn("1 个下载链接", self.sent[0])

    def test_dl_command_without_link_shows_usage(self):
        with patch.object(
            TelegramBotService, "_start_url_download"
        ) as start, self._patch_send_text():
            self.service._handle_command("/dl", "1", "9", [], "/dl")
        start.assert_not_called()
        self.assertIn("用法", self.sent[0])

    def test_dl_command_pushes_links(self):
        with patch.object(
            TelegramBotService, "_start_url_download"
        ) as start, self._patch_send_text():
            self.service._handle_command(
                "/dl", "1", "9", [], "/dl https://a/x.mkv magnet:?xt=urn:btih:abc"
            )
        start.assert_called_once()
        self.service._handle_command(
            "/dl", "1", "9", [], "/dl https://a/x.mkv magnet:?xt=urn:btih:abc"
        )

    def test_plain_message_with_link_triggers_download(self):
        with patch.object(
            TelegramBotService, "_start_url_download"
        ) as start, self._patch_send_text():
            self.service._handle_update(
                {
                    "message": {
                        "text": "帮我下 https://a/x.mkv",
                        "chat": {"id": "1"},
                        "from": {"id": "9"},
                    }
                }
            )
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0], ["https://a/x.mkv"])

    def test_plain_message_without_link_keeps_hint(self):
        with patch.object(
            TelegramBotService, "_start_url_download"
        ) as start, self._patch_send_text():
            self.service._handle_update(
                {
                    "message": {
                        "text": "随便说点什么",
                        "chat": {"id": "1"},
                        "from": {"id": "9"},
                    }
                }
            )
        start.assert_not_called()
        self.assertIn("回复", self.sent[0])

    def test_auto_detect_can_be_disabled(self):
        self.service._config["telegram"]["url_download_enabled"] = False
        with patch.object(
            TelegramBotService, "_start_url_download"
        ) as start, self._patch_send_text():
            self.service._handle_update(
                {
                    "message": {
                        "text": "https://a/x.mkv",
                        "chat": {"id": "1"},
                        "from": {"id": "9"},
                    }
                }
            )
        start.assert_not_called()
        self.assertIn("回复", self.sent[0])


if __name__ == "__main__":
    unittest.main()
