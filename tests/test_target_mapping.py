"""目标映射表 / 未完成下载过滤 / 重复上传防护 / 报错定时提醒 的回归测试"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


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


class TestTargetMappingStore(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._setup_temp_db()

    def tearDown(self):
        self._teardown_temp_db()

    def test_title_mapping_matches_all_episodes(self):
        from src.video_organizer.core.mapping_store import TargetMappingStore

        TargetMappingStore.create(
            keyword="时光代理人",
            item_type="vl",
            item_id="111",
            match_type="title",
            media_type="tv",
            label="时光代理人",
        )
        for episode in (1, 5, 9):
            match = TargetMappingStore.resolve(
                title="时光代理人",
                season=4,
                episode=episode,
                media_type="tv",
                file_name=f"时光代理人.S04E{episode:02d}.mkv",
            )
            self.assertIsNotNone(match)
            self.assertEqual(match["item_id"], "111")

    def test_episode_mapping_takes_precedence(self):
        from src.video_organizer.core.mapping_store import TargetMappingStore

        TargetMappingStore.create(
            keyword="时光代理人", item_type="vl", item_id="111", match_type="title"
        )
        TargetMappingStore.create(
            keyword="时光代理人",
            item_type="ve",
            item_id="999",
            match_type="episode",
            season_number=4,
            episode_number=9,
        )
        match = TargetMappingStore.resolve(
            title="时光代理人", season=4, episode=9, media_type="tv",
            file_name="时光代理人.S04E09.mkv",
        )
        self.assertEqual(match["item_id"], "999")
        other = TargetMappingStore.resolve(
            title="时光代理人", season=4, episode=8, media_type="tv",
            file_name="时光代理人.S04E08.mkv",
        )
        self.assertEqual(other["item_id"], "111")

    def test_remember_upserts_and_matches_filename(self):
        from src.video_organizer.core.mapping_store import TargetMappingStore

        TargetMappingStore.remember(
            file_path="/media/怪奇物语.S01E01.mkv",
            title="怪奇物语",
            media_type="tv",
            season=1,
            episode=1,
            target={"item_type": "ve", "item_id": "42", "label": "S01E01"},
            source="telegram",
        )
        TargetMappingStore.remember(
            file_path="/media/怪奇物语.S01E01.mkv",
            title="怪奇物语",
            media_type="tv",
            season=1,
            episode=1,
            target={"item_type": "ve", "item_id": "43", "label": "S01E01"},
            source="telegram",
        )
        self.assertEqual(len(TargetMappingStore.list_all()), 1)
        match = TargetMappingStore.resolve(
            title="", season=1, episode=1, media_type="tv",
            file_name="怪奇物语.S01E01.mkv",
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["item_id"], "43")


class TestIncompleteDownloads(unittest.TestCase):
    def test_path_key_and_is_incomplete(self):
        from src.video_organizer.core.incomplete_downloads import (
            collect_incomplete_paths,
            is_incomplete,
            path_key,
        )

        class _FakeMonitor:
            def get_incomplete_paths(self):
                return {"E:/Downloads/Show.S01E01.mkv"}

        keys = collect_incomplete_paths([_FakeMonitor()])
        self.assertIn(path_key("E:/Downloads/Show.S01E01.mkv"), keys)
        self.assertTrue(is_incomplete("E:\\Downloads\\Show.S01E01.mkv", keys))
        self.assertFalse(is_incomplete("E:/Downloads/Show.S01E02.mkv", keys))

    def test_monitor_without_method_is_ignored(self):
        from src.video_organizer.core.incomplete_downloads import collect_incomplete_paths

        class _Broken:
            def get_incomplete_paths(self):
                raise RuntimeError("boom")

        self.assertEqual(collect_incomplete_paths([_Broken(), object()]), set())


class TestOnlineUploadDedup(unittest.TestCase):
    def setUp(self):
        from src.video_organizer.core.online_upload import OnlineUploadService

        self._tmp = tempfile.TemporaryDirectory()
        self.service = OnlineUploadService()
        self.service.configure(
            {
                "emos": {"auth_token": "t"},
                "online_upload": {"video_root": self._tmp.name},
            }
        )
        # 不真正上传，仅测试任务创建与去重
        self.service._executor.submit = lambda *args, **kwargs: None
        self.path = Path(self._tmp.name) / "剧.S01E01.mkv"
        self.path.write_bytes(b"x" * 16)
        self.item = {
            "file_path": str(self.path),
            "item_type": "ve",
            "item_id": "9",
            "title": "剧",
            "media_type": "tv",
            "season_number": 1,
            "episode_number": 1,
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_duplicate_submit_is_rejected(self):
        first = self.service.create_tasks([self.item])
        self.assertEqual(len(first["tasks"]), 1)
        second = self.service.create_tasks([self.item])
        self.assertEqual(len(second["tasks"]), 0)
        self.assertEqual(len(second["duplicates"]), 1)

    def test_completed_file_is_not_reuploaded(self):
        first = self.service.create_tasks([self.item])
        task_id = first["tasks"][0]["id"]
        self.service._tasks[task_id].status = "completed"
        again = self.service.create_tasks([self.item])
        self.assertEqual(len(again["duplicates"]), 1)


class TestTelegramReminder(unittest.TestCase):
    def _service(self):
        from src.video_organizer.core.telegram_bot import TelegramBotService

        service = TelegramBotService()
        service._token = "bot-token"
        service._chat_id = "1"
        service._enabled = True
        service.sent = []
        service.send_text = lambda text, reply_to=None, chat_id=None: (
            service.sent.append(text) or len(service.sent)
        )
        return service

    def test_reminder_resends_after_interval(self):
        service = self._service()
        context = {"file_path": "E:/d/a.mkv", "file_name": "a.mkv", "title": "剧"}
        self.assertTrue(service.notify_error(context, "上传失败"))
        self.assertEqual(len(service.sent), 1)

        # 把上次发送时间拨回 6 分钟，模拟一直未解决
        key = "E:/d/a.mkv|上传失败"
        service._active_errors[key]["last_sent"] = time.time() - 360
        service._send_reminders()
        self.assertEqual(len(service.sent), 2)
        self.assertIn("仍未解决", service.sent[1])

    def test_clear_error_stops_reminder(self):
        service = self._service()
        context = {"file_path": "E:/d/a.mkv", "file_name": "a.mkv"}
        service.notify_error(context, "上传失败")
        service.clear_error("E:/d/a.mkv")
        key = "E:/d/a.mkv|上传失败"
        self.assertNotIn(key, service._active_errors)
        service._send_reminders()
        self.assertEqual(len(service.sent), 1)


class TestTelegramCorrectionMapping(_TempDbMixin, unittest.TestCase):
    """修正一集后记住整部剧的映射，并自动重传同名剧集的其他文件"""

    def setUp(self):
        self._setup_temp_db()
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()
        self._teardown_temp_db()

    def _service(self):
        from src.video_organizer.core.telegram_bot import TelegramBotService

        service = TelegramBotService()
        service._token = "t"
        service._chat_id = "1"
        service._enabled = True
        service.sent = []
        service.send_text = lambda text, reply_to=None, chat_id=None: (
            service.sent.append(text) or len(service.sent)
        )
        return service

    def test_remember_show_level_mapping(self):
        from src.video_organizer.core.mapping_store import TargetMappingStore
        from src.video_organizer.core.telegram_bot import TargetExpression

        service = self._service()
        expr = TargetExpression(title="测试剧", season=1, episode=2)
        candidates = [{"title": "测试剧", "item_type": "vl", "item_id": "777", "seasons": []}]
        match = {
            "item_type": "ve", "item_id": "888", "label": "S01E02",
            "season_number": 1, "episode_number": 2,
        }
        context = {"file_path": "/media/测试剧.S01E02.mkv", "title": "测试剧", "storage": ""}

        info = service._remember_correction_mapping(expr, "tv", match, candidates, context)

        self.assertTrue(info["saved"])
        self.assertTrue(info["show_level"])
        rows = TargetMappingStore.list_all()
        self.assertEqual(rows[0]["item_type"], "vl")
        self.assertEqual(rows[0]["item_id"], "777")
        self.assertIsNone(rows[0]["episode_number"])

    def test_retry_pending_same_title(self):
        from src.video_organizer.core.online_upload import OnlineUploadService

        service = self._service()
        pending_file = Path(self._tmp.name) / "测试剧.S01E03.mkv"
        pending_file.write_bytes(b"x" * 8)
        service._active_errors[str(pending_file) + "|未找到 Emos 上传目标"] = {
            "context": {"file_path": str(pending_file), "title": "测试剧"},
            "error": "未找到",
            "header": "未找到 Emos 上传目标",
            "last_sent": 0,
            "message_id": 1,
        }

        upload_service = OnlineUploadService.instance()
        created = []
        original_recognize = upload_service.recognize
        original_create = upload_service.create_task
        upload_service.recognize = lambda path: {
            "match": {"item_type": "vl", "item_id": "777"},
            "metadata": {"title": "测试剧", "season": 1, "episode": 3, "media_type": "tv"},
        }
        upload_service.create_task = lambda item: (
            created.append(item) or {"id": "task-1", "duplicate": False}
        )
        try:
            retried = service._retry_pending_same_title("测试剧", "tv")
        finally:
            upload_service.recognize = original_recognize
            upload_service.create_task = original_create

        self.assertEqual(retried, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["item_id"], "777")
        self.assertEqual(created[0]["episode_number"], 3)


if __name__ == "__main__":
    unittest.main()
