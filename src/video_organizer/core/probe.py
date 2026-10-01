# -*- coding: utf-8 -*-
"""
ffprobe 视频校验工具

在线识别上传前会先用 ffprobe 校验文件是否是一个完整可播放的视频，
并提取分辨率 / 编码 / 码率等基础信息。

未安装 ffprobe 时不会中断流程，只会返回 ``available=False``。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_PROBE_TIMEOUT = 120


class FFProbeNotFound(RuntimeError):
    """未找到 ffprobe 可执行文件"""


def find_ffprobe(configured_path: Optional[str] = None) -> Optional[str]:
    """按优先级查找 ffprobe：配置 > 环境变量 > PATH"""
    candidates = [configured_path, os.environ.get("FFPROBE_PATH"), "ffprobe"]
    for candidate in candidates:
        if not candidate:
            continue
        candidate = str(candidate).strip()
        if not candidate:
            continue
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _empty_summary() -> Dict[str, Any]:
    return {
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "video_codec": "",
        "audio_codec": "",
        "frame_rate": "",
        "bitrate": 0,
        "pixel_format": "",
        "color_space": "",
        "dynamic_range": "",
        "video_streams": 0,
        "audio_streams": 0,
    }


def _parse_frame_rate(value: Optional[str]) -> str:
    if not value or "/" not in value:
        return value or ""
    numerator, _, denominator = value.partition("/")
    try:
        num = float(numerator)
        den = float(denominator)
    except ValueError:
        return value
    if den == 0:
        return value
    fps = num / den
    if fps <= 0:
        return ""
    rounded = round(fps, 3)
    if abs(rounded - round(rounded)) < 0.001:
        return str(int(round(rounded)))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def _dynamic_range(video_stream: Dict[str, Any], color_transfer: str) -> str:
    transfer = (color_transfer or "").lower()
    side_data = video_stream.get("side_data_list") or []
    has_dovi = False
    if isinstance(side_data, list):
        for entry in side_data:
            if isinstance(entry, dict) and "dovi" in str(entry.get("side_data_type", "")).lower():
                has_dovi = True
                break
    if has_dovi:
        return "Dolby Vision"
    if transfer in {"smpte2084", "arib-std-b67"}:
        return "HDR"
    return "SDR"


def probe_video(
    file_path: str,
    ffprobe_path: Optional[str] = None,
    timeout: int = DEFAULT_PROBE_TIMEOUT,
) -> Dict[str, Any]:
    """使用 ffprobe 校验视频文件

    Returns:
        {
            "valid": bool,
            "available": bool,
            "summary": {...},
            "metadata": {...},   # ffprobe 原始结果
            "error": str,
        }
    """
    result: Dict[str, Any] = {
        "valid": False,
        "available": False,
        "summary": _empty_summary(),
        "metadata": {},
        "error": "",
    }

    if not file_path or not os.path.isfile(file_path):
        result["error"] = f"文件不存在: {file_path}"
        return result

    executable = find_ffprobe(ffprobe_path)
    if not executable:
        result["error"] = "未找到 ffprobe，请安装 ffmpeg 或配置 ffprobe 路径"
        return result
    result["available"] = True

    command = [
        executable,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["error"] = "ffprobe 执行超时"
        return result
    except Exception as exc:  # pragma: no cover - 环境相关
        result["error"] = f"ffprobe 执行失败: {exc}"
        return result

    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", "ignore").strip()
        result["error"] = stderr[-500:] or "ffprobe 返回非零退出码"
        return result

    try:
        payload = json.loads((completed.stdout or b"{}").decode("utf-8", "ignore"))
    except ValueError as exc:
        result["error"] = f"解析 ffprobe 输出失败: {exc}"
        return result

    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video_stream:
        result["error"] = "文件中没有视频流"
        result["metadata"] = payload
        return result

    summary = _empty_summary()
    summary["duration"] = float(fmt.get("duration") or video_stream.get("duration") or 0.0)
    summary["width"] = int(video_stream.get("width") or 0)
    summary["height"] = int(video_stream.get("height") or 0)
    summary["video_codec"] = str(video_stream.get("codec_name") or "").upper()
    summary["audio_codec"] = str(audio_stream.get("codec_name") or "").upper() if audio_stream else ""
    summary["frame_rate"] = _parse_frame_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    try:
        summary["bitrate"] = int(fmt.get("bit_rate") or video_stream.get("bit_rate") or 0)
    except (TypeError, ValueError):
        summary["bitrate"] = 0
    summary["pixel_format"] = str(video_stream.get("pix_fmt") or "")
    summary["color_space"] = str(video_stream.get("color_space") or "")
    summary["dynamic_range"] = _dynamic_range(video_stream, str(video_stream.get("color_transfer") or ""))
    summary["video_streams"] = sum(1 for s in streams if s.get("codec_type") == "video")
    summary["audio_streams"] = sum(1 for s in streams if s.get("codec_type") == "audio")

    result["summary"] = summary
    result["metadata"] = payload
    result["valid"] = True
    return result


def probe_summary_for_upload(probe_result: Dict[str, Any]) -> Dict[str, Any]:
    """转换为 Emos ``file_metadata`` 字段

    官方客户端（somebyteorg/emos_video_upload）直接把 ffprobe 的 JSON 原始结果
    作为 ``file_metadata`` 提交，这里保持一致；只有在拿不到原始结果时才退回精简摘要。
    """
    raw = probe_result.get("metadata")
    if isinstance(raw, dict) and raw:
        return raw
    summary = probe_result.get("summary") or {}
    metadata: Dict[str, Any] = {}
    if not summary:
        return metadata
    if summary.get("duration"):
        metadata["duration"] = round(float(summary["duration"]), 3)
    if summary.get("width") and summary.get("height"):
        metadata["resolution"] = f"{summary['width']}x{summary['height']}"
    if summary.get("video_codec"):
        metadata["video_codec"] = summary["video_codec"]
    if summary.get("audio_codec"):
        metadata["audio_codec"] = summary["audio_codec"]
    if summary.get("bitrate"):
        metadata["bitrate"] = int(summary["bitrate"])
    if summary.get("dynamic_range"):
        metadata["dynamic_range"] = summary["dynamic_range"]
    return metadata
