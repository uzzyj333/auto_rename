"""多下载器实例（多个 aria2）与上传并发数热更新测试。"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.config_loader import derive_downloader_type, load_config
from src.video_organizer.core.downloader_monitor import Aria2Monitor
from src.video_organizer.core.filesystem_monitor import FileSystemMonitor
from src.video_organizer.core.video_file_handler import VideoFileHandler
from src.video_organizer.web.routers.downloaders import _find_monitor


MULTI_DOWNLOADER_INI = """[monitoring]
watch_dir =
output_dir =
supported_extensions = .mp4,.mkv

[downloader.aria2]
type = aria2
name = 主 Aria2
rpc_url = http://127.0.0.1:6800/jsonrpc

[downloader.aria2_2]
type = aria2
name = 备用 Aria2
rpc_url = http://127.0.0.1:6801/jsonrpc

[downloader.qbittorrent_1]
rpc_url = http://127.0.0.1:8091/api/v2
username = admin
"""


class TestDownloaderTypeDerivation(unittest.TestCase):
    """downloader.<标识> 节名 -> 下载器类型"""

    def test_known_types(self):
        self.assertEqual(derive_downloader_type("aria2"), "aria2")
        self.assertEqual(derive_downloader_type("qbittorrent"), "qbittorrent")

    def test_numbered_instances(self):
        self.assertEqual(derive_downloader_type("aria2_2"), "aria2")
        self.assertEqual(derive_downloader_type("aria2-10"), "aria2")
        self.assertEqual(derive_downloader_type("ARIA2_3"), "aria2")

    def test_unknown_type_kept(self):
        self.assertEqual(derive_downloader_type("aria2x"), "aria2x")
        self.assertEqual(derive_downloader_type(""), "")


class TestMultiDownloaderConfig(unittest.TestCase):
    """同一类型多个实例的配置解析"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ini = Path(self.tmp.name) / "config.ini"
        self.ini.write_text(MULTI_DOWNLOADER_INI, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_multiple_instances(self):
        config = load_config(str(self.ini))
        downloaders = config["downloaders"]
        self.assertEqual(
            [d["type"] for d in downloaders], ["aria2", "aria2", "qbittorrent"]
        )
        self.assertEqual(
            [d["id"] for d in downloaders], ["aria2", "aria2_2", "qbittorrent_1"]
        )
        self.assertEqual(downloaders[0]["name"], "主 Aria2")
        self.assertEqual(downloaders[2]["section"], "downloader.qbittorrent_1")


class _TempDbMixin:
    """把测试用数据库写到临时目录，避免污染项目数据"""

    def _setup_temp_db(self):
        self._old_db = os.environ.get("VIDEO_ORGANIZER_DB_PATH")
        self._db_tmp = tempfile.TemporaryDirectory()
        os.environ["VIDEO_ORGANIZER_DB_PATH"] = str(
            Path(self._db_tmp.name) / "test.db"
        )

    def _teardown_temp_db(self):
        # 先释放 SQLite 连接，否则 Windows 下无法删除临时数据库文件
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
        except OSError:
            pass


class TestDownloaderMonitorHotReload(_TempDbMixin, unittest.TestCase):
    """在线修改下载器配置后无需重启容器即可生效"""

    def setUp(self):
        self._setup_temp_db()
        self.configs = [
            {"type": "aria2", "id": "aria2", "rpc_url": "http://127.0.0.1:6800/jsonrpc"},
            {"type": "aria2", "id": "aria2_2", "rpc_url": "http://127.0.0.1:6801/jsonrpc"},
        ]
        self.monitor = FileSystemMonitor(
            watch_path=str(Path(self._db_tmp.name) / "watch"),
            processed_path=str(Path(self._db_tmp.name) / "out"),
            tmdb_api_key="",
            supported_extensions=[".mp4"],
            downloader_configs=list(self.configs),
            config={"monitoring": {}, "downloaders": list(self.configs)},
        )

    def tearDown(self):
        self.monitor.stop()
        self._teardown_temp_db()

    def test_monitors_created_per_instance(self):
        ids = [m.id for m in self.monitor.downloader_monitors]
        self.assertEqual(ids, ["aria2", "aria2_2"])
        self.assertEqual(
            [m.id for m in self.monitor.event_handler.downloaders], ids
        )
        self.assertIsNot(
            self.monitor.downloader_monitors[0], self.monitor.downloader_monitors[1]
        )

    def test_reload_unchanged_config_keeps_instances(self):
        before = [id(m) for m in self.monitor.downloader_monitors]
        self.monitor.reload_downloader_monitors(list(self.configs))
        self.assertEqual([id(m) for m in self.monitor.downloader_monitors], before)

    def test_reload_applies_add_and_remove(self):
        added = list(self.configs) + [
            {"type": "aria2", "id": "aria2_3", "rpc_url": "http://127.0.0.1:6802/jsonrpc"}
        ]
        monitors = self.monitor.reload_downloader_monitors(added)
        self.assertEqual([m.id for m in monitors], ["aria2", "aria2_2", "aria2_3"])

        monitors = self.monitor.reload_downloader_monitors([self.configs[0]])
        self.assertEqual([m.id for m in monitors], ["aria2"])

    def test_display_name_does_not_break_addressing(self):
        named = [
            {"type": "aria2", "id": "aria2", "name": "同名", "rpc_url": "http://127.0.0.1:6800/jsonrpc"},
            {"type": "aria2", "id": "aria2_2", "name": "同名", "rpc_url": "http://127.0.0.1:6801/jsonrpc"},
        ]
        monitors = self.monitor.reload_downloader_monitors(named)
        self.assertEqual([m.name for m in monitors], ["同名", "同名"])
        self.assertIs(_find_monitor(monitors, "aria2_2"), monitors[1])
        self.assertIs(_find_monitor(monitors, "aria2"), monitors[0])


class TestSupportedExtensionsHotReload(_TempDbMixin, unittest.TestCase):
    """在线修改「支持的扩展名」后立即生效（以前改完不重启容器不生效）"""

    def setUp(self):
        self._setup_temp_db()
        self.monitor = FileSystemMonitor(
            watch_path=str(Path(self._db_tmp.name) / "watch"),
            processed_path=str(Path(self._db_tmp.name) / "out"),
            tmdb_api_key="",
            supported_extensions=[".mp4", ".mkv"],
            downloader_configs=[
                {"type": "aria2", "id": "aria2", "rpc_url": "http://127.0.0.1:6800/jsonrpc"}
            ],
            config={"monitoring": {}, "downloaders": []},
        )

    def tearDown(self):
        try:
            self.monitor.event_handler.stop_upload_queue()
        except Exception:
            pass
        self.monitor.stop()
        self._teardown_temp_db()

    def test_monitor_updates_handler_and_downloaders(self):
        self.monitor.update_supported_extensions([".mp4", ".mkv", ".TS", " .srt "])

        expected = [".mp4", ".mkv", ".ts", ".srt"]
        self.assertEqual(self.monitor.supported_extensions, expected)
        self.assertEqual(
            self.monitor.event_handler.supported_extensions, expected
        )
        self.assertEqual(
            self.monitor.downloader_monitors[0].supported_extensions,
            tuple(expected),
        )

    def test_empty_extensions_are_ignored(self):
        self.monitor.update_supported_extensions([])
        self.monitor.update_supported_extensions(None)
        self.assertEqual(self.monitor.supported_extensions, [".mp4", ".mkv"])

    def test_handler_apply_config_updates_extensions(self):
        handler = self.monitor.event_handler
        handler.apply_config(
            {
                "monitoring": {"supported_extensions": [".mkv", ".ISO", ".srt"]},
                "processing": {"max_upload_workers": 1},
                "emos": {},
            }
        )

        self.assertEqual(handler.supported_extensions, [".mkv", ".iso", ".srt"])
        self.assertTrue(handler._is_supported_file("剧集.S01E01.iso"))
        self.assertTrue(handler._is_subtitle_file("剧集.S01E01.srt"))


class TestFindMonitor(unittest.TestCase):
    """按实例名称 / 类型定位下载器（API 用）"""

    def setUp(self):
        self.monitors = [
            Aria2Monitor(lambda path: None, name="aria2"),
            Aria2Monitor(lambda path: None, name="aria2_2"),
        ]

    def test_match_by_instance_name(self):
        self.assertIs(_find_monitor(self.monitors, "aria2_2"), self.monitors[1])

    def test_match_by_type_returns_first(self):
        self.assertIs(_find_monitor(self.monitors, "aria2"), self.monitors[0])

    def test_unknown_identifier(self):
        self.assertIsNone(_find_monitor(self.monitors, "qbittorrent"))
        self.assertIsNone(_find_monitor(self.monitors, ""))


class TestUploadWorkerHotReload(_TempDbMixin, unittest.TestCase):
    """在线修改「同时上传数量」后立即生效"""

    def setUp(self):
        self._setup_temp_db()
        self.handler = VideoFileHandler(
            output_dir=self._db_tmp.name,
            supported_extensions=[".mp4"],
            processing_config={"max_upload_workers": 1},
            config={
                "processing": {"max_upload_workers": 1},
                "monitoring": {},
                "emos": {},
            },
        )

    def tearDown(self):
        self.handler.stop_upload_queue()
        self._teardown_temp_db()

    def _apply_workers(self, value):
        self.handler.apply_config(
            {
                "processing": {"max_upload_workers": value},
                "monitoring": {},
                "emos": {},
            }
        )

    def _wait_workers(self, expected, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.handler.get_upload_worker_count() == expected:
                return
            time.sleep(0.2)

    def test_worker_count_follows_config(self):
        self.assertEqual(self.handler.get_upload_worker_count(), 1)

        self._apply_workers(4)
        self.assertEqual(self.handler.max_upload_workers, 4)
        self.assertEqual(self.handler.get_upload_worker_count(), 4)

        self._apply_workers(2)
        self._wait_workers(2)
        self.assertEqual(self.handler.get_upload_worker_count(), 2)

        self._apply_workers(1)
        self._wait_workers(1)
        self.assertEqual(self.handler.get_upload_worker_count(), 1)

    def test_invalid_value_falls_back_to_default(self):
        self._apply_workers("abc")
        self.assertEqual(self.handler.max_upload_workers, 3)
        self.assertEqual(self.handler.get_upload_worker_count(), 3)


if __name__ == "__main__":
    unittest.main()
