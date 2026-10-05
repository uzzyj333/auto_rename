"""
配置管理路由

提供配置的读取和更新 API，包括 INI 配置和数据库配置。
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.state import get_state_manager
from ...database.session import get_session_local
from ...database.models import ManualRule, ReleaseGroupMapping, LlmProvider

logger = logging.getLogger(__name__)

router = APIRouter()


def _apply_runtime_config(config: Dict[str, Any]) -> None:
    """把最新配置同步到视频处理器、下载器监控、在线识别上传服务与 Telegram 机器人（无需重启容器）"""
    handler = None
    try:
        handler = get_state_manager().get_video_handler()
        if handler is not None and hasattr(handler, "apply_config"):
            handler.apply_config(config)
    except Exception as e:
        logger.warning(f"配置热更新同步到视频处理器失败: {e}")

    # 日志等级在线生效（无需重启容器）：日志只需配置等级，控制台/文件输出始终开启
    try:
        from ...utils.logging_utils import setup_logging

        setup_logging(config.get("logging") or {})
    except Exception as e:
        logger.warning(f"配置热更新同步到日志系统失败: {e}")

    # 下载器配置（含多个 aria2 实例）在线修改后立即生效，无需重启容器
    try:
        parent = getattr(handler, "_parent_monitor", None) if handler is not None else None
        if parent is not None and hasattr(parent, "reload_downloader_monitors"):
            monitors = parent.reload_downloader_monitors(config.get("downloaders") or [])
            get_state_manager().set_downloader_monitors(monitors)
    except Exception as e:
        logger.warning(f"配置热更新同步到下载器监控失败: {e}")
    # 「支持的扩展名」在线修改后立即生效（文件处理器 + 各下载器监控）
    try:
        parent = getattr(handler, "_parent_monitor", None) if handler is not None else None
        extensions = (config.get("monitoring") or {}).get("supported_extensions")
        if parent is not None and extensions and hasattr(
            parent, "update_supported_extensions"
        ):
            parent.update_supported_extensions(extensions)
    except Exception as e:
        logger.warning(f"配置热更新同步支持的扩展名失败: {e}")
    # 监控器自身持有的配置（路径映射等）也要刷新，否则在线改完不生效
    try:
        parent = getattr(handler, "_parent_monitor", None) if handler is not None else None
        if parent is not None and hasattr(parent, "apply_config"):
            parent.apply_config(config)
    except Exception as e:
        logger.warning(f"配置热更新同步到监控器失败: {e}")
    try:
        config_path = get_state_manager().get_config_path()
    except Exception:
        config_path = None
    try:
        from ...core.online_upload import OnlineUploadService

        OnlineUploadService.instance().configure(config, config_path)
    except Exception as e:
        logger.warning(f"配置热更新同步到在线识别上传服务失败: {e}")
    try:
        from ...core.telegram_bot import TelegramBotService

        TelegramBotService.instance().configure(config, config_path)
    except Exception as e:
        logger.warning(f"配置热更新同步到 Telegram 机器人失败: {e}")


class ConfigUpdateRequest(BaseModel):
    section: str
    key: str
    value: Any


class ConfigSectionUpdateRequest(BaseModel):
    section: str
    values: Dict[str, Any]


class ConfigResponse(BaseModel):
    success: bool
    message: str
    config: Optional[Dict[str, Any]] = None


# ===== DB Config Request Models =====

class ManualRuleCreateRequest(BaseModel):
    rule_text: str
    enabled: bool = True
    sort_order: int = 0


class ReleaseGroupCreateRequest(BaseModel):
    group_name: str
    content_type: str


class LlmProviderCreateRequest(BaseModel):
    name: str
    api_url: str
    api_key: Optional[str] = ""
    model: Optional[str] = ""
    enabled: bool = True
    weight: int = 1
    timeout: int = 30
    max_retries: int = 2


# ===== DB Config CRUD Endpoints (must be before /{section} catch-all) =====


# 手动规则
@router.get("/db/manual-rules")
async def get_manual_rules():
    try:
        with get_session_local()() as db:
            rules = db.query(ManualRule).order_by(ManualRule.sort_order).all()
            return {"success": True, "rules": [
                {"id": r.id, "rule_text": r.rule_text, "enabled": r.enabled, "sort_order": r.sort_order,
                 "created_at": r.created_at.isoformat() if r.created_at else None,
                 "updated_at": r.updated_at.isoformat() if r.updated_at else None}
                for r in rules
            ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取手动规则失败: {e}")


@router.post("/db/manual-rules", status_code=201)
async def create_manual_rule(request: ManualRuleCreateRequest):
    try:
        with get_session_local()() as db:
            rule = ManualRule(
                rule_text=request.rule_text,
                enabled=request.enabled,
                sort_order=request.sort_order,
                created_at=datetime.now(),
            )
            db.add(rule)
            db.commit()
            return {"success": True, "id": rule.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建手动规则失败: {e}")


@router.put("/db/manual-rules/{rule_id}")
async def update_manual_rule(rule_id: int, request: ManualRuleCreateRequest):
    try:
        with get_session_local()() as db:
            rule = db.query(ManualRule).filter(ManualRule.id == rule_id).first()
            if not rule:
                raise HTTPException(status_code=404, detail="规则不存在")
            rule.rule_text = request.rule_text
            rule.enabled = request.enabled
            rule.sort_order = request.sort_order
            rule.updated_at = datetime.now()
            db.commit()
            return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新手动规则失败: {e}")


@router.delete("/db/manual-rules/{rule_id}")
async def delete_manual_rule(rule_id: int):
    try:
        with get_session_local()() as db:
            rule = db.query(ManualRule).filter(ManualRule.id == rule_id).first()
            if not rule:
                raise HTTPException(status_code=404, detail="规则不存在")
            db.delete(rule)
            db.commit()
            return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除手动规则失败: {e}")


# 字幕组映射
@router.get("/db/release-groups")
async def get_release_groups():
    try:
        with get_session_local()() as db:
            groups = db.query(ReleaseGroupMapping).order_by(ReleaseGroupMapping.group_name).all()
            return {"success": True, "groups": [
                {"id": g.id, "group_name": g.group_name, "content_type": g.content_type,
                 "created_at": g.created_at.isoformat() if g.created_at else None}
                for g in groups
            ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取字幕组映射失败: {e}")


@router.post("/db/release-groups", status_code=201)
async def create_release_group(request: ReleaseGroupCreateRequest):
    try:
        with get_session_local()() as db:
            existing = db.query(ReleaseGroupMapping).filter(
                ReleaseGroupMapping.group_name == request.group_name
            ).first()
            if existing:
                raise HTTPException(status_code=409, detail="字幕组已存在")
            group = ReleaseGroupMapping(
                group_name=request.group_name,
                content_type=request.content_type,
                created_at=datetime.now(),
            )
            db.add(group)
            db.commit()
            return {"success": True, "id": group.id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建字幕组映射失败: {e}")


@router.put("/db/release-groups/{group_id}")
async def update_release_group(group_id: int, request: ReleaseGroupCreateRequest):
    try:
        with get_session_local()() as db:
            group = db.query(ReleaseGroupMapping).filter(ReleaseGroupMapping.id == group_id).first()
            if not group:
                raise HTTPException(status_code=404, detail="字幕组不存在")
            group.group_name = request.group_name
            group.content_type = request.content_type
            group.updated_at = datetime.now()
            db.commit()
            return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新字幕组映射失败: {e}")


@router.delete("/db/release-groups/{group_id}")
async def delete_release_group(group_id: int):
    try:
        with get_session_local()() as db:
            group = db.query(ReleaseGroupMapping).filter(ReleaseGroupMapping.id == group_id).first()
            if not group:
                raise HTTPException(status_code=404, detail="字幕组不存在")
            db.delete(group)
            db.commit()
            return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除字幕组映射失败: {e}")


# LLM 提供商
@router.get("/db/llm-providers")
async def get_llm_providers():
    try:
        with get_session_local()() as db:
            providers = db.query(LlmProvider).order_by(LlmProvider.weight.desc()).all()
            return {"success": True, "providers": [
                {"id": p.id, "name": p.name, "api_url": p.api_url, "api_key": '***' if p.api_key else '',
                 "has_key": bool(p.api_key),
                 "model": p.model, "enabled": p.enabled, "weight": p.weight,
                 "timeout": p.timeout, "max_retries": p.max_retries,
                 "created_at": p.created_at.isoformat() if p.created_at else None}
                for p in providers
            ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取 LLM 提供商失败: {e}")


@router.post("/db/llm-providers", status_code=201)
async def create_llm_provider(request: LlmProviderCreateRequest):
    try:
        with get_session_local()() as db:
            provider = LlmProvider(
                name=request.name,
                api_url=request.api_url,
                api_key=request.api_key or "",
                model=request.model or "",
                enabled=request.enabled,
                weight=request.weight,
                timeout=request.timeout,
                max_retries=request.max_retries,
                created_at=datetime.now(),
            )
            db.add(provider)
            db.commit()
            return {"success": True, "id": provider.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建 LLM 提供商失败: {e}")


@router.put("/db/llm-providers/{provider_id}")
async def update_llm_provider(provider_id: int, request: LlmProviderCreateRequest):
    try:
        with get_session_local()() as db:
            p = db.query(LlmProvider).filter(LlmProvider.id == provider_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="提供商不存在")
            p.name = request.name
            p.api_url = request.api_url
            if request.api_key:
                p.api_key = request.api_key
            p.model = request.model or ""
            p.enabled = request.enabled
            p.weight = request.weight
            p.timeout = request.timeout
            p.max_retries = request.max_retries
            p.updated_at = datetime.now()
            db.commit()
            return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新 LLM 提供商失败: {e}")


@router.delete("/db/llm-providers/{provider_id}")
async def delete_llm_provider(provider_id: int):
    try:
        with get_session_local()() as db:
            p = db.query(LlmProvider).filter(LlmProvider.id == provider_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="提供商不存在")
            db.delete(p)
            db.commit()
            return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除 LLM 提供商失败: {e}")


class TargetMappingRequest(BaseModel):
    match_type: str = "title"
    keyword: str
    media_type: Optional[str] = ""
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    item_type: str
    item_id: Any
    label: Optional[str] = ""
    storage: Optional[str] = ""
    enabled: bool = True


# 目标映射表（文字映射 / 剧集集数映射）
@router.get("/db/target-mappings")
async def get_target_mappings():
    try:
        from ...core.mapping_store import TargetMappingStore

        return {"success": True, "mappings": TargetMappingStore.list_all()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取目标映射表失败: {e}")


@router.post("/db/target-mappings", status_code=201)
async def create_target_mapping(request: TargetMappingRequest):
    try:
        from ...core.mapping_store import TargetMappingStore

        mapping_id = TargetMappingStore.create(
            keyword=request.keyword,
            item_type=request.item_type,
            item_id=request.item_id,
            match_type=request.match_type,
            media_type=request.media_type or "",
            season_number=request.season_number,
            episode_number=request.episode_number,
            label=request.label or "",
            storage=request.storage or "",
            source="web",
            enabled=request.enabled,
        )
        if mapping_id is None:
            raise HTTPException(status_code=500, detail="新增目标映射失败")
        return {"success": True, "id": mapping_id}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"新增目标映射失败: {e}")


@router.put("/db/target-mappings/{mapping_id}")
async def update_target_mapping(mapping_id: int, request: TargetMappingRequest):
    try:
        from ...core.mapping_store import TargetMappingStore

        ok = TargetMappingStore.update(
            mapping_id,
            match_type=request.match_type,
            keyword=request.keyword,
            media_type=request.media_type or "",
            season_number=request.season_number,
            episode_number=request.episode_number,
            item_type=request.item_type,
            item_id=request.item_id,
            label=request.label or "",
            storage=request.storage or "",
            enabled=request.enabled,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="映射不存在")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新目标映射失败: {e}")


@router.delete("/db/target-mappings/{mapping_id}")
async def delete_target_mapping(mapping_id: int):
    try:
        from ...core.mapping_store import TargetMappingStore

        if not TargetMappingStore.delete(mapping_id):
            raise HTTPException(status_code=404, detail="映射不存在")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除目标映射失败: {e}")


# ===== INI Config Endpoints =====


# ===== Telegram 机器人 =====


@router.get("/telegram/status")
async def get_telegram_status():
    """Telegram 机器人状态（运行中 / 已绑定 / 待回复报错数）"""
    try:
        from ...core.telegram_bot import TelegramBotService

        state = get_state_manager()
        service = TelegramBotService.instance()
        service.configure(state.get_config(), state.get_config_path())
        return {"success": True, **service.status()}
    except Exception as e:
        logger.error(f"获取 Telegram 状态失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取 Telegram 状态失败: {e}")


@router.post("/telegram/test")
async def send_telegram_test():
    """发送一条 Telegram 测试消息，验证 bot_token / chat_id 是否可用"""
    try:
        from ...core.telegram_bot import TelegramBotService

        state = get_state_manager()
        service = TelegramBotService.instance()
        service.configure(state.get_config(), state.get_config_path())
        return service.test_message()
    except Exception as e:
        logger.error(f"发送 Telegram 测试消息失败: {e}")
        raise HTTPException(status_code=500, detail=f"发送测试消息失败: {e}")


@router.get("", response_model=ConfigResponse)
async def get_config():
    try:
        state = get_state_manager()
        config = state.get_config()
        return ConfigResponse(success=True, message="获取配置成功", config=config)
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取配置失败: {e}")


@router.get("/{section}", response_model=ConfigResponse)
async def get_config_section(section: str):
    try:
        state = get_state_manager()
        config = state.get_config()
        if section not in config:
            raise HTTPException(status_code=404, detail=f"配置节 '{section}' 不存在")
        return ConfigResponse(
            success=True, message=f"获取配置节 '{section}' 成功",
            config={section: config[section]},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取配置节失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取配置节失败: {e}")


@router.put("/item", response_model=ConfigResponse)
async def update_config_item(request: ConfigUpdateRequest):
    try:
        state = get_state_manager()
        config = state.get_config()
        config_path = state.get_config_path()
        if request.section not in config:
            raise HTTPException(status_code=404, detail=f"配置节 '{request.section}' 不存在")
        value = request.value
        # 路径映射这类字典配置在 Web 端是文本框，保存的是字符串；
        # 直接写回共享配置字典会让监控器读到字符串并抛
        # 'str' object has no attribute 'items'，导致下载完成后不再自动上传
        if request.key == "path_mappings":
            from ...core.config_loader import normalize_path_mappings

            value = normalize_path_mappings(value)
        config[request.section][request.key] = value
        if config_path:
            from ...core.config_loader import update_config
            update_config(config, config_path)
        state.set_config(config, config_path)
        _apply_runtime_config(config)
        return ConfigResponse(
            success=True, message=f"配置项 '{request.section}.{request.key}' 已更新", config=config,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新配置失败: {e}")


@router.put("/section", response_model=ConfigResponse)
async def update_config_section(request: ConfigSectionUpdateRequest):
    try:
        state = get_state_manager()
        config = state.get_config()
        config_path = state.get_config_path()
        if request.section not in config:
            config[request.section] = {}
        values = dict(request.values or {})
        if "path_mappings" in values:
            from ...core.config_loader import normalize_path_mappings

            values["path_mappings"] = normalize_path_mappings(values["path_mappings"])
        config[request.section].update(values)
        if config_path:
            from ...core.config_loader import update_config, load_config
            update_config(config, config_path)
            new_config = load_config(config_path)
            state.set_config(new_config, config_path)
            effective_config = new_config
        else:
            state.set_config(config, config_path)
            effective_config = config
        _apply_runtime_config(effective_config)
        return ConfigResponse(success=True, message=f"配置节 '{request.section}' 已更新", config=config)
    except Exception as e:
        logger.error(f"更新配置节失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新配置节失败: {e}")


@router.delete("/section/{section}", response_model=ConfigResponse)
async def delete_config_section(section: str):
    try:
        state = get_state_manager()
        config = state.get_config()
        config_path = state.get_config_path()
        if section not in config:
            raise HTTPException(status_code=404, detail=f"配置节 '{section}' 不存在")
        del config[section]
        if config_path:
            from ...core.config_loader import update_config, load_config
            update_config(config, config_path)
            new_config = load_config(config_path)
            state.set_config(new_config, config_path)
            _apply_runtime_config(new_config)
        else:
            state.set_config(config, config_path)
        return ConfigResponse(success=True, message=f"配置节 '{section}' 已删除", config=config)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除配置节失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除配置节失败: {e}")

@router.post("/reload", response_model=ConfigResponse)
async def reload_config():
    try:
        state = get_state_manager()
        config_path = state.get_config_path()
        if not config_path or not config_path.exists():
            raise HTTPException(status_code=404, detail="配置文件不存在")
        from ...core.config_loader import load_config
        new_config = load_config(config_path)
        state.set_config(new_config, config_path)
        _apply_runtime_config(new_config)
        return ConfigResponse(success=True, message="配置已重新加载", config=new_config)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"重新加载配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"重新加载配置失败: {e}")


@router.get("/schema/description")
async def get_config_schema():
    schema = {
        "monitoring": {
            "description": "监控配置",
            "fields": {
                "watch_dir": "监控目录路径",
                "output_dir": "输出目录路径",
                "poll_interval": "轮询间隔（秒）",
                "supported_extensions": "支持的视频扩展名列表",
                "use_polling": "是否使用轮询模式",
                "polling_interval": "轮询模式扫描间隔（秒）",
            }
        },
        "tmdb": {
            "description": "TMDB API 配置",
            "fields": {
                "api_key": "TMDB API 密钥",
                "language": "语言代码（如 zh-CN）",
                "region": "地区代码（如 CN）",
            }
        },
        "logging": {
            "description": "日志配置（只需选择日志等级，日志文件由系统自动维护）",
            "fields": {
                "log_level": "日志等级（DEBUG / INFO / WARNING / ERROR）",
            }
        },
        "emos": {
            "description": "Emos 官方 API 配置",
            "fields": {
                "auth_token": "Emos 认证令牌（Bearer Token）",
                "base_url": "Emos 服务地址（如 https://emos.best）",
                "file_storage": "默认文件存储类型",
                "file_storages": "在线上传可选的存储列表",
                "chunk_size_mb": "分片大小（MB）",
                "upload_concurrency": "分片并发上传数（1-32）",
                "upload_subtitles": "上传视频时顺带上传同目录同名的外挂字幕",
                "timeout": "请求超时（秒）",
            }
        },
        "online_upload": {
            "description": "在线识别上传配置",
            "fields": {
                "video_root": "允许浏览/上传的视频根目录（多个用逗号分隔）",
                "probe_enabled": "上传前是否用 ffprobe 校验视频",
                "ffprobe_path": "ffprobe 可执行文件路径",
                "path_type": "saveInternal 使用的 path_type",
            }
        },
        "naming": {
            "description": "命名规则配置",
            "fields": {
                "tv_show": "电视剧命名模板",
                "movie": "电影命名模板",
                "anime": "动漫命名模板",
            }
        },
        "telegram": {
            "description": "Telegram 报错通知与机器人回复修正",
            "fields": {
                "bot_token": "Telegram Bot Token（@BotFather 获取）",
                "chat_id": "绑定的会话 ID，留空可在 Telegram 发送 /bind 自动绑定",
                "enabled": "是否启用 Telegram 通知",
                "reply_enabled": "是否启用机器人回复修正（长轮询接收消息）",
                "allowed_user_ids": "允许操作的用户 ID，逗号分隔，留空不限制",
                "poll_timeout": "getUpdates 长轮询超时（秒）",
            }
        },
        "processing": {
            "description": "处理配置",
            "fields": {
                "upload_targets": "上传目标（固定为 emos）",
                "delete_after_upload": "上传完成后是否删除源文件",
                "max_upload_workers": "最大并发上传线程数（在线修改立即生效）",
            }
        },
        "guessit": {
            "description": "GuessIt 增强识别配置",
            "fields": {
                "enabled": "是否启用 GuessIt 增强识别",
                "prefer_guessit": "是否优先使用 GuessIt 结果",
            }
        },
    }
    return {"success": True, "schema": schema}
