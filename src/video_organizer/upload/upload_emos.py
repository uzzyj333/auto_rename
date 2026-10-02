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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from ..core.emos_client import EmosApiError, EmosClient, detect_video_mime

logger = logging.getLogger(__name__)

# Telegram Bot API 根地址（如需自建反代可修改此处）
_TG_API_BASE = "https://api.telegram.org"

# 同一文件的 Telegram 消息 id 缓存：文件失败后每分钟会重试一次，
# 复用同一条消息（编辑）而不是每次新建一条，避免一直报错时刷屏
_TG_MESSAGE_LOCK = threading.Lock()
_TG_MESSAGE_IDS: Dict[str, int] = {}
_TG_MESSAGE_IDS_LIMIT = 200


def _remember_tg_message(key: str, message_id: int) -> None:
    with _TG_MESSAGE_LOCK:
        _TG_MESSAGE_IDS[key] = message_id
        while len(_TG_MESSAGE_IDS) > _TG_MESSAGE_IDS_LIMIT:
            _TG_MESSAGE_IDS.pop(next(iter(_TG_MESSAGE_IDS)), None)


def _recall_tg_message(key: str) -> Optional[int]:
    with _TG_MESSAGE_LOCK:
        return _TG_MESSAGE_IDS.get(key)


ProgressCallback = Callable[[float, int, int, str], None]

DEFAULT_CHUNK_MB = 50
MIN_CHUNK_MB = 10
MAX_CHUNK_MB = 200
MAX_PARTS = 1000
DEFAULT_UPLOAD_CONCURRENCY = 10
MIN_UPLOAD_CONCURRENCY = 1
MAX_UPLOAD_CONCURRENCY = 32


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


def format_time(seconds: float) -> str:
    """格式化时间（用于「已用时间 / 剩余时间」显示）"""
    try:
        value = max(float(seconds), 0.0)
    except (TypeError, ValueError):
        return "-"
    if value < 60:
        return f"{value:.1f}秒"
    if value < 3600:
        return f"{int(value // 60)}分{value % 60:.1f}秒"
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    return f"{hours}时{minutes}分{value % 60:.1f}秒"


def progress_bar(progress: float, length: int = 20) -> str:
    """生成进度条（█ 已完成 / ░ 未完成）"""
    try:
        ratio = max(0.0, min(1.0, float(progress) / 100.0))
    except (TypeError, ValueError):
        ratio = 0.0
    filled = int(round(length * ratio))
    return "█" * filled + "░" * (length - filled)


