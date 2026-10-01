# -*- coding: utf-8 -*-
"""在线识别上传路由

提供「浏览服务器视频 → 识别 Emos 条目 → 直接上传」的完整 API。
所有接口都走 Emos 官方 API，配置修改后立即生效，无需重启容器。
"""

import logging
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.state import get_state_manager
from ...core.emos_client import EmosApiError
from ...core.online_upload import OnlineUploadService

logger = logging.getLogger(__name__)

router = APIRouter()


class PathRequest(BaseModel):
    path: Optional[str] = None


class ScanRequest(BaseModel):
    path: Optional[str] = None
    recursive: bool = True


class ProbeRequest(BaseModel):
    path: str
    use_cache: bool = True


class RecognizeRequest(BaseModel):
    path: str


class TaskCreateRequest(BaseModel):
    file_path: str
    item_type: str
    item_id: Any
    storage: Optional[str] = None
    title: Optional[str] = ""
    media_type: Optional[str] = ""
    season_number: Optional[Any] = None
    episode_number: Optional[Any] = None


class TasksCreateRequest(BaseModel):
    items: List[TaskCreateRequest]


class SaveInternalRequest(BaseModel):
    item_type: str
    item_id: Any
    file_path: str
    file_size: Optional[int] = 0
    path_type: Optional[str] = None
    upload_username: Optional[str] = None


def _service() -> OnlineUploadService:
    """获取在线上传服务（首次访问时按当前配置初始化）"""
    service = OnlineUploadService.instance()
    try:
        service.ensure_configured(get_state_manager().get_config())
    except Exception:
        pass
    return service


def _fail(exc: Exception) -> HTTPException:
    """把内部异常转换为合适的 HTTP 状态码"""
    if isinstance(exc, EmosApiError):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, (PermissionError, FileNotFoundError, NotADirectoryError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    logger.error("在线识别上传接口错误: %s", exc)
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/config")
async def get_online_config():
    """获取在线上传相关配置（不含 token 明文）"""
    try:
        return {"success": True, **_service().config_snapshot()}
    except Exception as exc:
        raise _fail(exc)


@router.get("/roots")
async def get_roots():
    """获取允许浏览的视频根目录"""
    try:
        return {"success": True, "roots": _service().roots()}
    except Exception as exc:
        raise _fail(exc)


@router.post("/browse")
async def browse(request: PathRequest):
    """浏览服务器目录"""
    try:
        return _service().list_directory(request.path)
    except Exception as exc:
        raise _fail(exc)


@router.post("/scan")
async def scan(request: ScanRequest):
    """扫描目录中的视频文件"""
    try:
        return _service().scan_videos(request.path, recursive=request.recursive)
    except Exception as exc:
        raise _fail(exc)


@router.post("/probe")
async def probe(request: ProbeRequest):
    """用 ffprobe 校验视频文件"""
    try:
        return _service().probe(request.path, use_cache=request.use_cache)
    except Exception as exc:
        raise _fail(exc)


@router.post("/recognize")
async def recognize(request: RecognizeRequest):
    """在线识别视频对应的 Emos 条目"""
    try:
        return _service().recognize(request.path)
    except Exception as exc:
        raise _fail(exc)


@router.get("/search")
async def search(
    q: Optional[str] = None,
    video_type: Optional[str] = None,
    todb_id: Optional[str] = None,
):
    """搜索 Emos 视频目录树（识别候选）"""
    if not q and not todb_id:
        raise HTTPException(status_code=400, detail="请提供搜索关键词 q 或 todb_id")
    try:
        results = _service().search_targets(
            video_type=video_type or None,
            title=q or None,
            todb_id=todb_id or None,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        raise _fail(exc)


@router.get("/video/base")
async def video_base(item_type: str, item_id: str):
    """获取 Emos 上传目标详情"""
    try:
        return {"success": True, "data": _service().video_base(item_type, item_id)}
    except Exception as exc:
        raise _fail(exc)


# 注意：/tasks/clear 必须注册在 /tasks/{task_id} 之前
@router.delete("/tasks/clear")
async def clear_tasks():
    """清空已完成/失败的任务记录"""
    try:
        return {"success": True, "removed": _service().clear_finished()}
    except Exception as exc:
        raise _fail(exc)


@router.get("/tasks")
async def list_tasks():
    """列出在线上传任务"""
    try:
        return {"success": True, "tasks": _service().list_tasks()}
    except Exception as exc:
        raise _fail(exc)


@router.post("/tasks")
async def create_tasks(request: TasksCreateRequest):
    """批量创建在线上传任务"""
    try:
        return _service().create_tasks([item.model_dump() for item in request.items])
    except Exception as exc:
        raise _fail(exc)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """获取单个任务状态"""
    try:
        task = _service().get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"success": True, "task": task}
    except HTTPException:
        raise
    except Exception as exc:
        raise _fail(exc)


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    """重试失败任务"""
    try:
        return {"success": True, "task": _service().retry_task(task_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise _fail(exc)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务记录"""
    try:
        if not _service().delete_task(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise _fail(exc)


@router.post("/save-internal")
async def save_internal(request: SaveInternalRequest):
    """内部入库（文件已经位于 Emos 存储上时使用）"""
    try:
        data = _service().save_internal(
            request.item_type,
            request.item_id,
            request.file_path,
            request.file_size or 0,
            path_type=request.path_type,
            upload_username=request.upload_username,
        )
        return {"success": True, "data": data}
    except Exception as exc:
        raise _fail(exc)
