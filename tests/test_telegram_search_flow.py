"""Telegram 「关键词搜索 → 点选作品 / 季 / 集」与「删除连带下载器任务」回归测试

需求背景：

1. 回复报错信息时只发片名关键词，机器人先搜索 Emos 并列出候选，
   用户再点选作品 → 季 → 集，避免「狂王 S02E04」定位不到时的死胡同；
2. 回复「删除」时，除了删除上传任务与提醒，还要连带删除
   aria2 / qBittorrent 里的对应下载任务；
3. 文件浏览键盘统一成 3 列网格，避免行与行之间宽窄不一。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.telegram_bot import TelegramBotService
from src.video_organizer.core.downloader_monitor import remove_downloader_tasks


def _video_candidate():
    """一部带 S02（含 E03 / E04）的作品候选"""
    return {
        "title": "狂王",
        "item_type": "vl",
        "item_id": "vl1",
        "seasons": [
            {
                "season_number": 2,
                "season_title": "第二季",
                "item_type": "vs",
                "item_id": "vs2",
                "episodes": [
                    {
                        "episode_number": 3,
                        "episode_title": "第三集",
                        "item_type": "ve",
                        "item_id": "ve23",
                    },
                    {
                        "episode_number": 4,
                        "episode_title": "第四集",
                        "item_type": "ve",
                        "item_id": "ve24",
                    },
                ],
            }
        ],
    }


def _video_candidate_season_years():
    """多季剧按季标年份：第一季 2024、第二季 2026（作品级仍是 2024）"""
    return {
        "title": "狂王",
        "item_type": "vl",
        "item_id": "vl1",
        "date_air": "2024-05-01",
        "seasons": [
            {
                "season_number": 1,
                "season_title": "第一季",
                "date_air": "2024-05-01",
                "item_type": "vs",
                "item_id": "vs1",
                "episodes": [
                    {
                        "episode_number": 1,
                        "episode_title": "第一集",
                        "item_type": "ve",
                        "item_id": "ve11",
                    }
                ],
            },
            {
                "season_number": 2,
                "season_title": "第二季",
                "date_air": "2026-01-01",
                "item_type": "vs",
                "item_id": "vs2",
                "episodes": [
                    {
                        "episode_number": 3,
                        "episode_title": "第三集",
                        "item_type": "ve",
                        "item_id": "ve23",
                    },
                    {
                        "episode_number": 4,
                        "episode_title": "第四集",
                        "item_type": "ve",
                        "item_id": "ve24",
                    },
                ],
            },
        ],
    }


def _other_show_2026():
    """另一部 2026 年的同名剧（作品级 2026，只有第一季）"""
    return {
        "title": "狂王",
        "item_type": "vl",
        "item_id": "vl9",
        "date_air": "2026-03-01",
        "seasons": [
            {
                "season_number": 1,
                "date_air": "2026-03-01",
                "item_type": "vs",
                "item_id": "vs9",
                "episodes": [],
            }
        ],
    }


class _FakeUploadService:
    """只记录 create_task 调用，不触碰真实上传服务 / Emos"""

    _current = None

    def __init__(self, candidates=None, titles=None):
        self._candidates = list(candidates or [])
        # 指定 titles 时，只有这些标题能搜到结果（模拟 Emos 只认简称）
        self._titles = set(titles) if titles is not None else None
        self.created = []
        self.searched = []

    @classmethod
    def instance(cls):
        return cls._current

    def search_targets(self, video_type=None, title=None, todb_id=None):
        self.searched.append(str(title or ""))
        if self._titles is not None and str(title or "") not in self._titles:
            return []
        return list(self._candidates)

    def pick_target(
        self, candidates, season=None, episode=None, media_type="", year=None, title=None
    ):
        return None

    def resolve_episode_from_candidates(
        self, candidates, season, episode, media_type="", title=None
    ):
        return None

    def create_task(self, item):
        self.created.append(dict(item))
        return {"id": f"task-{len(self.created)}"}

    def delete_after_upload_enabled(self):
        return False

    def delete_tasks_for_file(self, file_path):
        return 0


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
        # 不写映射表 / 不自动重传，专注按钮流程
        self.service._remember_correction_mapping = lambda *a, **k: {
            "saved": False,
            "show_level": False,
        }

    def _fake_send(self, text, reply_to=None, chat_id=None):
        self.sent.append(text)
        return len(self.sent)

    def _fake_keyboard(self, chat_id, text, rows, edit_message_id=None):
        self.keyboards.append((text, rows))

    def _buttons(self, index=-1):
        return [button for row in self.keyboards[index][1] for button in row]

    def _find_button(self, prefix, index=-1):
        for button in self._buttons(index):
            if str(button.get("callback_data") or "").startswith(prefix):
                return button
        self.fail(f"没有找到 {prefix} 按钮：{self._buttons(index)}")

    def _context(self):
        return {
            "file_path": "/media/狂王.Asura.S02E04.mkv",
            "file_name": "狂王.Asura.S02E04.mkv",
            "title": "和离！权倾朝野摄政王他不装了",
            "media_type": "tv",
            "season_number": 2,
            "episode_number": 4,
        }


class TestSearchFlow(_BotHarness):
    def setUp(self):
        super().setUp()
        self.fake = _FakeUploadService([_video_candidate()])
        _FakeUploadService._current = self.fake

    def test_keyword_reply_lists_candidate_buttons(self):
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王", "1", 55)

        self.assertEqual(self.fake.created, [])  # 只搜索，不直接建任务
        button = self._find_button("fx:work:")
        self.assertIn("狂王", button["text"])
        self.assertIn("S02", button["text"])

    def test_work_then_season_then_episode_creates_task(self):
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王", "1", 55)
            work_token = self._find_button("fx:work:")["callback_data"].split(":")[-1]
            self.service._handle_flow_callback("1", "work", work_token, None)

            season_button = self._find_button("fx:season:")
            self.assertIn("S02", season_button["text"])
            self.assertIn("2 集", season_button["text"])
            season_token = season_button["callback_data"].split(":")[-1]
            self.service._handle_flow_callback("1", "season", season_token, None)

            episode_button = self._find_button("fx:ep:")
            self.assertIn("E03", episode_button["text"])
            ep_token = [
                button
                for button in self._buttons()
                if button["text"].startswith("E04")
            ][0]["callback_data"].split(":")[-1]
            self.service._handle_flow_callback("1", "ep", ep_token, None)

        self.assertEqual(len(self.fake.created), 1)
        created = self.fake.created[0]
        self.assertEqual(created["item_type"], "ve")
        self.assertEqual(created["item_id"], "ve24")
        self.assertEqual(created["season_number"], 2)
        self.assertEqual(created["episode_number"], 4)
        self.assertTrue(self.sent[-1].startswith("✅ 已按选择的目标提交上传"))

    def test_season_all_uses_season_item(self):
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王", "1", 55)
            work_token = self._find_button("fx:work:")["callback_data"].split(":")[-1]
            self.service._handle_flow_callback("1", "work", work_token, None)
            season_token = self._find_button("fx:season:")["callback_data"].split(":")[-1]
            self.service._handle_flow_callback("1", "season", season_token, None)
            all_token = self._find_button("fx:season_all:")["callback_data"].split(":")[-1]
            self.service._handle_flow_callback("1", "season_all", all_token, None)

        created = self.fake.created[0]
        self.assertEqual(created["item_type"], "vs")
        self.assertEqual(created["item_id"], "vs2")
        self.assertEqual(created["season_number"], 2)
        self.assertIsNone(created["episode_number"])

    def test_expired_token_is_reported(self):
        self.service._handle_flow_callback("1", "work", "f999", None)
        self.assertIn("已过期", self.sent[-1])

    def test_year_filters_same_name_candidates(self):
        """同名作品用年份区分：只保留年份匹配的候选"""
        older = dict(_video_candidate(), item_id="vl0", date_air="2019-01-01")
        newer = dict(_video_candidate(), item_id="vl1", date_air="2024-05-01")
        _FakeUploadService._current = _FakeUploadService([older, newer])
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王 2024", "1", 55)

        work_buttons = [
            button
            for button in self._buttons()
            if str(button.get("callback_data") or "").startswith("fx:work:")
        ]
        self.assertEqual(len(work_buttons), 1)
        self.assertIn("2024", work_buttons[0]["text"])

    def test_dotted_title_falls_back_to_keyword(self):
        """粘贴「狂王.Asura.S02E04」：标题搜不到时依次换关键词，最终用「狂王」搜到"""
        _FakeUploadService._current = _FakeUploadService(
            [_video_candidate()], titles={"狂王"}
        )
        fake = _FakeUploadService._current
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王.Asura.S02E04", "1", 55)

        self.assertEqual(fake.searched[0], "狂王.Asura")
        self.assertIn("狂王", fake.searched)
        button = self._find_button("fx:work:")
        self.assertIn("狂王", button["text"])

    def test_dotted_year_reply_filters_candidates(self):
        """「狂王.2024.S01E01」这种粘贴写法：年份生效，同名作品按年份筛选"""
        older = dict(_video_candidate(), item_id="vl0", date_air="2019-01-01")
        newer = dict(_video_candidate(), item_id="vl1", date_air="2024-05-01")
        _FakeUploadService._current = _FakeUploadService([older, newer])
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王.2024.S01E01", "1", 55)

        work_buttons = [
            button
            for button in self._buttons()
            if str(button.get("callback_data") or "").startswith("fx:work:")
        ]
        self.assertEqual(len(work_buttons), 1)
        self.assertIn("2024", work_buttons[0]["text"])

    def test_release_tags_are_dropped_from_search_keywords(self):
        """「狂王.2024.1080p.S01E01」里的压制标签不能带进搜索词"""
        self.assertIn("狂王", TelegramBotService._title_variants("狂王 1080p"))
        self.assertIn(
            "狂王 Asura 2024",
            TelegramBotService._title_variants(
                "狂王.Asura 2024 2160p WEB-DL H265 DDP2.0-ADWeb"
            ),
        )


class TestSeasonYearMatching(_BotHarness):
    """多季剧按季标年份（第一季 2024、第二季 2026）时的年份匹配回归

    用户回复「狂王.2026.S02E04」里的 2026 属于第二季，不能因为作品级
    date_air 是 2024 就被判成「另一部 2026 年的同名剧」而过滤掉。
    """

    def test_season_year_keeps_candidate(self):
        kept = TelegramBotService._filter_by_year([_video_candidate_season_years()], 2026)
        self.assertEqual(len(kept), 1)

    def test_video_level_other_year_is_not_dropped(self):
        """季级年份命中的候选不能在同名剧混在一起时被丢掉"""
        kept = TelegramBotService._filter_by_year(
            [_video_candidate_season_years(), _other_show_2026()], 2026
        )
        self.assertEqual(len(kept), 2)

    def test_reply_season_year_lists_correct_candidate(self):
        _FakeUploadService._current = _FakeUploadService(
            [_video_candidate_season_years(), _other_show_2026()]
        )
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王.2026.S02E04", "1", 55)

        labels = [button["text"] for button in self._buttons()]
        self.assertTrue(any("S02" in label for label in labels), labels)

    def test_work_label_shows_season_years(self):
        label = TelegramBotService._work_label(_video_candidate_season_years())
        self.assertIn("S01 2024", label)
        self.assertIn("S02 2026", label)

    def test_title_variants_strip_year(self):
        self.assertIn("狂王", TelegramBotService._title_variants("狂王 2026"))

    def test_year_note_reports_filtering(self):
        """年份筛掉了同名作品时要说明，避免用户以为条目消失"""
        older = dict(_video_candidate(), item_id="vl0", date_air="2019-01-01")
        newer = dict(_video_candidate(), item_id="vl1", date_air="2024-05-01")
        _FakeUploadService._current = _FakeUploadService([older, newer])
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王 2024", "1", 55)

        self.assertIn("已按年份 2024 优先筛选", self.keyboards[-1][0])

    def test_year_note_reports_fallback(self):
        """年份一个都对不上时退回全部候选并说明，而不是清空结果"""
        _FakeUploadService._current = _FakeUploadService([_video_candidate()])
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(self._context(), "狂王 2099", "1", 55)

        self.assertIn("没有年份为 2099 的候选", self.keyboards[-1][0])
        self.assertTrue(
            any(
                str(button.get("callback_data") or "").startswith("fx:work:")
                for button in self._buttons()
            )
        )

    def test_fallback_to_context_title(self):
        """回复关键词搜不到时，用报错文件标题再搜一次"""
        _FakeUploadService._current = _FakeUploadService(
            [_video_candidate()], titles={"狂王"}
        )
        context = self._context()
        context["title"] = "狂王"
        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ):
            self.service._handle_correction(context, "查无此剧", "1", 55)

        self.assertTrue(any("狂王" in button["text"] for button in self._buttons()))


class TestOnlinePickSeasonYear(unittest.TestCase):
    """在线识别目标选择（OnlineUploadService._pick_from_candidates）也要认季级年份"""

    def test_pick_uses_season_year(self):
        from src.video_organizer.core.online_upload import OnlineUploadService

        match = OnlineUploadService._pick_from_candidates(
            [_video_candidate_season_years()], 2, 4, "tv", year=2026, title="狂王"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["item_id"], "ve24")
        self.assertEqual(match["season_number"], 2)
        self.assertEqual(match["episode_number"], 4)


class TestMissingFileNotifications(unittest.TestCase):
    """本地文件已不存在时不再推送报错 / 定时提醒"""

    def _service(self):
        service = TelegramBotService()
        service._token = "bot-token"
        service._chat_id = "1"
        service._enabled = True
        service.sent = []
        service.send_text = lambda text, reply_to=None, chat_id=None: (
            service.sent.append(text) or len(service.sent)
        )
        return service

    def test_notify_error_skips_missing_file(self):
        service = self._service()
        ok = service.notify_error(
            {
                "file_path": "/media/does-not-exist.mkv",
                "file_name": "does-not-exist.mkv",
            },
            "boom",
        )
        self.assertFalse(ok)
        self.assertEqual(service.sent, [])
        self.assertEqual(service._active_errors, {})

    def test_notify_error_sends_for_existing_file(self):
        service = self._service()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.mkv"
            path.write_bytes(b"x")
            ok = service.notify_error(
                {"file_path": str(path), "file_name": "a.mkv"}, "boom"
            )
        self.assertTrue(ok)
        self.assertEqual(len(service.sent), 1)
        self.assertTrue(service._active_errors)

    def test_reminders_skip_and_clear_missing_file(self):
        service = self._service()
        service._active_errors["/media/gone.mkv|上传失败"] = {
            "context": {"file_path": "/media/gone.mkv", "file_name": "gone.mkv"},
            "error": "boom",
            "header": "上传失败",
            "last_sent": 0.0,
        }
        service._send_reminders()
        self.assertEqual(service.sent, [])
        self.assertEqual(service._active_errors, {})

    def test_error_notification_has_no_reply_hint_block(self):
        """报错通知不再附「回复本条消息即可修正目标」提示块"""
        service = self._service()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.mkv"
            path.write_bytes(b"x")
            service.notify_error(
                {"file_path": str(path), "file_name": "a.mkv"}, "boom"
            )
        body = service.sent[-1]
        self.assertNotIn("回复本条消息即可修正目标", body)
        self.assertNotIn("只发片名关键词", body)
        self.assertIn("原因：boom", body)

    def test_reminder_has_no_reply_hint_block(self):
        """定时提醒也不再附回复提示，只保留提醒本身"""
        service = self._service()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.mkv"
            path.write_bytes(b"x")
            service._active_errors[f"{path}|上传失败"] = {
                "context": {"file_path": str(path), "file_name": "a.mkv"},
                "error": "boom",
                "header": "上传失败",
                "last_sent": 0.0,
            }
            service._send_reminders()
        body = service.sent[-1]
        self.assertNotIn("回复本条消息", body)
        self.assertIn("仍未解决", body)


class TestQuickKeyboard(_BotHarness):
    def test_quick_reply_text_maps_to_command(self):
        calls = []
        self.service._handle_command = lambda command, *a: calls.append(command)
        self.service._handle_update(
            {
                "message": {
                    "text": "📤 上传文件",
                    "chat": {"id": 1},
                    "from": {"id": 1},
                }
            }
        )
        self.assertEqual(calls, ["/upload"])

    def test_help_sends_quick_keyboard(self):
        sent_keyboards = []

        def _fake_keyboard(chat_id=None, text=None):
            sent_keyboards.append(chat_id)

        self.service.send_quick_keyboard = _fake_keyboard
        self.service._handle_command("/help", "1", "1", [])
        self.assertEqual(sent_keyboards, ["1"])


class TestDownloaderCleanup(unittest.TestCase):
    def test_remove_downloader_tasks_reports_names(self):
        class _Monitor:
            def __init__(self, name, ok=True):
                self.name = name
                self.ok = ok
                self.calls = []

            def delete_download(self, file_path, delete_files=True):
                self.calls.append((file_path, delete_files))
                return self.ok

        aria = _Monitor("aria2")
        qb = _Monitor("qbittorrent", ok=False)
        removed = remove_downloader_tasks("/media/a.mkv", monitors=[aria, qb])

        self.assertEqual(removed, ["aria2"])
        self.assertEqual(aria.calls, [("/media/a.mkv", True)])

    def test_delete_reply_reports_downloader_cleanup(self):
        service = TelegramBotService()
        service._token = "bot-token"
        service._chat_id = "1"
        service._enabled = True
        sent = []
        service.send_text = lambda text, reply_to=None, chat_id=None: sent.append(text)
        service._active_errors["/media/a.mkv|未识别到条目"] = {"context": {}}
        context = {"file_path": "/media/a.mkv", "file_name": "a.mkv"}

        with patch(
            "src.video_organizer.core.online_upload.OnlineUploadService",
            _FakeUploadService,
        ), patch(
            "src.video_organizer.core.downloader_monitor.remove_downloader_tasks",
            return_value=["aria2", "qbittorrent"],
        ) as cleanup:
            _FakeUploadService._current = _FakeUploadService()
            service._handle_delete_reply(context, "1", 55)

        self.assertIn("已同步删除下载器任务：aria2、qbittorrent", sent[-1])
        # 只清下载器任务，不动本地文件（删文件走 /upload 里的删除键）
        cleanup.assert_called_once_with("/media/a.mkv", delete_files=False)


class TestBrowseGrid(_BotHarness):
    def test_browse_rows_are_uniform_three_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.mkv").write_bytes(b"x")
            (Path(tmp) / "sub").mkdir()
            fake = _FakeUploadService()
            fake.roots = lambda: [tmp]
            fake.resolve = lambda raw: Path(str(raw))
            _FakeUploadService._current = fake
            with patch(
                "src.video_organizer.core.online_upload.OnlineUploadService",
                _FakeUploadService,
            ):
                self.service._send_browse("1", tmp)

        text, rows = self.keyboards[-1]
        self.assertGreaterEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(len(row), 3, f"行列数不一致：{row}")
        flat = [button["text"] for row in rows for button in row]
        self.assertTrue(any("上传全部" in label for label in flat))
        self.assertTrue(any("🗑️ 删除" in label for label in flat))


if __name__ == "__main__":
    unittest.main()