class RobustEmosVideoUploader:
    """Emos 视频上传器"""

    def __init__(
        self,
        auth_token: str,
        base_url: str = "https://emos.best",
        chunk_size_mb: int = DEFAULT_CHUNK_MB,
        upload_concurrency: int = DEFAULT_UPLOAD_CONCURRENCY,
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
        # 直传对象存储 / Google Drive 时必须使用「干净」的会话：
        # Emos 的 Authorization / origin / referer 等头一旦带到预签名 URL 上，
        # 对象存储会认为同时提供了两种鉴权方式（Authorization 头 + 签名查询参数），
        # 从而直接返回 HTTP 400，表现为「分片 1/N 上传失败: HTTP 400」。
        self.storage_session = requests.Session()
        try:
            chunk = int(chunk_size_mb)
        except (TypeError, ValueError):
            chunk = DEFAULT_CHUNK_MB
        self.chunk_size_mb = max(MIN_CHUNK_MB, min(MAX_CHUNK_MB, chunk))
        try:
            concurrency = int(upload_concurrency)
        except (TypeError, ValueError):
            concurrency = DEFAULT_UPLOAD_CONCURRENCY
        self.upload_concurrency = max(
            MIN_UPLOAD_CONCURRENCY, min(MAX_UPLOAD_CONCURRENCY, concurrency)
        )
        # 连接池按分片并发数放大：requests 默认 pool_maxsize=10，并发数超过它时
        # 连接会被丢弃重建，表现为「并发调高了速度也上不去」
        adapter = HTTPAdapter(
            pool_connections=self.upload_concurrency,
            pool_maxsize=self.upload_concurrency,
        )
        self.storage_session.mount("http://", adapter)
        self.storage_session.mount("https://", adapter)
        self.timeout = int(timeout or 60)
        self.max_retries = max(1, int(max_retries or 1))
        self.progress_callback = progress_callback

        telegram_config = telegram_config or {}
        self.tg_bot_token = str(telegram_config.get("bot_token", "") or "").strip()
        self.tg_chat_id = str(telegram_config.get("chat_id", "") or "").strip()
        self._tg_message_id: Optional[int] = None
        self._tg_last_update = 0.0
        self._tg_interval = 3.0
        # Telegram 进度消息交给后台线程发送（网络请求不能拖慢分片上传），
        # 终态消息同步发送，保证最后一条一定是上传结果
        self._tg_lock = threading.Lock()
        self._tg_send_lock = threading.Lock()
        self._tg_pending: Optional[str] = None
        self._tg_worker: Optional[threading.Thread] = None
        self._tg_finalized = False
        self._tg_base_text: Optional[str] = None  # 最近一条进度文本（结果追加在它后面）
        self._tg_message_key = ""  # 同文件复用同一条 Telegram 消息
        # 进度上下文（用于 Telegram 进度消息中的分片 / 速度 / 剩余时间）
        self._progress_started_at = 0.0
        self._progress_parts_done = 0
        self._progress_total_parts = 0
        self.last_error: str = ""  # 最近一次失败原因（供调用方展示 / 报错通知使用）
        self._skip_reason: str = ""  # 命中「已上传过」跳过传输时的说明

    def close(self) -> None:
        """释放底层会话"""
        try:
            self.storage_session.close()
        except Exception:
            pass
        self.client.close()

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
        self._progress_started_at = started_at
        self._progress_parts_done = 0
        self._progress_total_parts = 0
        self._skip_reason = ""
        self._tg_finalized = False
        self._tg_base_text = None
        self._tg_message_key = f"{self.tg_chat_id}:{file_name}"
        self._tg_message_id = _recall_tg_message(self._tg_message_key)
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
            existed = EmosClient.is_already_uploaded(token)

            if not file_id:
                if existed:
                    reason = str(
                        token.get("message") or token.get("msg") or "该资源此前已上传过"
                    )
                    if self._episode_has_media(
                        item_type, item_id, file_name, file_size
                    ):
                        # 目标条目下确实已经有这份文件，跳过传输直接算完成
                        self._skip_reason = reason
                        logger.info(
                            "跳过上传（目标条目已存在该文件）: %s - %s",
                            file_name,
                            reason,
                        )
                        self._report(
                            str(path),
                            file_name,
                            100,
                            file_size,
                            file_size,
                            "",
                            "completed",
                        )
                        return {
                            "file_id": "",
                            "media_id": "",
                            "skipped": True,
                            "existed": True,
                        }
                    # 目标条目下找不到这份文件，说明是此前失败的上传尝试在 Emos 侧
                    # 留下的记录（Emos 对同一资源有一周内不允许重复上传的限制）。
                    # 这既不能算上传成功，短时间内重试也没有意义，标记为「暂不重试」。
                    detail = (
                        f"Emos 限制：{reason}；但目标条目下没有找到该文件，"
                        "很可能是此前失败的上传尝试留下的记录，需等限制解除后再试"
                    )
                    logger.warning(
                        "上传被 Emos 拒绝且目标条目无该文件: %s - %s", file_name, detail
                    )
                    self._report(
                        str(path), file_name, 0, 0, file_size, "", "failed", detail
                    )
                    return {"deferred": True, "reason": detail}
                raise EmosApiError("上传凭证缺少 file_id")

            if existed:
                self._skip_reason = str(
                    token.get("message") or token.get("msg") or "该资源此前已上传过"
                )
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

    def _episode_has_media(
        self, item_type: str, item_id: Any, file_name: str, file_size: int
    ) -> bool:
        """查询目标条目下是否已经存在这份文件

        用于区分「真的已经上传过」和「Emos 因此前失败的尝试而拒绝重复上传」。
        查询失败时按「不存在」处理（宁可让用户确认，也不要误报上传成功）。
        """
        try:
            base_info = self.client.get_video_base(item_type, item_id)
        except Exception as exc:
            logger.warning("查询目标条目媒体列表失败: %s", exc)
            return False
        medias = base_info.get("video_medias")
        if not isinstance(medias, list):
            return False
        for media in medias:
            if not isinstance(media, dict):
                continue
            name = str(media.get("media_name") or "").strip()
            if name and name == file_name:
                return True
            try:
                size = int(media.get("media_file_size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size and size == int(file_size):
                return True
        return False

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
        file_type = detect_video_mime(path.name, "video")
        if presigns and isinstance(presigns[0], dict):
            first = presigns[0]
            host = urlsplit(
                str(first.get("upload_url") or first.get("url") or "")
            ).netloc
            if host:
                logger.info(
                    "分片上传目标: %s（共 %d 片，每片 %s）",
                    host,
                    number,
                    format_size(part_size),
                )
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

        total_parts = len(by_number)
        workers = max(1, min(self.upload_concurrency, total_parts))
        logger.info("分片上传并发数: %d", workers)

        parts: List[Dict[str, Any]] = []
        uploaded_bytes = 0
        started_at = time.time()
        lock = threading.Lock()
        self._progress_started_at = started_at
        self._progress_total_parts = total_parts
        self._progress_parts_done = 0

        def upload_one(
            part_number: int, item: Dict[str, Any]
        ) -> Optional[Dict[str, Any]]:
            """上传单个分片（每个分片独立打开文件，避免多线程共用句柄）"""
            nonlocal uploaded_bytes
            offset = (part_number - 1) * part_size
            length = min(part_size, file_size - offset)
            if length <= 0:
                return None
            upload_url = item.get("upload_url") or item.get("url")
            if not upload_url:
                raise EmosApiError(f"分片 {part_number} 缺少上传地址")

            with open(path, "rb") as handle:
                handle.seek(offset)
                payload = handle.read(length)
            etag = self._put_part(
                upload_url, payload, length, part_number, number, file_type
            )

            with lock:
                uploaded_bytes += length
                self._progress_parts_done += 1
                done = uploaded_bytes
            # 进度上报（含 Telegram / Web 推送）放在锁外，
            # 避免网络请求占着锁把其它分片线程一起堵住
            progress = 10 + 85 * (done / file_size) if file_size else 95
            elapsed = max(time.time() - started_at, 0.001)
            self._report(
                str(path),
                path.name,
                progress,
                done,
                file_size,
                format_speed(done / elapsed),
                "uploading",
            )
            return {"number": part_number, "etag": etag}

        ordered = sorted(by_number)
        if workers == 1:
            for part_number in ordered:
                result = upload_one(part_number, by_number[part_number])
                if result:
                    parts.append(result)
        else:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="emos-part"
            ) as pool:
                futures = [
                    pool.submit(upload_one, part_number, by_number[part_number])
                    for part_number in ordered
                ]
                try:
                    for future in as_completed(futures):
                        result = future.result()
                        if result:
                            parts.append(result)
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise

        if not parts:
            raise EmosApiError("没有可用的分片数据")

        parts.sort(key=lambda part: part["number"])
        self.client.multipart_complete(file_id, parts)

    def _put_part(
        self,
        upload_url: str,
        payload: bytes,
        length: int,
        part_number: int,
        total_parts: int,
        content_type: str,
    ) -> str:
        """上传单个分片，返回 ETag

        预签名 URL 只能使用干净会话（``storage_session``）：带上 Emos 的
        ``Authorization`` 头会被对象存储判定为「同时使用两种鉴权方式」而返回 400。
        """
        headers = {
            "Content-Length": str(length),
            "Content-Type": content_type,
        }
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.storage_session.put(
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
                body = "" if 200 <= status < 300 else self._response_snippet(response)
            finally:
                try:
                    response.close()
                except Exception:
                    pass

            if 200 <= status < 300:
                if not etag:
                    raise EmosApiError(f"分片 {part_number}/{total_parts} 响应缺少 ETag")
                return etag

            detail = f" - {body}" if body else ""
            last_error = EmosApiError(
                f"分片 {part_number}/{total_parts} 上传失败: HTTP {status}{detail}"
            )
            if status in {408, 425, 429, 500, 502, 503, 504} and attempt < self.max_retries:
                time.sleep(min(attempt, 5))
                continue
            raise last_error

        raise EmosApiError(f"分片 {part_number}/{total_parts} 上传失败: {last_error}")

    @staticmethod
    def _response_snippet(response: requests.Response, limit: int = 300) -> str:
        """截取响应体用于报错（对象存储返回的 XML/JSON 错误信息）"""
        try:
            raw = response.content[:limit]
        except Exception:
            return ""
        return raw.decode("utf-8", "replace").strip()

    # ------------------------------------------------------------------
    # Google Drive 直传（支持断点续传）
    # ------------------------------------------------------------------

    def _upload_google_drive(self, path: Path, token: Dict[str, Any], file_size: int) -> None:
        upload_url = str((token.get("data") or {}).get("upload_url") or "")
        if not upload_url:
            raise EmosApiError("Google Drive 上传地址缺失")

        offset = 0
        started_at = time.time()
        self._progress_started_at = started_at
        self._progress_total_parts = 0
        self._progress_parts_done = 0
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
            body = str(headers.get("Body") or "")
            detail = f" - {body}" if body else ""
            raise EmosApiError(f"Google Drive 上传失败: HTTP {status}{detail}")

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
                response = self.storage_session.put(
                    upload_url,
                    data=reader,
                    headers=headers,
                    timeout=max(self.timeout, 600),
                )
            except requests.exceptions.RequestException as exc:
                raise EmosApiError(f"Google Drive 上传失败: {exc}") from exc
            try:
                status = response.status_code
                body = (
                    ""
                    if 200 <= status < 300 or status == 308
                    else self._response_snippet(response)
                )
                payload = {
                    "Location": response.headers.get("Location", ""),
                    "Range": response.headers.get("Range", ""),
                    "Body": body,
                }
                return status, payload
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
        if status == "uploading":
            if progress < 1:
                return
            now = time.time()
            with self._tg_lock:
                if self._tg_finalized or now - self._tg_last_update < self._tg_interval:
                    return
                self._tg_last_update = now
            # 进度消息走后台线程发送，不阻塞正在上传的分片线程
            text = self._progress_text(file_name, progress, uploaded, total)
            self._tg_base_text = text
            self._tg_enqueue(text)
            return
        if status == "completed":
            if self._skip_reason:
                text = (
                    f"♻️ Emos 已存在该资源，跳过上传\n"
                    f"文件: `{file_name}`\n原因: {self._skip_reason}"
                )
            else:
                text = f"✅ 上传完成\n文件: `{file_name}`\n大小: {format_size(total)}"
        else:
            text = f"❌ Emos 上传失败\n文件: `{file_name}`\n原因: {error or '未知错误'}"
        self._tg_finish(None, None, text=text)

    def _tg_enqueue(self, text: str) -> None:
        """把进度消息交给后台线程发送（只保留最新一条，避免刷屏）"""
        with self._tg_lock:
            if self._tg_finalized:
                return
            self._tg_pending = text
            if self._tg_worker is None:
                self._tg_worker = threading.Thread(
                    target=self._tg_loop, name="emos-tg", daemon=True
                )
                self._tg_worker.start()

    def _tg_loop(self) -> None:
        """后台线程：串行发送进度消息，取不到待发文本即退出"""
        while True:
            with self._tg_lock:
                text = self._tg_pending
                self._tg_pending = None
                if text is None:
                    self._tg_worker = None
                    return
            with self._tg_send_lock:
                self._tg_send(text)

    def _progress_text(
        self, file_name: str, progress: float, uploaded: int, total: int
    ) -> str:
        """构建 Telegram 上传进度文本（进度条 + 分片 + 速度 + 剩余时间）"""
        uploaded = max(0, int(uploaded))
        total = max(0, int(total))
        elapsed = (
            max(time.time() - self._progress_started_at, 0.001)
            if self._progress_started_at
            else 0.0
        )
        average_speed = uploaded / elapsed if elapsed > 0 else 0.0
        remaining = max(total - uploaded, 0)
        remaining_time = remaining / average_speed if average_speed > 0 else 0.0

        lines = [
            "📤 *上传进度*",
            "",
            f"文件: `{file_name}`",
            f"进度: {progress:.1f}%",
            f"[{progress_bar(progress)}]",
            "",
        ]
        if self._progress_total_parts > 0:
            lines.append(
                f"分片: {self._progress_parts_done}/{self._progress_total_parts}"
            )
        lines.extend(
            [
                f"已上传: {format_size(uploaded)}",
                f"平均速度: {format_speed(average_speed)}",
                f"已用时间: {format_time(elapsed)}",
                f"剩余时间: {format_time(remaining_time)}",
            ]
        )
        return "\n".join(lines)

    def _tg_finish(self, file_name, title, text: Optional[str] = None, error: Optional[str] = None) -> None:
        if not self.tg_bot_token or not self.tg_chat_id:
            return
        if text is None:
            if error:
                block = f"❌ Emos 上传失败\n文件: `{file_name}`\n原因: {error}"
            else:
                block = f"✅ 上传完成\n文件: `{file_name}`" + (
                    f"\n标题: {title}" if title else ""
                )
        else:
            block = text
        # 上传过程中不覆盖进度消息，只在最后追加结果（进度条 / 速度信息保留下来）
        base = (self._tg_base_text or "").strip()
        full = f"{base}\n\n{block}" if base else block
        # 终态消息：丢弃还没发出的进度消息，等正在发送的那条发完再发，保证顺序
        with self._tg_lock:
            self._tg_finalized = True
            self._tg_pending = None
        with self._tg_send_lock:
            self._tg_send(full)

    def _tg_send(self, text: str) -> None:
        """实际发送 Telegram 消息（通知失败不影响上传流程）"""
        try:
            if self._tg_message_id is None:
                url = f"{_TG_API_BASE}/bot{self.tg_bot_token}/sendMessage"
                payload = {"chat_id": self.tg_chat_id, "text": text, "parse_mode": "Markdown"}
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    body = response.json()
                    if body.get("ok"):
                        self._tg_message_id = body["result"]["message_id"]
                        if self._tg_message_key:
                            _remember_tg_message(
                                self._tg_message_key, self._tg_message_id
                            )
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
