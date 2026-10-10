# -*- coding: utf-8 -*-
"""Telegram 机器人：上传报错通知 + 回复修正上传目标

流程：

1. 上传失败（识别不到条目 / Emos 接口报错 / 重试仍失败）时，机器人把一条「报错信息」
   推送到已绑定的 Telegram 会话；
2. 用户直接「回复」这条报错信息，用自然表达指定正确目标，例如::

       时光代理人S04E09
       时光代理人 第4季第9集
       时光代理人 4x09
       时光代理人 S04          （只改季）
       时光代理人 (2024)       （电影）

3. 机器人解析这段文本 → 重新在 Emos 目录树里定位条目 → 用原文件重新发起上传。

绑定方式：在 Web「配置管理 → Telegram」填好 bot_token（chat_id 可留空），
然后在 Telegram 里给机器人发送 ``/bind``，机器人会把当前会话的 chat_id 写回
config.ini，在线即时生效，无需重启容器。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"   # 可通过该常量替换为自建反代地址
_MAX_REPLY_MAP = 200          # 最多记住多少条报错信息（供回复修正）
_MAX_MESSAGE_CHARS = 3500     # Telegram 单条消息上限约 4096，留出余量
_SEND_TIMEOUT = 15
_ERROR_NOTIFY_INTERVAL = 300  # 同一文件的同类报错 5 分钟只推一次
_REMINDER_INTERVAL = 30       # 报错提醒检查间隔（秒）
_MAX_BROWSE_TOKENS = 500      # 最多记住多少个「浏览/上传」路径令牌
_BROWSE_PAGE_SIZE = 20        # 目录浏览每页显示的条目数
_BROWSE_NAME_WIDTH = 34       # 按钮名称按显示宽度折行（CJK 记 2），保证在按钮里完整显示
_BROWSE_NAME_ROWS = 3         # 单个名称最多折成几行按钮（再多就省略中段）
_MAX_FLOW_TOKENS = 300        # 最多记住多少个「搜索 → 选季 → 选集」会话令牌

# 旧版底部快捷键盘按钮文字 → 指令的映射：仅用于兼容客户端上残留的旧键盘
_QUICK_REPLY_MAP = {
    "📤 上传文件": "/upload",
    "⚙️ 快捷配置": "/config",
    "📊 运行状态": "/status",
    "❓ 使用帮助": "/help",
}

# 原生命令菜单（setMyCommands）：输入框左侧「菜单」按钮 + 输入 / 时的命令列表
_BOT_COMMANDS = [
    {"command": "upload", "description": "浏览本地文件：上传 / 删除文件或文件夹"},
    {"command": "config", "description": "快捷配置：布尔开关与常用参数"},
    {"command": "status", "description": "查看机器人运行状态"},
    {"command": "help", "description": "查看用法：修正目标 / 删除任务"},
    {"command": "bind", "description": "绑定当前 Telegram 会话"},
]

# 回复这些关键词表示「删除该文件的任务并停止提醒」
_DELETE_KEYWORDS = {
    "删除", "删掉", "删了", "移除", "取消", "不要了", "不再提醒",
    "delete", "del", "remove", "cancel",
}

# Telegram 里可选择上传的文件类型（视频 + 外挂字幕）
_UPLOAD_FILE_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts",
    ".webm", ".m4v", ".mpg", ".mpeg", ".rmvb", ".iso", ".strm",
    ".srt", ".ass", ".ssa", ".vtt", ".sub",
}

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

_HELP_TEXT = (
    "可用指令：\n"
    "/bind  绑定当前会话（把 chat_id 写回配置）\n"
    "/upload  浏览本地文件：上传 / 删除文件或文件夹\n"
    "/config  快捷配置（开关）+「处理配置」/「Emos API」\n"
    "/status  查看机器人状态\n"
    "/help  查看本帮助\n\n"
    "点输入框左侧「菜单」或输入「/」查看全部指令\n\n"
    "修正上传目标：直接「回复」某条报错信息并发送片名关键词，\n"
    "机器人会搜索 Emos 并列出候选，点选作品后再选季 / 集即可上传；\n"
    "也可以一步到位直接写：\n"
    "　时光代理人S04E09\n"
    "　时光代理人 第4季第9集\n"
    "　时光代理人 4x09\n"
    "　时光代理人 (2024)\n"
    "同名作品较多时可带上年份区分，如「狂王 2024」「狂王 (2024) S02E04」\n\n"
    "删除任务：回复某条报错信息并发送「删除」，会同时删除该文件的上传任务、\n"
    "aria2 / qBittorrent 下载任务并停止提醒"
)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_size(num_bytes: Any) -> str:
    """把字节数格式化成可读大小（如 1.2 GB）"""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


def _shorten_text(text: str, limit: int) -> str:
    """按钮文字过长时保留头尾、中间省略，避免被 Telegram 截断"""
    text = str(text or "")
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    keep = limit - 1
    head = keep // 2 + keep % 2
    tail = keep - head
    return f"{text[:head]}…{text[-tail:]}"


def _display_width(text: str) -> int:
    """粗略计算显示宽度：CJK 全角字符记 2，英文/数字/半角记 1"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in str(text or "")
    )


def _truncate_by_width(text: str, limit: int) -> str:
    """按显示宽度从头部截断文本（不超宽）"""
    if limit <= 0:
        return ""
    width = 0
    head: List[str] = []
    for ch in str(text or ""):
        char_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + char_width > limit:
            break
        head.append(ch)
        width += char_width
    return "".join(head)


