# -*- coding: utf-8 -*-
"""字幕自动识别上传回归测试

修复前的两个问题：

1. ``find_matching_video`` 会把字幕文件自己当成「完全匹配的视频」（supported_extensions
   里含 .srt/.ass），于是字幕永远挂不到视频上，日志表现为「目标字幕文件已存在，跳过」；
2. 视频上传后原文件被删除、只剩外挂字幕时，字幕只做重命名/跳过，永远不会单独上传。

现在：字幕找不到同目录视频（或视频已上传）时，按字幕自身识别目标并单独补传；
识别不到目标时推送 Telegram 报错，用户回复该消息即可指定目标。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.subtitle_handler import SubtitleHandler
from src.video_organizer.core.video_file_handler import VideoFileHandler


class _TempDbMixin:
    """把测试用数据库写到临时目录，避免污染项目数据"""

    def _setup_temp_db(self):
        self._old_db = os.environ.get("VIDEO_ORGANIZER_DB_PATH")
        self._db_tmp = tempfile.TemporaryDirectory()
        os.environ["VIDEO_ORGANIZER_DB_PATH"] = str(Path(self._db_tmp.name) / "test.db")

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
    calls = []

    @classmethod
    def instance(cls):
        return cls()

    def notify_error(self, context, error, header="上传失败"):
        type(self).calls.append((dict(context or {}), error, header))
        return True


def _make_handler(tmp_dir, supported=(".mp4", ".mkv", ".srt", ".ass")):
    return VideoFileHandler(
        output_dir=str(Path(tmp_dir) / "out"),
        supported_extensions=list(supported),
        tmdb_config={"api_key": ""},
    )


class TestFindMatchingVideoSkipsSubtitles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.handler = SubtitleHandler()
        self.video = self.root / "Show.S01E01.mkv"
        self.video.write_bytes(b"v" * 32)
        self.subtitle = self.root / "Show.S01E01_track9_chi.srt"
        self.subtitle.write_text("1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_subtitle_never_matches_itself(self):
        found = self.handler.find_matching_video(
            self.subtitle, (".mp4", ".mkv", ".srt", ".ass")
        )
        self.assertEqual(found, self.video)

    def test_subtitle_only_directory_returns_none(self):
        self.video.unlink()
        found = self.handler.find_matching_video(
            self.subtitle, (".mp4", ".mkv", ".srt", ".ass")
        )
        self.assertIsNone(found)


class TestProcessSubtitleFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.handler = _make_handler(self.tmp.name)

    def tearDown(self):
        try:
            self.handler.stop_upload_queue()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_no_video_falls_back_to_standalone_upload(self):
        subtitle = self.root / "Show.S01E01_track9_chi.srt"
        subtitle.write_text("1\n", encoding="utf-8")
        calls = []
        self.handler._upload_standalone_subtitle = (
            lambda path: calls.append(path) or True
        )

        self.assertTrue(self.handler._process_subtitle_file(str(subtitle)))
        self.assertEqual(calls, [str(subtitle)])

    def test_video_present_renames_instead_of_uploading(self):
        video = self.root / "Show.S01E01.mkv"
        video.write_bytes(b"v" * 32)
        subtitle = self.root / "Show.S01E01.eng.srt"
        subtitle.write_text("1\n", encoding="utf-8")
        calls = []
        self.handler._upload_standalone_subtitle = (
            lambda path: calls.append(path) or True
        )

        self.assertTrue(self.handler._process_subtitle_file(str(subtitle)))
        self.assertEqual(calls, [])
        self.assertTrue((self.root / "Show.S01E01.English.srt").exists())
        self.assertFalse(subtitle.exists())

    def test_uploaded_video_triggers_standalone_subtitle_upload(self):
        video = self.root / "Show.S01E01.mkv"
        video.write_bytes(b"v" * 32)
        subtitle = self.root / "Show.S01E01.eng.srt"
        subtitle.write_text("1\n", encoding="utf-8")
        self.handler._uploaded_files.add(str(video))
        calls = []
        self.handler._upload_standalone_subtitle = (
            lambda path: calls.append(path) or True
        )

        self.assertTrue(self.handler._process_subtitle_file(str(subtitle)))
        self.assertEqual(calls, [str(subtitle)])


class TestStandaloneSubtitleUpload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.handler = _make_handler(self.tmp.name)
        self.handler.emos_file_storage = "internal"
        self.subtitle = self.root / "Show.S01E01_track9_chi.srt"
        self.subtitle.write_text("1\n", encoding="utf-8")

    def tearDown(self):
        try:
            self.handler.stop_upload_queue()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_recognized_subtitle_creates_upload_task(self):
        created = []

        class _FakeService:
            def ensure_configured(self, config=None):
                pass

            def recognize(self, path):
                return {
                    "match": {"item_type": "ve", "item_id": "42", "label": "S01E01"},
                    "metadata": {
                        "title": "Show",
                        "media_type": "tv",
                        "season": 1,
                        "episode": 1,
                    },
                }

            def create_task(self, item):
                created.append(item)
                return {"id": "t1"}

        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService.instance",
            lambda: _FakeService(),
        ):
            self.assertTrue(
                self.handler._upload_standalone_subtitle(str(self.subtitle))
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["item_type"], "ve")
        self.assertEqual(created[0]["item_id"], "42")
        self.assertEqual(created[0]["storage"], "internal")
        self.assertIn(str(self.subtitle), self.handler._uploaded_files)

    def test_unrecognized_subtitle_notifies_telegram(self):
        _FakeBot.calls = []

        class _FakeService:
            def ensure_configured(self, config=None):
                pass

            def recognize(self, path):
                return {
                    "match": None,
                    "metadata": {
                        "title": "Show",
                        "media_type": "tv",
                        "season": 1,
                        "episode": 1,
                    },
                    "error": "Emos 中没有这一集",
                }

            def create_task(self, item):  # pragma: no cover - 不应被调用
                raise AssertionError("识别失败时不应创建上传任务")

        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService.instance",
            lambda: _FakeService(),
        ), patch("src.video_organizer.core.telegram_bot.TelegramBotService", _FakeBot):
            self.assertFalse(
                self.handler._upload_standalone_subtitle(str(self.subtitle))
            )

        self.assertEqual(len(_FakeBot.calls), 1)
        context, error, header = _FakeBot.calls[0]
        self.assertEqual(header, "字幕未识别到上传目标")
        self.assertIn("Emos 中没有这一集", error)
        self.assertEqual(context["file_name"], self.subtitle.name)
        self.assertEqual(context["episode_number"], 1)


class TestTmdbNoMatchNotifiesTelegram(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._setup_temp_db()
        _FakeBot.calls = []
        self.handler = VideoFileHandler(
            output_dir=self._db_tmp.name,
            supported_extensions=[".mkv"],
            tmdb_config={"api_key": ""},
        )
        self.handler.renamer.extract_metadata = lambda path: {
            "show_name": "Jade Cause Of Death",
            "media_type": "tv",
            "season": "1",
            "episode": "19",
        }

    def tearDown(self):
        self.handler.stop_upload_queue()
        self._teardown_temp_db()

    def test_no_tmdb_id_pushes_match_error(self):
        with patch(
            "src.video_organizer.core.telegram_bot.TelegramBotService", _FakeBot
        ):
            self.handler._process_file_internal("/media/Jade.Cause Of Death.Ep19.ts", 0)

        self.assertEqual(len(_FakeBot.calls), 1)
        context, error, header = _FakeBot.calls[0]
        self.assertEqual(header, "未识别到条目")
        self.assertIn("未找到 TMDB 匹配结果", error)
        self.assertEqual(context["title"], "Jade Cause Of Death")
        self.assertEqual(context["episode_number"], "19")


if __name__ == "__main__":
    unittest.main()
