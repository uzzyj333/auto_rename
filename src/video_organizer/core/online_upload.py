# -*- coding: utf-8 -*-
"""在线识别上传服务

把「识别 + 上传」的完整流程搬到 Web 中，全部走 Emos 官方 API：

1. 浏览/扫描服务器上配置的视频根目录
2. ffprobe 校验视频文件并提取基础信息
3. 在线识别：解析文件名（Renamer + TMDB）→ ``/api/video/getVideoId`` 定位 item_type / item_id，
   失败时用标题调用 ``/api/video/tree`` 搜索候选，交由前端人工选择
4. 调用 ``/api/upload/getUploadToken`` → 分片/直传 → ``/api/upload/video/save`` 完成上传

任务在后台线程池中执行，状态保存在内存中，供 Web 轮询。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .emos_client import EmosApiError, EmosClient
from .probe import probe_summary_for_upload, probe_video
from ..upload.upload_emos import (
    DEFAULT_UPLOAD_CONCURRENCY,
    SUBTITLE_EXTENSIONS,
    RobustEmosVideoUploader,
    find_subtitle_files,
    format_size,
)

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts",
    ".webm", ".m4v", ".mpg", ".mpeg", ".rmvb", ".iso", ".strm",
}

MAX_SCAN_FILES = 2000
MAX_TASKS = 200


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class OnlineUploadTask:
    """在线识别上传任务"""

    id: str
    file_path: str
    file_name: str
    file_size: int
    item_type: str
    item_id: str
    storage: str
    title: str = ""
    media_type: str = ""
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    status: str = "queued"          # queued / uploading / completed / failed
    stage: str = "等待上传"
    progress: float = 0.0
    uploaded_bytes: int = 0
    total_bytes: int = 0
    speed: str = ""
    error: str = ""
    file_id: str = ""
    media_id: str = ""
    original_deleted: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "file_path": self.file_path,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "file_size_text": format_size(self.file_size),
            "item_type": self.item_type,
            "item_id": self.item_id,
            "storage": self.storage,
            "title": self.title,
            "media_type": self.media_type,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 2),
            "uploaded_bytes": self.uploaded_bytes,
            "total_bytes": self.total_bytes,
            "speed": self.speed,
            "error": self.error,
            "file_id": self.file_id,
            "media_id": self.media_id,
            "original_deleted": self.original_deleted,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OnlineUploadService:
    """在线识别上传服务（单例）"""

    _instance: Optional["OnlineUploadService"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "OnlineUploadService":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, OnlineUploadTask] = {}
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="online-upload")
        self._config: Dict[str, Any] = {}
        self._config_path: Optional[str] = None
        self._client: Optional[EmosClient] = None
        self._renamer = None
        self._probe_cache: Dict[str, Dict[str, Any]] = {}
        # 可选：上传后清理原文件时的下载器回调（见 core/source_cleanup.py）
        self._downloader_cleanup: Optional[Callable[[str], Optional[bool]]] = None

    def set_downloader_cleanup(
        self, cleanup: Optional[Callable[[str], Optional[bool]]]
    ) -> None:
        """注册「上传后删除原文件」时的下载器清理回调（由 VideoFileHandler 提供）

        没有这个回调时，手动选片 / Telegram 回复修正上传成功后只会删掉文件、
        不会删除下载器任务，qBittorrent 里会留下「文件已不存在」的空种子。
        """
        self._downloader_cleanup = cleanup

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def configure(self, config: Optional[Dict[str, Any]], config_path: Optional[Any] = None) -> None:
        """更新配置（在线修改配置后立即生效，无需重启）"""
        with self._lock:
            self._config = dict(config or {})
            if config_path is not None:
                self._config_path = str(config_path)
            self._client = None
            self._renamer = None
            self._probe_cache.clear()

    def ensure_configured(self, config: Optional[Dict[str, Any]] = None) -> None:
        """仅在尚未配置时初始化，避免每次请求都清空缓存"""
        if self._config:
            return
        self.configure(config)

    def _emos_config(self) -> Dict[str, Any]:
        return dict(self._config.get("emos") or {})

    def _online_config(self) -> Dict[str, Any]:
        return dict(self._config.get("online_upload") or {})

    def _token(self) -> str:
        raw = str(self._emos_config().get("auth_token") or "")
        return raw.split("#")[0].split(";")[0].strip()

    def _base_url(self) -> str:
        return str(self._emos_config().get("base_url") or "https://emos.best").strip()

    def get_client(self) -> EmosClient:
        """获取（惰性创建）Emos 客户端"""
        with self._lock:
            if self._client is None:
                token = self._token()
                if not token:
                    raise EmosApiError("未配置 Emos auth_token，请先在「配置管理 → Emos」中填写")
                self._client = EmosClient(base_url=self._base_url(), auth_token=token)
            return self._client

    def get_renamer(self):
        """获取（惰性创建）文件名识别器"""
        with self._lock:
            if self._renamer is None:
                from .renamer import VideoRenamer

                tmdb_config = self._config.get("tmdb") or {}
                self._renamer = VideoRenamer(
                    tmdb_api_key=tmdb_config.get("api_key") or None,
                    naming_rules=self._config.get("naming_rules"),
                    config=self._config,
                )
            return self._renamer

    def delete_after_upload_enabled(self) -> bool:
        """是否开启「上传后删除原文件」（processing.delete_after_upload）"""
        processing = self._config.get("processing") or {}
        return bool(processing.get("delete_after_upload", False))

    def _delete_source_if_configured(self, file_path: str) -> Tuple[bool, str]:
        """上传成功后按配置处理原文件

        Returns:
            (是否已删除, 追加到任务阶段文案的后缀)
        """
        if not self.delete_after_upload_enabled():
            return False, ""
        try:
            from .source_cleanup import cleanup_uploaded_source

            cleanup = self._downloader_cleanup or self._handler_downloader_cleanup()
            outcome = cleanup_uploaded_source(file_path, downloader_cleanup=cleanup)
        except Exception as exc:
            logger.warning("上传后处理原文件失败: %s", exc)
            return False, "（原文件删除失败）"
        deleted = bool(outcome.get("deleted"))
        reason = str(outcome.get("reason") or "")
        logger.info("上传后处理原文件: %s -> %s", file_path, reason)
        if deleted:
            return True, "（已删除原文件）"
        return False, (f"（原文件未删除：{reason}）" if reason else "（原文件未删除）")

    def _handler_downloader_cleanup(
        self,
    ) -> Optional[Callable[[str], Optional[bool]]]:
        """兜底：从全局状态里取 VideoFileHandler 的下载器清理回调"""
        try:
            from ..web.services.state import get_state_manager

            handler = get_state_manager().get_video_handler()
        except Exception:
            return None
        cleanup = getattr(handler, "_downloader_cleanup_state", None)
        if callable(cleanup):
            self._downloader_cleanup = cleanup
            return cleanup
        return None

    @staticmethod
    def _subtitle_stage_note(summary: Optional[Dict[str, Any]]) -> str:
        """把字幕上传结果整理成任务阶段文案后缀"""
        if not isinstance(summary, dict):
            return ""
        try:
            found = int(summary.get("found") or 0)
        except (TypeError, ValueError):
            return ""
        if not found:
            return ""
        uploaded = len(summary.get("uploaded") or [])
        if uploaded == found:
            return f"（含 {found} 个字幕）"
        return f"（字幕 {uploaded}/{found}）"

    @staticmethod
    def _delete_uploaded_subtitles(result: Dict[str, Any]) -> None:
        """视频源文件已删除时，顺带清理已上传成功的外挂字幕"""
        for raw in result.get("subtitle_paths") or []:
            try:
                Path(raw).unlink()
                logger.info("已删除已上传的字幕: %s", raw)
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning("删除字幕失败: %s - %s", raw, exc)

    def config_snapshot(self) -> Dict[str, Any]:
        """返回前端需要的配置信息（不含 token 明文）"""
        emos = self._emos_config()
        online = self._online_config()
        token = self._token()
        storages = [
            item.strip()
            for item in re.split(r"[,;\s]+", str(emos.get("file_storages") or "internal,default,google_drive,zn_r2_upload"))
            if item.strip()
        ]
        default_storage = str(emos.get("file_storage") or "internal").strip() or "internal"
        if default_storage not in storages:
            storages.insert(0, default_storage)
        return {
            "base_url": self._base_url(),
            "token_configured": bool(token),
            "default_storage": default_storage,
            "storages": storages,
            "video_root": str(online.get("video_root") or ""),
            "roots": self.roots(),
            "probe_enabled": bool(online.get("probe_enabled", True)),
            "chunk_size_mb": int(emos.get("chunk_size_mb") or 50),
        }

    # ------------------------------------------------------------------
    # 路径安全
    # ------------------------------------------------------------------

    def roots(self) -> List[str]:
        """允许浏览的根目录列表"""
        online = self._online_config()
        raw = online.get("video_root") or ""
        roots = [item.strip() for item in re.split(r"[,;\n]", str(raw)) if item.strip()]
        if not roots:
            monitoring = self._config.get("monitoring") or {}
            for key in ("watch_dir", "output_dir", "directory_watch_dir"):
                value = monitoring.get(key)
                if value:
                    roots.append(str(value).strip())
        resolved: List[str] = []
        for root in roots:
            try:
                real = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
            except Exception:
                continue
            if real not in resolved:
                resolved.append(real)
        return resolved

    def resolve(self, raw_path: Optional[str]) -> Path:
        """把用户输入路径解析为绝对路径，并做越界校验"""
        roots = self.roots()
        if not roots:
            raise PermissionError(
                "未配置视频根目录，请在「配置管理 → 在线上传」中设置 video_root"
            )
        if not raw_path:
            return Path(roots[0])
        text = str(raw_path).strip()
        candidate = os.path.realpath(os.path.abspath(os.path.expanduser(text)))
        for root in roots:
            if candidate == root or candidate.startswith(root + os.sep):
                return Path(candidate)
        raise PermissionError(f"路径不在允许的视频根目录内: {raw_path}")

    # ------------------------------------------------------------------
    # 目录浏览 / 扫描
    # ------------------------------------------------------------------

    def list_directory(self, raw_path: Optional[str] = None) -> Dict[str, Any]:
        """列出目录内容"""
        roots = self.roots()
        if not roots:
            raise PermissionError(
                "未配置视频根目录，请在「配置管理 → 在线上传」中设置 video_root"
            )
        if not raw_path and len(roots) > 1:
            entries = [
                {"name": os.path.basename(root) or root, "path": root, "kind": "root", "file_count": None}
                for root in roots
            ]
            return {"success": True, "path": "", "parent": None, "entries": entries}

        directory = self.resolve(raw_path)
        if not directory.exists():
            raise FileNotFoundError(f"目录不存在: {directory}")
        if not directory.is_dir():
            raise NotADirectoryError(f"不是目录: {directory}")

        entries: List[Dict[str, Any]] = []
        try:
            children = sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        except PermissionError as exc:
            raise PermissionError(f"无法读取目录: {directory}") from exc

        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child), "kind": "directory", "file_count": None})
                elif child.suffix.lower() in VIDEO_EXTENSIONS or child.suffix.lower() in SUBTITLE_EXTENSIONS:
                    stat = child.stat()
                    entries.append(
                        {
                            "name": child.name,
                            "path": str(child),
                            "kind": "subtitle" if child.suffix.lower() in SUBTITLE_EXTENSIONS else "video",
                            "size": stat.st_size,
                            "size_text": format_size(stat.st_size),
                            "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        }
                    )
            except OSError:
                continue

        parent = None
        for root in roots:
            if str(directory) != root and str(directory).startswith(root + os.sep):
                parent = str(directory.parent)
                break

        return {"success": True, "path": str(directory), "parent": parent, "entries": entries}

    def scan_videos(self, raw_path: Optional[str] = None, recursive: bool = True) -> Dict[str, Any]:
        """扫描目录中的视频文件"""
        directory = self.resolve(raw_path)
        if not directory.is_dir():
            raise NotADirectoryError(f"不是目录: {directory}")
        files: List[Dict[str, Any]] = []
        iterator = directory.rglob("*") if recursive else directory.glob("*")
        for item in iterator:
            try:
                suffix = item.suffix.lower()
                if not item.is_file() or (
                    suffix not in VIDEO_EXTENSIONS and suffix not in SUBTITLE_EXTENSIONS
                ):
                    continue
                stat = item.stat()
            except OSError:
                continue
            files.append(
                {
                    "id": f"{item}:{stat.st_size}:{int(stat.st_mtime)}",
                    "name": item.name,
                    "path": str(item),
                    "kind": "subtitle" if suffix in SUBTITLE_EXTENSIONS else "video",
                    "size": stat.st_size,
                    "size_text": format_size(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                }
            )
            if len(files) >= MAX_SCAN_FILES:
                break
        files.sort(key=lambda entry: entry["modified_at"], reverse=True)
        return {"success": True, "path": str(directory), "count": len(files), "files": files}

    # ------------------------------------------------------------------
    # 探测 + 识别
    # ------------------------------------------------------------------

    def probe(self, raw_path: str, use_cache: bool = True) -> Dict[str, Any]:
        """ffprobe 校验视频文件"""
        path = self.resolve(raw_path)
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")
        online = self._online_config()
        if not bool(online.get("probe_enabled", True)):
            return {"success": True, "valid": True, "available": False, "skipped": True, "summary": {}, "error": ""}

        cache_key = str(path)
        if use_cache:
            try:
                stat = path.stat()
                cache_key = f"{path}:{stat.st_size}:{int(stat.st_mtime)}"
                cached = self._probe_cache.get(cache_key)
                if cached is not None:
                    return cached
            except OSError:
                pass

        ffprobe_path = str(online.get("ffprobe_path") or "").strip() or None
        result = probe_video(str(path), ffprobe_path=ffprobe_path)
        payload = {"success": True, **result}
        if use_cache:
            self._probe_cache[cache_key] = payload
            if len(self._probe_cache) > 200:
                self._probe_cache.clear()
        return payload

    def recognize(self, raw_path: str) -> Dict[str, Any]:
        """在线识别：解析文件名 + 查询 Emos 条目"""
        path = self.resolve(raw_path)
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")

        is_subtitle = path.suffix.lower() in SUBTITLE_EXTENSIONS
        metadata: Dict[str, Any] = {}
        errors: List[str] = []
        try:
            metadata = self.get_renamer().extract_metadata(str(path)) or {}
        except Exception as exc:  # TMDB 失败不阻塞识别
            logger.warning("识别文件名失败: %s", exc)
            errors.append(f"文件名识别失败: {exc}")

        tmdb_id = str(metadata.get("tmdb_id") or "").strip()
        media_type = str(metadata.get("media_type") or "").strip().lower()
        title = str(metadata.get("title") or metadata.get("show_name") or metadata.get("movie_name") or "").strip()
        season = _to_int(metadata.get("season"))
        episode = _to_int(metadata.get("episode"))
        year = _to_int(metadata.get("year"))

        match: Optional[Dict[str, Any]] = None
        candidates: List[Dict[str, Any]] = []

        try:
            client = self.get_client()
            if tmdb_id:
                tmdb_type = "movie" if media_type == "movie" else "tv"
                payload = client.get_video_id(
                    tmdb_id,
                    "tmdb",
                    tmdb_type=tmdb_type,
                    season_number=season if tmdb_type == "tv" else None,
                    episode_number=episode if tmdb_type == "tv" else None,
                )
                match = self._pick_match(payload, media_type, season, episode)
                if not match and tmdb_type == "tv":
                    # getVideoId 没给到具体集时，用该剧的目录树兜底定位
                    match = self.resolve_episode_from_tree(
                        client, payload.get("item_id"), season, episode
                    )
            if not match and title:
                candidates = self.search_targets(
                    video_type="movie" if media_type == "movie" else ("tv" if media_type else None),
                    title=title,
                    todb_id=None,
                )
                # 没有 TMDB ID（或接口未返回具体集）时，用目录树候选自动定位季/集
                match = self._pick_from_candidates(candidates, season, episode, media_type)
            if not match and (media_type == "tv" or (media_type != "movie" and episode is not None)):
                if episode is None:
                    errors.append("未能从文件名解析出季/集号，无法定位到具体某一集")
                else:
                    errors.append(
                        f"Emos 中没有「{title or tmdb_id}」S{season if season is not None else '?'}"
                        f"E{episode} 这一集，请先在 Emos 建集，或在「在线识别上传」里手动选择目标"
                    )
        except EmosApiError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"查询 Emos 失败: {exc}")

        if not media_type and metadata:
            media_type = "tv" if metadata.get("show_name") else "movie"

        return {
            "success": True,
            "file_path": str(path),
            "file_name": path.name,
            "file_size": path.stat().st_size,
            "metadata": {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": title,
                "year": year,
                "season": season,
                "episode": episode,
                "quality_tags": metadata.get("quality_tags") or "",
                "release_group": metadata.get("release_group") or "",
            },
            "match": match,
            "candidates": candidates,
            "file_kind": "subtitle" if is_subtitle else "video",
            "subtitles": [] if is_subtitle else [item.name for item in find_subtitle_files(path)],
            "error": "；".join(errors),
        }

    @staticmethod
    def _pick_match(
        payload: Dict[str, Any],
        media_type: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """从 getVideoId 返回结果中挑选最合适的上传目标

        电视剧必须定位到具体某一集（ve）：季/集号对不上或接口未返回 episode_info 时返回 None，
        由调用方改用目录树兜底或给出明确提示，绝不退回整部剧（vl）/整季（vs），
        否则上传接口 /api/upload/video/base 会返回 404。
        """
        if not isinstance(payload, dict):
            return None

        # 只要不是电影且能确定集号，就按电视剧处理，必须定位到具体某一集
        is_tv = payload.get("video_type") == "tv" or (
            media_type != "movie" and episode is not None
        )

        episode_info = payload.get("episode_info") or {}
        if isinstance(episode_info, dict) and episode_info.get("item_id"):
            if is_tv:
                got_episode = _to_int(episode_info.get("episode_number"))
                got_season = _to_int(episode_info.get("season_number"))
                if episode is None or got_episode is None or got_episode != episode:
                    return None
                if season is not None and got_season is not None and got_season != season:
                    return None
                if str(episode_info.get("item_type") or "ve") != "ve":
                    return None
            return {
                "item_type": episode_info.get("item_type") or "ve",
                "item_id": str(episode_info.get("item_id")),
                "label": episode_info.get("episode_title") or payload.get("title") or "",
                "kind": "episode",
                "season_number": _to_int(episode_info.get("season_number")),
                "episode_number": _to_int(episode_info.get("episode_number")),
            }

        if media_type == "movie" or payload.get("video_type") == "movie":
            if payload.get("item_id"):
                return {
                    "item_type": payload.get("item_type") or "vl",
                    "item_id": str(payload.get("item_id")),
                    "label": payload.get("title") or payload.get("video_list_name") or "",
                    "kind": "movie",
                }

        # 电视剧找不到具体某一集时不再退回整季/整剧，避免上传到错误目标
        if is_tv:
            return None

        season_info = payload.get("season_info") or {}
        if isinstance(season_info, dict) and season_info.get("item_id"):
            return {
                "item_type": season_info.get("item_type") or "vs",
                "item_id": str(season_info.get("item_id")),
                "label": season_info.get("season_title") or payload.get("title") or "",
                "kind": "season",
                "season_number": _to_int(season_info.get("season_number")),
            }

        if payload.get("item_id"):
            return {
                "item_type": payload.get("item_type") or "vl",
                "item_id": str(payload.get("item_id")),
                "label": payload.get("title") or "",
                "kind": "video",
            }
        return None

    @staticmethod
    def resolve_episode_from_tree(
        client: EmosClient,
        vl_id: Any,
        season: Optional[int],
        episode: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """getVideoId 未返回 episode_info 时，用视频目录树兜底定位具体某一集（ve）"""
        if not vl_id or episode is None:
            return None
        try:
            tree = client.get_video_tree(video_id=vl_id)
        except Exception as exc:
            logger.debug("Emos 目录树兜底查询失败: %s", exc)
            return None
        narrowed = [
            item
            for item in (tree or [])
            if isinstance(item, dict) and str(item.get("item_id")) == str(vl_id)
        ]
        return OnlineUploadService._pick_from_candidates(
            narrowed or tree, season, episode, "tv"
        )

    @staticmethod
    def _pick_from_candidates(
        candidates: List[Dict[str, Any]],
        season: Optional[int],
        episode: Optional[int],
        media_type: str,
        year: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """从目录树候选中挑选目标（没有 TMDB ID 时使用）"""
        for video in candidates or []:
            if not isinstance(video, dict):
                continue
            seasons = video.get("seasons") or []
            if media_type == "movie" or not seasons:
                item_id = video.get("item_id")
                if media_type == "movie" and item_id:
                    if year is not None:
                        date_air = str(video.get("date_air") or "")
                        if date_air[:4].isdigit() and date_air[:4] != str(year):
                            continue
                    return {
                        "item_type": video.get("item_type") or "vl",
                        "item_id": str(item_id),
                        "label": video.get("title") or "",
                        "kind": "movie",
                    }
                continue
            for season_item in seasons:
                if not isinstance(season_item, dict):
                    continue
                season_number = _to_int(season_item.get("season_number"))
                if season is not None and season_number is not None and season_number != season:
                    continue
                episodes = season_item.get("episodes") or []
                if episode is not None:
                    for episode_item in episodes:
                        if not isinstance(episode_item, dict):
                            continue
                        if _to_int(episode_item.get("episode_number")) == episode and episode_item.get("item_id"):
                            label = episode_item.get("episode_title") or video.get("title") or ""
                            return {
                                "item_type": episode_item.get("item_type") or "ve",
                                "item_id": str(episode_item.get("item_id")),
                                "label": label,
                                "kind": "episode",
                                "season_number": season_number,
                                "episode_number": _to_int(episode_item.get("episode_number")),
                            }
                if episode is None and season_item.get("item_id"):
                    return {
                        "item_type": season_item.get("item_type") or "vs",
                        "item_id": str(season_item.get("item_id")),
                        "label": season_item.get("season_title") or video.get("title") or "",
                        "kind": "season",
                        "season_number": season_number,
                    }
        return None

    def pick_target(
        self,
        candidates: List[Dict[str, Any]],
        season: Optional[int] = None,
        episode: Optional[int] = None,
        media_type: str = "",
        year: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """公开的目标选择入口（在线识别与 Telegram 修正共用）"""
        return self._pick_from_candidates(candidates, season, episode, media_type, year=year)

    def search_targets(
        self,
        video_type: Optional[str] = None,
        title: Optional[str] = None,
        todb_id: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """在线搜索 Emos 视频目录树（识别候选）"""
        if not (title or todb_id):
            return []
        tree = self.get_client().get_video_tree(video_type=video_type, title=title, todb_id=todb_id)
        results: List[Dict[str, Any]] = []
        for item in tree:
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "title": item.get("title") or "",
                    "video_type": item.get("video_type") or "",
                    "item_type": item.get("item_type") or "vl",
                    "item_id": str(item.get("item_id") or ""),
                    "tmdb_id": item.get("tmdb_id"),
                    "todb_id": item.get("todb_id"),
                    "date_air": item.get("date_air") or "",
                    "has_media": bool(item.get("has_media")),
                    "kind": "video",
                    "seasons": [
                        {
                            "season_number": season.get("season_number"),
                            "season_title": season.get("season_title") or "",
                            "item_type": season.get("item_type") or "vs",
                            "item_id": str(season.get("item_id") or ""),
                            "episodes": [
                                {
                                    "episode_number": episode.get("episode_number"),
                                    "episode_title": episode.get("episode_title") or "",
                                    "item_type": episode.get("item_type") or "ve",
                                    "item_id": str(episode.get("item_id") or ""),
                                    "has_media": bool(episode.get("has_media")),
                                }
                                for episode in (season.get("episodes") or [])
                                if isinstance(episode, dict)
                            ],
                        }
                        for season in (item.get("seasons") or [])
                        if isinstance(season, dict)
                    ],
                }
            )
        return results

    def video_base(self, item_type: str, item_id: Any) -> Dict[str, Any]:
        """获取上传目标详情"""
        return self.get_client().get_video_base(item_type, item_id)

    def save_internal(
        self,
        item_type: str,
        item_id: Any,
        file_path: str,
        file_size: int,
        path_type: Optional[str] = None,
        upload_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """内部入库（文件已在 Emos 存储上）"""
        online = self._online_config()
        return self.get_client().save_internal(
            item_type=item_type,
            item_id=item_id,
            path_type=path_type or str(online.get("path_type") or "local_emos_1"),
            file_path=file_path,
            file_size=file_size,
            upload_username=upload_username,
        )

    # ------------------------------------------------------------------
    # 上传任务
    # ------------------------------------------------------------------

    def create_tasks(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量创建上传任务"""
        created: List[Dict[str, Any]] = []
        errors: List[str] = []
        for item in items or []:
            try:
                created.append(self.create_task(item))
            except Exception as exc:
                errors.append(f"{item.get('file_path', '')}: {exc}")
                self._notify_failure(
                    {
                        "file_path": item.get("file_path"),
                        "file_name": os.path.basename(str(item.get("file_path") or "")),
                        "title": item.get("title"),
                        "media_type": item.get("media_type"),
                        "item_type": item.get("item_type"),
                        "item_id": item.get("item_id"),
                        "storage": item.get("storage"),
                        "season_number": item.get("season_number"),
                        "episode_number": item.get("episode_number"),
                    },
                    str(exc),
                    header="上传任务创建失败",
                )
        return {"success": not errors, "tasks": created, "errors": errors}

    def create_task(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """创建单个上传任务并开始后台上传"""
        path = self.resolve(str(item.get("file_path") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")
        item_type = str(item.get("item_type") or "").strip()
        item_id = str(item.get("item_id") or "").strip()
        if not item_type or not item_id:
            raise ValueError("缺少上传目标（item_type / item_id）")

        media_type = str(item.get("media_type") or "").strip().lower()
        season_number = _to_int(item.get("season_number"))
        episode_number = _to_int(item.get("episode_number"))
        # 电视剧上传到「整部作品（vl）/ 整季（vs）」时 Emos 的 /api/upload/video/base
        # 会返回 404，这里按季/集号自动纠正到具体某一集（ve）
        item_type, item_id, season_number, episode_number = self._resolve_episode_target(
            item_type, item_id, media_type, season_number, episode_number
        )

        emos = self._emos_config()
        storage = str(item.get("storage") or emos.get("file_storage") or "internal").strip() or "internal"
        stat = path.stat()
        task = OnlineUploadTask(
            id=uuid.uuid4().hex[:12],
            file_path=str(path),
            file_name=path.name,
            file_size=stat.st_size,
            item_type=item_type,
            item_id=item_id,
            storage=storage,
            title=str(item.get("title") or ""),
            media_type=str(item.get("media_type") or ""),
            season_number=season_number,
            episode_number=episode_number,
            total_bytes=stat.st_size,
        )
        with self._lock:
            if len(self._tasks) >= MAX_TASKS:
                for old_id in list(self._tasks)[:50]:
                    if self._tasks[old_id].status in {"completed", "failed"}:
                        self._tasks.pop(old_id, None)
            self._tasks[task.id] = task
        self._executor.submit(self._run_task, task.id)
        return task.to_dict()

    def _resolve_episode_target(
        self,
        item_type: str,
        item_id: str,
        media_type: str,
        season_number: Optional[int],
        episode_number: Optional[int],
    ) -> Tuple[str, str, Optional[int], Optional[int]]:
        """把「整部作品（vl）/ 整季（vs）」纠正到具体某一集（ve）

        手动选片 / 搜索结果里如果选中了整部作品，电视剧的上传接口会返回 404。
        这里有季/集号时用 Emos 目录树自动定位到 ve；定位不到就明确报错，
        而不是让它去撞 404（电影仍然允许直接传 vl）。
        """
        if item_type not in {"vl", "vs"}:
            return item_type, item_id, season_number, episode_number
        if media_type == "movie":
            return item_type, item_id, season_number, episode_number
        if episode_number is None:
            if media_type == "tv":
                # 没有集号就没法定位到 ve，与其让它去撞 404，不如直接说清楚
                raise ValueError(
                    "电视剧必须选到具体某一集（ve）：当前选择的是整部作品/整季，"
                    "请在「在线识别上传」中展开季/集后选择对应剧集"
                )
            return item_type, item_id, season_number, episode_number

        try:
            resolved = self.resolve_episode_from_tree(
                self.get_client(), item_id, season_number, episode_number
            )
        except Exception as exc:
            logger.debug("定位具体剧集失败: %s", exc)
            resolved = None

        if not resolved or not resolved.get("item_id"):
            label = (
                f"S{season_number if season_number is not None else '?'}"
                f"E{episode_number}"
            )
            raise ValueError(
                f"电视剧必须选到具体某一集（ve）：未在 Emos 目录树中找到 {label}，"
                "请在「在线识别上传」中展开季/集后选择对应剧集"
            )

        resolved_season = _to_int(resolved.get("season_number"))
        resolved_episode = _to_int(resolved.get("episode_number"))
        return (
            str(resolved.get("item_type") or "ve"),
            str(resolved.get("item_id")),
            resolved_season if resolved_season is not None else season_number,
            resolved_episode if resolved_episode is not None else episode_number,
        )

    def list_tasks(self) -> List[Dict[str, Any]]:
        """按创建时间倒序列出任务"""
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            return [task.to_dict() for task in tasks]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def retry_task(self, task_id: str) -> Dict[str, Any]:
        """重试失败任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError("任务不存在")
            if task.status == "uploading":
                raise RuntimeError("任务正在上传中")
            task.status = "queued"
            task.stage = "等待重试"
            task.progress = 0.0
            task.uploaded_bytes = 0
            task.error = ""
            task.updated_at = _now()
        self._executor.submit(self._run_task, task_id)
        return self.get_task(task_id) or {}

    def delete_task(self, task_id: str) -> bool:
        """删除任务记录"""
        with self._lock:
            return self._tasks.pop(task_id, None) is not None

    def clear_finished(self) -> int:
        """清空已完成/失败的任务"""
        with self._lock:
            removable = [tid for tid, task in self._tasks.items() if task.status in {"completed", "failed"}]
            for task_id in removable:
                self._tasks.pop(task_id, None)
            return len(removable)

    def _update_task(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for key, value in changes.items():
                setattr(task, key, value)
            task.updated_at = _now()

    def _notify_failure(self, context: Dict[str, Any], error: str, header: str = "上传失败") -> None:
        """上传失败时推送 Telegram 报错信息（推送失败不影响主流程）"""
        try:
            from .telegram_bot import TelegramBotService

            TelegramBotService.instance().notify_error(context or {}, error, header=header)
        except Exception as exc:
            logger.debug("推送 Telegram 报错信息失败: %s", exc)

    def _run_task(self, task_id: str) -> None:
        """后台执行上传"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            snapshot = task.to_dict()

        self._update_task(task_id, status="uploading", stage="准备上传", progress=0.0, error="")

        def on_progress(progress: float, uploaded: int, total: int, status: str) -> None:
            self._update_task(
                task_id,
                progress=progress,
                uploaded_bytes=uploaded,
                total_bytes=total,
                status="completed" if status == "completed" else ("failed" if status == "failed" else "uploading"),
                stage="上传中" if status == "uploading" else ("已完成" if status == "completed" else "上传失败"),
            )

        try:
            emos = self._emos_config()
            metadata: Dict[str, Any] = {}
            online = self._online_config()
            is_subtitle = str(snapshot.get("file_path") or "").lower().endswith(
                tuple(SUBTITLE_EXTENSIONS)
            )
            if not is_subtitle and bool(online.get("probe_enabled", True)):
                try:
                    metadata = probe_summary_for_upload(self.probe(snapshot["file_path"]))
                except Exception as exc:
                    logger.warning("ffprobe 校验失败（忽略）: %s", exc)

            uploader = RobustEmosVideoUploader(
                auth_token=self._token(),
                base_url=self._base_url(),
                chunk_size_mb=int(emos.get("chunk_size_mb") or 50),
                upload_concurrency=int(
                    emos.get("upload_concurrency") or DEFAULT_UPLOAD_CONCURRENCY
                ),
                upload_subtitles=bool(emos.get("upload_subtitles", True)),
                telegram_config=self._config.get("telegram") or {},
                progress_callback=on_progress,
            )
            try:
                self._update_task(task_id, stage="获取上传凭证")
                if is_subtitle:
                    result = uploader.upload_subtitle(
                        snapshot["file_path"],
                        snapshot["item_type"],
                        snapshot["item_id"],
                        snapshot["storage"],
                    )
                else:
                    result = uploader.upload_video(
                        snapshot["file_path"],
                        snapshot["item_type"],
                        snapshot["item_id"],
                        snapshot["storage"],
                        metadata=metadata or None,
                    )
            finally:
                uploader.close()
            if result and result.get("deferred"):
                # Emos 限制一周内不能重复上传，而目标条目下又找不到该文件：
                # 不能算成功，短时间内重试也无意义，如实报失败并保留本地文件
                reason = str(result.get("reason") or "Emos 暂不允许上传该资源")
                self._update_task(
                    task_id, status="failed", stage="上传被 Emos 拒绝", error=reason
                )
                self._notify_failure(snapshot, reason)
            elif result:
                if is_subtitle:
                    deleted, delete_note = self._delete_source_if_configured(snapshot["file_path"])
                    self._update_task(
                        task_id,
                        status="completed",
                        stage="字幕已上传" + delete_note,
                        progress=100.0,
                        uploaded_bytes=snapshot["file_size"],
                        total_bytes=snapshot["file_size"],
                        media_id=str(result.get("subtitle_id") or ""),
                        original_deleted=deleted,
                        error="",
                    )
                else:
                    # 上传成功后按配置处理原文件（手动选片 / 自动上传 / Telegram 修正三条链路一致）
                    deleted, delete_note = self._delete_source_if_configured(snapshot["file_path"])
                    if deleted:
                        self._delete_uploaded_subtitles(result)
                    subtitle_note = self._subtitle_stage_note(result.get("subtitles"))
                    self._update_task(
                        task_id,
                        status="completed",
                        stage="已完成" + subtitle_note + delete_note,
                        progress=100.0,
                        uploaded_bytes=snapshot["file_size"],
                        total_bytes=snapshot["file_size"],
                        file_id=str(result.get("file_id") or ""),
                        media_id=str(result.get("media_id") or ""),
                        original_deleted=deleted,
                        error="",
                    )
            else:
                reason = str(getattr(uploader, "last_error", "") or "上传失败，请查看日志")
                self._update_task(task_id, status="failed", stage="上传失败", error=reason)
                self._notify_failure(snapshot, reason)
        except Exception as exc:
            logger.error("在线识别上传失败: %s", exc)
            self._update_task(task_id, status="failed", stage="上传失败", error=str(exc))
            self._notify_failure(snapshot, str(exc))
