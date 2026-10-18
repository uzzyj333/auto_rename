# -*- coding: utf-8 -*-
"""WebDAV 目录浏览客户端（Telegram「WebDAV 网盘」入口用）

只实现需求所需的最小能力：

1. ``PROPFIND`` 列出一个目录下的子目录 / 文件；
2. 拼出「可以直接交给 aria2 下载」的文件 URL（含账号认证头）。

文件路径在内部统一用「相对 WebDAV 服务器根的 POSIX 路径」（``/a/b/c.mkv``），
只在拼 URL 时才逐段做百分号编码，避免中文名 / 空格 / ``#`` 等字符出错。
"""

from __future__ import annotations

import base64
import logging
import posixpath
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

_DAV_NS = "{DAV:}"

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop>"
    "<d:displayname/>"
    "<d:getcontentlength/>"
    "<d:getlastmodified/>"
    "<d:resourcetype/>"
    "</d:prop>"
    "</d:propfind>"
)


class WebDAVError(Exception):
    """WebDAV 操作失败（配置缺失 / 网络异常 / 认证失败 / 目录不存在等）"""


def normalize_path(path: Any) -> str:
    """把任意路径统一成 ``/a/b`` 形式的 POSIX 路径（不含结尾斜杠）"""
    text = unquote(str(path or "")).replace("\\", "/").strip()
    if not text:
        return "/"
    if not text.startswith("/"):
        text = "/" + text
    parts: List[str] = []
    for piece in text.split("/"):
        if not piece or piece == ".":
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return "/" + "/".join(parts)


def _text(element: Optional[ElementTree.Element]) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _merge_props(node: ElementTree.Element) -> Dict[str, Any]:
    """把响应的多个 ``propstat`` 里的属性合并成一个字典（只取关心的几个）"""
    merged: Dict[str, Any] = {}
    candidates = node.findall(f"{_DAV_NS}propstat")
    if not candidates:
        candidates = [node]
    for propstat in candidates:
        prop = propstat.find(f"{_DAV_NS}prop")
        if prop is None:
            continue
        if "is_dir" not in merged:
            merged["is_dir"] = (
                prop.find(f"{_DAV_NS}resourcetype/{_DAV_NS}collection") is not None
            )
        if not merged.get("displayname"):
            merged["displayname"] = _text(prop.find(f"{_DAV_NS}displayname"))
        if not merged.get("size"):
            merged["size"] = _text(prop.find(f"{_DAV_NS}getcontentlength"))
        if not merged.get("modified"):
            merged["modified"] = _text(prop.find(f"{_DAV_NS}getlastmodified"))
    return merged


def parse_propfind(
    xml_text: str, base_path: str = "/", prefix: str = ""
) -> List[Dict[str, Any]]:
    """解析 PROPFIND 响应，返回该目录下的条目列表（不含目录自身）

    条目字段：``name``（显示名）/ ``path``（相对根的 POSIX 路径）/
    ``is_dir`` / ``size`` / ``modified``。``path`` 已 URL 解码，可直接用于展示。

    ``prefix`` 是配置的 WebDAV 地址里带的路径（例如 ``https://host/dav`` 的
    ``/dav``）：多数服务器返回的 ``href`` 含这段前缀，先去掉才能得到统一的
    「相对 WebDAV 根」路径。
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise WebDAVError(f"WebDAV 返回的目录数据无法解析：{exc}") from exc

    base = normalize_path(base_path)
    prefix = normalize_path(prefix) if str(prefix or "").strip() else "/"
    entries: List[Dict[str, Any]] = []
    for node in root.iter(f"{_DAV_NS}response"):
        href = _text(node.find(f"{_DAV_NS}href"))
        if not href:
            continue
        path = normalize_path(urlparse(href).path or "/")
        if prefix != "/":
            if path == prefix:
                path = "/"
            elif path.startswith(prefix + "/"):
                path = path[len(prefix) :]
            path = normalize_path(path)
        if path == base:
            # 第一个 response 通常是目录自身
            continue
        if base != "/" and path in ("", "/"):
            continue
        props = _merge_props(node)
        name = str(props.get("displayname") or "").strip()
        if not name:
            name = posixpath.basename(path.rstrip("/")) or path
        size_text = str(props.get("size") or "").strip()
        try:
            size = int(size_text)
        except (TypeError, ValueError):
            size = 0
        entries.append(
            {
                "name": name,
                "path": path,
                "is_dir": bool(props.get("is_dir")),
                "size": size,
                "modified": str(props.get("modified") or ""),
            }
        )
    return entries


class WebDAVClient:
    """极简 WebDAV 客户端：列目录 + 生成下载直链"""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        verify_ssl: bool = True,
        timeout: int = 30,
    ) -> None:
        text = str(base_url or "").strip().rstrip("/")
        if not text:
            raise WebDAVError("未配置 WebDAV 地址")
        if not re.match(r"^https?://", text, re.IGNORECASE):
            text = "http://" + text
        self.base_url = text
        self.username = str(username or "")
        self.password = str(password or "")
        self.verify_ssl = bool(verify_ssl)
        try:
            self.timeout = max(5, int(timeout or 30))
        except (TypeError, ValueError):
            self.timeout = 30

    @property
    def auth(self) -> Optional[tuple]:
        """requests 的 basic auth 参数（没配用户名时为 None）"""
        if not self.username:
            return None
        return (self.username, self.password)

    def auth_header(self) -> str:
        """给 aria2 用的 Authorization 头（没配用户名时返回空串）"""
        if not self.username:
            return ""
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def url_for(self, path: Any) -> str:
        """把内部 POSIX 路径拼成可下载的完整 URL（逐段百分号编码）"""
        norm = normalize_path(path)
        if norm == "/":
            return self.base_url + "/"
        quoted = "/".join(quote(piece, safe="") for piece in norm.strip("/").split("/"))
        return f"{self.base_url}/{quoted}"

    def list_dir(self, path: Any = "/") -> List[Dict[str, Any]]:
        """列出目录内容（PROPFIND Depth: 1）"""
        norm = normalize_path(path)
        url = self.url_for(norm)
        try:
            response = requests.request(
                "PROPFIND",
                url,
                headers={
                    "Depth": "1",
                    "Content-Type": "application/xml; charset=utf-8",
                },
                data=_PROPFIND_BODY.encode("utf-8"),
                auth=self.auth,
                verify=self.verify_ssl,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise WebDAVError(f"连接 WebDAV 失败：{exc}") from exc
        if response.status_code == 401:
            raise WebDAVError("WebDAV 认证失败，请检查用户名 / 密码")
        if response.status_code == 404:
            raise WebDAVError("WebDAV 目录不存在（404）")
        if response.status_code == 405:
            raise WebDAVError("该地址不支持 WebDAV（PROPFIND 405）")
        if response.status_code >= 400:
            raise WebDAVError(f"WebDAV 返回 HTTP {response.status_code}")
        return parse_propfind(
            response.text, norm, prefix=(urlparse(self.base_url).path or "")
        )


def client_from_config(config: Optional[Dict[str, Any]]) -> WebDAVClient:
    """按配置节 ``[webdav]`` 创建客户端（缺地址时抛 WebDAVError）"""
    section = (config or {}).get("webdav") or {}
    if not isinstance(section, dict):
        section = {}
    return WebDAVClient(
        base_url=str(section.get("base_url") or ""),
        username=str(section.get("username") or ""),
        password=str(section.get("password") or ""),
        verify_ssl=bool(section.get("verify_ssl", True)),
        timeout=int(section.get("timeout") or 30),
    )
