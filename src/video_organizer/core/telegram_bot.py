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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    "/upload  浏览并选择本地文件 / 整个文件夹上传\n"
    "/config  查看/修改「处理配置」与「Emos API」\n"
    "/status  查看机器人状态\n"
    "/help  查看本帮助\n\n"
    "修正上传目标：直接「回复」某条报错信息并发送目标，例如\n"
    "　时光代理人S04E09\n"
    "　时光代理人 第4季第9集\n"
    "　时光代理人 4x09\n"
    "　时光代理人 (2024)"
)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
        """tv / movie / unknown"""
        if self.season is not None or self.episode is not None:
            return "tv"
        if self.year is not None:
            return "movie"
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
_RE_YEAR = re.compile(r"(?:[\(\[]|\s|^)(?P<y>(?:19|20)\d{2})(?:[\)\]]|\s|$)")
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

    if expr.season is None and expr.episode is None:
        match = _RE_YEAR.search(text)
        if match:
            expr.year = int(match.group("y"))
            cut(match)

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
        message_id = self.send_text("✅ Video Organizer Telegram 机器人连接正常。", chat_id=chat_id)
        if message_id:
            return {"success": True, "message": "测试消息已发送", "message_id": message_id}
        with self._lock:
            reason = self._last_error or "未配置 bot_token / chat_id"
        return {"success": False, "message": f"发送失败: {reason}"}

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
        lines.append("")
        lines.append("回复本条消息即可修正目标，例如：")
        lines.append("　时光代理人S04E09")
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

    def clear_error(self, file_path: str, header: Optional[str] = None) -> None:
        """上传成功 / 问题解决后停止该文件的定时提醒"""
        file_path = str(file_path or "").strip()
        if not file_path:
            return
        prefix = file_path + "|"
        with self._lock:
            keys = [
                key
                for key in list(self._active_errors)
                if key.startswith(prefix) and (header is None or key.endswith("|" + header))
            ]
            for key in keys:
                self._active_errors.pop(key, None)

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
            header = item.get("header") or "上传失败"
            context = item.get("context") or {}
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
            lines.append("")
            lines.append("回复本条消息即可修正目标（未解决前每 5 分钟提醒一次）")
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
                self.send_text("机器人已绑定当前会话 ✅\n发送 /help 查看用法。", chat_id=chat_id)
                return
            self._bind_chat(chat_id)
            self.send_text(
                f"绑定成功 ✅\nchat_id = {chat_id}\n\n上传失败时会推送报错信息，直接回复即可修正目标。",
                chat_id=chat_id,
            )
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
        entries += [{"kind": "file", "path": f} for f in files]
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

        rows: List[List[Dict[str, str]]] = [
            [
                {
                    "text": "⬆️ 上一级",
                    "callback_data": f"up:ls:{self._token_for_path(str(base.parent))}:0",
                },
                {"text": "🏠 根目录", "callback_data": "up:roots"},
            ]
        ]
        if dirs or files:
            rows.append(
                [
                    {
                        "text": "📤 上传此文件夹全部视频",
                        "callback_data": f"up:dir:{base_token}",
                    }
                ]
            )
        for entry in page_entries:
            target = entry["path"]
            token = self._token_for_path(str(target))
            if entry["kind"] == "dir":
                rows.append(
                    [
                        {"text": f"📁 {target.name}", "callback_data": f"up:ls:{token}:0"},
                        {"text": "📤 上传", "callback_data": f"up:dir:{token}"},
                    ]
                )
            else:
                rows.append(
                    [{"text": f"🎬 {target.name}", "callback_data": f"up:file:{token}"}]
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
        text = f"目录：{base}\n子目录 {len(dirs)} 个 · 视频/字幕 {len(files)} 个"
        if total_pages > 1:
            text += f"（第 {page + 1}/{total_pages} 页）"
        self._send_keyboard(chat_id, text, rows, edit_message_id)

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
            if data == "cfg:root":
                self._answer_callback(query_id, "")
                self._send_config_menu(chat_id, message_id)
                return
            if data.startswith("cfg:sec:"):
                section = data[len("cfg:sec:"):]
                self._answer_callback(query_id, "")
                self._send_config_section(chat_id, section, message_id)
                return
            if data.startswith("cfg:edit:"):
                payload = data[len("cfg:edit:"):]
                section, _, key = payload.partition(":")
                self._answer_callback(query_id, "请回复新值")
                self._prompt_config_edit(chat_id, section, key)
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
        lines = ["未能自动识别上传目标，请「回复」本条消息指定目标，例如：", "　时光代理人S04E09"]
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

        self.send_text(
            f"📂 开始上传文件夹：{base.name}\n共 {len(files)} 个文件，正在逐个识别上传…",
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
        rows = [
            [{"text": f"⚙️ {label}", "callback_data": f"cfg:sec:{section}"}]
            for section, label in self._CONFIG_SECTIONS.items()
        ]
        self._send_keyboard(chat_id, "请选择要查看/修改的配置：", rows, edit_message_id)

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
            candidates = (
                service.search_targets(video_type=video_type, title=expr.title) if expr.title else []
            )
        except Exception as exc:
            self.send_text(f"查询 Emos 失败：{exc}", reply_to=reply_to, chat_id=chat_id)
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
                candidates = retry_candidates
                match = self._locate_target(service, candidates, expr, media_type)
        if not match:
            hint = self._candidate_hint(candidates)
            message = f"未在 Emos 中找到匹配条目：{expr.describe()}"
            if hint:
                message += f"\n\n可能的目标：\n{hint}"
            message += "\n\n请调整标题 / 季集后重新回复。"
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
            seasons = video.get("seasons") or []
            if seasons:
                names = [
                    f"S{int(season.get('season_number')):02d}"
                    for season in seasons
                    if isinstance(season, dict) and season.get("season_number") is not None
                ]
                if names:
                    lines.append(f"· {title} （{'、'.join(names)}）")
                    continue
            date_air = str(video.get("date_air") or "")
            lines.append(f"· {title}" + (f" （{date_air[:4]}）" if date_air[:4].isdigit() else ""))
        return "\n".join(lines)
