"""qBittorrent 删除任务回归测试

修复点：
1. 用户回复「删除」时必须真正调用 /torrents/delete，而不是静默 AttributeError；
2. 同一文件匹配到多个种子时全部删除；
3. 路径映射 / URL 编码后的文件名仍能匹配；
4. 上传完成后的自动清理不再因路径归一化不一致而误判“还有未处理视频”。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core.downloader_monitor import QBittorrentMonitor


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, torrents, files, info_statuses=None):
        self.torrents = torrents
        self.files = files
        self.info_statuses = list(info_statuses or [])
        self.cookies = {}
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, timeout=None):
        self.get_calls.append((url, dict(params or {})))
        if url.endswith("/torrents/info"):
            status = self.info_statuses.pop(0) if self.info_statuses else 200
            return _Response(status, self.torrents)
        if url.endswith("/torrents/files"):
            torrent_hash = str((params or {}).get("hash") or "")
            return _Response(200, self.files.get(torrent_hash, []))
        return _Response(404)

    def post(self, url, data=None, timeout=None):
        self.post_calls.append((url, dict(data or {})))
        if url.endswith("/auth/login"):
            return _Response(200, text="Ok.")
        return _Response(200, text="")


class _Callback:
    def __call__(self, file_path):
        return None


class TestQBittorrentDelete(unittest.TestCase):
    def _monitor(self, session):
        monitor = QBittorrentMonitor(
            callback=_Callback(), rpc_url="http://qb:8080/api/v2"
        )
        monitor.session = session
        return monitor

    def test_delete_download_removes_all_matching_torrents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Show S01E01.mkv"
            path.write_bytes(b"x")
            torrents = [
                {"hash": "h1", "name": "Show", "save_path": "/downloads"},
                {"hash": "h2", "name": "Show", "save_path": "/downloads"},
            ]
            files = {
                "h1": [{"name": "Show%20S01E01.mkv"}],
                "h2": [{"name": "Show%20S01E01.mkv"}],
            }
            session = _FakeSession(torrents, files)
            monitor = self._monitor(session)
            monitor.path_mappings = {"/downloads": tmp}

            result = monitor.delete_download(str(path), delete_files=False)

        self.assertTrue(result)
        delete_calls = [
            (url, data)
            for url, data in session.post_calls
            if url.endswith("/torrents/delete")
        ]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1]["hashes"], "h1|h2")
        self.assertEqual(delete_calls[0][1]["deleteFiles"], "false")

    def test_remove_download_deletes_after_upload_with_mapped_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Show S01E01.mkv"
            path.write_bytes(b"x")
            torrents = [{"hash": "h1", "name": "Show", "save_path": "/downloads"}]
            files = {"h1": [{"name": "Show%20S01E01.mkv"}]}
            session = _FakeSession(torrents, files)
            monitor = self._monitor(session)
            monitor.path_mappings = {"/downloads": tmp}

            result = monitor.remove_download(str(path))

        self.assertTrue(result)
        delete_calls = [
            (url, data)
            for url, data in session.post_calls
            if url.endswith("/torrents/delete")
        ]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1]["hashes"], "h1")
        self.assertEqual(delete_calls[0][1]["deleteFiles"], "true")

    def test_force_remove_deletes_all_matching_torrents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Show S01E01.mkv"
            path.write_bytes(b"x")
            torrents = [
                {"hash": "h1", "name": "Show", "save_path": "/downloads"},
                {"hash": "h2", "name": "Show", "save_path": "/downloads"},
            ]
            files = {
                "h1": [{"name": "Show%20S01E01.mkv"}],
                "h2": [{"name": "Show%20S01E01.mkv"}],
            }
            session = _FakeSession(torrents, files)
            monitor = self._monitor(session)
            monitor.path_mappings = {"/downloads": tmp}
            monitor._processed_files.add(str(path))

            result = monitor.force_remove_download(str(path))

        self.assertTrue(result)
        delete_calls = [
            (url, data)
            for url, data in session.post_calls
            if url.endswith("/torrents/delete")
        ]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1]["hashes"], "h1|h2")
        self.assertEqual(delete_calls[0][1]["deleteFiles"], "true")

    def test_delete_download_relogins_after_session_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Show S01E01.mkv"
            path.write_bytes(b"x")
            torrents = [{"hash": "h1", "name": "Show", "save_path": tmp}]
            files = {"h1": [{"name": "Show S01E01.mkv"}]}
            session = _FakeSession(torrents, files, info_statuses=[403, 200])
            monitor = self._monitor(session)

            result = monitor.delete_download(str(path), delete_files=False)

        self.assertTrue(result)
        login_calls = [
            url for url, _ in session.post_calls if url.endswith("/auth/login")
        ]
        self.assertEqual(len(login_calls), 1)


if __name__ == "__main__":
    unittest.main()
