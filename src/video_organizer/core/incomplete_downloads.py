# -*- coding: utf-8 -*-
"""未完成下载检测

从已启动的下载器监控（aria2 / qBittorrent）收集「还在下载中」的文件路径，
用于在手动识别本地文件时把这些尚未下载完的文件排除掉，避免识别到半成品。

下载器不可用或未配置时安全降级为空集合；整体带超时，避免某个下载器无响应
时拖慢目录扫描。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Iterable, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0


def path_key(path: Any) -> str:
    """路径归一化：统一分隔符 + 转小写，用于跨平台比较"""
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normpath(text).replace("\\", "/").lower()


def _resolve_monitors(monitors: Optional[Iterable[Any]]) -> Iterable[Any]:
    if monitors is not None:
        return monitors
    try:
        from ..web.services.state import get_state_manager

        return get_state_manager().get_downloader_monitors()
    except Exception as exc:
        logger.debug("获取下载器监控失败，跳过未完成下载检测: %s", exc)
        return []


def _collect(monitors: Optional[Iterable[Any]]) -> Set[str]:
    result: Set[str] = set()
    for monitor in _resolve_monitors(monitors) or []:
        getter = getattr(monitor, "get_incomplete_paths", None)
        if not callable(getter):
            continue
        try:
            result |= {path_key(p) for p in (getter() or set()) if p}
        except Exception as exc:
            logger.debug("收集下载器未完成文件失败: %s", exc)
    result.discard("")
    return result


def collect_incomplete_paths(
    monitors: Optional[Iterable[Any]] = None, timeout: float = DEFAULT_TIMEOUT
) -> Set[str]:
    """汇总所有下载器中未完成下载的文件路径（归一化）

    Args:
        monitors: 下载器监控列表，None 表示从全局状态管理器获取
        timeout: 最长等待秒数，超时返回空集合（避免拖慢扫描）
    """
    if not timeout or timeout <= 0:
        return _collect(monitors)
    holder: Set[str] = set()

    def _work() -> None:
        nonlocal holder
        holder = _collect(monitors)

    worker = threading.Thread(target=_work, name="incomplete-downloads", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.debug("未完成下载检测超时（%.1fs），本次跳过", timeout)
        return set()
    return holder


def is_incomplete(path: Any, incomplete: Optional[Iterable[str]] = None) -> bool:
    """判断给定文件是否仍在下载中"""
    if incomplete is None:
        incomplete = collect_incomplete_paths()
    key = path_key(path)
    return bool(key) and key in set(incomplete or ())
