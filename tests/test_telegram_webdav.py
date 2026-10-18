# -*- coding: utf-8 -*-
"""Telegram「WebDAV 网盘」入口的回归测试

覆盖：PROPFIND 解析（地址带路径前缀 / URL 编码 / 中文名）、下载直链拼接、
aria2.addUri 参数拼装（token / header / dir / out），以及 /webdav 浏览键盘
与「点选文件即推送 aria2 下载」的流程。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.video_organizer.core import downloader_monitor as dm
from src.video_organizer.core.telegram_bot import TelegramBotService
from src.video_organizer.core.webdav_client import (
    WebDAVClient,
    WebDAVError,
    normalize_path,
    parse_propfind,
    client_from_config,
)

_PROPFIND_XML = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/dav/tv/</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:displayname>tv</D:displayname>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/dav/tv/Season%201/</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:displayname>Season 1</D:displayname>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/dav/tv/%E6%97%B6%E5%85%89%E4%BB%A3%E7%90%86%E4%BA%BA.S01E01.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:displayname>时光代理人.S01E01.mkv</D:displayname>
        <D:getcontentlength>1234567</D:getcontentlength>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
"""


class _FakeResponse:
    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload if payload is not None else {}
        self.text = text
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise dm.requests.HTTPError(f"HTTP {self.status_code}")


class TestWebDAVParsing(unittest.TestCase):
    def test_parse_propfind_strips_base_path_prefix(self):
        entries = parse_propfind(_PROPFIND_XML, "/tv", prefix="/dav")
        self.assertEqual(len(entries), 2)
        folder, video = entries
        self.assertTrue(folder["is_dir"])
        self.assertEqual(folder["name"], "Season 1")
        self.assertEqual(folder["path"], "/tv/Season 1")
        self.assertFalse(video["is_dir"])
        self.assertEqual(video["path"], "/tv/时光代理人.S01E01.mkv")
        self.assertEqual(video["size"], 1234567)

    def test_parse_propfind_without_prefix(self):
        xml = _PROPFIND_XML.replace("/dav/tv", "/tv")
        entries = parse_propfind(xml, "/tv")
        self.assertEqual(
            [item["path"] for item in entries],
            ["/tv/Season 1", "/tv/时光代理人.S01E01.mkv"],
        )

    def test_normalize_path_blocks_parent_escape(self):
        self.assertEqual(normalize_path("/tv/../secret"), "/secret")
        self.assertEqual(normalize_path("tv/season 1/"), "/tv/season 1")
        self.assertEqual(normalize_path(""), "/")

    def test_url_for_quotes_each_segment(self):
        client = WebDAVClient("https://dav.example.com/dav")
        self.assertEqual(
            client.url_for("/tv/时光代理人 01.mkv"),
            "https://dav.example.com/dav/tv/"
            "%E6%97%B6%E5%85%89%E4%BB%A3%E7%90%86%E4%BA%BA%2001.mkv",
        )
        self.assertEqual(client.url_for("/"), "https://dav.example.com/dav/")

    def test_auth_header_from_credentials(self):
        client = WebDAVClient("https://dav.example.com", "u", "p")
        self.assertEqual(client.auth_header(), "Basic dTpw")
        self.assertEqual(client.auth, ("u", "p"))
        self.assertEqual(WebDAVClient("https://dav.example.com").auth_header(), "")

    def test_client_from_config_requires_base_url(self):
        with self.assertRaises(WebDAVError):
            client_from_config({"webdav": {"enabled": True}})

    def test_list_dir_sends_propfind_depth_1(self):
        captured = {}

        def fake_request(
            method,
            url,
            headers=None,
            data=None,
            auth=None,
            verify=None,
            timeout=None,
            **kwargs,
        ):
            captured.update(
                method=method, url=url, headers=headers, data=data, auth=auth
            )
            return _FakeResponse(text=_PROPFIND_XML)

        with patch(
            "src.video_organizer.core.webdav_client.requests.request", fake_request
        ):
            entries = WebDAVClient("https://dav.example.com/dav", "u", "p").list_dir(
                "/tv"
            )

        self.assertEqual(captured["method"], "PROPFIND")
        self.assertEqual(captured["url"], "https://dav.example.com/dav/tv")
        self.assertEqual(captured["headers"]["Depth"], "1")
        self.assertEqual(captured["auth"], ("u", "p"))
        self.assertEqual(len(entries), 2)


