# -*- coding: utf-8 -*-
"""Emos 视频上传器（官方 API 实现）

上传流程（对应官方 API 文档 https://www.postman.com/somebyteorg/emos/overview）::

    GET  /api/upload/video/base                 # 目标信息（标题等）
    POST /api/upload/getUploadToken             # 获取上传凭证
    PUT  <presigned-url>                        # google_drive 直传 / multipart 分片
    POST /api/upload/multipart/{file_id}/complete
    POST /api/upload/video/save                 # 绑定到 item_type / item_id

同时支持：

* 分片上传（R2 等对象存储）与 Google Drive 断点续传直传
* 上传进度回调（Web 仪表盘 + Telegram 通知）
"""

from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from ..core.emos_client import EmosApiError, EmosClient

logger = logging.getLogger(__name__)

# Telegram Bot API 根地址（如需自建反代可修改此处）
_TG_API_BASE = "https://api.telegram.org"

ProgressCallback = Callable[[float, int, int, str], None]

DEFAULT_CHUNK_MB = 50
MIN_CHUNK_MB = 10
MAX_CHUNK_MB = 200
MAX_PARTS = 1000


def _report_upload_progress(
    file_path: str,
    filename: str,
    uploader: str,
    progress: float,
    uploaded_bytes: int,
    total_bytes: int,
    speed: str = "",
    status: str = "uploading",
    error: Optional[str] = None,
) -> None:
    """上报上传进度到 Web 状态管理器（Web 未启用时静默忽略）"""
    try:
        from ..web.services.state import report_upload_progress

        report_upload_progress(
            file_path=file_path,
            filename=filename,
            uploader=uploader,
            progress=progress,
            uploaded_bytes=uploaded_bytes,
            total_bytes=total_bytes,
            speed=speed,
            status=status,
            error=error,
        )
    except Exception:
        pass


def format_size(bytes_size: float) -> str:
    """格式化文件大小"""
    try:
        size = float(bytes_size)
    except (TypeError, ValueError):
        return "-"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} TB"


def format_speed(bytes_per_second: float) -> str:
    """格式化上传速度"""
    try:
        speed = float(bytes_per_second)
    except (TypeError, ValueError):
        return "-"
    if speed <= 0:
        return "-"
    return f"{format_size(speed)}/s"


