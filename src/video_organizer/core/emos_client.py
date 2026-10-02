# -*- coding: utf-8 -*-
"""
Emos 官方 API 客户端

依据官方 API 文档实现（https://www.postman.com/somebyteorg/emos/overview）：

* 在线识别：``/api/video/tree``、``/api/video/search``、``/api/video/getVideoId``
* 上传播放：``/api/upload/getUploadToken`` → ``/api/upload/multipart/{file_id}/presign``
  → ``/api/upload/multipart/{file_id}/complete`` → ``/api/upload/video/save``
* 内部入库：``/api/upload/video/saveInternal``

该模块只做 HTTP 调用，不依赖任何数据库。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".rmvb": "application/vnd.rn-realmedia-vbr",
    # 蓝光 / DVD 原盘镜像：Emos 侧按普通视频资源存储，MIME 用镜像的真实类型
    ".iso": "application/x-iso9660-image",
}

SUBTITLE_MIME_TYPES = {
    ".srt": "application/x-subrip",
    ".ass": "text/x-ssa",
    ".ssa": "text/x-ssa",
    ".vtt": "text/vtt",
    ".sub": "text/plain",
}

# 这些状态码允许重试（网络抖动 / 服务端限流）
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class EmosApiError(RuntimeError):
    """Emos 接口异常"""

    def __init__(self, message: str, status_code: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or ""


def normalize_base_url(base_url: str) -> str:
    """规范化 Emos 服务地址

    允许用户填写站点根地址（如 https://emos.best）；如果误填了结尾的 /api
    （官方 Postman 里的 {{url}} 习惯带 /api），这里会自动去掉，
    避免拼成 /api/api/... 导致 404。
    """
    base = str(base_url or "https://emos.best").strip().rstrip("/")
    if not base:
        base = "https://emos.best"
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    if path.lower().endswith("/api"):
        path = path[: -len("/api")].rstrip("/")
    if path != parts.path.rstrip("/"):
        base = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return base.rstrip("/") or base


def detect_video_mime(file_name: str, resource_type: str = "video") -> str:
    """根据文件扩展名推断 MIME 类型"""
    ext = os.path.splitext(str(file_name or ""))[1].lower()
    if resource_type == "subtitle":
        return SUBTITLE_MIME_TYPES.get(ext, "text/plain")
    return VIDEO_MIME_TYPES.get(ext, "video/octet-stream")


class EmosClient:
    """Emos 官方 API 客户端"""

    def __init__(
        self,
        base_url: str = "https://emos.best",
        auth_token: str = "",
        timeout: int = 30,
        user_agent: Optional[str] = None,
        max_retries: int = 3,
    ):
        self.base_url = normalize_base_url(base_url)
        self.auth_token = str(auth_token or "").strip()
        self.timeout = int(timeout or 30)
        self.max_retries = max(1, int(max_retries or 1))
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9",
                "user-agent": self.user_agent,
                "origin": self.base_url,
                "referer": self.base_url + "/",
            }
        )
        if self.auth_token:
            self.session.headers["authorization"] = f"Bearer {self.auth_token}"

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭底层会话"""
        try:
            self.session.close()
        except Exception:
            pass

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        expected: Optional[List[int]] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> requests.Response:
        """发送请求（带重试），返回原始响应"""
        expected = expected or [200, 201, 202, 204]
        retries = self.max_retries if max_retries is None else max(1, int(max_retries))
        url = self._build_url(path)
        request_headers = dict(headers or {})
        if json_body is not None:
            request_headers.setdefault("content-type", "application/json")

        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                response = self.session.request(
                    method.upper(),
                    url,
                    params=params,
                    data=json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                    if json_body is not None
                    else None,
                    headers=request_headers,
                    timeout=timeout or self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(min(attempt, 5))
                    continue
                raise EmosApiError(f"请求 Emos 失败: {method.upper()} {path}: {exc}") from exc

            if response.status_code in expected:
                return response

            body = ""
            try:
                body = (response.text or "").strip()[:1000]
            except Exception:
                body = ""
            message = (
                f"Emos 接口返回 HTTP {response.status_code}: "
                f"{method.upper()} {path}" + (f" - {body}" if body else "")
            )
            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                last_error = EmosApiError(message, response.status_code, body)
                time.sleep(min(attempt, 5))
                continue
            raise EmosApiError(message, response.status_code, body)

        raise EmosApiError(f"请求 Emos 失败: {method.upper()} {path}: {last_error}")

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        expected: Optional[List[int]] = None,
        timeout: Optional[int] = None,
    ) -> Any:
        response = self.request(
            method,
            path,
            params=params,
            json_body=json_body,
            expected=expected,
            timeout=timeout,
        )
        try:
            text = response.text
        finally:
            try:
                response.close()
            except Exception:
                pass
        if not text or not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            raise EmosApiError(f"解析 Emos 响应失败: {method.upper()} {path}") from exc

    @staticmethod
    def _clean_params(params: Dict[str, Any]) -> Dict[str, Any]:
        cleaned: Dict[str, Any] = {}
        for key, value in params.items():
            if value is None or value == "":
                continue
            cleaned[key] = value
        return cleaned

    @staticmethod
    def _parse_error_payload(exc: "EmosApiError") -> Dict[str, Any]:
        """把接口异常响应体解析成字典（便于识别「已上传过」等业务提示）"""
        body = str(getattr(exc, "body", "") or "").strip()
        if not body:
            return {}
        try:
            data = json.loads(body)
        except ValueError:
            return {"message": body}
        if isinstance(data, dict):
            return data
        return {"data": data}

    @staticmethod
    def is_already_uploaded(payload: Any) -> bool:
        """判断响应是否表示「该资源此前已上传过」

        Emos 对同一资源有一周内不允许重复上传的限制，命中时
        ``getUploadToken`` 返回 HTTP 422 且 message 形如
        「此资源您一周内上传过」，这类响应都应按「已存在」处理。
        """
        if not isinstance(payload, dict):
            return False
        if payload.get("existed") or payload.get("exists"):
            return True
        text = " ".join(
            str(payload.get(key) or "") for key in ("message", "msg", "error", "detail")
        )
        return "上传过" in text

    @staticmethod
    def _as_id(value: Any) -> Any:
        """Emos 的 item_id 在官方示例中是数字，尽量保持数字类型"""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        text = str(value or "").strip()
        if text.isdigit():
            return int(text)
        return text

    # ------------------------------------------------------------------
    # 在线识别 / 查询
    # ------------------------------------------------------------------

    def get_user_base(self) -> Dict[str, Any]:
        """获取当前用户简要信息"""
        data = self._json("GET", "/api/user/base")
        return data if isinstance(data, dict) else {}

    def get_video_tree(
        self,
        video_type: Optional[str] = None,
        title: Optional[str] = None,
        todb_id: Optional[Any] = None,
        tmdb_id: Optional[Any] = None,
        video_id: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """搜索视频目录树（在线识别的核心接口）"""
        params = self._clean_params(
            {
                "type": video_type,
                "title": title,
                "todb_id": todb_id,
                "tmdb_id": tmdb_id,
                "video_id": video_id,
            }
        )
        data = self._json("GET", "/api/video/tree", params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "items", "list", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    def search_videos(
        self,
        video_type: Optional[str] = None,
        title: Optional[str] = None,
        todb_id: Optional[Any] = None,
        tmdb_id: Optional[Any] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """搜索视频列表"""
        params = self._clean_params(
            {
                "type": video_type,
                "title": title,
                "todb_id": todb_id,
                "tmdb_id": tmdb_id,
                "with_media": 1,
                "page": page,
                "page_size": page_size,
            }
        )
        data = self._json("GET", "/api/video/search", params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "items", "list", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    def get_video_id(
        self,
        video_id_value: Any,
        video_id_type: str = "tmdb",
        tmdb_type: Optional[str] = None,
        season_number: Optional[Any] = None,
        episode_number: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """通过 TMDB/TODB ID 获取 Emos 的 item_type / item_id"""
        params = self._clean_params(
            {
                "video_id_type": video_id_type,
                "video_id_value": video_id_value,
                "tmdb_type": tmdb_type,
                "season_number": season_number,
                "episode_number": episode_number,
            }
        )
        data = self._json("GET", "/api/video/getVideoId", params=params)
        return data if isinstance(data, dict) else {}

    def get_video_base(self, item_type: str, item_id: Any) -> Dict[str, Any]:
        """获取上传目标的基本信息"""
        params = self._clean_params({"item_type": item_type, "item_id": self._as_id(item_id)})
        data = self._json("GET", "/api/upload/video/base", params=params)
        return data if isinstance(data, dict) else {}

    def get_seasons(self, video_id: Any) -> List[Dict[str, Any]]:
        """获取季列表"""
        data = self._json("GET", f"/api/video/{video_id}/season")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    def get_episodes(self, video_id: Any, season_number: Optional[Any] = None) -> List[Dict[str, Any]]:
        """获取集列表"""
        params = self._clean_params({"season_number": season_number})
        data = self._json("GET", f"/api/video/{video_id}/episode", params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------

    def get_upload_token(
        self,
        file_name: str,
        file_size: int,
        file_storage: str = "internal",
        resource_type: str = "video",
        file_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取上传凭证（返回 type / file_id / data）"""
        body = {
            "type": resource_type,
            "file_type": file_type or detect_video_mime(file_name, resource_type),
            "file_name": os.path.basename(str(file_name)),
            "file_size": int(file_size),
            "file_storage": file_storage or "internal",
        }
        try:
            data = self._json("POST", "/api/upload/getUploadToken", json_body=body)
        except EmosApiError as exc:
            payload = self._parse_error_payload(exc)
            if self.is_already_uploaded(payload):
                payload = dict(payload)
                payload["existed"] = True
                return payload
            raise
        if not isinstance(data, dict):
            raise EmosApiError("获取上传凭证失败: 响应格式异常")
        if not data.get("file_id"):
            if self.is_already_uploaded(data):
                data = dict(data)
                data["existed"] = True
                return data
            raise EmosApiError(
                "获取上传凭证失败: " + str(data.get("message") or data.get("msg") or data)
            )
        return data

    def multipart_presign(self, file_id: str, number: int) -> List[Dict[str, Any]]:
        """获取分片上传凭证"""
        data = self._json(
            "POST",
            f"/api/upload/multipart/{file_id}/presign",
            json_body={"number": int(number)},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        raise EmosApiError("获取分片上传凭证失败: 响应格式异常")

    def multipart_complete(self, file_id: str, parts: List[Dict[str, Any]]) -> Any:
        """合并分片"""
        return self._json(
            "POST",
            f"/api/upload/multipart/{file_id}/complete",
            json_body={"parts": parts},
        )

    def multipart_abort(self, file_id: str) -> Any:
        """取消分片上传"""
        return self._json("DELETE", f"/api/upload/multipart/{file_id}/abort")

    def save_video(
        self,
        item_type: str,
        item_id: Any,
        file_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """保存上传结果（将文件绑定到指定视频/剧集）"""
        body: Dict[str, Any] = {
            "item_type": item_type,
            "item_id": self._as_id(item_id),
            "file_id": file_id,
        }
        if metadata:
            body["file_metadata"] = metadata
        data = self._json(
            "POST",
            "/api/upload/video/save",
            json_body=body,
            expected=[200, 201, 204],
        )
        return data if isinstance(data, dict) else {}

    def save_internal(
        self,
        item_type: str,
        item_id: Any,
        path_type: str,
        file_path: str,
        file_size: int,
        upload_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """内部入库（文件已在 Emos 存储上时使用）"""
        body: Dict[str, Any] = {
            "item_type": item_type,
            "item_id": self._as_id(item_id),
            "path_type": path_type,
            "file_path": file_path,
            "file_size": int(file_size),
        }
        if upload_username:
            body["upload_username"] = upload_username
        data = self._json(
            "POST",
            "/api/upload/video/saveInternal",
            json_body=body,
            expected=[200, 201, 204],
        )
        return data if isinstance(data, dict) else {}

    def check_sign(self) -> Dict[str, Any]:
        """判断当前 token 是否已登录"""
        data = self._json("GET", "/api/sign/check")
        return data if isinstance(data, dict) else {}
