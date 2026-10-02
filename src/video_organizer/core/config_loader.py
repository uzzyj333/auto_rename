import os
import re
import sys
import json
import configparser
import logging
from typing import Dict, Any, List, Optional

# 配置日志记录器
logger = logging.getLogger(__name__)

# 支持多种下载器类型（同一类型可配置多个实例）
KNOWN_DOWNLOADER_TYPES = ("aria2", "qbittorrent")


def normalize_extensions(value) -> List[str]:
    """把「支持的扩展名」配置统一成小写扩展名列表

    配置可能来自 INI 字符串（``.mp4,.mkv``）或 Web 端 JSON 数组；
    如果直接对字符串做迭代会得到单个字符（``['.', 'm', 'p', ...]``），
    导致 ``str.endswith()`` 匹配到几乎所有文件。这里统一按逗号 / 空白切分。
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [item for item in re.split(r"[,\s;]+", value) if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            if item is None:
                continue
            parts.extend(
                [piece for piece in re.split(r"[,\s;]+", str(item)) if piece.strip()]
            )
    else:
        parts = [str(value)]
    normalized: List[str] = []
    for item in parts:
        text = str(item).strip().lower()
        if not text:
            continue
        if not text.startswith("."):
            text = "." + text
        if text not in normalized:
            normalized.append(text)
    return normalized


def _split_path_mapping(text: str):
    """拆分 ``下载器路径:本地路径``（Windows 盘符里的冒号不算分隔符）"""
    text = text.strip()
    if not text:
        return None
    start = 2 if (len(text) > 2 and text[1] == ":" and text[2] in "\\/") else 0
    idx = text.find(":", start)
    if idx == -1:
        return None
    key = text[:idx].strip()
    value = text[idx + 1 :].strip()
    if not key or not value:
        return None
    return key, value


def normalize_path_mappings(value) -> Dict[str, str]:
    """把「路径映射」配置统一成 ``{下载器路径: 本地路径}`` 字典

    支持多种来源：INI 旧格式字符串（``/downloads:F:/Downloads``）、
    Web 端 JSON 字符串（``{"\\/downloads": "F:/Downloads"}``）、
    以及已经是字典 / 列表的情况。

    以前只有 ``load_config`` 会做转换，Web 端把表单文本直接写回内存里的
    共享配置字典，``path_mappings`` 就变成字符串；下载完成事件在
    ``path_mappings.items()`` 处抛 ``'str' object has no attribute 'items'``，
    表现就是「下载完了却一直不识别 / 不自动上传」。
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return {
            str(k).strip(): str(v).strip()
            for k, v in value.items()
            if str(k).strip()
        }
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        # Web 端可能写入 json.dumps 的结果
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return {
                    str(k).strip(): str(v).strip()
                    for k, v in parsed.items()
                    if str(k).strip()
                }
        items = [item for item in re.split(r"[,\n;]+", text) if item.strip()]
    else:
        return {}
    result: Dict[str, str] = {}
    for item in items:
        if isinstance(item, dict):
            for k, v in item.items():
                key = str(k).strip()
                if key:
                    result[key] = str(v).strip()
            continue
        pair = _split_path_mapping(str(item))
        if pair:
            result[pair[0]] = pair[1]
    return result


def format_path_mappings(mappings) -> str:
    """把路径映射序列化成 INI 旧格式字符串（``a:b,c:d``），便于 load_config 解析回来"""
    return ",".join(f"{k}:{v}" for k, v in normalize_path_mappings(mappings).items())


def derive_downloader_type(instance_id: str) -> str:
    """
    从 downloader.<instance_id> 节名推导下载器类型。

    支持同一类型配置多个实例：
    - "aria2"       -> "aria2"
    - "aria2_2"     -> "aria2"
    - "qbittorrent" -> "qbittorrent"

    Args:
        instance_id: 配置节中 downloader. 后面的部分

    Returns:
        下载器类型，无法识别时返回清洗后的名称
    """
    text = (instance_id or "").strip().lower()
    if not text:
        return ""
    if text in KNOWN_DOWNLOADER_TYPES:
        return text
    # 去掉结尾的实例序号，例如 aria2_2 / aria2-2 / aria2.2
    base = re.sub(r"[\-_.]?\d+$", "", text)
    return base or text


