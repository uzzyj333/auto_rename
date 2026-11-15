# -*- coding: utf-8 -*-
"""Telegram「本地文件 → 指定剧上传」的回归测试

覆盖：/upload 指定剧模式开关、按文件生成 up:pick 回调、点击后进入等待剧名状态、
直接发送剧名后走既有的「搜索 → 选作品 → 选季 → 选集」流程，以及取消 / 回复时清理状态。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.online_upload import OnlineUploadService
from src.video_organizer.core.telegram_bot import TelegramBotService


def _update(text, reply_to=None):
    message = {
        "text": text,
        "chat": {"id": "1"},
        "from": {"id": "9"},
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to, "text": "旧消息"}
    return {"message": message}


def _callback(data):
    return {
        "id": "q",
        "data": data,
        "message": {"message_id": 5, "chat": {"id": "1"}},
        "from": {"id": "9"},
    }


class TestLocalSpecifiedUpload(unittest.TestCase):
    def setUp(self):
        self.service = TelegramBotService()
        self.service._chat_id = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / "Show.S01E01.mkv"
        self.video.write_bytes(b"x")
        self.subtitle = self.root / "Show.S01E01.srt"
        self.subtitle.write_text("1", encoding="utf-8")

    def _browse(self, manual=None):
        captured = {}

        def fake_keyboard(_self, chat_id, text, rows, edit=None, context=None, reply_to=None):
            captured.update(text=text, rows=rows)

        with patch.object(
            OnlineUploadService, "roots", return_value=[str(self.root)]
        ), patch.object(
            OnlineUploadService, "resolve", side_effect=lambda raw: Path(str(raw))
        ), patch.object(TelegramBotService, "_send_keyboard", fake_keyboard):
            self.service._send_browse(
                "1", str(self.root), None, 0, manual_pick=manual
            )
        return captured

    def test_default_listing_has_mode_toggle_and_auto_upload(self):
        captured = self._browse()
        callbacks = [cell["callback_data"] for row in captured["rows"] for cell in row]
        self.assertTrue(any(cb.startswith("up:mode:pick:") for cb in callbacks))
        self.assertEqual(sum(cb.startswith("up:file:") for cb in callbacks), 4)
        self.assertNotIn("指定剧上传模式", captured["text"])

    def test_manual_mode_makes_video_buttons_pick_target(self):
        captured = self._browse(manual=True)
        callbacks = [cell["callback_data"] for row in captured["rows"] for cell in row]
        self.assertIn("指定剧上传模式", captured["text"])
        self.assertTrue(any(cb.startswith("up:mode:auto:") for cb in callbacks))
        # 视频走指定剧，字幕不能单独指定剧集、仍走自动上传
        self.assertEqual(sum(cb.startswith("up:pick:") for cb in callbacks), 2)
        self.assertEqual(sum(cb.startswith("up:file:") for cb in callbacks), 2)

    def test_pick_callback_waits_for_title_and_cancel_clears(self):
        captured = {}

        def fake_keyboard(_self, chat_id, text, rows, edit=None, context=None, reply_to=None):
            captured.update(text=text, rows=rows, context=context)

        token = self.service._token_for_path(str(self.video))
        with patch.object(TelegramBotService, "_answer_callback", lambda *a, **k: None), patch.object(
            TelegramBotService, "_send_keyboard", fake_keyboard
        ):
            self.service._handle_callback_query(_callback(f"up:pick:{token}"))

        self.assertIn("指定剧上传", captured["text"])
        self.assertEqual(captured["context"]["file_path"], str(self.video))
        self.assertEqual(
            self.service._pending_local_targets["1"]["file_path"], str(self.video)
        )

        with patch.object(TelegramBotService, "_answer_callback", lambda *a, **k: None), patch.object(
            TelegramBotService, "_send_keyboard", lambda *a, **k: None
        ):
            self.service._handle_callback_query(_callback("up:pickcancel"))
        self.assertNotIn("1", self.service._pending_local_targets)

    def test_plain_text_after_pick_starts_target_flow(self):
        self.service._set_pending_local_target("1", str(self.video))
        captured = {}

        def fake_correction(_self, context, text, chat_id, reply_to):
            captured.update(context=context, text=text, chat_id=chat_id, reply_to=reply_to)

        with patch.object(TelegramBotService, "_handle_correction", fake_correction):
            self.service._handle_update(_update("时光代理人S04E09"))

        self.assertEqual(captured["text"], "时光代理人S04E09")
        self.assertEqual(captured["context"]["file_path"], str(self.video))
        self.assertEqual(captured["chat_id"], "1")
        self.assertIsNone(captured["reply_to"])
        self.assertNotIn("1", self.service._pending_local_targets)

    def test_delete_text_cancels_pending_target(self):
        self.service._set_pending_local_target("1", str(self.video))
        sent = []
        with patch.object(
            TelegramBotService,
            "send_text",
            lambda _self, text, **kwargs: sent.append(text),
        ), patch.object(TelegramBotService, "_handle_correction", lambda *a, **k: None):
            self.service._handle_update(_update("删除"))
        self.assertTrue(any("已取消指定剧上传" in item for item in sent))
        self.assertNotIn("1", self.service._pending_local_targets)

    def test_reply_correction_discards_pending_target(self):
        self.service._set_pending_local_target("1", str(self.video))
        with patch.object(
            TelegramBotService,
            "_resolve_reply_context",
            lambda *a, **k: {"file_path": str(self.video)},
        ), patch.object(TelegramBotService, "_handle_correction", lambda *a, **k: None):
            self.service._handle_update(_update("别的剧 S01E02", reply_to=3))
        self.assertNotIn("1", self.service._pending_local_targets)


if __name__ == "__main__":
    unittest.main()