class RobustEmosVideoUploader:
    """Emos 视频上传器"""

    def __init__(
        self,
        auth_token: str,
        base_url: str = "https://emos.best",
        chunk_size_mb: int = DEFAULT_CHUNK_MB,
        telegram_config: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        max_retries: int = 3,
        progress_callback: Optional[ProgressCallback] = None,
        **_legacy_kwargs,
    ):
        self.client = EmosClient(
            base_url=base_url,
            auth_token=auth_token,
            timeout=timeout,
            max_retries=max_retries,
        )
        self.session = self.client.session
        self.base_url = self.client.base_url
        try:
            chunk = int(chunk_size_mb)
        except (TypeError, ValueError):
            chunk = DEFAULT_CHUNK_MB
        self.chunk_size_mb = max(MIN_CHUNK_MB, min(MAX_CHUNK_MB, chunk))
        self.timeout = int(timeout or 60)
        self.max_retries = max(1, int(max_retries or 1))
        self.progress_callback = progress_callback

        telegram_config = telegram_config or {}
        self.tg_bot_token = str(telegram_config.get("bot_token", "") or "").strip()
        self.tg_chat_id = str(telegram_config.get("chat_id", "") or "").strip()
        self._tg_message_id: Optional[int] = None
        self._tg_last_update = 0.0
        self._tg_interval = 3.0
        self.last_error: str = ""  # 最近一次失败原因（供调用方展示 / 报错通知使用）

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def upload_video(
        self,
        file_path: str,
        item_type: str,
        item_id: Any,
        file_storage: str = "internal",
        enable_resume: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """上传视频并绑定到指定的 Emos 条目

        Args:
            file_path: 本地视频文件路径
            item_type: Emos 条目类型（vl / ve ...）
            item_id: Emos 条目 ID
            file_storage: 存储位置（internal / global / google_drive ...）
            enable_resume: 兼容旧参数，分片上传本身支持失败重试
            metadata: 额外写入的 file_metadata

        Returns:
            成功返回结果字典，失败返回 None
        """
        path = Path(file_path)
        if not path.is_file():
            logger.error("上传文件不存在: %s", file_path)
            return None

        file_size = path.stat().st_size
        file_name = path.name
        started_at = time.time()
        self._report(str(path), file_name, 0, 0, file_size, "", "uploading")

        try:
            base_info = self.client.get_video_base(item_type, item_id)
            title = str(base_info.get("title") or "")
            if title:
                logger.info("上传目标: %s (%s/%s)", title, item_type, item_id)

            token = self.client.get_upload_token(
                file_name=file_name,
                file_size=file_size,
                file_storage=file_storage,
                resource_type="video",
            )

            token_data = token.get("data") if isinstance(token.get("data"), dict) else {}
            token_type = str(token.get("type") or "").lower()
            file_id = str(token.get("file_id") or "")
            existed = bool(token.get("existed")) or "之前上传过" in str(token.get("message") or "")

            if not file_id:
                if existed:
                    self._report(str(path), file_name, 100, file_size, file_size, "", "completed")
                    return {"file_id": "", "media_id": "", "skipped": True, "existed": True}
                raise EmosApiError("上传凭证缺少 file_id")

            if existed:
                logger.info("该资源此前已上传过，跳过文件传输直接入库: %s", file_name)
            elif token_type == "multipart" or (not token_data.get("upload_url") and token_data.get("multipart_size")):
                self._upload_multipart(path, token, file_size)
            elif token_data.get("upload_url"):
                # 官方 API 里 internal / global / google_drive 等存储都会返回 upload_url
                self._upload_google_drive(path, token, file_size)
            else:
                raise EmosApiError(f"不支持的上传方式: {token_type or '未知'}")

            self._report(str(path), file_name, 97, file_size, file_size, "", "uploading")
            save_result = self.client.save_video(item_type, item_id, file_id, metadata)

            elapsed = max(time.time() - started_at, 0.001)
            self._report(
                str(path),
                file_name,
                100,
                file_size,
                file_size,
                format_speed(file_size / elapsed),
                "completed",
            )
            self._tg_finish(file_name, title)

            result: Dict[str, Any] = dict(save_result or {})
            result.setdefault("file_id", file_id)
            result["title"] = title
            logger.info("上传完成: %s -> %s", file_name, result.get("media_id"))
            return result

        except Exception as exc:
            logger.error("上传失败: %s - %s", file_name, exc)
            self._report(str(path), file_name, 0, 0, file_size, "", "failed", str(exc))
            self._tg_finish(file_name, "", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # 分片上传
    # ------------------------------------------------------------------

    def _multipart_part_size(self, token: Dict[str, Any]) -> int:
        """计算分片大小（遵循服务端 min/max 限制）"""
        part_size = self.chunk_size_mb * 1024 * 1024
        limits = (token.get("data") or {}).get("multipart_size") or {}
        try:
            min_size = int(limits.get("min") or 0)
            max_size = int(limits.get("max") or 0)
        except (TypeError, ValueError):
            min_size, max_size = 0, 0
        if min_size > 0:
            part_size = max(part_size, min_size)
        if max_size > 0:
            part_size = min(part_size, max_size)
        return max(part_size, 1024 * 1024)

    def _upload_multipart(self, path: Path, token: Dict[str, Any], file_size: int) -> None:
        file_id = str(token.get("file_id"))
        part_size = self._multipart_part_size(token)
        number = max(1, math.ceil(file_size / part_size))
        while number > MAX_PARTS:
            part_size *= 2
            number = max(1, math.ceil(file_size / part_size))

        presigns = self.client.multipart_presign(file_id, number)
        by_number: Dict[int, Dict[str, Any]] = {}
        for index, item in enumerate(presigns):
            if not isinstance(item, dict):
                continue
            try:
                key = int(item.get("number") or index + 1)
            except (TypeError, ValueError):
                key = index + 1
            by_number[key] = item

        if not by_number:
            raise EmosApiError("获取分片上传凭证失败: 未返回任何分片")

        parts: List[Dict[str, Any]] = []
        uploaded_bytes = 0
        started_at = time.time()

        with open(path, "rb") as handle:
            for part_number in sorted(by_number):
                item = by_number[part_number]
                offset = (part_number - 1) * part_size
                length = min(part_size, file_size - offset)
                if length <= 0:
                    continue
                upload_url = item.get("upload_url") or item.get("url")
                if not upload_url:
                    raise EmosApiError(f"分片 {part_number} 缺少上传地址")

                handle.seek(offset)
                payload = handle.read(length)
                etag = self._put_part(upload_url, payload, length, part_number, number)
                parts.append({"number": part_number, "etag": etag})

                uploaded_bytes += length
                progress = 10 + 85 * (uploaded_bytes / file_size) if file_size else 95
                elapsed = max(time.time() - started_at, 0.001)
                self._report(
                    str(path),
                    path.name,
                    progress,
                    uploaded_bytes,
                    file_size,
                    format_speed(uploaded_bytes / elapsed),
                    "uploading",
                )

        if not parts:
            raise EmosApiError("没有可用的分片数据")

        parts.sort(key=lambda part: part["number"])
        self.client.multipart_complete(file_id, parts)

    def _put_part(self, upload_url: str, payload: bytes, length: int, part_number: int, total_parts: int) -> str:
        """上传单个分片，返回 ETag"""
        headers = {
            "Content-Length": str(length),
            "Content-Type": "application/octet-stream",
        }
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.put(
                    upload_url,
                    data=payload,
                    headers=headers,
                    timeout=max(self.timeout, 600),
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(attempt, 5))
                    continue
                raise EmosApiError(f"分片 {part_number}/{total_parts} 上传失败: {exc}") from exc

            try:
                status = response.status_code
                etag = (
                    response.headers.get("ETag")
                    or response.headers.get("Etag")
                    or response.headers.get("etag")
                    or ""
                ).strip().strip('"')
            finally:
                try:
                    response.close()
                except Exception:
                    pass

            if 200 <= status < 300:
                if not etag:
                    raise EmosApiError(f"分片 {part_number}/{total_parts} 响应缺少 ETag")
                return etag

            last_error = EmosApiError(f"分片 {part_number}/{total_parts} 上传失败: HTTP {status}")
            if status in {408, 425, 429, 500, 502, 503, 504} and attempt < self.max_retries:
                time.sleep(min(attempt, 5))
                continue
            raise last_error

        raise EmosApiError(f"分片 {part_number}/{total_parts} 上传失败: {last_error}")

    # ------------------------------------------------------------------
    # Google Drive 直传（支持断点续传）
    # ------------------------------------------------------------------

    def _upload_google_drive(self, path: Path, token: Dict[str, Any], file_size: int) -> None:
        upload_url = str((token.get("data") or {}).get("upload_url") or "")
        if not upload_url:
            raise EmosApiError("Google Drive 上传地址缺失")

        offset = 0
        started_at = time.time()
        for _ in range(5):
            status, headers = self._put_google_range(upload_url, path, offset, file_size, started_at)
            if 200 <= status < 300:
                return
            if status == 308:
                next_offset = self._parse_range_header(headers.get("Range"))
                if next_offset is None or next_offset <= offset:
                    raise EmosApiError("Google Drive 上传中断，且未返回有效断点")
                offset = next_offset
                continue
            raise EmosApiError(f"Google Drive 上传失败: HTTP {status}")

        raise EmosApiError("Google Drive 上传多次中断，已放弃")

    def _put_google_range(
        self,
        upload_url: str,
        path: Path,
        offset: int,
        file_size: int,
        started_at: float,
    ):
        """上传 [offset, file_size) 区间的数据，返回 (status, headers)"""
        headers = {
            "Content-Length": str(max(file_size - offset, 0)),
            "Content-Type": "application/octet-stream",
        }
        if offset:
            headers["Content-Range"] = f"bytes {offset}-{file_size - 1}/{file_size}"

        with open(path, "rb") as handle:
            handle.seek(offset)
            reader = _ProgressReader(
                handle,
                offset,
                file_size,
                lambda uploaded: self._report(
                    str(path),
                    path.name,
                    10 + 85 * (uploaded / file_size) if file_size else 95,
                    uploaded,
                    file_size,
                    format_speed(uploaded / max(time.time() - started_at, 0.001)),
                    "uploading",
                ),
            )
            try:
                response = self.session.put(
                    upload_url,
                    data=reader,
                    headers=headers,
                    timeout=max(self.timeout, 600),
                )
            except requests.exceptions.RequestException as exc:
                raise EmosApiError(f"Google Drive 上传失败: {exc}") from exc
            try:
                payload = {
                    "Location": response.headers.get("Location", ""),
                    "Range": response.headers.get("Range", ""),
                }
                return response.status_code, payload
            finally:
                try:
                    response.close()
                except Exception:
                    pass

    @staticmethod
    def _parse_range_header(value: Optional[str]) -> Optional[int]:
        """解析 ``bytes=0-1023`` / ``bytes */1024`` 形式的断点"""
        if not value:
            return None
        text = str(value).split("=", 1)[-1].strip()
        if text.startswith("*"):
            return 0
        end = text.split("-", 1)[-1].strip()
        try:
            return int(end) + 1
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # 进度上报
    # ------------------------------------------------------------------

    def _report(
        self,
        file_path: str,
        file_name: str,
        progress: float,
        uploaded: int,
        total: int,
        speed: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        progress = max(0.0, min(100.0, float(progress)))
        if error:
            self.last_error = str(error)
        _report_upload_progress(
            file_path=file_path,
            filename=file_name,
            uploader="emos",
            progress=progress,
            uploaded_bytes=int(uploaded),
            total_bytes=int(total),
            speed=speed,
            status=status,
            error=error,
        )
        if self.progress_callback:
            try:
                self.progress_callback(progress, int(uploaded), int(total), status)
            except Exception:
                pass
        self._tg_update(file_name, progress, uploaded, total, speed, status, error)

    # ------------------------------------------------------------------
    # Telegram 通知（失败不影响主流程）
    # ------------------------------------------------------------------

    def _tg_update(self, file_name, progress, uploaded, total, speed, status, error=None) -> None:
        if not self.tg_bot_token or not self.tg_chat_id:
            return
        now = time.time()
        if status == "uploading" and now - self._tg_last_update < self._tg_interval:
            return
        if status == "uploading" and progress < 1:
            return
        self._tg_last_update = now
        if status == "completed":
            text = f"✅ Emos 上传完成\n文件: `{file_name}`\n大小: {format_size(total)}"
        elif status == "failed":
            text = f"❌ Emos 上传失败\n文件: `{file_name}`\n原因: {error or '未知错误'}"
        else:
            text = (
                f"📤 Emos 上传中\n文件: `{file_name}`\n"
                f"进度: {progress:.1f}% ({format_size(uploaded)}/{format_size(total)})\n速度: {speed or '-'}"
            )
        self._tg_finish(None, None, text=text)

    def _tg_finish(self, file_name, title, text: Optional[str] = None, error: Optional[str] = None) -> None:
        if not self.tg_bot_token or not self.tg_chat_id:
            return
        if text is None:
            if error:
                text = f"❌ Emos 上传失败\n文件: `{file_name}`\n原因: {error}"
            else:
                text = f"✅ Emos 上传完成\n文件: `{file_name}`" + (f"\n标题: {title}" if title else "")
        try:
            if self._tg_message_id is None:
                url = f"{_TG_API_BASE}/bot{self.tg_bot_token}/sendMessage"
                payload = {"chat_id": self.tg_chat_id, "text": text, "parse_mode": "Markdown"}
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    body = response.json()
                    if body.get("ok"):
                        self._tg_message_id = body["result"]["message_id"]
            else:
                url = f"{_TG_API_BASE}/bot{self.tg_bot_token}/editMessageText"
                payload = {
                    "chat_id": self.tg_chat_id,
                    "message_id": self._tg_message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                }
                requests.post(url, json=payload, timeout=10)
        except Exception:
            # Telegram 通知失败不影响上传
            pass


class _ProgressReader:
    """包装文件对象，按读取进度回调（用于直传上报进度）"""

    def __init__(self, handle, offset: int, total: int, callback):
        self._handle = handle
        self._read = offset
        self._total = total
        self._callback = callback
        self._last_report = 0.0

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        if data:
            self._read += len(data)
            now = time.time()
            if now - self._last_report >= 0.5 or self._read >= self._total:
                self._last_report = now
                try:
                    self._callback(self._read)
                except Exception:
                    pass
        return data

    def __len__(self) -> int:
        return max(self._total - (self._read - len(b"")), 0)