class TestAria2Push(unittest.TestCase):
    _CONFIG = {
        "downloaders": [
            {"id": "aria2", "type": "aria2", "rpc_url": "http://a:6800/jsonrpc"},
            {
                "id": "aria2_2",
                "type": "aria2",
                "name": "备用 Aria2",
                "rpc_url": "http://b:6800/jsonrpc",
                "secret": "s2",
            },
            {"id": "qbit", "type": "qbittorrent", "rpc_url": "http://c/api/v2"},
        ]
    }

    def test_pick_configured_instance(self):
        instance = dm.find_downloader_instance(self._CONFIG, "aria2_2", "aria2")
        self.assertEqual(instance["rpc_url"], "http://b:6800/jsonrpc")
        # 未指定时用第一个 aria2（而不是 qbittorrent）
        fallback = dm.find_downloader_instance(self._CONFIG, "", "aria2")
        self.assertEqual(fallback["id"], "aria2")
        self.assertIsNone(dm.find_downloader_instance({}, "", "aria2"))

    def test_add_uri_payload_carries_secret_and_options(self):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            calls.append((url, json))
            return _FakeResponse({"result": "gid-1"})

        with patch.object(dm.requests, "post", fake_post):
            gid = dm.aria2_add_uri(
                self._CONFIG["downloaders"][1],
                ["https://dav.example.com/dav/a.mkv"],
                {"out": "a.mkv"},
            )

        self.assertEqual(gid, "gid-1")
        url, payload = calls[0]
        self.assertEqual(url, "http://b:6800/jsonrpc")
        self.assertEqual(payload["method"], "aria2.addUri")
        self.assertEqual(
            payload["params"],
            [
                "token:s2",
                ["https://dav.example.com/dav/a.mkv"],
                {"out": "a.mkv"},
            ],
        )

    def test_add_uri_raises_on_aria2_error(self):
        with patch.object(
            dm.requests,
            "post",
            lambda *a, **k: _FakeResponse({"error": {"message": "Unauthorized"}}),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                dm.aria2_add_uri({"rpc_url": "http://a/jsonrpc"}, ["http://x"])
        self.assertIn("Unauthorized", str(ctx.exception))

    def test_push_uri_without_aria2_instance(self):
        with self.assertRaises(RuntimeError):
            dm.push_uri_to_aria2({"downloaders": []}, "https://x/a.mkv")


class TestTelegramWebDAVFlow(unittest.TestCase):
    def setUp(self):
        self.service = TelegramBotService()
        self.service._chat_id = "1"

    def _config(self, **webdav):
        section = {
            "enabled": True,
            "base_url": "https://dav.example.com/dav",
            "root_path": "/tv",
        }
        section.update(webdav)
        return {
            "webdav": section,
            "downloaders": [
                {"id": "aria2", "type": "aria2", "rpc_url": "http://a:6800/jsonrpc"}
            ],
        }

    def test_disabled_entry_explains_how_to_enable(self):
        self.service._config = {"webdav": {"enabled": False}, "downloaders": []}
        sent = []
        with patch.object(
            TelegramBotService, "send_text", lambda self, text, **kw: sent.append(text)
        ):
            self.service._send_webdav_browse("1")
        self.assertIn("未开启", sent[0])

    def test_browse_keyboard_lists_dirs_and_files(self):
        self.service._config = self._config()
        entries = [
            {
                "name": "Season 1",
                "path": "/tv/Season 1",
                "is_dir": True,
                "size": 0,
                "modified": "",
            },
            {
                "name": "时光代理人.S01E01.mkv",
                "path": "/tv/时光代理人.S01E01.mkv",
                "is_dir": False,
                "size": 1234567,
                "modified": "",
            },
            {
                "name": "readme.txt",
                "path": "/tv/readme.txt",
                "is_dir": False,
                "size": 10,
                "modified": "",
            },
        ]
        captured = {}
        sent = []
        with patch.object(
            WebDAVClient, "list_dir", lambda self, path="/": entries
        ), patch.object(
            TelegramBotService,
            "_send_keyboard",
            lambda self, chat_id, text, rows, edit=None: captured.update(
                rows=rows, text=text
            ),
        ), patch.object(
            TelegramBotService, "send_text", lambda self, text, **kw: sent.append(text)
        ):
            self.service._send_webdav_browse("1", "/tv")

        callbacks = [cell["callback_data"] for row in captured["rows"] for cell in row]
        self.assertTrue(any(cb.startswith("wd:ls:") for cb in callbacks))
        self.assertTrue(any(cb.startswith("wd:get:") for cb in callbacks))
        # 默认只列视频/字幕：txt 不出现
        self.assertIn("时光代理人.S01E01.mkv", captured["text"])
        self.assertNotIn("readme.txt", captured["text"])

    def test_browse_keeps_path_inside_configured_root(self):
        self.service._config = self._config()
        listed = []

        def fake_list(self, path="/"):
            listed.append(path)
            return []

        with patch.object(WebDAVClient, "list_dir", fake_list), patch.object(
            TelegramBotService, "_send_keyboard", lambda *a, **k: None
        ), patch.object(TelegramBotService, "send_text", lambda *a, **k: None):
            self.service._send_webdav_browse("1", "/other/secret")

        self.assertEqual(listed, ["/tv"])

    def test_download_pushes_uri_with_auth_header_and_dir(self):
        self.service._config = self._config(
            username="u",
            password="p",
            save_dir="/downloads",
            downloader="aria2_2",
        )
        self.service._config["downloaders"].append(
            {
                "id": "aria2_2",
                "type": "aria2",
                "rpc_url": "http://b:6800/jsonrpc",
                "secret": "s2",
            }
        )
        calls = []
        sent = []

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            calls.append((url, json))
            return _FakeResponse({"result": "gid-9"})

        with patch.object(dm.requests, "post", fake_post), patch.object(
            TelegramBotService, "send_text", lambda self, text, **kw: sent.append(text)
        ):
            self.service._webdav_download("/tv/时光代理人.S01E01.mkv", "1")

        url, payload = calls[0]
        self.assertEqual(url, "http://b:6800/jsonrpc")
        self.assertEqual(payload["params"][0], "token:s2")
        self.assertEqual(
            payload["params"][1],
            [
                "https://dav.example.com/dav/tv/"
                "%E6%97%B6%E5%85%89%E4%BB%A3%E7%90%86%E4%BA%BA.S01E01.mkv"
            ],
        )
        options = payload["params"][2]
        self.assertEqual(options["out"], "时光代理人.S01E01.mkv")
        self.assertEqual(options["dir"], "/downloads")
        self.assertEqual(options["header"], ["Authorization: Basic dTpw"])
        self.assertIn("gid-9", sent[0])

    def test_download_without_aria2_reports_reason(self):
        self.service._config = {
            "webdav": {"enabled": True, "base_url": "https://dav.example.com"},
            "downloaders": [],
        }
        sent = []
        with patch.object(
            TelegramBotService, "send_text", lambda self, text, **kw: sent.append(text)
        ):
            self.service._webdav_download("/tv/a.mkv", "1")
        self.assertIn("aria2", sent[0])


def _dir_entry(path):
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "is_dir": True,
        "size": 0,
        "modified": "",
    }


