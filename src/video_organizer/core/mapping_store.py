# -*- coding: utf-8 -*-
"""上传目标映射表

记录「文件名/剧名 → Emos 上传目标」的对应关系，命中后直接使用记录里的
``item_type`` / ``item_id`` 上传，跳过 TMDB 查询与 Emos 目录树搜索，实现
「手动指定过一次，以后类似文件直接上传」。

两类映射：

- ``title``（文字映射）：标题或文件名包含关键词即命中；
- ``episode``（剧集映射）：标题匹配且季/集号一致才命中，优先级更高。

数据持久化在数据库 ``config_target_mapping`` 表中，Telegram 修正目标时
自动写入，也可在 Web「配置管理 → 目标映射表」里手动维护。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MATCH_TYPE_TITLE = "title"
MATCH_TYPE_EPISODE = "episode"


def normalize_text(value: Any) -> str:
    """归一化文本：去掉空格/标点并转小写，便于中英文标题比较"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


class TargetMappingStore:
    """目标映射表读写（数据库不可用时安全降级为空结果）"""

    @staticmethod
    def _session_local():
        try:
            from ..database.session import get_session_local

            return get_session_local()
        except Exception:
            return None

    @staticmethod
    def list_all(enabled_only: bool = False) -> List[Dict[str, Any]]:
        session_local = TargetMappingStore._session_local()
        if session_local is None:
            return []
        try:
            from ..database.models import TargetMapping

            with session_local() as db:
                query = db.query(TargetMapping)
                if enabled_only:
                    query = query.filter(TargetMapping.enabled == True)  # noqa: E712
                rows = query.order_by(TargetMapping.updated_at.desc(), TargetMapping.id.desc()).all()
                return [TargetMappingStore._to_dict(row) for row in rows]
        except Exception as exc:
            logger.debug("读取目标映射表失败: %s", exc)
            return []

    @staticmethod
    def _to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row.id,
            "match_type": row.match_type or MATCH_TYPE_TITLE,
            "keyword": row.keyword or "",
            "media_type": row.media_type or "",
            "season_number": row.season_number,
            "episode_number": row.episode_number,
            "item_type": row.item_type or "",
            "item_id": str(row.item_id or ""),
            "label": row.label or "",
            "storage": row.storage or "",
            "source": row.source or "web",
            "enabled": bool(row.enabled),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def create(
        keyword: str,
        item_type: str,
        item_id: Any,
        match_type: str = MATCH_TYPE_TITLE,
        media_type: str = "",
        season_number: Any = None,
        episode_number: Any = None,
        label: str = "",
        storage: str = "",
        source: str = "web",
        enabled: bool = True,
    ) -> Optional[int]:
        session_local = TargetMappingStore._session_local()
        if session_local is None:
            return None
        keyword = str(keyword or "").strip()
        item_type = str(item_type or "").strip()
        item_id = str(item_id or "").strip()
        if not keyword or not item_type or not item_id:
            raise ValueError("映射需要关键词与上传目标（item_type / item_id）")
        season = _to_int(season_number)
        episode = _to_int(episode_number)
        match_type = MATCH_TYPE_EPISODE if match_type == MATCH_TYPE_EPISODE else MATCH_TYPE_TITLE
        if match_type == MATCH_TYPE_EPISODE and episode is None:
            raise ValueError("剧集映射必须指定集数")
        try:
            from ..database.models import TargetMapping

            now = datetime.now()
            with session_local() as db:
                row = TargetMapping(
                    match_type=match_type,
                    keyword=keyword,
                    media_type=str(media_type or "").strip().lower(),
                    season_number=season,
                    episode_number=episode,
                    item_type=item_type,
                    item_id=item_id,
                    label=str(label or "").strip(),
                    storage=str(storage or "").strip(),
                    source=str(source or "web"),
                    enabled=bool(enabled),
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.commit()
                return row.id
        except Exception as exc:
            logger.warning("新增目标映射失败: %s", exc)
            return None

    @staticmethod
    def update(mapping_id: int, **fields: Any) -> bool:
        session_local = TargetMappingStore._session_local()
        if session_local is None:
            return False
        try:
            from ..database.models import TargetMapping

            with session_local() as db:
                row = db.query(TargetMapping).filter(TargetMapping.id == mapping_id).first()
                if not row:
                    return False
                if "match_type" in fields:
                    row.match_type = (
                        MATCH_TYPE_EPISODE
                        if fields["match_type"] == MATCH_TYPE_EPISODE
                        else MATCH_TYPE_TITLE
                    )
                for key in ("keyword", "media_type", "item_type", "item_id", "label", "storage", "source"):
                    if key in fields and fields[key] is not None:
                        setattr(row, key, str(fields[key]).strip())
                if "season_number" in fields:
                    row.season_number = _to_int(fields["season_number"])
                if "episode_number" in fields:
                    row.episode_number = _to_int(fields["episode_number"])
                if "enabled" in fields and fields["enabled"] is not None:
                    row.enabled = bool(fields["enabled"])
                row.updated_at = datetime.now()
                db.commit()
                return True
        except Exception as exc:
            logger.warning("更新目标映射失败: %s", exc)
            return False

    @staticmethod
    def delete(mapping_id: int) -> bool:
        session_local = TargetMappingStore._session_local()
        if session_local is None:
            return False
        try:
            from ..database.models import TargetMapping

            with session_local() as db:
                row = db.query(TargetMapping).filter(TargetMapping.id == mapping_id).first()
                if not row:
                    return False
                db.delete(row)
                db.commit()
                return True
        except Exception as exc:
            logger.warning("删除目标映射失败: %s", exc)
            return False

    @staticmethod
    def resolve(
        title: str = "",
        season: Any = None,
        episode: Any = None,
        media_type: str = "",
        file_name: str = "",
    ) -> Optional[Dict[str, Any]]:
        """按标题/文件名与季集号解析上传目标，未命中返回 None"""
        rows = TargetMappingStore.list_all(enabled_only=True)
        if not rows:
            return None
        norm_title = normalize_text(title)
        stem = Path(str(file_name or "")).stem if file_name else ""
        norm_file = normalize_text(stem)
        if not norm_title and not norm_file:
            return None
        season = _to_int(season)
        episode = _to_int(episode)
        media_type = str(media_type or "").strip().lower()

        best: Optional[Dict[str, Any]] = None
        best_score = -1
        for row in rows:
            keyword = normalize_text(row.get("keyword"))
            if not keyword:
                continue
            if not (keyword in norm_title or (norm_file and keyword in norm_file)):
                continue
            mapping_media = str(row.get("media_type") or "").strip().lower()
            if mapping_media and media_type and mapping_media != media_type:
                continue
            row_season = _to_int(row.get("season_number"))
            row_episode = _to_int(row.get("episode_number"))
            if row.get("match_type") == MATCH_TYPE_EPISODE:
                if row_episode is None:
                    continue
                if episode is None or episode != row_episode:
                    continue
                if row_season is not None and season is not None and season != row_season:
                    continue
                score = 100 + (10 if row_season is not None else 0) + (10 if mapping_media else 0)
                score += min(len(keyword), 20)
            else:
                score = 50 + min(len(keyword), 20) + (5 if mapping_media else 0)
            if score > best_score:
                best_score = score
                best = {
                    "item_type": row.get("item_type"),
                    "item_id": str(row.get("item_id")),
                    "label": row.get("label") or row.get("keyword"),
                    "kind": "episode" if row.get("match_type") == MATCH_TYPE_EPISODE else "mapping",
                    "season_number": row_season if row_season is not None else season,
                    "episode_number": row_episode if row_episode is not None else episode,
                    "mapping_id": row.get("id"),
                    "mapping_match_type": row.get("match_type"),
                }
        return best

    @staticmethod
    def remember(
        file_path: str,
        title: str,
        media_type: str,
        season: Any,
        episode: Any,
        target: Dict[str, Any],
        source: str = "telegram",
        keyword: Optional[str] = None,
    ) -> Optional[int]:
        """把一次手动指定的目标写入映射表（同键更新，避免重复）"""
        item_type = str((target or {}).get("item_type") or "").strip()
        item_id = str((target or {}).get("item_id") or "").strip()
        if not item_type or not item_id:
            return None
        keyword = (keyword or title or Path(str(file_path or "")).stem or "").strip()
        if not keyword:
            return None
        season = _to_int(season)
        episode = _to_int(episode)
        if episode is None:
            episode = _to_int((target or {}).get("episode_number"))
        if season is None:
            season = _to_int((target or {}).get("season_number"))
        match_type = MATCH_TYPE_EPISODE if episode is not None else MATCH_TYPE_TITLE
        label = str((target or {}).get("label") or "").strip()
        storage = str((target or {}).get("storage") or "").strip()

        session_local = TargetMappingStore._session_local()
        if session_local is None:
            return None
        try:
            from ..database.models import TargetMapping

            now = datetime.now()
            with session_local() as db:
                existing = (
                    db.query(TargetMapping)
                    .filter(
                        TargetMapping.match_type == match_type,
                        TargetMapping.keyword == keyword,
                        TargetMapping.season_number == season,
                        TargetMapping.episode_number == episode,
                    )
                    .first()
                )
                if existing:
                    existing.item_type = item_type
                    existing.item_id = item_id
                    existing.label = label
                    existing.storage = storage
                    existing.media_type = str(media_type or "").strip().lower()
                    existing.source = source
                    existing.enabled = True
                    existing.updated_at = now
                    db.commit()
                    return existing.id
                row = TargetMapping(
                    match_type=match_type,
                    keyword=keyword,
                    media_type=str(media_type or "").strip().lower(),
                    season_number=season,
                    episode_number=episode,
                    item_type=item_type,
                    item_id=item_id,
                    label=label,
                    storage=storage,
                    source=source,
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.commit()
                return row.id
        except Exception as exc:
            logger.warning("记录目标映射失败: %s", exc)
            return None