# 默认配置值
DEFAULT_CONFIG = {
    "monitoring": {
        "watch_dir": "",
        "output_dir": "",
        "poll_interval": 10,
        "supported_extensions": [
            ".mp4",
            ".mkv",
            ".avi",
            ".mov",
            ".wmv",
            ".flv",
            ".ts",
            ".m2ts",
            ".iso",
            ".strm",
            ".webm",
            ".m4v",
            ".mpg",
            ".mpeg",
            ".rmvb",
            ".srt",
            ".ass",
            ".ssa",
            ".vtt",
            ".sub",
        ],
        "use_polling": False,
        "polling_interval": 5,
        "path_mappings": {},  # 用于将下载器返回的路径映射到主机实际路径，例如："/downloads": "F:/Downloads"
        # 目录监控相关配置
        "enable_directory_monitor": False,
        "directory_watch_dir": "",
        "directory_output_dir": "",
        "directory_organize_mode": "copy",
        "directory_scrape_metadata": True,
        "directory_metadata_format": "nfo",
        "directory_polling_interval": 5,
    },
    "emos": {
        "auth_token": "",
        "base_url": "https://emos.best",
        "file_storage": "internal",  # internal / global / default / google_drive / zn_r2_upload
        "file_storages": "internal,default,google_drive,zn_r2_upload",  # 在线上传页面可选的存储列表
        "chunk_size_mb": 50,
        "upload_concurrency": 10,  # 分片并发上传数（1-32）
        "upload_subtitles": True,  # 上传视频时顺带上传同目录同名的外挂字幕
        "timeout": 60,
    },
    "online_upload": {
        "video_root": "",  # 允许浏览/上传的视频根目录，多个用逗号分隔（留空则使用监控目录）
        "probe_enabled": True,  # 上传前用 ffprobe 校验并提取视频信息
        "ffprobe_path": "",  # ffprobe 可执行文件路径，留空则使用 PATH
        "path_type": "local_emos_1",  # saveInternal 使用的 path_type
    },
    "naming": {
        "tv_show_format": "{show_name}/Season {season:02d}/{show_name} {season_episode} {quality_tags}",
        "movie_format": "{movie_name}{year_suffix}/{movie_name}{year_suffix} {quality_tags}",
        "anime_format": "{anime_name}/{season_name}/{anime_name} - S{season:02d}E{episode:02d} {quality_tags}",
        "simple_format": "{title} {quality_tags}",
    },
    "tmdb": {
        "api_key": "",
        "language": "zh-CN",
        "region": "CN",
        "retry_count": 3,
        "timeout": 30,
    },
    "processing": {
        "rename_only": False,
        "copy_mode": False,
        "delete_original": False,
        "delete_after_upload": False,
        "min_file_size": 0,
        "ignore_patterns": [],
        "upload_targets": "emos",
        "max_upload_workers": 3,  # 并行上传工作线程数
    },
    "logging": {
        "log_level": "INFO",
        "log_file": "video-organizer.log",
        "console_log": True,
        "file_log": True,
    },
    "telegram": {
        "bot_token": "",  # Telegram Bot Token（@BotFather 获取）
        "chat_id": "",  # 绑定的会话 ID，可留空后在 Telegram 发送 /bind 自动绑定
        "enabled": True,  # 是否启用 Telegram 通知
        "reply_enabled": True,  # 是否启用机器人回复修正（长轮询接收消息）
        "allowed_user_ids": "",  # 允许操作的用户 ID，逗号分隔，留空表示不限制
        "poll_timeout": 30,  # getUpdates 长轮询超时（秒）
    },
    "llm_fallback": {"enabled": False, "max_concurrent": 2},
    "llm_provider_1": {"name": "", "api_url": "", "api_key": "", "model": "", "enabled": False, "weight": 1, "timeout": 30, "max_retries": 2},
    "llm_provider_2": {"name": "", "api_url": "", "api_key": "", "model": "", "enabled": False, "weight": 1, "timeout": 30, "max_retries": 2},
    "llm_provider_3": {"name": "", "api_url": "", "api_key": "", "model": "", "enabled": False, "weight": 1, "timeout": 30, "max_retries": 2},
    "guessit": {
        "enabled": True,  # 是否启用 GuessIt 增强识别
        "prefer_guessit": False,  # 是否优先使用 GuessIt 结果
    },
    "manual_rules": {
        "enabled": False,  # 是否启用手动规则
        "normalize_symbols": True,  # 是否归一化规则中的符号
        "rules": [],  # 手动规则列表
    },
    "downloaders": [],
    "auth": {
        "enabled": False,
        "username": "admin",
        "password": "admin",
    },
}

