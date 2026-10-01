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

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

_HELP_TEXT = (
    "可用指令：\n"
    "/bind  绑定当前会话（把 chat_id 写回配置）\n"
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
        worker.start()
        logger.info("Telegram 机器人已启动（长轮询）")

    def stop(self) -> None:
        """停止长轮询线程"""
        with self._lock:
            thread = self._thread
            self._thread = None
            self._thread_token = ""
            self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
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

    def notify_error(self, context: Dict[str, Any], error: str, header: str = "上传失败") -> bool:
        """推送一条上传报错信息，并记住它以便用户回复修正"""
        with self._lock:
            if not (self._token and self._chat_id and self._enabled):
                return False
            task_id = str((context or {}).get("task_id") or "")
        lines = [f"❌ {header}" + (f" #{task_id}" if task_id else "")]
        name = (context or {}).get("file_name") or os.path.basename(str((context or {}).get("file_path") or "")) or "-"
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
            return False
        with self._lock:
            self._replies[str(message_id)] = dict(context or {}, _error=str(error or ""), _at=_now_text())
            while len(self._replies) > _MAX_REPLY_MAP:
                self._replies.pop(next(iter(self._replies)), None)
        return True

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
                "allowed_updates": json.dumps(["message"]),
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
        if command == "/status":
            self.send_text(self._status_text(), chat_id=chat_id)
            return
        self.send_text("未知指令，发送 /help 查看用法。", chat_id=chat_id)

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

        match = service.pick_target(candidates, expr.season, expr.episode, media_type, year=expr.year)
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

        try:
            task = service.create_task(
                {
                    "file_path": file_path,
                    "item_type": match.get("item_type"),
                    "item_id": match.get("item_id"),
                    "storage": context.get("storage"),
                    "title": expr.title or str(context.get("title") or ""),
                    "media_type": media_type,
                    "season_number": match.get("season_number"),
                    "episode_number": match.get("episode_number"),
                }
            )
        except Exception as exc:
            self.send_text(f"创建上传任务失败：{exc}", reply_to=reply_to, chat_id=chat_id)
            return

        self.send_text(
            "✅ 已按修正目标重新上传\n"
            f"文件：{os.path.basename(file_path)}\n"
            f"目标：{match.get('label') or expr.describe()}\n"
            f"任务：{task.get('id')}",
            reply_to=reply_to,
            chat_id=chat_id,
        )
        logger.info("Telegram 修正目标成功: %s -> %s", expr.describe(), match.get("label"))

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