def _shorten_by_width(text: str, limit: int) -> str:
    """按显示宽度截断，超宽时保留头尾并省略中间"""
    text = str(text or "")
    if limit <= 0 or _display_width(text) <= limit:
        return text
    if limit <= 3:
        return _truncate_by_width(text, limit)
    keep = limit - 1
    head = _truncate_by_width(text, keep // 2 + keep % 2)
    tail_limit = keep - _display_width(head)
    tail = _truncate_by_width(text[::-1], tail_limit)[::-1]
    return f"{head}…{tail}"


def _wrap_by_width(text: str, limit: int) -> List[str]:
    """按显示宽度把长名称折成多段（每段 ≤ limit），供多行按钮完整显示"""
    text = str(text or "")
    if limit <= 0:
        return [text]
    chunks: List[str] = []
    current = ""
    width = 0
    for ch in text:
        char_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if current and width + char_width > limit:
            chunks.append(current)
            current = ""
            width = 0
        current += ch
        width += char_width
    if current or not chunks:
        chunks.append(current)
    return chunks


def _browse_name_lines(
    name: str,
    suffix: str = "",
    limit: int = _BROWSE_NAME_WIDTH,
    max_rows: int = _BROWSE_NAME_ROWS,
) -> List[str]:
    """把名称（含可选后缀）折成多行按钮文本，供 Telegram 按钮内完整显示"""
    text = str(name or "")
    allowed = limit * max_rows - _display_width(suffix)
    allowed = max(limit, allowed)
    if _display_width(text) > allowed:
        text = _shorten_by_width(text, allowed)
    chunks = _wrap_by_width(text, limit)
    if len(chunks) > max_rows:
        keep = max_rows - 1
        merged = "".join(chunks[keep:])
        chunks = chunks[:keep] + [_shorten_by_width(merged, limit)]
    if suffix:
        if _display_width(chunks[-1]) + _display_width(suffix) <= limit:
            chunks[-1] += suffix
        else:
            chunks.append(suffix)
    return chunks


def _candidate_contains_item(video: Dict[str, Any], item_id: str) -> bool:
    """判断作品候选自身或其季/集里是否包含指定条目 id"""
    if not isinstance(video, dict) or not item_id:
        return False
    if str(video.get("item_id") or "") == item_id:
        return True
    for season in video.get("seasons") or []:
        if not isinstance(season, dict):
            continue
        if str(season.get("item_id") or "") == item_id:
            return True
        for episode in season.get("episodes") or []:
            if isinstance(episode, dict) and str(episode.get("item_id") or "") == item_id:
                return True
    return False


def _item_year(item: Any) -> str:
    """取条目上的 4 位播出年份（date_air / air_date / year… 取不到返回空串）"""
    if not isinstance(item, dict):
        return ""
    for key in ("date_air", "air_date", "release_date", "first_air_date", "premiere_date", "year"):
        value = item.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if len(text) >= 4 and text[:4].isdigit():
            return text[:4]
    return ""


def _year_matches(video: Any, year: Optional[int]) -> bool:
    """年份是否命中作品 / 任意季 / 任意集（多季剧按季标年份：S01=2024、S02=2026）

    只看作品级 date_air 会把「第二季 2026」当成另一部 2026 年的同名剧，
    反而过滤掉正确候选，所以这里逐层比对。
    """
    if not year or not isinstance(video, dict):
        return False
    target = str(year)
    if _item_year(video) == target:
        return True
    for season in video.get("seasons") or []:
        if not isinstance(season, dict):
            continue
        if _item_year(season) == target:
            return True
        for episode in season.get("episodes") or []:
            if _item_year(episode) == target:
                return True
    return False


def _cn_to_int(text: str) -> Optional[int]:
    """把「一」「十二」「二十三」「一百」这类中文数字转成整数"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "百" in text:
        head, _, tail = text.partition("百")
        hundreds = _cn_to_int(head) if head else 1
        rest = _cn_to_int(tail) if tail else 0
        if hundreds is None or rest is None:
            return None
        return hundreds * 100 + rest
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _cn_to_int(head) if head else 1
        units = _cn_to_int(tail) if tail else 0
        if tens is None or units is None:
            return None
        return tens * 10 + units
    total = 0
    for ch in text:
        if ch not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[ch]
    return total


@dataclass
class TargetExpression:
    """从回复文本里解析出来的目标"""

    title: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    year: Optional[int] = None

    @property
    def kind(self) -> str:
        """tv / unknown

        年份单独出现时不视为电影：它主要用于在同名作品之间区分，
        到底是剧集还是电影由搜索结果让用户确认，避免「狂王 2024」
        这类剧集被强制当成电影搜不到。
        """
        if self.season is not None or self.episode is not None:
            return "tv"
        return "unknown"

    @property
    def parsed(self) -> bool:
        return bool(self.title or self.season is not None or self.episode is not None or self.year)

    def describe(self) -> str:
        parts = [self.title or "?"]
        if self.season is not None:
            parts.append(f"S{self.season:02d}")
        if self.episode is not None:
            parts.append(f"E{self.episode:02d}")
        if self.year is not None:
            parts.append(f"({self.year})")
        return " ".join(parts)


_CN_NUM = r"[0-9一二三四五六七八九十百两〇零]+"
_RE_CN_BOTH = re.compile(rf"第\s*(?P<s>{_CN_NUM})\s*[季部]\s*第?\s*(?P<e>{_CN_NUM})\s*[集话話期]")
_RE_SXXEXX = re.compile(r"[Ss](?P<s>\d{1,3})\s*[\-_.·]?\s*[Ee](?P<e>\d{1,4})")
_RE_NXNN = re.compile(r"(?P<s>\d{1,3})\s*[xX×]\s*(?P<e>\d{1,4})")
_RE_CN_SEASON = re.compile(rf"第\s*(?P<s>{_CN_NUM})\s*[季部]")
_RE_CN_EPISODE = re.compile(rf"第\s*(?P<e>{_CN_NUM})\s*[集话話期]")
_RE_S_ONLY = re.compile(r"[Ss](?P<s>\d{1,3})(?![\dEe])")
# 年份两侧可能是空格 / 点 / 短横线（粘贴文件名：狂王.2024.S01E01）
_RE_YEAR = re.compile(
    r"(?:[\(\[]|[\s._\-·|]|^)(?P<y>(?:19|20)\d{2})(?:[\)\]]|[\s._\-·|]|$)"
)
# 括号里的年份（(2024) / [2024]）：即使同时写了季集也当成年份
_RE_YEAR_PAREN = re.compile(r"[\(\[]\s*(?P<y>(?:19|20)\d{2})\s*[\)\]]")
# 粘贴文件名时混进标题的压制 / 片源标签（1080p、WEB-DL、H265、DDP2.0…）
_RE_RELEASE_TAG = re.compile(
    r"(?:\d{3,4}[pi]|4k|8k|"
    r"web(?:[-_. ]?(?:dl|rip))?|blu[-_. ]?ray|bdrip|hdtv|remux|dvdrip|hdrip|"
    r"h\.?26[45]|hevc|avc|x26[45]|xvid|av1|"
    r"dd\+?p?\d?(?:\.\d)?|aac|ac3|eac3|dts(?:[-_. ]?hd)?|truehd|atmos|flac|mp3|"
    r"hdr10\+?|hdr|dv|sdr|10bit|8bit|repack|proper|extended|ma\d\.\d)$",
    re.IGNORECASE,
)
# 用户经常直接粘贴文件名（大王饶命.S03E03.mkv），标题里要清掉视频/字幕后缀
_RE_MEDIA_EXT = re.compile(
    r"\.(?:mkv|mp4|avi|mov|wmv|flv|ts|m2ts|iso|strm|webm|m4v|mpg|mpeg|rmvb|srt|ass|ssa|vtt|sub)$",
    re.IGNORECASE,
)
# 标题两侧常见分隔符（含 ASCII 点，「大王饶命.S03E03」解析后不能留下「大王饶命.」）
_TITLE_EDGE_CHARS = " .-_·|,，。、:：;；!！?？/\\"


def parse_target_expression(raw: str) -> TargetExpression:
    """解析「时光代理人S04E09」这类回复文本

    支持的写法：S04E09 / s4e9 / 4x09 / 第4季第9集 / 第四季第九集 / S04 / (2024)
    """
    expr = TargetExpression()
    text = (raw or "").strip()
    if not text:
        return expr
    text = re.sub(r"^[/＠@]\w+\s*", "", text)          # 去掉开头的指令
    text = re.sub(r"^(?:正确的?应该是|其实?是|应该?是|正确的?|修正|改为|应该)\s*[:：]?\s*", "", text).strip()
    text = _RE_MEDIA_EXT.sub("", text).strip()          # 去掉粘贴文件名带的后缀

    def cut(match: re.Match) -> None:
        nonlocal text
        text = f"{text[:match.start()]} {text[match.end():]}"

    for rx in (_RE_CN_BOTH, _RE_SXXEXX, _RE_NXNN):
        match = rx.search(text)
        if match:
            expr.season = _cn_to_int(match.group("s"))
            expr.episode = _cn_to_int(match.group("e"))
            cut(match)
            break

    if expr.season is None and expr.episode is None:
        for rx, key in ((_RE_CN_SEASON, "season"), (_RE_S_ONLY, "season"), (_RE_CN_EPISODE, "episode")):
            match = rx.search(text)
            if match:
                setattr(expr, key, _cn_to_int(match.group("s") if key == "season" else match.group("e")))
                cut(match)
                break

    # 年份：括号写法始终识别，裸年份也识别；但只有去掉年份后仍留有片名时才
    # 当成年份，避免把片名本身就是年份的作品（如「1899 S01E01」）吃掉
    year_match = _RE_YEAR_PAREN.search(text) or _RE_YEAR.search(text)
    if year_match:
        remainder = f"{text[:year_match.start()]} {text[year_match.end():]}"
        if re.sub(r"[\s\-_·|,，。:：\[\]【】\(\)（）0-9]+", "", remainder):
            expr.year = int(year_match.group("y"))
            cut(year_match)

    title = re.sub(r"[\s\-_·|,，。:：\[\]【】\(\)（）]+", " ", text).strip()
    title = title.strip(_TITLE_EDGE_CHARS)
    expr.title = title
    return expr


class TelegramBotService:
    """Telegram 机器人服务（单例）

    - 负责发送上传报错通知
    - 长轮询 getUpdates，处理用户对报错信息的回复
    - 处理 /bind /status /help 指令
    """

    _instance: Optional["TelegramBotService"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "TelegramBotService":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config: Dict[str, Any] = {}
        self._config_path: Optional[str] = None
        self._token = ""
        self._chat_id = ""
        self._enabled = False
        self._reply_enabled = False
        self._allowed_users: List[str] = []
        self._poll_timeout = 30
        self._thread_token = ""
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._offset = 0
        self._replies: Dict[str, Dict[str, Any]] = {}
        self._error_notified_at: Dict[str, float] = {}
        self._active_errors: Dict[str, Dict[str, Any]] = {}
        self._reminder_thread: Optional[threading.Thread] = None
        self._browse_tokens: Dict[str, str] = {}
        self._browse_token_seq = 0
        self._flow_tokens: Dict[str, Dict[str, Any]] = {}
        self._flow_seq = 0
        self._sent_count = 0
        self._last_error = ""
        self._last_update_at = ""

    # ------------------------------------------------------------------
    # 配置 / 生命周期
    # ------------------------------------------------------------------

    def configure(self, config: Optional[Dict[str, Any]], config_path: Optional[Any] = None) -> None:
        """按最新配置启动/停止机器人（在线修改配置后立即调用）"""
        section = (config or {}).get("telegram")
        if section is None:
            # 传入的配置里没有 telegram 节（例如其它模块只带了部分配置），
            # 视为「无有效信息」，保留现有绑定，避免误停正在运行的机器人
            with self._lock:
                if self._token:
                    return
        telegram = dict(section or {})
        token = str(telegram.get("bot_token") or "").strip()
        chat_id = str(telegram.get("chat_id") or "").strip()
        enabled = bool(telegram.get("enabled", True))
        reply_enabled = bool(telegram.get("reply_enabled", True))
        try:
            poll_timeout = max(0, int(telegram.get("poll_timeout") or 30))
        except (TypeError, ValueError):
            poll_timeout = 30
        allowed = [
            item for item in re.split(r"[,;\s]+", str(telegram.get("allowed_user_ids") or "")) if item
        ]

        with self._lock:
            self._config = config if isinstance(config, dict) else {}
            if config_path is not None:
                self._config_path = str(config_path)
            self._token = token
            self._chat_id = chat_id
            self._enabled = enabled
            self._reply_enabled = reply_enabled
            self._allowed_users = allowed
            self._poll_timeout = poll_timeout
            should_run = bool(token) and enabled and reply_enabled
            token_changed = token != self._thread_token

        if not should_run:
            self.stop()
            return
        self.start(token_changed=token_changed)

    def start(self, token_changed: bool = False) -> None:
        """启动长轮询线程（已在运行且 token 未变时直接返回）"""
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            if alive and not token_changed:
                return
            if alive:
                self._stop_event.set()
                thread = self._thread
                self._thread = None
            else:
                thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._token:
                return
            self._stop_event = threading.Event()
            self._thread_token = self._token
            self._thread = threading.Thread(
                target=self._poll_loop, name="telegram-bot", daemon=True
            )
            worker = self._thread
            if self._reminder_thread is None or not self._reminder_thread.is_alive():
                self._reminder_thread = threading.Thread(
                    target=self._reminder_loop, name="telegram-reminder", daemon=True
                )
                reminder = self._reminder_thread
            else:
                reminder = None
        worker.start()
        if reminder is not None:
            reminder.start()
        # 在后台注册原生命令菜单，避免网络慢时阻塞配置保存请求
        threading.Thread(
            target=self.sync_command_menu, name="telegram-commands", daemon=True
        ).start()
        logger.info("Telegram 机器人已启动（长轮询）")

    def stop(self) -> None:
        """停止长轮询线程"""
        with self._lock:
            thread = self._thread
            self._thread = None
            self._thread_token = ""
            reminder = self._reminder_thread
            self._reminder_thread = None
            self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        if reminder is not None and reminder.is_alive():
            reminder.join(timeout=3)
        if thread is not None:
            logger.info("Telegram 机器人已停止")

    def status(self) -> Dict[str, Any]:
        """机器人状态（供 Web 界面展示）"""
        with self._lock:
            return {
                "enabled": self._enabled,
                "reply_enabled": self._reply_enabled,
                "token_configured": bool(self._token),
                "bound": bool(self._chat_id),
                "chat_id": self._chat_id,
                "running": bool(self._thread is not None and self._thread.is_alive()),
                "pending_replies": len(self._replies),
                "sent_count": self._sent_count,
                "last_error": self._last_error,
                "last_update_at": self._last_update_at,
            }

    # ------------------------------------------------------------------
    # 发送消息
    # ------------------------------------------------------------------

    def send_text(self, text: str, reply_to: Optional[int] = None, chat_id: Optional[str] = None) -> Optional[int]:
        """发送纯文本消息（不用 Markdown，避免文件名里的符号导致解析失败）"""
        with self._lock:
            token = self._token
            target_chat = str(chat_id or self._chat_id or "").strip()
        if not token or not target_chat:
            return None
        payload: Dict[str, Any] = {
            "chat_id": target_chat,
            "text": (text or "")[:_MAX_MESSAGE_CHARS],
            "disable_web_page_preview": True,
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            response = requests.post(
                f"{_API_BASE}/bot{token}/sendMessage",
                json=payload,
                timeout=_SEND_TIMEOUT,
            )
            body = response.json() if response.content else {}
        except Exception as exc:
            self._record_error(str(exc))
            logger.warning("Telegram 发送异常: %s", exc)
            return None
        if body.get("ok"):
            with self._lock:
                self._sent_count += 1
            return body.get("result", {}).get("message_id")
        self._record_error(str(body.get("description") or response.status_code))
        logger.warning("Telegram 发送失败: %s", body.get("description"))
        return None

    def test_message(self, chat_id: Optional[str] = None) -> Dict[str, Any]:
        """发送一条测试消息，用于验证绑定是否可用"""
        # 顺手注册原生命令菜单，用户不必等下一次重启 / 绑定
        self.sync_command_menu()
        message_id = self.send_text("✅ Video Organizer Telegram 机器人连接正常。", chat_id=chat_id)
        if message_id:
            return {"success": True, "message": "测试消息已发送", "message_id": message_id}
        with self._lock:
            reason = self._last_error or "未配置 bot_token / chat_id"
        return {"success": False, "message": f"发送失败: {reason}"}

    def hide_quick_keyboard(
        self,
        chat_id: Optional[str] = None,
        text: str = "已收起底部快捷键盘，改用 Telegram 原生命令菜单：点输入框左侧「菜单」或输入 / 。",
    ) -> None:
        """收起底部快捷键盘（回复键盘），配合原生命令菜单使用"""
        with self._lock:
            token = self._token
            target_chat = str(chat_id or self._chat_id or "").strip()
        if not token or not target_chat:
            return
        payload: Dict[str, Any] = {
            "chat_id": target_chat,
            "text": text,
            "reply_markup": {"remove_keyboard": True},
        }
        try:
            requests.post(
                f"{_API_BASE}/bot{token}/sendMessage",
                json=payload,
                timeout=_SEND_TIMEOUT,
            )
        except Exception as exc:
            logger.debug("收起 Telegram 快捷键盘失败: %s", exc)

    def sync_command_menu(self) -> bool:
        """注册原生命令菜单（Telegram「菜单」按钮 + 输入 / 时的命令列表）

        setMyCommands 只需要 bot_token，与是否已绑定 chat_id 无关；
        再把默认菜单按钮设为 commands，未弹出底部键盘时输入框左侧会显示「菜单」。
        老版本 Bot API 不支持 setChatMenuButton 时忽略失败，命令列表照常可用。
        """
        with self._lock:
            token = self._token
        if not token:
            return False
        ok = True
        try:
            response = requests.post(
                f"{_API_BASE}/bot{token}/setMyCommands",
                json={"commands": _BOT_COMMANDS},
                timeout=_SEND_TIMEOUT,
            )
            body = response.json() if response.content else {}
            if not body.get("ok"):
                ok = False
                description = str(body.get("description") or response.status_code)
                self._record_error(description)
                logger.warning("注册 Telegram 命令菜单失败: %s", description)
        except Exception as exc:
            ok = False
            logger.warning("注册 Telegram 命令菜单异常: %s", exc)
        try:
            requests.post(
                f"{_API_BASE}/bot{token}/setChatMenuButton",
                json={"menu_button": {"type": "commands"}},
                timeout=_SEND_TIMEOUT,
            )
        except Exception as exc:
            logger.debug("设置 Telegram 菜单按钮异常: %s", exc)
        return ok

    def notify_error(
        self, context: Dict[str, Any], error: str, header: str = "上传失败"
    ) -> bool:
        """推送一条上传报错信息，并记住它以便用户回复修正

        同一个文件的同类报错 5 分钟内只推一次：文件一直失败（每分钟重试一次）时
        不会反复刷屏，用户仍可回复此前那条消息来修正目标。
        """
        with self._lock:
            if not (self._token and self._chat_id and self._enabled):
                return False
            task_id = str((context or {}).get("task_id") or "")
        # 本地文件已经不存在（被移动 / 删除 / 已成功上传后清理）就不再打扰用户
        local_path = str((context or {}).get("file_path") or "").strip()
        if local_path and not os.path.exists(local_path):
            logger.debug("本地文件已不存在，跳过报错通知: %s", local_path)
            return False
        name = (
            (context or {}).get("file_name")
            or os.path.basename(str((context or {}).get("file_path") or ""))
            or "-"
        )
        notify_key = f"{str((context or {}).get('file_path') or name)}|{header}"
        now = time.time()
        with self._lock:
            last = self._error_notified_at.get(notify_key, 0.0)
            if now - last < _ERROR_NOTIFY_INTERVAL:
                # 记住最新的上下文，定时提醒时用最新信息
                existing = self._active_errors.get(notify_key)
                if existing is not None:
                    existing["context"] = dict(context or {})
                    existing["error"] = str(error or "")
                    existing["header"] = header
                logger.debug("同类报错 5 分钟内已推送过，跳过: %s", notify_key)
                return False
            self._error_notified_at[notify_key] = now
            while len(self._error_notified_at) > _MAX_REPLY_MAP:
                self._error_notified_at.pop(next(iter(self._error_notified_at)), None)
        lines = [f"❌ {header}" + (f" #{task_id}" if task_id else "")]
        lines.append(f"文件：{name}")
        target = self._describe_context(context)
        if target:
            lines.append(f"识别目标：{target}")
        lines.append(f"原因：{(error or '未知错误')[:600]}")
        if (context or {}).get("auto_retry"):
            lines.append("（已每 1 分钟自动重试）")
        message_id = self.send_text("\n".join(lines))
        if not message_id:
            # 发送失败（网络等）不算已通知，下次还能重试
            with self._lock:
                self._error_notified_at.pop(notify_key, None)
            return False
        with self._lock:
            self._replies[str(message_id)] = dict(context or {}, _error=str(error or ""), _at=_now_text())
            while len(self._replies) > _MAX_REPLY_MAP:
                self._replies.pop(next(iter(self._replies)), None)
            self._active_errors[notify_key] = {
                "context": dict(context or {}),
                "error": str(error or ""),
                "header": header,
                "last_sent": time.time(),
                "message_id": message_id,
            }
            while len(self._active_errors) > _MAX_REPLY_MAP:
                self._active_errors.pop(next(iter(self._active_errors)), None)
        return True

    @staticmethod
    def _norm_path(path: Any) -> str:
        """归一化路径用于比较（解析真实路径 + 统一大小写 / 分隔符）"""
        text = str(path or "").strip()
        if not text:
            return ""
        try:
            return os.path.normcase(os.path.realpath(text))
        except Exception:
            return os.path.normcase(text)

    def clear_error(self, file_path: str, header: Optional[str] = None) -> None:
        """上传成功 / 问题解决后停止该文件的定时提醒

        既按 key 前缀匹配（兼容 context 为空的旧数据），也按归一化路径匹配，
        避免调用方传来相对 / 未解析路径时清不掉提醒。
        """
        file_path = str(file_path or "").strip()
        if not file_path:
            return
        prefix = file_path + "|"
        target = self._norm_path(file_path)
        with self._lock:
            keys: List[str] = []
            for key, item in self._active_errors.items():
                if header is not None and not (
                    key.endswith("|" + header) or (item or {}).get("header") == header
                ):
                    continue
                if key.startswith(prefix):
                    keys.append(key)
                    continue
                context = (item or {}).get("context") or {}
                if target and self._norm_path(context.get("file_path")) == target:
                    keys.append(key)
            for key in keys:
                self._active_errors.pop(key, None)

    def clear_errors_under(self, path: str) -> int:
        """删除文件 / 目录后清掉该路径（含子文件）的所有报错提醒"""
        base = self._norm_path(path)
        if not base:
            return 0
        sep = os.sep
        removed = 0
        with self._lock:
            for key, item in list(self._active_errors.items()):
                context = (item or {}).get("context") or {}
                candidate = self._norm_path(context.get("file_path"))
                if not candidate:
                    continue
                if candidate == base or candidate.startswith(base + sep):
                    self._active_errors.pop(key, None)
                    removed += 1
        return removed

    def clear_all_errors(self) -> int:
        """停止所有文件的报错定时提醒（清空全部失败任务时调用）"""
        with self._lock:
            count = len(self._active_errors)
            self._active_errors.clear()
            self._error_notified_at.clear()
        return count

    def _reminder_loop(self) -> None:
        """未解决的报错每 5 分钟提醒一次"""
        while not self._stop_event.is_set():
            try:
                self._send_reminders()
            except Exception as exc:
                logger.debug("Telegram 报错提醒异常: %s", exc)
            self._stop_event.wait(_REMINDER_INTERVAL)

    def _send_reminders(self) -> None:
        now = time.time()
        with self._lock:
            if not (self._token and self._chat_id and self._enabled):
                return
            due = [
                (key, dict(item))
                for key, item in self._active_errors.items()
                if now - float(item.get("last_sent") or 0.0) >= _ERROR_NOTIFY_INTERVAL
                and not item.get("quiet")
            ]
        for key, item in due:
            context = item.get("context") or {}
            local_path = str(context.get("file_path") or "").strip()
            if local_path and not os.path.exists(local_path):
                # 本地文件已经不存在（被移动 / 删除），不再提醒，直接清掉这条报错
                logger.debug("本地文件已不存在，停止报错提醒: %s", local_path)
                with self._lock:
                    self._active_errors.pop(key, None)
                continue
            header = item.get("header") or "上传失败"
            name = (
                context.get("file_name")
                or os.path.basename(str(context.get("file_path") or ""))
                or "-"
            )
            lines = [f"⏰ 仍未解决 · {header}"]
            lines.append(f"文件：{name}")
            target = self._describe_context(context)
            if target:
                lines.append(f"识别目标：{target}")
            lines.append(f"原因：{str(item.get('error') or '未知错误')[:600]}")
            if context.get("auto_retry"):
                lines.append("（已每 1 分钟自动重试；未解决前每 5 分钟提醒一次）")
            else:
                lines.append("（未解决前每 5 分钟提醒一次）")
            message_id = self.send_text("\n".join(lines))
            with self._lock:
                current = self._active_errors.get(key)
                if current is None:
                    continue
                current["last_sent"] = time.time()
                if message_id:
                    current["message_id"] = message_id
                    self._replies[str(message_id)] = dict(
                        context, _error=str(item.get("error") or ""), _at=_now_text()
                    )
                    while len(self._replies) > _MAX_REPLY_MAP:
                        self._replies.pop(next(iter(self._replies)), None)

    @staticmethod
    def _describe_context(context: Optional[Dict[str, Any]]) -> str:
        context = context or {}
        parts: List[str] = []
        title = str(context.get("title") or "").strip()
        if title:
            parts.append(title)
        season = context.get("season_number")
        episode = context.get("episode_number")
        if season is not None:
            try:
                parts.append(f"S{int(season):02d}")
            except (TypeError, ValueError):
                pass
        if episode is not None:
            try:
                parts.append(f"E{int(episode):02d}")
            except (TypeError, ValueError):
                pass
        item_type = str(context.get("item_type") or "")
        item_id = str(context.get("item_id") or "")
        suffix = f"{item_type}/{item_id}" if item_type or item_id else ""
        text = " ".join(parts)
        if suffix:
            text = f"{text} ({suffix})" if text else f"({suffix})"
        return text

    # ------------------------------------------------------------------
    # 长轮询
    # ------------------------------------------------------------------

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                self._record_error(str(exc))
                logger.debug("Telegram 轮询异常: %s", exc)
                self._stop_event.wait(5)
        logger.debug("Telegram 轮询线程退出")

    def _poll_once(self) -> None:
        with self._lock:
            token = self._token
            timeout = self._poll_timeout
            offset = self._offset
        if not token:
            self._stop_event.wait(5)
            return
        response = requests.get(
            f"{_API_BASE}/bot{token}/getUpdates",
            params={
                "offset": offset,
                "timeout": max(0, timeout),
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=max(10, timeout) + 20,
        )
        body = response.json() if response.content else {}
        if not body.get("ok"):
            self._record_error(str(body.get("description") or response.status_code))
            self._stop_event.wait(5)
            return
        for update in body.get("result") or []:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                with self._lock:
                    self._offset = update_id + 1
            try:
                self._handle_update(update)
            except Exception as exc:
                logger.warning("处理 Telegram 更新失败: %s", exc)

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    def _handle_update(self, update: Dict[str, Any]) -> None:
        if update.get("callback_query"):
            self._handle_callback_query(update.get("callback_query") or {})
            return
        message = update.get("message") or update.get("edited_message") or {}
        text = str(message.get("text") or "").strip()
        chat_id = str((message.get("chat") or {}).get("id") or "")
        user_id = str((message.get("from") or {}).get("id") or "")
        if not text or not chat_id:
            return
        with self._lock:
            bound_chat = self._chat_id
            allowed = list(self._allowed_users)
            self._last_update_at = _now_text()

        command = ""
        if text.startswith("/"):
            command = text.split()[0].split("@")[0].lower()
        elif text in _QUICK_REPLY_MAP:
            # 底部快捷键盘按钮：把按钮文字映射回对应指令
            command = _QUICK_REPLY_MAP[text]
        if command in ("/bind", "/start", "/help") and not bound_chat:
            self._handle_command(command, chat_id, user_id, allowed)
            return
        if not bound_chat or bound_chat != chat_id:
            self.send_text(
                "当前会话未绑定。请在 Web「配置管理 → Telegram」填写 chat_id，或发送 /bind 绑定。",
                chat_id=chat_id,
            )
            return
        if allowed and user_id not in allowed:
            self.send_text("你没有权限操作此机器人。", chat_id=chat_id)
            return
        if command:
            self._handle_command(command, chat_id, user_id, allowed)
            return

        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        context: Optional[Dict[str, Any]] = None
        if reply_to is not None:
            with self._lock:
                context = self._replies.get(str(reply_to))
        if context is None:
            self.send_text(
                "请直接「回复」某条报错信息来修正上传目标，或发送 /help 查看用法。",
                chat_id=chat_id,
            )
            return
        if context.get("_config_key"):
            self._apply_config_value(context, text, chat_id, reply_to)
            return
        if self._is_delete_intent(text):
            self._handle_delete_reply(context, chat_id, reply_to)
            return
        self._handle_correction(context, text, chat_id, reply_to)

    def _handle_command(
        self,
        command: str,
        chat_id: str,
        user_id: str,
        allowed: List[str],
    ) -> None:
        if allowed and user_id not in allowed:
            self.send_text("你没有权限操作此机器人。", chat_id=chat_id)
            return
        if command in ("/bind", "/start"):
            with self._lock:
                bound_chat = self._chat_id
            if bound_chat and bound_chat != chat_id:
                self.send_text("机器人已绑定其他会话，如需改绑请先清空配置里的 chat_id。", chat_id=chat_id)
                return
            if bound_chat == chat_id:
                self.sync_command_menu()
                self.send_text(
                    "机器人已绑定当前会话 ✅\n"
                    "发送 /help 查看用法，或点输入框左侧「菜单」选择指令。",
                    chat_id=chat_id,
                )
                self.hide_quick_keyboard(chat_id)
                return
            self._bind_chat(chat_id)
            self.sync_command_menu()
            self.send_text(
                f"绑定成功 ✅\nchat_id = {chat_id}\n\n"
                "上传失败时会推送报错信息，直接回复即可修正目标。\n"
                "点输入框左侧「菜单」或输入 / 可查看全部指令。",
                chat_id=chat_id,
            )
            # 只用 Telegram 原生命令菜单：顺手收起以前显示过的底部快捷键盘
            self.hide_quick_keyboard(chat_id)
            return
        if command == "/help":
            self.send_text(_HELP_TEXT, chat_id=chat_id)
            return
        if command in ("/upload", "/files"):
            self._send_browse(chat_id, "", None)
            return
        if command in ("/config", "/设置"):
            self._send_config_menu(chat_id)
            return
        if command == "/status":
            self.send_text(self._status_text(), chat_id=chat_id)
            return
        self.send_text("未知指令，发送 /help 查看用法。", chat_id=chat_id)

    # ------------------------------------------------------------------
    # 选择本地上传文件（/upload + 内联按钮）
    # ------------------------------------------------------------------

    def _token_for_path(self, path: str) -> str:
        """为路径生成短令牌，规避 Telegram callback_data 64 字节限制"""
        with self._lock:
            self._browse_token_seq += 1
            token = f"t{self._browse_token_seq}"
            self._browse_tokens[token] = str(path)
            while len(self._browse_tokens) > _MAX_BROWSE_TOKENS:
                self._browse_tokens.pop(next(iter(self._browse_tokens)), None)
            return token

    def _path_for_token(self, token: str) -> Optional[str]:
        with self._lock:
            return self._browse_tokens.get(str(token))

    def _answer_callback(self, query_id: str, text: str = "") -> None:
        if not query_id:
            return
        with self._lock:
            token = self._token
        if not token:
            return
        try:
            requests.post(
                f"{_API_BASE}/bot{token}/answerCallbackQuery",
                json={"callback_query_id": query_id, "text": text[:200]},
                timeout=_SEND_TIMEOUT,
            )
        except Exception as exc:
            logger.debug("应答 Telegram 按钮失败: %s", exc)

    def _send_keyboard(
        self,
        chat_id: str,
        text: str,
        rows: List[List[Dict[str, str]]],
        edit_message_id: Optional[int] = None,
    ) -> None:
        with self._lock:
            token = self._token
        if not token:
            return
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "reply_markup": {"inline_keyboard": rows},
        }
        if edit_message_id:
            payload["message_id"] = edit_message_id
            try:
                response = requests.post(
                    f"{_API_BASE}/bot{token}/editMessageText",
                    json=payload,
                    timeout=_SEND_TIMEOUT,
                )
                body = response.json() if response.content else {}
                if body.get("ok"):
                    return
            except Exception as exc:
                logger.debug("编辑 Telegram 消息失败: %s", exc)
            payload.pop("message_id", None)
        try:
            requests.post(
                f"{_API_BASE}/bot{token}/sendMessage", json=payload, timeout=_SEND_TIMEOUT
            )
        except Exception as exc:
            logger.debug("发送 Telegram 按钮消息失败: %s", exc)

    def _send_browse(
        self,
        chat_id: str,
        path: str,
        edit_message_id: Optional[int] = None,
        page: int = 0,
    ) -> None:
        """展示目录浏览键盘：目录 / 视频字幕文件，支持翻页与整目录上传"""
        from .online_upload import OnlineUploadService

        service = OnlineUploadService.instance()
        try:
            roots = service.roots()
        except Exception:
            roots = []

        directory = None
        if path:
            try:
                directory = service.resolve(path)
            except Exception:
                directory = None

        if directory is None or not Path(str(directory)).is_dir():
            if not roots:
                self.send_text(
                    "未配置视频根目录，请先在 Web「配置管理 → 在线识别上传」里填写 video_root。",
                    chat_id=chat_id,
                )
                return
            rows = [
                [
                    {
                        "text": f"📁 {root}",
                        "callback_data": f"up:ls:{self._token_for_path(root)}:0",
                    }
                ]
                for root in roots[:_BROWSE_PAGE_SIZE]
            ]
            self._send_keyboard(
                chat_id, "请选择要上传文件所在的视频根目录：", rows, edit_message_id
            )
            return

        base = Path(str(directory))
        dirs: List[Path] = []
        files: List[Path] = []
        try:
            for entry in sorted(base.iterdir(), key=lambda item: item.name.lower()):
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        dirs.append(entry)
                    elif entry.is_file() and entry.suffix.lower() in _UPLOAD_FILE_EXTENSIONS:
                        files.append(entry)
                except OSError:
                    continue
        except OSError as exc:
            self.send_text(f"无法读取目录：{exc}", chat_id=chat_id)
            return

        # 子目录 + 文件合并成一个可翻页列表（目录在前）
        entries: List[Dict[str, Any]] = [{"kind": "dir", "path": d} for d in dirs]
        total_size = 0
        for item in files:
            try:
                size = item.stat().st_size
            except OSError:
                size = 0
            total_size += size
            entries.append({"kind": "file", "path": item, "size": size})
        total = len(entries)
        total_pages = max(1, (total + _BROWSE_PAGE_SIZE - 1) // _BROWSE_PAGE_SIZE)
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
        page = max(0, min(page, total_pages - 1))
        start = page * _BROWSE_PAGE_SIZE
        page_entries = entries[start : start + _BROWSE_PAGE_SIZE]
        base_token = self._token_for_path(str(base))

        # 每个条目占多行：名称按显示宽度折成若干整行按钮（按钮里能显示完整名字），
        # 最后一行才是「上传 / 删除」操作键
        rows: List[List[Dict[str, str]]] = []
        nav_row: List[Dict[str, str]] = [
            {
                "text": "⬆️ 上一级",
                "callback_data": f"up:ls:{self._token_for_path(str(base.parent))}:0",
            },
            {"text": "🏠 根目录", "callback_data": "up:roots"},
        ]
        if dirs or files:
            nav_row.append({"text": "📤 上传全部", "callback_data": f"up:dir:{base_token}"})
        else:
            nav_row.append({"text": "🚫 无可上传", "callback_data": "up:noop"})
        rows.append(nav_row)
        for number, entry in enumerate(page_entries, start=1):
            target = entry["path"]
            token = self._token_for_path(str(target))
            if entry["kind"] == "dir":
                name_lines = _browse_name_lines(target.name)
                rows.append(
                    [
                        {
                            "text": f"📂 {name_lines[0]}",
                            "callback_data": f"up:ls:{token}:0",
                        }
                    ]
                )
                for extra in name_lines[1:]:
                    rows.append([{"text": extra, "callback_data": "up:noop"}])
                rows.append(
                    [
                        {
                            "text": f"📤 {number} 上传全部",
                            "callback_data": f"up:dir:{token}",
                        },
                        {"text": "🗑️ 删除", "callback_data": f"up:del:{token}"},
                    ]
                )
            else:
                size_text = _format_size(entry.get("size"))
                suffix = f"（{size_text}）" if size_text else ""
                name_lines = _browse_name_lines(target.name, suffix)
                rows.append(
                    [{"text": f"🎬 {name_lines[0]}", "callback_data": "up:noop"}]
                )
                for extra in name_lines[1:]:
                    rows.append([{"text": extra, "callback_data": "up:noop"}])
                rows.append(
                    [
                        {
                            "text": f"📤 {number} 上传",
                            "callback_data": f"up:file:{token}",
                        },
                        {"text": "🗑️ 删除", "callback_data": f"up:del:{token}"},
                    ]
                )
        if total_pages > 1:
            nav: List[Dict[str, str]] = []
            if page > 0:
                nav.append(
                    {
                        "text": "⬅️ 上一页",
                        "callback_data": f"up:ls:{base_token}:{page - 1}",
                    }
                )
            nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "up:noop"})
            if page < total_pages - 1:
                nav.append(
                    {
                        "text": "下一页 ➡️",
                        "callback_data": f"up:ls:{base_token}:{page + 1}",
                    }
                )
            rows.append(nav)
        if not page_entries:
            rows.append([{"text": "（此目录没有可上传文件）", "callback_data": "up:noop"}])
        lines = [f"目录：{base}", f"子目录 {len(dirs)} 个 · 视频/字幕 {len(files)} 个"]
        size_text = _format_size(total_size)
        tail = ""
        if size_text:
            tail += f"（共 {size_text}）"
        if total_pages > 1:
            tail += f"（第 {page + 1}/{total_pages} 页）"
        lines[-1] += tail
        self._send_keyboard(chat_id, "\n".join(lines), rows, edit_message_id)

    def _send_delete_confirm(self, chat_id: str, path: str) -> None:
        """发送删除确认（文件/目录），避免误删"""
        from .online_upload import OnlineUploadService

        try:
            service = OnlineUploadService.instance()
            target = service.resolve(path)
            roots = [os.path.realpath(str(root)) for root in service.roots()]
        except Exception:
            target = Path(str(path))
            roots = []
        if os.path.realpath(str(target)) in roots:
            self.send_text("不允许删除视频根目录本身。", chat_id=chat_id)
            return
        kind = "目录（含其中全部文件）" if Path(str(target)).is_dir() else "文件"
        rows = [
            [
                {
                    "text": "✅ 确认删除",
                    "callback_data": f"up:delok:{self._token_for_path(str(target))}",
                },
                {"text": "↩️ 取消", "callback_data": "up:delcancel"},
            ]
        ]
        self._send_keyboard(
            chat_id,
            f"⚠️ 确定要删除该{kind}吗？此操作不可恢复。\n{target}",
            rows,
        )

    def _delete_path(self, path: str, chat_id: str, message_id: Optional[int]) -> None:
        """删除服务器上的文件/目录，并清理对应的任务与报错提醒"""
        import shutil

        from .online_upload import OnlineUploadService

        service = OnlineUploadService.instance()
        try:
            target = service.resolve(path)
            roots = [os.path.realpath(str(root)) for root in service.roots()]
        except Exception as exc:
            self._send_keyboard(chat_id, f"删除失败：{exc}", [], message_id)
            return
        if os.path.realpath(str(target)) in roots:
            self._send_keyboard(chat_id, "不允许删除视频根目录本身。", [], message_id)
            return
        if not target.exists():
            self._send_keyboard(chat_id, f"文件不存在或已删除：{target.name}", [], message_id)
            return
        is_dir = target.is_dir()
        try:
            if is_dir:
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception as exc:
            logger.warning("Telegram 删除失败: %s", exc)
            self._send_keyboard(chat_id, f"删除失败：{exc}", [], message_id)
            return
        try:
            service.delete_tasks_for_file(str(target))
        except Exception:
            pass
        removed_downloads: List[str] = []
        try:
            from .downloader_monitor import remove_downloader_tasks

            # 只清下载器里的任务，本地文件由上面的删除逻辑负责
            removed_downloads = remove_downloader_tasks(str(target), delete_files=False)
        except Exception as exc:
            logger.debug("删除下载器任务失败: %s", exc)
        # 目录删除时把子文件遗留的报错提醒一起清掉
        self.clear_errors_under(str(target))
        kind = "目录" if is_dir else "文件"
        text = f"🗑️ 已删除{kind}：{target.name}"
        if removed_downloads:
            text += f"\n已同步删除下载器任务：{'、'.join(removed_downloads)}"
        self._send_keyboard(chat_id, text, [], message_id)

    def _handle_callback_query(self, query: Dict[str, Any]) -> None:
        query_id = str(query.get("id") or "")
        data = str(query.get("data") or "")
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        user_id = str((query.get("from") or {}).get("id") or "")
        message_id = message.get("message_id")
        with self._lock:
            bound_chat = self._chat_id
            allowed = list(self._allowed_users)
            self._last_update_at = _now_text()
        if not chat_id:
            return
        if bound_chat and bound_chat != chat_id:
            self._answer_callback(query_id, "当前会话未绑定")
            return
        if allowed and user_id not in allowed:
            self._answer_callback(query_id, "你没有权限操作此机器人")
            return
        try:
            if data == "up:noop":
                self._answer_callback(query_id, "")
                return
            if data == "up:roots":
                self._answer_callback(query_id, "")
                self._send_browse(chat_id, "", message_id)
                return
            if data.startswith("up:ls:"):
                payload = data[len("up:ls:"):]
                token, _, page_text = payload.partition(":")
                path = self._path_for_token(token)
                self._answer_callback(query_id, "")
                if path is None:
                    self.send_text("目录已过期，请重新发送 /upload", chat_id=chat_id)
                    return
                try:
                    page = int(page_text) if page_text else 0
                except ValueError:
                    page = 0
                self._send_browse(chat_id, path, message_id, page)
                return
            if data.startswith("up:dir:"):
                path = self._path_for_token(data[len("up:dir:"):])
                self._answer_callback(query_id, "已收到，开始上传文件夹…")
                if path is None:
                    self.send_text("目录已过期，请重新发送 /upload", chat_id=chat_id)
                    return
                # 整目录识别/上传耗时较长，放到后台线程，避免阻塞长轮询
                threading.Thread(
                    target=self._start_upload_from_folder,
                    args=(path, chat_id),
                    name="telegram-upload-folder",
                    daemon=True,
                ).start()
                return
            if data.startswith("up:file:"):
                path = self._path_for_token(data[len("up:file:"):])
                self._answer_callback(query_id, "已收到，开始识别…")
                if path is None:
                    self.send_text("文件已过期，请重新发送 /upload", chat_id=chat_id)
                    return
                # 识别/上传可能耗时，放到后台线程，避免阻塞长轮询
                threading.Thread(
                    target=self._start_upload_from_file,
                    args=(path, chat_id),
                    name="telegram-upload",
                    daemon=True,
                ).start()
                return
            if data.startswith("up:del:"):
                path = self._path_for_token(data[len("up:del:"):])
                self._answer_callback(query_id, "")
                if path is None:
                    self.send_text("条目已过期，请重新发送 /upload", chat_id=chat_id)
                    return
                self._send_delete_confirm(chat_id, path)
                return
            if data.startswith("up:delok:"):
                path = self._path_for_token(data[len("up:delok:"):])
                self._answer_callback(query_id, "正在删除…")
                if path is None:
                    self._send_keyboard(chat_id, "条目已过期，请重新发送 /upload。", [], message_id)
                    return
                self._delete_path(path, chat_id, message_id)
                return
            if data == "up:delcancel":
                self._answer_callback(query_id, "已取消")
                self._send_keyboard(chat_id, "已取消删除。", [], message_id)
                return
            if data == "cfg:root":
                self._answer_callback(query_id, "")
                self._send_config_menu(chat_id, message_id)
                return
            if data.startswith("cfg:sec:"):
                section = data[len("cfg:sec:"):]
                self._answer_callback(query_id, "")
                self._send_config_section(chat_id, section, message_id)
                return
            if data.startswith("cfg:tog:"):
                payload = data[len("cfg:tog:"):]
                section, _, key = payload.partition(":")
                self._answer_callback(query_id, "")
                self._toggle_config_bool(chat_id, section, key, message_id)
                return
            if data.startswith("cfg:edit:"):
                payload = data[len("cfg:edit:"):]
                section, _, key = payload.partition(":")
                self._answer_callback(query_id, "请回复新值")
                self._prompt_config_edit(chat_id, section, key)
                return
            if data.startswith("fx:"):
                self._answer_callback(query_id, "")
                action, _, token = data[len("fx:"):].partition(":")
                self._handle_flow_callback(chat_id, action, token, message_id)
                return
            self._answer_callback(query_id, "未知操作")
        except Exception as exc:
            logger.warning("处理 Telegram 按钮失败: %s", exc)
            self._answer_callback(query_id, "操作失败")

    def _start_upload_from_file(self, path: str, chat_id: str) -> None:
        """选择本地文件后：自动识别并上传，识别不到则请用户回复指定目标"""
        from .online_upload import OnlineUploadService

        service = OnlineUploadService.instance()
        name = os.path.basename(path)
        self.send_text(f"正在识别：{name} …", chat_id=chat_id)
        try:
            result = service.recognize(path)
        except Exception as exc:
            self.send_text(f"识别失败：{exc}", chat_id=chat_id)
            return
        meta = result.get("metadata") or {}
        match = result.get("match")
        if match:
            season_number = match.get("season_number")
            if season_number is None:
                season_number = meta.get("season")
            episode_number = match.get("episode_number")
            if episode_number is None:
                episode_number = meta.get("episode")
            try:
                task = service.create_task(
                    {
                        "file_path": path,
                        "item_type": match.get("item_type"),
                        "item_id": match.get("item_id"),
                        "storage": None,
                        "title": meta.get("title") or "",
                        "media_type": meta.get("media_type") or "",
                        "season_number": season_number,
                        "episode_number": episode_number,
                    }
                )
            except Exception as exc:
                self.send_text(f"创建上传任务失败：{exc}", chat_id=chat_id)
                return
            if task.get("duplicate"):
                self.send_text(
                    f"未重复提交：{task.get('duplicate_reason')}\n文件：{name}",
                    chat_id=chat_id,
                )
                return
            self.send_text(
                "✅ 已提交上传\n"
                f"文件：{name}\n"
                f"目标：{match.get('label') or ''}\n"
                f"任务：{task.get('id')}",
                chat_id=chat_id,
            )
            return

        context = {
            "file_path": path,
            "file_name": name,
            "title": meta.get("title") or "",
            "media_type": meta.get("media_type") or "",
            "season_number": meta.get("season"),
            "episode_number": meta.get("episode"),
            "storage": "",
        }
        lines = [
            "未能自动识别上传目标，请「回复」本条消息指定目标，例如：",
            "　时光代理人S04E09",
            "或回复「删除」取消该任务并停止提醒",
        ]
        if result.get("error"):
            lines.append(f"识别信息：{str(result.get('error'))[:300]}")
        message_id = self.send_text("\n".join(lines), chat_id=chat_id)
        if message_id:
            with self._lock:
                self._replies[str(message_id)] = dict(
                    context, _error=str(result.get("error") or ""), _at=_now_text()
                )

    def _start_upload_from_folder(self, path: str, chat_id: str) -> None:
        """选择文件夹后：递归识别并上传其中所有视频/字幕

        识别不到的文件登记为待修正报错（不逐条提醒，只发一条汇总），
        回复指定一次目标后，同名剧集的其它文件由
        ``_retry_pending_same_title`` 自动一起处理。
        """
        from .online_upload import OnlineUploadService

        service = OnlineUploadService.instance()
        base = Path(str(path))
        if not base.is_dir():
            self.send_text(f"目录不存在：{path}", chat_id=chat_id)
            return

        try:
            from .incomplete_downloads import collect_incomplete_paths, is_incomplete

            incomplete = collect_incomplete_paths()
        except Exception:
            incomplete = set()

        files: List[Path] = []
        for entry in sorted(base.rglob("*"), key=lambda item: str(item).lower()):
            try:
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in _UPLOAD_FILE_EXTENSIONS:
                    continue
                if incomplete and is_incomplete(entry, incomplete):
                    continue
                files.append(entry)
            except OSError:
                continue

        if not files:
            self.send_text(f"该文件夹下没有可上传的视频/字幕：{base}", chat_id=chat_id)
            return

        total_size = 0
        for item in files:
            try:
                total_size += item.stat().st_size
            except OSError:
                continue
        size_text = _format_size(total_size)
        summary = f"共 {len(files)} 个文件" + (f"（{size_text}）" if size_text else "")
        self.send_text(
            f"📂 开始上传文件夹：{base.name}\n{summary}，正在逐个识别上传…",
            chat_id=chat_id,
        )

        created = 0
        duplicates = 0
        failed: List[Dict[str, Any]] = []
        for entry in files:
            try:
                result = service.recognize(str(entry))
            except Exception as exc:
                failed.append({"path": str(entry), "meta": {}, "error": str(exc)})
                continue
            meta = result.get("metadata") or {}
            match = result.get("match")
            if not match:
                failed.append(
                    {
                        "path": str(entry),
                        "meta": meta,
                        "error": str(result.get("error") or "未找到上传目标"),
                    }
                )
                continue
            season_number = match.get("season_number")
            if season_number is None:
                season_number = meta.get("season")
            episode_number = match.get("episode_number")
            if episode_number is None:
                episode_number = meta.get("episode")
            try:
                task = service.create_task(
                    {
                        "file_path": str(entry),
                        "item_type": match.get("item_type"),
                        "item_id": match.get("item_id"),
                        "storage": None,
                        "title": meta.get("title") or "",
                        "media_type": meta.get("media_type") or "",
                        "season_number": season_number,
                        "episode_number": episode_number,
                    }
                )
            except Exception as exc:
                failed.append({"path": str(entry), "meta": meta, "error": str(exc)})
                continue
            if task.get("duplicate"):
                duplicates += 1
            else:
                created += 1

        lines = [
            "📂 文件夹上传完成",
            f"目录：{base}",
            f"已提交 {created} 个 · 跳过重复 {duplicates} 个 · 待处理 {len(failed)} 个",
        ]
        if failed:
            lines.append("")
            lines.append("以下文件未能自动识别目标：")
            for item in failed[:10]:
                lines.append(f"· {os.path.basename(item['path'])}")
            if len(failed) > 10:
                lines.append(f"…… 其余 {len(failed) - 10} 个")
            lines.append("")
            lines.append("回复本条消息指定目标（如：剧名 S01E01），同名剧集的其它文件会自动一起处理。")
        message_id = self.send_text("\n".join(lines), chat_id=chat_id)

        if not failed:
            return
        first = failed[0]
        first_meta = first.get("meta") or {}
        if message_id:
            with self._lock:
                self._replies[str(message_id)] = dict(
                    {
                        "file_path": first["path"],
                        "file_name": os.path.basename(first["path"]),
                        "title": first_meta.get("title") or "",
                        "media_type": first_meta.get("media_type") or "",
                        "season_number": first_meta.get("season"),
                        "episode_number": first_meta.get("episode"),
                        "storage": "",
                    },
                    _error=first.get("error") or "",
                    _at=_now_text(),
                )
        # 其余待处理文件登记为报错（quiet，不逐条提醒），修正一次后一起重传
        now = time.time()
        for item in failed:
            meta = item.get("meta") or {}
            with self._lock:
                self._active_errors[f"{item['path']}|未找到 Emos 上传目标"] = {
                    "context": {
                        "file_path": item["path"],
                        "file_name": os.path.basename(item["path"]),
                        "title": meta.get("title") or "",
                        "media_type": meta.get("media_type") or "",
                        "season_number": meta.get("season"),
                        "episode_number": meta.get("episode"),
                        "storage": "",
                    },
                    "error": item.get("error") or "未找到上传目标",
                    "header": "未找到 Emos 上传目标",
                    "last_sent": now,
                    "message_id": message_id or 0,
                    "quiet": True,
                }

    # ------------------------------------------------------------------
    # 配置管理（处理配置 / Emos API）
    # ------------------------------------------------------------------

    _CONFIG_SECTIONS = {
        "processing": "处理配置",
        "emos": "Emos API",
    }

    # 快捷配置：布尔项以开关呈现，点按即切换
    _QUICK_TOGGLES = [
        ("processing", "delete_after_upload", "上传后删除原文件"),
        ("emos", "upload_subtitles", "顺带上传字幕"),
        ("online_upload", "probe_enabled", "上传前 ffprobe 校验"),
        ("guessit", "enabled", "GuessIt 增强识别"),
        ("llm_fallback", "enabled", "LLM 兜底识别"),
        ("monitoring", "enable_directory_monitor", "目录监控"),
    ]
    # 快捷配置：需要输入数值/文本的项
    _QUICK_INPUTS = [
        ("processing", "max_upload_workers", "上传并发数"),
        ("emos", "chunk_size_mb", "分片大小(MB)"),
        ("emos", "upload_concurrency", "分片上传并发"),
        ("logging", "log_level", "日志等级"),
    ]

    def _current_config(self) -> Dict[str, Any]:
        with self._lock:
            config = self._config
        if isinstance(config, dict) and config:
            return config
        try:
            from ..web.services.state import get_state_manager

            return get_state_manager().get_config()
        except Exception:
            return config if isinstance(config, dict) else {}

    @staticmethod
    def _mask_config_value(key: Any, value: Any) -> str:
        key_l = str(key or "").lower()
        if any(token in key_l for token in ("token", "password", "secret", "api_key")):
            text = str(value or "")
            if not text:
                return "(空)"
            return "***" if len(text) <= 6 else f"{text[:3]}***{text[-2:]}"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)

    def _send_config_menu(self, chat_id: str, edit_message_id: Optional[int] = None) -> None:
        config = self._current_config()
        rows: List[List[Dict[str, str]]] = []
        for section, key, label in self._QUICK_TOGGLES:
            values = config.get(section)
            if not isinstance(values, dict) or key not in values:
                continue
            enabled = bool(values.get(key))
            mark = "🟢" if enabled else "⚪"
            rows.append(
                [
                    {
                        "text": f"{mark} {label}：{'开' if enabled else '关'}",
                        "callback_data": f"cfg:tog:{section}:{key}",
                    }
                ]
            )
        for section, key, label in self._QUICK_INPUTS:
            values = config.get(section)
            if not isinstance(values, dict) or key not in values:
                continue
            rows.append(
                [
                    {
                        "text": f"✏️ {label} = {self._mask_config_value(key, values.get(key))}",
                        "callback_data": f"cfg:edit:{section}:{key}",
                    }
                ]
            )
        rows.append(
            [
                {"text": "⚙️ 处理配置", "callback_data": "cfg:sec:processing"},
                {"text": "⚙️ Emos API", "callback_data": "cfg:sec:emos"},
            ]
        )
        self._send_keyboard(
            chat_id,
            "⚡ 快捷配置（点按开关即可切换；需要输入的点按后回复新值）：",
            rows,
            edit_message_id,
        )

    def _send_config_section(
        self, chat_id: str, section: str, edit_message_id: Optional[int] = None
    ) -> None:
        if section not in self._CONFIG_SECTIONS:
            self.send_text("未知配置节。", chat_id=chat_id)
            return
        config = self._current_config()
        values = config.get(section)
        if not isinstance(values, dict) or not values:
            self.send_text(f"配置节 {section} 为空。", chat_id=chat_id)
            return
        rows: List[List[Dict[str, str]]] = []
        for key, value in values.items():
            if isinstance(value, (dict, list)):
                continue
            text = f"{key} = {self._mask_config_value(key, value)}"
            rows.append([{"text": text[:60], "callback_data": f"cfg:edit:{section}:{key}"}])
        rows.append([{"text": "⬅️ 返回", "callback_data": "cfg:root"}])
        self._send_keyboard(
            chat_id,
            f"{self._CONFIG_SECTIONS[section]}：点按要修改的项，然后回复新值。",
            rows,
            edit_message_id,
        )

    def _prompt_config_edit(self, chat_id: str, section: str, key: str) -> None:
        config = self._current_config()
        values = config.get(section)
        if not isinstance(values, dict) or key not in values:
            self.send_text("配置项不存在，请重新发送 /config。", chat_id=chat_id)
            return
        message_id = self.send_text(
            f"请回复本条消息，输入 {section}.{key} 的新值：\n"
            f"当前值：{self._mask_config_value(key, values.get(key))}",
            chat_id=chat_id,
        )
        if message_id:
            with self._lock:
                self._replies[str(message_id)] = {
                    "_config_section": section,
                    "_config_key": key,
                    "_at": _now_text(),
                }

    def _toggle_config_bool(
        self, chat_id: str, section: str, key: str, edit_message_id: Optional[int] = None
    ) -> None:
        """快捷配置开关：切换布尔值并即时保存"""
        config = self._current_config()
        values = config.get(section) if isinstance(config.get(section), dict) else None
        if not section or not key or values is None or key not in values:
            self.send_text("配置修改已失效，请重新发送 /config。", chat_id=chat_id)
            return
        current = values.get(key)
        if not isinstance(current, bool):
            self._prompt_config_edit(chat_id, section, key)
            return
        values[key] = not current
        ok, message = self._save_config(config)
        if not ok:
            self.send_text(f"⚠️ 未能生效 {section}.{key}\n{message}", chat_id=chat_id)
        self._send_config_menu(chat_id, edit_message_id)

    @staticmethod
    def _coerce_config_value(current: Any, text: str) -> Any:
        raw = str(text or "").strip()
        if isinstance(current, bool):
            lowered = raw.lower()
            if lowered in ("true", "1", "yes", "y", "on", "是", "开", "启用"):
                return True
            if lowered in ("false", "0", "no", "n", "off", "否", "关", "停用"):
                return False
            raise ValueError("请输入 true/false")
        if isinstance(current, int):
            return int(raw)
        if isinstance(current, float):
            return float(raw)
        if isinstance(current, list):
            return [item.strip() for item in re.split(r"[,，;；]", raw) if item.strip()]
        return raw

    def _save_config(self, config: Dict[str, Any]):
        config_path = self._config_path
        try:
            if config_path:
                from .config_loader import update_config

                update_config(config, config_path)
            with self._lock:
                self._config = config
            try:
                from ..web.services.state import get_state_manager

                get_state_manager().set_config(
                    config, Path(config_path) if config_path else None
                )
            except Exception as exc:
                logger.debug("同步配置到状态管理器失败: %s", exc)
            try:
                from ..web.routers.config import _apply_runtime_config

                _apply_runtime_config(config)
            except Exception as exc:
                logger.debug("配置热更新失败: %s", exc)
            return True, "已保存并即时生效"
        except Exception as exc:
            logger.warning("保存配置失败: %s", exc)
            return False, f"保存失败：{exc}"

    def _apply_config_value(
        self, context: Dict[str, Any], text: str, chat_id: str, reply_to: Optional[int]
    ) -> None:
        section = str(context.get("_config_section") or "")
        key = str(context.get("_config_key") or "")
        config = self._current_config()
        values = config.get(section) if isinstance(config.get(section), dict) else None
        if not section or not key or values is None or key not in values:
            self.send_text("配置修改已失效，请重新发送 /config。", chat_id=chat_id, reply_to=reply_to)
            return
        try:
            new_value = self._coerce_config_value(values.get(key), text)
        except Exception as exc:
            self.send_text(f"值格式不正确：{exc}", chat_id=chat_id, reply_to=reply_to)
            return
        values[key] = new_value
        ok, message = self._save_config(config)
        with self._lock:
            if reply_to is not None:
                self._replies.pop(str(reply_to), None)
        prefix = "✅ 已更新" if ok else "⚠️ 未能生效"
        self.send_text(
            f"{prefix} {section}.{key} = {self._mask_config_value(key, new_value)}\n{message}",
            chat_id=chat_id,
            reply_to=reply_to,
        )

    def _status_text(self) -> str:
        status = self.status()
        return (
            "🤖 Video Organizer Telegram 机器人\n"
            f"运行中：{'是' if status['running'] else '否'}\n"
            f"已绑定：{'是' if status['bound'] else '否'}\n"
            f"chat_id：{status['chat_id'] or '-'}\n"
            f"待回复报错：{status['pending_replies']}\n"
            f"已发送：{status['sent_count']}"
        )

    def _bind_chat(self, chat_id: str) -> None:
        """把 chat_id 写回配置（内存 + config.ini），在线即时生效"""
        with self._lock:
            self._chat_id = chat_id
            config = self._config
            config_path = self._config_path
        try:
            config.setdefault("telegram", {})["chat_id"] = chat_id
            if config_path:
                from .config_loader import update_config

                update_config(config, config_path)
            from ..web.services.state import get_state_manager

            get_state_manager().set_config(config, Path(config_path) if config_path else None)
            logger.info("Telegram 机器人已绑定会话: %s", chat_id)
        except Exception as exc:
            logger.warning("保存 Telegram chat_id 失败: %s", exc)

    # ------------------------------------------------------------------
    # 回复修正
    # ------------------------------------------------------------------

    @staticmethod
    def _is_delete_intent(text: str) -> bool:
        """判断回复内容是否为「删除该任务并停止提醒」"""
        return str(text or "").strip().lower() in _DELETE_KEYWORDS

    def _handle_delete_reply(
        self, context: Dict[str, Any], chat_id: str, reply_to: Optional[int]
    ) -> None:
        """回复「删除」：删除该文件的任务记录并停止所有后续提醒"""
        file_path = str(context.get("file_path") or "")
        name = context.get("file_name") or os.path.basename(file_path) or "-"
        removed = 0
        removed_downloads: List[str] = []
        if file_path:
            try:
                from .online_upload import OnlineUploadService

                removed = OnlineUploadService.instance().delete_tasks_for_file(file_path)
            except Exception as exc:
                logger.debug("删除文件任务失败: %s", exc)
            try:
                from .downloader_monitor import remove_downloader_tasks

                # 只清下载器里的任务，本地文件保留（需要删文件用 /upload 里的删除键）
                removed_downloads = remove_downloader_tasks(file_path, delete_files=False)
            except Exception as exc:
                logger.debug("删除下载器任务失败: %s", exc)
            self.clear_error(file_path)
        with self._lock:
            if reply_to is not None:
                self._replies.pop(str(reply_to), None)
        if removed:
            message = f"🗑️ 已删除该文件的任务并停止提醒\n文件：{name}\n（共移除 {removed} 个任务）"
        else:
            message = f"🗑️ 已停止该文件的提醒\n文件：{name}\n（未找到对应任务，可能已删除）"
        if removed_downloads:
            message += (
                f"\n已同步删除下载器任务：{'、'.join(removed_downloads)}"
                "（本地文件未删除，需要删文件用 /upload 里的删除键）"
            )
        self.send_text(message, chat_id=chat_id, reply_to=reply_to)

    def _handle_correction(self, context: Dict[str, Any], text: str, chat_id: str, reply_to: Optional[int]) -> None:
        expr = parse_target_expression(text)
        if not expr.title:
            expr.title = str(context.get("title") or "").strip()
        if not expr.parsed:
            self.send_text(
                "没能解析出目标，示例：\n　时光代理人S04E09\n　时光代理人 第4季第9集",
                reply_to=reply_to,
                chat_id=chat_id,
            )
            return

        file_path = str(context.get("file_path") or "")
        if not file_path:
            self.send_text("这条报错信息没有关联到文件，无法重传。", reply_to=reply_to, chat_id=chat_id)
            return

        from .online_upload import OnlineUploadService

        service = OnlineUploadService.instance()
        media_type = str(context.get("media_type") or "").strip().lower()
        if not media_type:
            media_type = expr.kind if expr.kind != "unknown" else ""
        video_type = "movie" if media_type == "movie" else ("tv" if media_type == "tv" else None)

        try:
            candidates, matched_title = self._search_by_title(service, expr.title, video_type)
        except Exception as exc:
            self.send_text(f"查询 Emos 失败：{exc}", reply_to=reply_to, chat_id=chat_id)
            return
        if matched_title:
            expr.title = matched_title
        raw_candidates = candidates
        candidates = self._filter_by_year(raw_candidates, expr.year)
        year_note = self._year_filter_note(expr.year, raw_candidates, candidates)

        if not candidates:
            # 回复里的关键词搜不到时，退回报错文件本身的标题再搜一次兜底
            fallback_title = str(context.get("title") or "").strip()
            if fallback_title and fallback_title != expr.title:
                try:
                    candidates, fallback_matched = self._search_by_title(
                        service, fallback_title, video_type
                    )
                except Exception as exc:
                    logger.debug("用报错标题兜底搜索失败: %s", exc)
                    candidates, fallback_matched = [], ""
                if candidates:
                    if fallback_matched:
                        expr.title = fallback_matched
                    raw_candidates = candidates
                    candidates = self._filter_by_year(raw_candidates, expr.year)
                    year_note = self._year_filter_note(
                        expr.year, raw_candidates, candidates
                    )

        # 没写季 / 集时不做一次性匹配，直接列出候选让用户点选作品 / 季 / 集
        if expr.season is None and expr.episode is None:
            self._send_search_results(
                chat_id,
                context,
                candidates,
                expr.title,
                reply_to=reply_to,
                note=year_note,
            )
            return

        match = self._locate_target(service, candidates, expr, media_type)
        if not match and video_type:
            # Emos 带 type 过滤时可能返回「有结果但不含季/集」的候选，让明明存在
            # 的条目被判成未找到；search_targets 只在“完全搜不到”时兜底，这里补上
            # 「有结果但定位不到」的兜底：去掉类型再搜一次。
            try:
                retry_candidates = service.search_targets(title=expr.title)
            except Exception:
                retry_candidates = []
            if retry_candidates:
                raw_candidates = retry_candidates
                candidates = self._filter_by_year(raw_candidates, expr.year)
                year_note = self._year_filter_note(
                    expr.year, raw_candidates, candidates
                )
                match = self._locate_target(service, candidates, expr, media_type)
        if not match:
            # 直接匹配不到时不再死胡同：列出候选，让用户点选正确的作品 / 季 / 集
            if candidates:
                note = f"未找到 {expr.describe()}，可直接在下面点选正确的作品 / 季 / 集。"
                if year_note:
                    note += f"\n{year_note}"
                hint = self._candidate_hint(candidates)
                if hint:
                    note += f"\n\n搜索到：\n{hint}"
                self._send_search_results(
                    chat_id, context, candidates, expr.title, reply_to=reply_to, note=note
                )
                return
            message = (
                f"未在 Emos 中找到匹配条目：{expr.describe()}"
                "\n\n请换几个关键词后重新回复本条报错消息。"
            )
            self.send_text(message, reply_to=reply_to, chat_id=chat_id)
            return

        # 命中后这条报错信息不再需要回复
        with self._lock:
            if reply_to is not None:
                self._replies.pop(str(reply_to), None)

        season_number = match.get("season_number")
        if season_number is None:
            season_number = expr.season
        episode_number = match.get("episode_number")
        if episode_number is None:
            episode_number = expr.episode
        try:
            task = service.create_task(
                {
                    "file_path": file_path,
                    "item_type": match.get("item_type"),
                    "item_id": match.get("item_id"),
                    "storage": context.get("storage"),
                    "title": expr.title or str(context.get("title") or ""),
                    "media_type": media_type,
                    "season_number": season_number,
                    "episode_number": episode_number,
                }
            )
        except Exception as exc:
            self.send_text(f"创建上传任务失败：{exc}", reply_to=reply_to, chat_id=chat_id)
            return

        # 记住这次手动指定的目标，并自动重传同剧集其他报错文件
        mapping_info = self._remember_correction_mapping(
            expr, media_type, match, candidates, context
        )
        retried = 0
        if mapping_info.get("show_level"):
            retried = self._retry_pending_same_title(
                expr.title or str(context.get("title") or ""),
                media_type,
                exclude_file=file_path,
            )
        self.clear_error(file_path)

        note = ""
        try:
            if service.delete_after_upload_enabled():
                note = "\n（按配置，上传完成后会删除原文件）"
        except Exception:
            note = ""
        if retried:
            extra = f"\n已记住映射，同名剧集后续直接上传（本次自动重传 {retried} 个）"
        elif mapping_info.get("saved"):
            extra = "\n已记住映射，之后同名文件可直接上传"
        else:
            extra = ""
        self.send_text(
            "✅ 已按修正目标重新上传\n"
            f"文件：{os.path.basename(file_path)}\n"
            f"目标：{match.get('label') or expr.describe()}\n"
            f"任务：{task.get('id')}{note}{extra}",
            reply_to=reply_to,
            chat_id=chat_id,
        )
        logger.info("Telegram 修正目标成功: %s -> %s", expr.describe(), match.get("label"))

    @staticmethod
    def _show_anchor(
        candidates: List[Dict[str, Any]],
        title: str,
        match: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """在候选里找「作品级（vl）」条目，作为同名剧集的锚点

        先按命中的具体条目 id 反查它所属的作品（标题归一化可能对不上），
        再退回按标题匹配，尽量保证「指定一集 -> 同名剧集全部生效」。
        """
        try:
            from .mapping_store import normalize_text
        except Exception:
            return None
        match_id = str((match or {}).get("item_id") or "")
        if match_id:
            for video in candidates or []:
                if not isinstance(video, dict):
                    continue
                if not _candidate_contains_item(video, match_id):
                    continue
                item_id = video.get("item_id")
                if item_id:
                    return {
                        "item_type": video.get("item_type") or "vl",
                        "item_id": str(item_id),
                        "label": video.get("title") or title,
                    }
        norm = normalize_text(title)
        if not norm:
            return None
        for video in candidates or []:
            if not isinstance(video, dict):
                continue
            video_title = normalize_text(video.get("title"))
            if not video_title:
                continue
            if video_title == norm or norm in video_title or video_title in norm:
                item_id = video.get("item_id")
                if item_id:
                    return {
                        "item_type": video.get("item_type") or "vl",
                        "item_id": str(item_id),
                        "label": video.get("title") or title,
                    }
        return None

    def _remember_correction_mapping(
        self,
        expr: "TargetExpression",
        media_type: str,
        match: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """把这次手动指定的目标写入映射表

        电视剧优先记录「作品级」映射：以后同名剧集只要解析出季/集号，
        上传时会自动落到各自的 ve，无需再逐集手动指定。
        """
        result = {"saved": False, "show_level": False}
        try:
            from .mapping_store import TargetMappingStore, normalize_text
        except Exception:
            return result
        title = (expr.title or str(context.get("title") or "")).strip()
        if not title:
            return result
        anchor = self._show_anchor(candidates, title, match)
        show_level = bool(anchor) and (
            media_type == "tv" or expr.episode is not None or expr.season is not None
        )
        if show_level:
            target = {
                "item_type": anchor.get("item_type"),
                "item_id": anchor.get("item_id"),
                "label": anchor.get("label") or title,
                "storage": context.get("storage"),
            }
            season_for_mapping = None
            episode_for_mapping = None
        else:
            target = {
                "item_type": match.get("item_type"),
                "item_id": match.get("item_id"),
                "label": match.get("label"),
                "storage": context.get("storage"),
            }
            season_for_mapping = expr.season if expr.season is not None else match.get("season_number")
            episode_for_mapping = expr.episode if expr.episode is not None else match.get("episode_number")
        if not str(target.get("item_type") or "").strip() or not str(target.get("item_id") or "").strip():
            return result
        try:
            mapping_id = TargetMappingStore.remember(
                file_path=str(context.get("file_path") or ""),
                title=title,
                media_type=media_type,
                season=season_for_mapping,
                episode=episode_for_mapping,
                target=target,
                source="telegram",
                keyword=title,
            )
            result["saved"] = mapping_id is not None
            result["show_level"] = show_level

            # 用户手填的标题（如中文译名）常常和自动识别出的标题（如英文原名）
            # 不一样。只记录手填标题的话，以后自动处理同名文件时匹配不上映射。
            # 这里把自动识别到的标题也存一份别名，保证「指定过一次以后直接上传」。
            auto_title = str(context.get("title") or "").strip()
            if auto_title and normalize_text(auto_title) != normalize_text(title):
                TargetMappingStore.remember(
                    file_path=str(context.get("file_path") or ""),
                    title=auto_title,
                    media_type=media_type,
                    season=season_for_mapping,
                    episode=episode_for_mapping,
                    target=target,
                    source="telegram",
                    keyword=auto_title,
                )
        except Exception as exc:
            logger.debug("记录修正映射失败: %s", exc)
        return result

    def _retry_pending_same_title(
        self, title: str, media_type: str, exclude_file: str = ""
    ) -> int:
        """把同一剧名、仍在报错的文件按新映射自动重传"""
        try:
            from .mapping_store import TargetMappingStore, normalize_text
            from .online_upload import OnlineUploadService
        except Exception:
            return 0
        norm = normalize_text(title)
        with self._lock:
            pending = [(key, dict(item)) for key, item in self._active_errors.items()]
        service = OnlineUploadService.instance()
        count = 0
        for _key, item in pending:
            ctx = item.get("context") or {}
            ctx_title = str(ctx.get("title") or "")
            path = str(ctx.get("file_path") or "")
            if not path or path == exclude_file or not os.path.exists(path):
                continue
            # 优先用映射表判断：该文件现在能命中映射，就说明它和刚修正的是同一部剧，
            # 直接重传。这样即便用户填的是中文译名、自动识别到的是英文原名也能全部生效。
            mapped = None
            try:
                mapped = TargetMappingStore.resolve(
                    title=ctx_title,
                    season=ctx.get("season_number"),
                    episode=ctx.get("episode_number"),
                    media_type=str(ctx.get("media_type") or media_type or ""),
                    file_name=os.path.basename(path),
                )
            except Exception:
                mapped = None
            if not mapped:
                ctx_norm = normalize_text(ctx_title)
                if not ctx_norm:
                    # 报错上下文没有标题时，退回用文件名判断是否同一部剧
                    ctx_norm = normalize_text(os.path.basename(path))
                if not norm or not ctx_norm or not (
                    ctx_norm == norm or norm in ctx_norm or ctx_norm in norm
                ):
                    continue
            try:
                recognized = service.recognize(path)
            except Exception:
                continue
            match = recognized.get("match")
            meta = recognized.get("metadata") or {}
            if not match:
                continue
            season_number = match.get("season_number")
            if season_number is None:
                season_number = meta.get("season")
            episode_number = match.get("episode_number")
            if episode_number is None:
                episode_number = meta.get("episode")
            try:
                task = service.create_task(
                    {
                        "file_path": path,
                        "item_type": match.get("item_type"),
                        "item_id": match.get("item_id"),
                        "storage": ctx.get("storage"),
                        "title": meta.get("title") or ctx_title,
                        "media_type": media_type or meta.get("media_type") or "",
                        "season_number": season_number,
                        "episode_number": episode_number,
                    }
                )
            except Exception as exc:
                logger.debug("自动重传同剧集文件失败: %s - %s", path, exc)
                continue
            if task.get("duplicate"):
                continue
            self.clear_error(path)
            count += 1
        return count

    # ------------------------------------------------------------------
    # 搜索 → 选作品 → 选季 → 选集（回复报错信息后的交互式修正）
    # ------------------------------------------------------------------

    def _flow_token(self, payload: Dict[str, Any]) -> str:
        """为「搜索 / 选季 / 选集」会话生成短令牌（规避 callback_data 64 字节限制）"""
        with self._lock:
            self._flow_seq += 1
            token = f"f{self._flow_seq}"
            self._flow_tokens[token] = payload
            while len(self._flow_tokens) > _MAX_FLOW_TOKENS:
                self._flow_tokens.pop(next(iter(self._flow_tokens)), None)
            return token

    def _flow_payload(self, token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._flow_tokens.get(str(token))

    @staticmethod
    def _work_label(video: Dict[str, Any]) -> str:
        """作品候选按钮文案：《片名》（S01 2024、S02 2026）"""
        title = str(video.get("title") or "").strip() or "?"
        year = _item_year(video)
        entries: List[Tuple[int, str]] = []
        for season in video.get("seasons") or []:
            if not isinstance(season, dict) or season.get("season_number") is None:
                continue
            entries.append((int(season.get("season_number")), _item_year(season)))
        # 季各自标了年份（第一季 2024、第二季 2026）时按季展示，否则显示作品级年份
        if entries and any(season_year for _, season_year in entries):
            bits = []
            for number, season_year in entries:
                shown = season_year or year
                bits.append(f"S{number:02d} {shown}" if shown else f"S{number:02d}")
            return f"《{title}》（{'、'.join(bits)}）"
        parts: List[str] = []
        if year:
            parts.append(year)
        if entries:
            parts.append("、".join(f"S{number:02d}" for number, _ in entries))
        if parts:
            return f"《{title}》（{' · '.join(parts)}）"
        return f"《{title}》"

    @staticmethod
    def _filter_by_year(
        candidates: List[Dict[str, Any]], year: Optional[int]
    ) -> List[Dict[str, Any]]:
        """回复里带了年份时，优先只保留年份匹配的候选（同名作品区分）

        年份可能标在作品上，也可能标在季 / 集上（第一季 2024、第二季 2026），
        逐层比对；一个候选都对不上时不再清空，退回全部候选兜底。
        """
        if not year:
            return candidates
        matched = [video for video in candidates if _year_matches(video, year)]
        return matched or candidates

    @staticmethod
    def _year_filter_note(
        year: Optional[int],
        raw: List[Dict[str, Any]],
        filtered: List[Dict[str, Any]],
    ) -> str:
        """按年份筛选后给用户的说明（区分「筛掉了同名作品」和「一个都对不上」）"""
        if not year or not raw:
            return ""
        if any(_year_matches(video, year) for video in raw):
            return f"已按年份 {year} 优先筛选同名作品。"
        return f"没有年份为 {year} 的候选，已列出全部结果。"

    @staticmethod
    def _title_variants(title: str) -> List[str]:
        """粘贴文件名时标题常带 ASCII 点 / 多余片段，给出依次尝试的搜索词

        例如「狂王.Asura.S02E04」解析出的标题是「狂王.Asura」，
        而「狂王.2024.1080p.S01E01」会剩下「狂王 1080p」；
        Emos 里通常只叫「狂王」，所以按「原样 → 点换空格 → 去掉压制标签
        → 去掉年份 → 第一段 → 去掉空格」依次搜。
        """
        base = str(title or "").strip()
        variants: List[str] = []
        spaced = base.replace(".", " ")
        for candidate in (
            base,
            spaced,
            TelegramBotService._strip_release_tags(base),
            TelegramBotService._strip_release_tags(spaced),
            TelegramBotService._strip_years(base),
            TelegramBotService._strip_years(spaced),
            base.split(".")[0],
            "".join(base.split()),
        ):
            candidate = " ".join(str(candidate).split()).strip()
            if candidate and candidate not in variants:
                variants.append(candidate)
        return variants

    @staticmethod
    def _strip_years(title: str) -> str:
        """去掉标题里混入的年份（狂王 2024 → 狂王），避免把年份当片名搜"""
        cleaned = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", str(title or ""))
        return " ".join(cleaned.split()).strip()

    @staticmethod
    def _strip_release_tags(title: str) -> str:
        """去掉标题里混入的压制 / 片源标签（1080p、WEB-DL、H265、DDP2.0…）"""
        tokens = re.split(r"[\s._\-·|]+", str(title or "").strip())
        kept: List[str] = []
        for token in tokens:
            if not token:
                continue
            if kept and _RE_RELEASE_TAG.match(token):
                break
            kept.append(token)
        return " ".join(kept)

    def _search_by_title(
        self, service: Any, title: str, video_type: Optional[str]
    ) -> Tuple[List[Dict[str, Any]], str]:
        """按标题变体依次搜索 Emos，返回（候选列表，真正搜到结果的标题）"""
        for variant in self._title_variants(title):
            candidates = service.search_targets(video_type=video_type, title=variant)
            if candidates:
                return candidates, variant
        return [], title

    @staticmethod
    def _fetch_tree(video: Dict[str, Any]) -> List[Dict[str, Any]]:
        """按作品 id 拉 Emos 完整目录树（失败时返回空列表）"""
        vl_id = video.get("item_id")
        if not vl_id:
            return []
        try:
            from .online_upload import OnlineUploadService

            client = OnlineUploadService.instance().get_client()
            tree = client.get_video_tree(video_id=vl_id) or []
        except Exception as exc:
            logger.debug("获取 Emos 目录树失败: %s", exc)
            return []
        items = [item for item in tree if isinstance(item, dict)]
        narrowed = [item for item in items if str(item.get("item_id")) == str(vl_id)]
        return narrowed or items

    def _load_video_seasons(self, video: Dict[str, Any]) -> List[Dict[str, Any]]:
        """取作品的季列表；搜索结果没带季时拉完整目录树补齐"""
        seasons = [s for s in (video.get("seasons") or []) if isinstance(s, dict)]
        if seasons:
            return seasons
        for item in self._fetch_tree(video):
            found = [s for s in (item.get("seasons") or []) if isinstance(s, dict)]
            if found:
                return found
        return self._load_seasons_via_api(video)

    def _load_seasons_via_api(
        self, video: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """目录树里没有季时，用 Emos 的季接口兜底（手动新增的季可能不在树里）"""
        vl_id = video.get("item_id")
        if not vl_id:
            return []
        try:
            from .online_upload import OnlineUploadService

            client = OnlineUploadService.instance().get_client()
            return [
                item
                for item in (client.get_seasons(vl_id) or [])
                if isinstance(item, dict)
            ]
        except Exception as exc:
            logger.debug("Emos 季接口兜底查询失败: %s", exc)
            return []

    def _load_season_episodes(
        self, video: Dict[str, Any], season: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """取某季的集列表；搜索结果没带集号时拉完整目录树补齐"""
        season_id = str(season.get("item_id") or "")
        season_number = season.get("season_number")
        for item in self._fetch_tree(video):
            for candidate in item.get("seasons") or []:
                if not isinstance(candidate, dict):
                    continue
                same_id = bool(season_id) and str(candidate.get("item_id")) == season_id
                same_number = (
                    season_number is not None
                    and candidate.get("season_number") is not None
                    and int(candidate.get("season_number")) == int(season_number)
                )
                if same_id or same_number:
                    episodes = [
                        episode
                        for episode in (candidate.get("episodes") or [])
                        if isinstance(episode, dict)
                    ]
                    if episodes:
                        return episodes
        return self._load_episodes_via_api(video, season_number)

    def _load_episodes_via_api(
        self, video: Dict[str, Any], season_number: Optional[Any]
    ) -> List[Dict[str, Any]]:
        """目录树里没有该季的集时，用 Emos 的集接口兜底（手动新增的集可能不在树里）"""
        vl_id = video.get("item_id")
        if not vl_id:
            return []
        try:
            from .online_upload import OnlineUploadService

            client = OnlineUploadService.instance().get_client()
            episodes = client.get_episodes(vl_id, season_number)
        except Exception as exc:
            logger.debug("Emos 集接口兜底查询失败: %s", exc)
            return []
        return [item for item in (episodes or []) if isinstance(item, dict)]

    def _send_search_results(
        self,
        chat_id: str,
        context: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        title: str,
        edit_message_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        note: str = "",
    ) -> None:
        """列出搜索结果按钮，供用户点选作品"""
        title = str(title or "").strip()
        if not candidates:
            text = (
                f"🔍 没有搜到与「{title}」相关的条目。\n"
                "请换几个关键词（例如片名简写 / 原名）后重新回复本条报错消息。"
            )
            if edit_message_id:
                self._send_keyboard(chat_id, text, [], edit_message_id)
            else:
                self.send_text(text, reply_to=reply_to, chat_id=chat_id)
            return
        rows: List[List[Dict[str, str]]] = []
        for video in candidates[:8]:
            if not isinstance(video, dict):
                continue
            token = self._flow_token(
                {
                    "context": dict(context or {}),
                    "video": video,
                    "candidates": candidates,
                    "title": title or str(video.get("title") or ""),
                }
            )
            rows.append(
                [
                    {
                        "text": _shorten_text(self._work_label(video), 60),
                        "callback_data": f"fx:work:{token}",
                    }
                ]
            )
        rows.append([{"text": "✖️ 取消", "callback_data": "fx:cancel"}])
        text = f"🔍 搜索「{title}」找到 {len(candidates)} 个结果，请点选作品："
        if note:
            text = f"{note}\n\n{text}"
        if edit_message_id:
            self._send_keyboard(chat_id, text, rows, edit_message_id)
        else:
            self._send_keyboard(chat_id, text, rows)

    def _send_work_choices(
        self, chat_id: str, payload: Dict[str, Any], edit_message_id: Optional[int] = None
    ) -> None:
        """点选作品后：电影直接上传，剧集列出可选季"""
        video = payload.get("video") or {}
        context = payload.get("context") or {}
        candidates = payload.get("candidates") or []
        title = str(payload.get("title") or video.get("title") or "")
        seasons = self._load_video_seasons(video)
        if not seasons:
            item_id = video.get("item_id")
            if not item_id:
                self._send_keyboard(chat_id, "该条目没有可上传的季 / 集。", [], edit_message_id)
                return
            self._apply_target_selection(
                chat_id,
                context,
                {
                    "item_type": video.get("item_type") or "vl",
                    "item_id": str(item_id),
                    "label": video.get("title") or title,
                    "season_number": None,
                    "episode_number": None,
                },
                candidates,
                title,
                edit_message_id,
            )
            return
        rows: List[List[Dict[str, str]]] = []
        for season in seasons:
            number = season.get("season_number")
            episodes = [e for e in (season.get("episodes") or []) if isinstance(e, dict)]
            label = f"S{int(number):02d}" if number is not None else "未标注季"
            if episodes:
                label += f"（{len(episodes)} 集）"
            token = self._flow_token(
                {
                    "context": context,
                    "video": video,
                    "season": season,
                    "candidates": candidates,
                    "title": title,
                }
            )
            rows.append([{"text": label, "callback_data": f"fx:season:{token}"}])
        back_token = self._flow_token(
            {"context": context, "candidates": candidates, "title": title}
        )
        rows.append([{"text": "↩️ 返回搜索结果", "callback_data": f"fx:search:{back_token}"}])
        self._send_keyboard(
            chat_id, f"{self._work_label(video)} 请选择要上传的季：", rows, edit_message_id
        )

    def _send_season_choices(
        self, chat_id: str, payload: Dict[str, Any], edit_message_id: Optional[int] = None
    ) -> None:
        """点选季后：列出该季的集，支持整季上传"""
        video = payload.get("video") or {}
        season = payload.get("season") or {}
        context = payload.get("context") or {}
        candidates = payload.get("candidates") or []
        title = str(payload.get("title") or video.get("title") or "")
        episodes = [e for e in (season.get("episodes") or []) if isinstance(e, dict)]
        if not episodes:
            episodes = self._load_season_episodes(video, season)
        rows: List[List[Dict[str, str]]] = []
        row: List[Dict[str, str]] = []
        for episode in episodes:
            number = episode.get("episode_number")
            label = f"E{int(number):02d}" if number is not None else "未标注集"
            episode_title = str(episode.get("episode_title") or "").strip()
            if episode_title:
                label += f" {_shorten_text(episode_title, 10)}"
            token = self._flow_token(
                {
                    "context": context,
                    "video": video,
                    "season": season,
                    "episode": episode,
                    "candidates": candidates,
                    "title": title,
                }
            )
            row.append({"text": label, "callback_data": f"fx:ep:{token}"})
            if len(row) >= 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if season.get("item_id"):
            all_token = self._flow_token(
                {
                    "context": context,
                    "video": video,
                    "season": season,
                    "candidates": candidates,
                    "title": title,
                }
            )
            rows.append([{"text": "📦 上传整季", "callback_data": f"fx:season_all:{all_token}"}])
        back_token = self._flow_token(
            {"context": context, "video": video, "candidates": candidates, "title": title}
        )
        rows.append([{"text": "↩️ 返回季列表", "callback_data": f"fx:work:{back_token}"}])
        if not episodes:
            rows.append([{"text": "（该季没有可上传的集）", "callback_data": "up:noop"}])
        number = season.get("season_number")
        head = f"S{int(number):02d}" if number is not None else "该季"
        self._send_keyboard(chat_id, f"{title} {head} 请选择要上传的集：", rows, edit_message_id)

    def _apply_target_selection(
        self,
        chat_id: str,
        context: Dict[str, Any],
        selection: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        title: str,
        edit_message_id: Optional[int] = None,
    ) -> None:
        """按用户点选的目标创建上传任务，并记住映射"""
        file_path = str(context.get("file_path") or "")
        if not file_path:
            self._send_keyboard(chat_id, "这条报错信息没有关联到文件，无法重传。", [], edit_message_id)
            return
        from .online_upload import OnlineUploadService

        service = OnlineUploadService.instance()
        title = str(title or context.get("title") or "")
        media_type = str(context.get("media_type") or "").strip().lower()
        if not media_type:
            media_type = (
                "tv"
                if selection.get("season_number") is not None
                or selection.get("episode_number") is not None
                else ""
            )
        try:
            task = service.create_task(
                {
                    "file_path": file_path,
                    "item_type": selection.get("item_type"),
                    "item_id": selection.get("item_id"),
                    "storage": context.get("storage"),
                    "title": title,
                    "media_type": media_type,
                    "season_number": selection.get("season_number"),
                    "episode_number": selection.get("episode_number"),
                }
            )
        except Exception as exc:
            self._send_keyboard(chat_id, f"创建上传任务失败：{exc}", [], edit_message_id)
            return
        if task.get("duplicate"):
            self._send_keyboard(
                chat_id,
                f"未重复提交：{task.get('duplicate_reason')}\n文件：{os.path.basename(file_path)}",
                [],
                edit_message_id,
            )
            return

        expr = TargetExpression(
            title=title,
            season=selection.get("season_number"),
            episode=selection.get("episode_number"),
        )
        match = {
            "item_type": selection.get("item_type"),
            "item_id": selection.get("item_id"),
            "label": selection.get("label"),
            "season_number": selection.get("season_number"),
            "episode_number": selection.get("episode_number"),
        }
        mapping_info = self._remember_correction_mapping(
            expr, media_type, match, candidates, context
        )
        retried = 0
        if mapping_info.get("show_level"):
            retried = self._retry_pending_same_title(title, media_type, exclude_file=file_path)
        self.clear_error(file_path)
        with self._lock:
            for key, item in list(self._replies.items()):
                if str((item or {}).get("file_path") or "") == file_path:
                    self._replies.pop(key, None)
        note = ""
        try:
            if service.delete_after_upload_enabled():
                note = "\n（按配置，上传完成后会删除原文件）"
        except Exception:
            note = ""
        if retried:
            extra = f"\n已记住映射，同名剧集后续直接上传（本次自动重传 {retried} 个）"
        elif mapping_info.get("saved"):
            extra = "\n已记住映射，之后同名文件可直接上传"
        else:
            extra = ""
        text = (
            "✅ 已按选择的目标提交上传\n"
            f"文件：{os.path.basename(file_path)}\n"
            f"目标：{selection.get('label') or title}\n"
            f"任务：{task.get('id')}{note}{extra}"
        )
        if edit_message_id:
            self._send_keyboard(chat_id, text, [], edit_message_id)
        else:
            self.send_text(text, chat_id=chat_id)
        logger.info("Telegram 手动锁定目标成功: %s -> %s", title, selection.get("label"))

    def _handle_flow_callback(
        self, chat_id: str, action: str, token: str, message_id: Optional[int]
    ) -> None:
        """处理「搜索 → 选作品 → 选季 → 选集」流程里的按钮"""
        if action == "cancel":
            self._send_keyboard(chat_id, "已取消。", [], message_id)
            return
        payload = self._flow_payload(token)
        if payload is None:
            self.send_text("该操作已过期，请重新回复报错信息搜索目标。", chat_id=chat_id)
            return
        context = payload.get("context") or {}
        candidates = payload.get("candidates") or []
        title = str(payload.get("title") or "")
        if action == "search":
            self._send_search_results(chat_id, context, candidates, title, message_id)
            return
        if action == "work":
            self._send_work_choices(chat_id, payload, message_id)
            return
        if action == "season":
            self._send_season_choices(chat_id, payload, message_id)
            return
        if action == "ep":
            season = payload.get("season") or {}
            episode = payload.get("episode") or {}
            self._apply_target_selection(
                chat_id,
                context,
                {
                    "item_type": episode.get("item_type") or "ve",
                    "item_id": str(episode.get("item_id") or ""),
                    "label": episode.get("episode_title")
                    or (payload.get("video") or {}).get("title")
                    or "",
                    "season_number": season.get("season_number"),
                    "episode_number": episode.get("episode_number"),
                },
                candidates,
                title,
                message_id,
            )
            return
        if action == "season_all":
            season = payload.get("season") or {}
            self._apply_target_selection(
                chat_id,
                context,
                {
                    "item_type": season.get("item_type") or "vs",
                    "item_id": str(season.get("item_id") or ""),
                    "label": season.get("season_title")
                    or (payload.get("video") or {}).get("title")
                    or "",
                    "season_number": season.get("season_number"),
                    "episode_number": None,
                },
                candidates,
                title,
                message_id,
            )
            return
        self._send_keyboard(chat_id, "未知操作。", [], message_id)

    @staticmethod
    def _locate_target(
        service: Any,
        candidates: List[Dict[str, Any]],
        expr: TargetExpression,
        media_type: str,
    ) -> Optional[Dict[str, Any]]:
        """在候选里定位目标；候选没有嵌套季/集时拉完整目录树再定位"""
        match = service.pick_target(
            candidates, expr.season, expr.episode, media_type, year=expr.year, title=expr.title
        )
        if match:
            return match
        if expr.episode is None:
            return None
        try:
            return service.resolve_episode_from_candidates(
                candidates, expr.season, expr.episode, media_type, title=expr.title
            )
        except Exception as exc:
            logger.debug("按候选定位剧集失败: %s", exc)
            return None

    @staticmethod
    def _candidate_hint(candidates: List[Dict[str, Any]], limit: int = 5) -> str:
        lines: List[str] = []
        for video in (candidates or [])[:limit]:
            if not isinstance(video, dict):
                continue
            title = str(video.get("title") or "").strip()
            if not title:
                continue
            lines.append(f"· {TelegramBotService._work_label(video)}")
        return "\n".join(lines)