def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载并验证配置文件
    
    Args:
        config_path: 配置文件路径，如果不提供则使用默认路径
    
    Returns:
        配置字典
    
    Raises:
        FileNotFoundError: 如果配置文件不存在
        ValueError: 如果配置无效
    """
    if not config_path:
        # 检查是否为打包后的环境
        if getattr(sys, "frozen", False):
            # 如果是打包后的exe，配置文件在exe同级目录
            base_dir = os.path.dirname(sys.executable)
            config_path = os.path.join(base_dir, "config.ini")
        else:
            # 开发环境：使用项目内配置文件路径
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "config.ini"
            )
    
    # 检查配置文件是否存在
    if not os.path.exists(config_path):
        logger.warning(f"配置文件不存在: {config_path}")
        try:
            directory = os.path.dirname(config_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            save_default_config(config_path)
            logger.info(f"已创建默认配置文件: {config_path}")
        except OSError as exc:
            # 目录只读（例如容器挂载权限不对）时不要直接崩溃，先用内存默认配置跑起来
            logger.warning(f"无法写入默认配置文件，改用内存中的默认配置: {exc}")
    
    config = configparser.ConfigParser()
    # 使用UTF-8编码读取配置文件，避免编码错误
    config.read(config_path, encoding="utf-8")
    
    # 转换为字典并验证
    config_dict = _config_to_dict(config)
    
    # 验证必要的配置项
    if not _validate_config(config_dict):
        raise ValueError("配置验证失败，请检查配置文件")
    
    return config_dict


def save_default_config(config_path: str) -> None:
    """
    保存默认配置到文件
    
    Args:
        config_path: 配置文件路径
    """
    config = configparser.ConfigParser()
    
    # 设置默认配置
    for section, options in DEFAULT_CONFIG.items():
        # 跳过列表类型的配置（如下载器），它们不能直接作为INI的一个节
        if not isinstance(options, dict):
            continue
        
        config[section] = {}
        for key, value in options.items():
            if isinstance(value, list):
                config[section][key] = ",".join(value)
            else:
                config[section][key] = str(value)
    
    # 写入配置文件
    with open(config_path, "w", encoding="utf-8") as f:
        config.write(f)


def _config_to_dict(config: configparser.ConfigParser) -> Dict[str, Any]:
    """
    将配置对象转换为字典，同时合并默认配置
    
    Args:
        config: 配置对象
    
    Returns:
        配置字典
    """
    config_dict = {}
    
    # 合并默认配置和用户配置
    for section, default_options in DEFAULT_CONFIG.items():
        config_dict[section] = default_options.copy()
        
        # 如果配置中有该节
        if section in config:
            for key, value in config[section].items():
                # 根据默认值类型转换
                if key in default_options:
                    if isinstance(default_options[key], bool):
                        config_dict[section][key] = config[section].getboolean(key)
                    elif isinstance(default_options[key], int):
                        config_dict[section][key] = config[section].getint(key)
                    elif isinstance(default_options[key], list):
                        # 对于 manual_rules.rules，稍后特殊处理
                        if section == "manual_rules" and key == "rules":
                            # 不在这里处理，稍后统一处理
                            pass
                        else:
                            config_dict[section][key] = [
                                item.strip()
                                for item in config[section].get(key, "").split(",")
                                if item.strip()
                            ]
                    elif isinstance(default_options[key], dict):
                        # 特殊处理字典类型，用于path_mappings配置
                        if key == "path_mappings":
                            config_dict[section][key] = normalize_path_mappings(
                                config[section].get(key, "")
                            )
                        else:
                            config_dict[section][key] = config[section].get(key)
                    else:
                        # 对于字符串类型（如 api_key），直接从配置文件读取值
                        config_dict[section][key] = value
                else:
                    # 对于未知配置项，保留为字符串
                    config_dict[section][key] = value
    
    # 保留不在 DEFAULT_CONFIG 中的自定义节（如 downloader.xxx）
    for section in config.sections():
        if section not in DEFAULT_CONFIG:
            config_dict[section] = {}
            for key, value in config[section].items():
                config_dict[section][key] = value
    
    # 特殊处理 manual_rules 节中的规则配置
    if "manual_rules" in config_dict and "manual_rules" in config:
        rules_list = []
        manual_section = config["manual_rules"]
        
        # 方式1：如果配置了 rules 键（用 | 分隔的规则字符串）
        if "rules" in manual_section:
            rules_str = manual_section.get("rules", "")
            if rules_str:
                for rule_str in rules_str.split("|"):
                    rule_str = rule_str.strip()
                    if rule_str:
                        rules_list.append({"rule": rule_str, "enabled": True})
        
        # 方式2：收集所有 rule 开头的键（rule1, rule2, ...）
        for k, v in manual_section.items():
            if k.startswith("rule") and k != "rules" and k != "enabled":
                rule_str = v.strip()
                if rule_str:
                    rules_list.append({"rule": rule_str, "enabled": True})
        
        # 更新 rules 列表
        config_dict["manual_rules"]["rules"] = rules_list
        if rules_list:
            logger.info(f"从配置文件加载了 {len(rules_list)} 条手动规则")
    
    # 特殊处理命名规则：保留 naming 节（供在线配置界面展示），同时派生 naming_rules
    if "naming" in config_dict:
        naming = config_dict["naming"]
        config_dict["naming_rules"] = {
            "tv_show": naming.get("tv_show_format", ""),
            "movie": naming.get("movie_format", ""),
            "anime": naming.get("anime_format", ""),
            "simple": naming.get("simple_format", ""),
        }
    
    # 特殊处理下载器配置（支持同一类型多个实例，如 downloader.aria2_1 / downloader.aria2_2）
    config_dict["downloaders"] = []
    for section in config.sections():
        if section.startswith("downloader."):
            instance_id = section.split(".", 1)[1]
            explicit_type = (config[section].get("type") or "").strip().lower()
            downloader_type = explicit_type or derive_downloader_type(instance_id)
            if not downloader_type:
                logger.warning(f"无法解析下载器类型，已跳过: {section}")
                continue
            downloader_config = {
                "type": downloader_type,
                "id": instance_id,
                "section": section,
            }
            for key, value in config[section].items():
                downloader_config[key] = value
            # 显式 type 字段优先，其余情况按节名推导
            downloader_config["type"] = downloader_type
            config_dict["downloaders"].append(downloader_config)
    
    return config_dict


def _validate_config(config: Dict[str, Any]) -> bool:
    """
    验证配置有效性
    
    Args:
        config: 配置字典
    
    Returns:
        配置是否有效
    """
    is_valid = True
    
    # 验证监控配置
    if "monitoring" in config:
        # 对于输出目录，不再强制验证，因为我们现在使用下载器监控模式
        output_dir = config["monitoring"].get("output_dir", "")
        if not output_dir:
            logger.info("输出目录未配置，当前使用下载器监控模式")
        elif not os.path.exists(output_dir):
            # 尝试创建输出目录
            try:
                os.makedirs(output_dir)
                logger.info(f"已创建输出目录: {output_dir}")
            except Exception as e:
                logger.info(f"创建输出目录失败: {e}，当前使用下载器监控模式")
        
        # 对于监控目录，不再强制验证，因为我们现在使用下载器监控
        watch_dir = config["monitoring"].get("watch_dir", "")
        if not watch_dir:
            logger.info("监控目录未配置，当前使用下载器监控模式")
        elif not os.path.exists(watch_dir):
            logger.info(f"监控目录不存在: {watch_dir}，当前使用下载器监控模式")
    
    # 验证TMDB API密钥
    if "tmdb" in config:
        api_key = config["tmdb"].get("api_key", "")
        if not api_key:
            logger.warning("TMDB API密钥未配置，元数据刮削功能将不可用")
    
    # 验证命名规则
    if "naming_rules" in config:
        for rule_type, rule in config["naming_rules"].items():
            if not rule:
                logger.error(f"命名规则 {rule_type} 不能为空")
                is_valid = False
    
    return is_valid


def update_config(
    config_dict: Dict[str, Any], config_path: Optional[str] = None
) -> None:
    """
    更新配置文件
    
    Args:
        config_dict: 配置字典
        config_path: 配置文件路径
    """
    if not config_path:
        # 检查是否为打包后的环境
        if getattr(sys, "frozen", False):
            # 如果是打包后的exe，配置文件在exe同级目录
            base_dir = os.path.dirname(sys.executable)
            config_path = os.path.join(base_dir, "config.ini")
        else:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "config.ini"
            )
    
    config = configparser.ConfigParser()
    
    # 转换字典为配置对象
    for section, options in config_dict.items():
        if not isinstance(options, dict):
            continue
        # 特殊处理naming_rules：以 naming 节为准（在线配置界面直接编辑 *_format）
        if section == "naming_rules":
            if "naming" not in config:
                config["naming"] = {}
            naming_section = config_dict.get("naming") or {}
            for key, value in options.items():
                fmt_key = f"{key}_format"
                if fmt_key in naming_section:
                    value = naming_section[fmt_key]
                config["naming"][fmt_key] = value
        else:
            config[section] = {}
            for key, value in options.items():
                # manual_rules.rules 由下面的 ruleN 写回，跳过
                if section == "manual_rules" and key == "rules":
                    continue
                if key == "path_mappings":
                    # 统一写成 INI 旧格式，保证 load_config 能原样解析回来
                    config[section][key] = format_path_mappings(value)
                elif isinstance(value, list):
                    try:
                        config[section][key] = ",".join(value)
                    except TypeError:
                        config[section][key] = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, dict):
                    config[section][key] = json.dumps(value, ensure_ascii=False)
                else:
                    config[section][key] = str(value)

    # 写回 manual_rules.rules 为 rule1, rule2, ... 条目
    manual_rules = config_dict.get("manual_rules", {})
    rules_list = manual_rules.get("rules", [])
    if isinstance(rules_list, list) and "manual_rules" in config:
        rule_idx = 1
        for rule_entry in rules_list:
            if isinstance(rule_entry, dict):
                rule_text = rule_entry.get("rule", "").strip()
                if rule_text:
                    config["manual_rules"][f"rule{rule_idx}"] = rule_text
                    rule_idx += 1
    
    # 写回 downloader.xxx 节（从 config_dict 中的 dict 键直接写入）
    for section_name in list(config_dict.keys()):
        if section_name.startswith("downloader."):
            options = config_dict[section_name]
            if not isinstance(options, dict):
                continue
            config[section_name] = {}
            for key, value in options.items():
                if key == "path_mappings":
                    config[section_name][key] = format_path_mappings(value)
                else:
                    config[section_name][key] = str(value)
    
    # 保存配置文件
    parent_dir = os.path.dirname(config_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        config.write(f)
