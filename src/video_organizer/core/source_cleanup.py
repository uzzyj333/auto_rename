# -*- coding: utf-8 -*-
"""上传成功后处理原文件（跟随 ``processing.delete_after_upload``）

手动选片上传、自动上传（监控/下载器）、Telegram 回复修正重传三条链路共用同一套逻辑：

1. 文件由下载器管理 → 先删除下载任务；任务删掉后若文件仍在（aria2 只删记录不删文件）再兜底删文件；
2. 下载器里还有未完成的任务 → 暂不删除，避免破坏正在做种的种子；
3. 文件与下载器无关（例如手动放进媒体库）→ 直接删除；
4. Windows 上文件被占用时，退化为 PowerShell 后台作业持续重试删除。
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 下载器清理回调：返回 True=已删除下载任务，False=下载器仍有未完成任务，None=与下载器无关
DownloaderCleanup = Callable[[str], Optional[bool]]


def _windows_background_delete(file_path: str, max_retries: int, retry_interval: int) -> bool:
    """Windows：用 PowerShell 后台作业持续重试删除（进程退出后仍可继续）"""
    try:
        ps_script = (
            f'$path = "{file_path}"; '
            f"$maxRetries = {max_retries}; "
            f"$delay = {retry_interval}; "
            "Start-Sleep -Seconds 3; "
            "for($i=0; $i -lt $maxRetries; $i++) { "
            "    if(Test-Path $path) { "
            "        try { "
            "            Remove-Item -LiteralPath $path -Force -ErrorAction Stop; "
            "            exit 0 "
            "        } catch { "
            "            Start-Sleep -Seconds $delay "
            "        } "
            "    } else { "
            "        exit 0 "
            "    } "
            "}; "
            "exit 1"
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_script],
            creationflags=creation_flags,
        )
        logger.info("已启动后台重试删除文件: %s", file_path)
        return True
    except Exception as exc:
        logger.warning("启动后台重试删除失败: %s", exc)
        return False


def delete_file_with_retry(
    file_path: str,
    max_retries: int = 20,
    retry_interval: int = 5,
) -> bool:
    """删除文件；Windows 上被占用时转为后台重试删除"""
    try:
        os.remove(file_path)
        logger.info("已删除原文件: %s", file_path)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("直接删除文件失败(%s)，尝试后台重试: %s", exc, file_path)
    if os.name == "nt":
        return _windows_background_delete(file_path, max_retries, retry_interval)
    return False


def cleanup_uploaded_source(
    file_path: str,
    downloader_cleanup: Optional[DownloaderCleanup] = None,
    *,
    max_retries: int = 20,
    retry_interval: int = 5,
) -> Dict[str, Any]:
    """上传成功后处理原文件

    Args:
        file_path: 原文件路径
        downloader_cleanup: 可选回调，见 ``DownloaderCleanup``

    Returns:
        {"deleted": bool, "reason": str, "path": str}
    """
    outcome: Dict[str, Any] = {"deleted": False, "reason": "", "path": str(file_path or "")}
    path = str(file_path or "")
    if not path:
        outcome["reason"] = "文件路径为空"
        return outcome
    if not os.path.exists(path):
        outcome.update(deleted=True, reason="文件已不存在")
        return outcome

    if downloader_cleanup is not None:
        try:
            state = downloader_cleanup(path)
        except Exception as exc:
            logger.warning("清理下载任务失败: %s", exc)
            state = None
        if state is False:
            outcome["reason"] = "下载器中仍有未完成的任务，暂不删除"
            return outcome
        if state is True:
            if not os.path.exists(path):
                outcome.update(deleted=True, reason="下载任务已删除（文件随之删除）")
                return outcome
            deleted = delete_file_with_retry(path, max_retries, retry_interval)
            outcome.update(
                deleted=deleted,
                reason="下载任务已删除，已兜底删除文件" if deleted else "下载任务已删除，但文件删除失败",
            )
            return outcome

    deleted = delete_file_with_retry(path, max_retries, retry_interval)
    outcome.update(deleted=deleted, reason="已删除原文件" if deleted else "原文件删除失败")
    return outcome