def _file_entry(path):
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "is_dir": False,
        "size": 10,
        "modified": "",
    }


class _FakeWebDAVClient:
    """按预设目录表返回条目的假客户端（只实现批量下载用到的方法）"""

    def __init__(self, tree=None):
        self.tree = tree or {}
        self.verify_ssl = True

    def list_dir(self, path="/"):
        return list(self.tree.get(path, []))

    def url_for(self, path):
        return f"https://dav.example.com/dav{path}"

    def auth_header(self):
        return ""


class TestWebDAVBatchPush(unittest.TestCase):
    def setUp(self):
        self.service = TelegramBotService()
        self.service._chat_id = "1"
        self.sent = []
        self.service._config = {
            "webdav": {
                "enabled": True,
                "base_url": "https://dav.example.com/dav",
                "root_path": "/tv",
                "save_dir": "/downloads",
            },
            "downloaders": [
                {"id": "aria2", "type": "aria2", "rpc_url": "http://a:6800/jsonrpc"}
            ],
        }

    def _send_text(self):
        sent = self.sent
        return patch.object(
            TelegramBotService,
            "send_text",
            lambda _self, text, **kw: sent.append(text),
        )

    def test_collect_dir_ignores_subfolders_and_txt(self):
        client = _FakeWebDAVClient(
            {
                "/tv": [
                    _dir_entry("/tv/Season 1"),
                    _file_entry("/tv/a.mkv"),
                    _file_entry("/tv/readme.txt"),
                ]
            }
        )
        files = self.service._webdav_collect(client, "/tv", "dir", False)
        self.assertEqual([item["name"] for item in files], ["a.mkv"])

    def test_collect_tree_walks_subfolders(self):
        client = _FakeWebDAVClient(
            {
                "/tv": [_dir_entry("/tv/Season 1"), _file_entry("/tv/a.mkv")],
                "/tv/Season 1": [
                    _file_entry("/tv/Season 1/b.mkv"),
                    _file_entry("/tv/Season 1/c.txt"),
                ],
            }
        )
        files = self.service._webdav_collect(client, "/tv", "tree", False)
        self.assertEqual(sorted(item["name"] for item in files), ["a.mkv", "b.mkv"])

    def test_browse_keyboard_offers_batch_download(self):
        entries = [_dir_entry("/tv/Season 1"), _file_entry("/tv/a.mkv")]
        captured = {}
        with patch.object(
            WebDAVClient, "list_dir", lambda self, path="/": entries
        ), patch.object(
            TelegramBotService,
            "_send_keyboard",
            lambda _self, chat_id, text, rows, edit=None: captured.update(rows=rows),
        ), self._send_text():
            self.service._send_webdav_browse("1", "/tv")
        callbacks = [cell["callback_data"] for row in captured["rows"] for cell in row]
        self.assertTrue(
            any(cb.startswith("wd:push:") and cb.endswith(":dir") for cb in callbacks)
        )
        self.assertTrue(
            any(cb.startswith("wd:push:") and cb.endswith(":tree") for cb in callbacks)
        )

    def test_confirm_push_shows_count_and_confirm_button(self):
        client = _FakeWebDAVClient(
            {"/tv": [_file_entry("/tv/a.mkv"), _file_entry("/tv/b.mkv")]}
        )
        captured = {}
        with patch(
            "src.video_organizer.core.webdav_client.client_from_config",
            lambda config: client,
        ), patch.object(
            TelegramBotService,
            "_send_keyboard",
            lambda _self, chat_id, text, rows, edit=None: captured.update(
                text=text, rows=rows
            ),
        ), self._send_text():
            self.service._webdav_confirm_push("1", "/tv", "dir")
        self.assertIn("2 个文件", captured["text"])
        callbacks = [cell["callback_data"] for row in captured["rows"] for cell in row]
        self.assertTrue(
            any(cb.startswith("wd:pushok:") and cb.endswith(":dir") for cb in callbacks)
        )
        self.assertIn("wd:pushcancel", callbacks)

    def test_batch_push_sends_every_file_to_aria2(self):
        client = _FakeWebDAVClient(
            {
                "/tv": [_dir_entry("/tv/Season 1"), _file_entry("/tv/a.mkv")],
                "/tv/Season 1": [_file_entry("/tv/Season 1/b.mkv")],
            }
        )
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            calls.append(json)
            return _FakeResponse({"result": f"gid-{len(calls)}"})

        with patch(
            "src.video_organizer.core.webdav_client.client_from_config",
            lambda config: client,
        ), patch.object(dm.requests, "post", fake_post), self._send_text():
            self.service._webdav_push_batch("/tv", "tree", "1")

        self.assertEqual(len(calls), 2)
        urls = sorted(call["params"][0][0] for call in calls)
        self.assertEqual(
            urls,
            [
                "https://dav.example.com/dav/tv/Season 1/b.mkv",
                "https://dav.example.com/dav/tv/a.mkv",
            ],
        )
        for call in calls:
            self.assertEqual(call["method"], "aria2.addUri")
            self.assertEqual(call["params"][1]["dir"], "/downloads")
        self.assertIn("成功 2 个", self.sent[0])

    def test_callback_routes_batch_actions(self):
        started = []

        class _RecordingThread:
            def __init__(self, target=None, args=(), name=None, daemon=None, **kwargs):
                started.append((target, args))

            def start(self):
                return None

        token = self.service._token_for_path("/tv")
        with patch(
            "src.video_organizer.core.telegram_bot.threading.Thread", _RecordingThread
        ), patch.object(
            TelegramBotService, "_answer_callback", lambda *a, **k: None
        ), patch.object(
            TelegramBotService, "_send_keyboard", lambda *a, **k: None
        ):
            for data in (
                f"wd:push:{token}:tree",
                f"wd:pushok:{token}:tree",
                "wd:pushcancel",
            ):
                self.service._handle_callback_query(
                    {
                        "id": "q",
                        "data": data,
                        "message": {"message_id": 5, "chat": {"id": "1"}},
                        "from": {"id": "9"},
                    }
                )

        names = [target.__name__ for target, _args in started]
        self.assertEqual(names, ["_webdav_confirm_push", "_webdav_push_batch"])
        self.assertEqual(started[0][1][2], "tree")
        self.assertEqual(started[1][1][1], "tree")


if __name__ == "__main__":
    unittest.main()
