"""上传阶段报错推送 Telegram 的回归测试

此前「被 Emos 拒绝（例如同一资源一周内已上传过）」和「上传失败」两条链路只写日志，
Telegram 上什么都收不到，看起来就像「文件不再自动上传」。现在这两类报错都会推送
（同一文件同类报错 5 分钟只推一次），用户可以直接回复该消息修正目标后重传。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.video_file_handler import VideoFileHandler


class _TempDbMixin:
    """把测试用数据库写到临时目录，避免污染项目数据"""

    def _setup_temp_db(self):
        self._old_db = os.environ.get("VIDEO_ORGANIZER_DB_PATH")
        self._db_tmp = tempfile.TemporaryDirectory()
        os.environ["VIDEO_ORGANIZER_DB_PATH"] = str(
            Path(self._db_tmp.name) / "test.db"
        )

    def _teardown_temp_db(self):
        try:
            from src.video_organizer.database import session as db_session

            if db_session._engine is not None:
                db_session._engine.dispose()
            db_session._engine = None
            db_session._SessionLocal = None
        except Exception:
            pass
        if self._old_db is None:
            os.environ.pop("VIDEO_ORGANIZER_DB_PATH", None)
        else:
            os.environ["VIDEO_ORGANIZER_DB_PATH"] = self._old_db
        try:
            self._db_tmp.cleanup()
        except Exception:
            pass


class _FakeBot:
    """只记录 notify_error 调用，不真的发消息"""

    calls = []

    @classmethod
    def instance(cls):
        return cls()

    def notify_error(self, context, error, header="上传失败"):
        type(self).calls.append((dict(context or {}), error, header))
        return True


class TestUploadErrorNotify(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._setup_temp_db()
        self._FakeBot_calls = _FakeBot.calls = []
        self.handler = VideoFileHandler(
            output_dir=self._db_tmp.name,
            supported_extensions=[".mkv", ".srt"],
            emos_config={"auth_token": "t", "base_url": "https://emos.best"},
            processing_config={"max_upload_workers": 1},
            config={
                "processing": {"max_upload_workers": 1},
                "monitoring": {},
                "emos": {"auth_token": "t"},
            },
        )
        self.handler.probe_enabled = False

    def tearDown(self):
        self.handler.stop_upload_queue()
        self._teardown_temp_db()

    def test_notify_upload_error_pushes_context_and_header(self):
        with patch(
            "src.video_organizer.core.telegram_bot.TelegramBotService", _FakeBot
        ):
            self.handler._notify_upload_error(
                "/media/a.mkv",
                "此资源您一周内上传过",
                title="花儿与少年",
                media_type="tv",
                season=8,
                episode=8,
                item_type="ve",
                item_id="9",
                header="上传被 Emos 拒绝",
            )

        self.assertEqual(len(_FakeBot.calls), 1)
        context, error, header = _FakeBot.calls[0]
        self.assertEqual(header, "上传被 Emos 拒绝")
        self.assertEqual(error, "此资源您一周内上传过")
        self.assertEqual(context["file_name"], "a.mkv")
        self.assertEqual(context["item_type"], "ve")
        self.assertEqual(context["item_id"], "9")

    def test_match_error_reuses_upload_error_notify(self):
        with patch(
            "src.video_organizer.core.telegram_bot.TelegramBotService", _FakeBot
        ):
            self.handler._notify_match_error(
                "/media/a.mkv", "花儿与少年", "tv", 8, 8, "Emos 中没有这一集"
            )

        self.assertEqual(_FakeBot.calls[0][2], "未找到 Emos 上传目标")

    def test_deferred_upload_notifies_telegram(self):
        """Emos 拒绝重复上传时也要推 Telegram，而不是只写日志"""

        class _FakeUploader:
            def __init__(self, **kwargs):
                pass

            def upload_video(self, *args, **kwargs):
                return {"deferred": True, "reason": "此资源您一周内上传过"}

            def close(self):
                pass

        with patch(
            "src.video_organizer.core.video_file_handler.RobustEmosVideoUploader",
            _FakeUploader,
        ), patch(
            "src.video_organizer.core.telegram_bot.TelegramBotService", _FakeBot
        ):
            self.handler._execute_upload(
                "/media/花儿与少年.S08E08.mkv",
                "ve",
                "9",
                0,
                "123",
                "tv",
                "花儿与少年",
                "S08E08",
                {"season": 8, "episode": 8},
            )

        headers = [call[2] for call in _FakeBot.calls]
        self.assertIn("上传被 Emos 拒绝", headers)
        context, error, _ = _FakeBot.calls[0]
        self.assertEqual(error, "此资源您一周内上传过")
        self.assertEqual(context["episode_number"], 8)


if __name__ == "__main__":
    unittest.main()
