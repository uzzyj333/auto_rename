"""在线识别上传「单独补传字幕」的回归测试

视频已经上传并删除原文件后，本地往往只剩一个外挂字幕（例如从内封多字幕里抽出的
``..._track9_chi.ass``）。这条链路允许直接把字幕单独传到指定条目：
不跑 ffprobe、不走视频上传，只调 ``/api/upload/subtitle/save``。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.online_upload import OnlineUploadService


class _InlineExecutor:
    """把后台上传改成同步执行，方便断言"""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class TestOnlineUploadSubtitleTask(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = OnlineUploadService()
        self.service.configure(
            {
                "emos": {"auth_token": "t", "base_url": "https://emos.best"},
                "processing": {"delete_after_upload": False},
                "online_upload": {"video_root": str(self.root)},
            }
        )
        self.service._executor = _InlineExecutor()

    def tearDown(self):
        self._tmp.cleanup()

    def _subtitle(self, name="Show.S01E01_track9_chi.ass"):
        path = self.root / name
        path.write_bytes(b"[Script Info]\n")
        return path

    def test_subtitle_task_calls_subtitle_upload_only(self):
        path = self._subtitle()
        calls = {}

        class _FakeUploader:
            def __init__(self, **kwargs):
                calls["kwargs"] = kwargs

            def upload_subtitle(self, file_path, item_type, item_id, storage):
                calls["subtitle"] = (file_path, item_type, item_id, storage)
                return {"subtitle_id": "s1", "kind": "subtitle"}

            def upload_video(self, *args, **kwargs):
                raise AssertionError("字幕任务不应调用视频上传")

            def close(self):
                calls["closed"] = True

        with patch(
            "src.video_organizer.core.online_upload.RobustEmosVideoUploader",
            _FakeUploader,
        ):
            task = self.service.create_task(
                {
                    "file_path": str(path),
                    "item_type": "ve",
                    "item_id": "42",
                    "storage": "internal",
                }
            )
        final = self.service.get_task(task["id"])

        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["stage"].startswith("字幕已上传"))
        self.assertEqual(final["media_id"], "s1")
        self.assertEqual(calls["subtitle"], (str(path), "ve", "42", "internal"))
        self.assertTrue(calls["closed"])
        self.assertTrue(path.exists())

    def test_subtitle_probe_is_skipped(self):
        """字幕文件不需要 ffprobe，也不该因为探测失败而报错"""
        path = self._subtitle()
        probed = []

        class _FakeUploader:
            def __init__(self, **kwargs):
                pass

            def upload_subtitle(self, file_path, item_type, item_id, storage):
                return {"subtitle_id": "s2"}

            def upload_video(self, *args, **kwargs):
                raise AssertionError("字幕任务不应调用视频上传")

            def close(self):
                pass

        with patch(
            "src.video_organizer.core.online_upload.RobustEmosVideoUploader",
            _FakeUploader,
        ), patch.object(self.service, "probe", lambda *a, **k: probed.append(a) or {}):
            task = self.service.create_task(
                {"file_path": str(path), "item_type": "ve", "item_id": "42"}
            )

        self.assertEqual(self.service.get_task(task["id"])["status"], "completed")
        self.assertEqual(probed, [])


if __name__ == "__main__":
    unittest.main()
