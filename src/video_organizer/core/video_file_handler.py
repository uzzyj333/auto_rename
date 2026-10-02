import os
import shutil
import threading
import logging
import time
from datetime import datetime
from queue import Queue, Empty
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# 导入项目内部的上传工具
from ..upload.upload_emos import RobustEmosVideoUploader

from .emos_client import EmosClient
from .probe import probe_summary_for_upload, probe_video
from .source_cleanup import cleanup_uploaded_source, delete_file_with_retry
from .renamer import VideoRenamer
from .tmdb_client import TMDBClient
from .subtitle_handler import SubtitleHandler
from .downloader_monitor import decode_file_path
from ..utils.logging_utils import get_logger, log_success, log_failure, log_exception
from ..database.operations import record_task
from ..database.session import init_db as init_task_db


# 获取模块级别的 logger
_logger = logging.getLogger(__name__)


def console_log(message: str):
    """
    统一的输出函数 - 同时输出到控制台和日志文件
    
    替代直接 print() 调用，确保日志被记录到文件
    """
    # 输出到控制台
    print(message)
    
    # 写入日志文件（移除 ANSI 颜色代码）
    import re
    clean_message = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', message)
    _logger.info(clean_message)


class VideoFileHandler:
    """
    视频文件处理器，用于处理文件系统事件
    """

    def __init__(
        self,
        output_dir: str,
        supported_extensions: List[str],
        naming_rules: Optional[Dict[str, str]] = None,
        tmdb_config: Optional[Dict[str, Any]] = None,
        emos_config: Optional[Dict[str, Any]] = None,
        processing_config: Optional[Dict[str, Any]] = None,
        path_mappings: Optional[Dict[str, str]] = None,
        telegram_config: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        """
        初始化视频文件处理器

        Args:
            output_dir: 输出目录
            supported_extensions: 支持的文件扩展名列表
            naming_rules: 命名规则字典
            tmdb_config: TMDB 配置字典
            emos_config: Emos 配置字典
            processing_config: 处理配置字典
            path_mappings: 路径映射字典 (下载器路径 -> 本地路径)
            telegram_config: Telegram 通知配置
            config: 完整配置字典（用于在线热更新）
        """
        # 初始化日志记录器
        self.logger = get_logger(__name__)

        self.output_dir = output_dir
        self.supported_extensions = supported_extensions
        self.path_mappings = path_mappings or {}

        # 运行时配置（在线修改后由 apply_config 热更新）
        self.config: Dict[str, Any] = dict(config or {})
        self.naming_rules = naming_rules or self.config.get("naming_rules") or {}
        self.tmdb_config = tmdb_config or self.config.get("tmdb") or {}
        self.processing_config = processing_config or self.config.get("processing") or {}
        self.telegram_config = telegram_config or self.config.get("telegram") or {}
        self.emos_config = emos_config or self.config.get("emos") or {}

        # 上传目标固定为 Emos 官方 API
        self.upload_targets = ["emos"]

        # 上传队列工作线程管理（支持在线调整并发数，无需重启容器）
        self._worker_lock = threading.Lock()
        self._upload_workers: List[Any] = []  # [(thread, stop_event), ...]
        self._worker_seq = 0
        self._queue_running = False

        self._apply_processing_config()
        self._apply_emos_config()
        self._build_emos_client()

        # 初始化文件重命名器
        try:
            self.renamer = VideoRenamer(
                tmdb_api_key=self.tmdb_config.get("api_key") or None,
                naming_rules=self.naming_rules,
                config=self.config,
            )
            self.logger.info("视频重命名器初始化成功")
        except Exception as e:
            log_exception(self.logger, "初始化视频重命名器失败")
            self.renamer = VideoRenamer(tmdb_api_key=None)

        # 初始化字幕处理器
        try:
            self.subtitle_handler = SubtitleHandler()
            self.logger.info("字幕处理器初始化成功")
        except Exception as e:
            log_exception(self.logger, "初始化字幕处理器失败")
            self.subtitle_handler = None

        # 父监控器引用
        self._parent_monitor = None

        # 初始化任务历史数据库（失败不阻塞）
        try:
            init_task_db()
        except Exception:
            self.logger.warning("初始化任务历史数据库失败（部分功能可能受限）")

        # 处理中的文件，用于跟踪文件写入完成状态
        self._processing_files = set()

        # 上传状态跟踪，用于防止重复上传
        self._uploading_files = set()  # 正在上传的文件
        self._uploaded_files = set()  # 已成功上传的文件
        self._failed_files = {}  # 失败的文件及原因
        self._max_set_size = 1000  # 限制集合大小，防止内存溢出

        # 队列去重机制
        self._queued_files = set()  # 追踪队列中的文件，防止重复添加
        self._queue_lock = threading.Lock()  # 队列操作锁，防止竞态条件
        self._file_downloader_map = {}  # 文件到下载器的映射，用于删除下载任务

        # 上传队列配置
        self._upload_queue = Queue()  # 上传队列
        self._use_queue = True  # 是否使用队列（可配置）

        # 注册的下载器列表，用于清理任务
        self.downloaders = []

        # 启动上传队列处理线程
        self._start_upload_queue()

    # ============================================================
    # 配置热更新（在线修改后立即生效，无需重启容器）
    # ============================================================

    @staticmethod
    def _clean_value(value: Any, default: str = "") -> str:
        """清理配置值中的行内注释与空白"""
        if value is None:
            return default
        text = str(value).split("#")[0].split(";")[0].strip()
        return text or default

    def _apply_processing_config(self) -> None:
        """应用处理配置"""
        processing = self.processing_config or {}
        self.delete_after_upload = bool(processing.get("delete_after_upload", False))
        try:
            self.max_upload_workers = max(1, int(processing.get("max_upload_workers", 3)))
        except (TypeError, ValueError):
            self.max_upload_workers = 3
        # 在线识别上传：ffprobe 校验配置（热更新时同步生效）
        online = self.config.get("online_upload") or {}
        self.probe_enabled = bool(online.get("probe_enabled", True))
        self.ffprobe_path = self._clean_value(online.get("ffprobe_path", ""))

        # 在线修改「同时上传数量」后立即调整工作线程（调大补线程 / 调小回收线程）
        if self._queue_running:
            self._sync_upload_workers()

    def _apply_emos_config(self) -> None:
        """应用 Emos 配置"""
        emos = self.emos_config or {}
        self.emos_auth_token = self._clean_value(emos.get("auth_token", ""))
        self.emos_base_url = self._clean_value(emos.get("base_url", ""), "https://emos.best")
        self.emos_file_storage = self._clean_value(emos.get("file_storage", ""), "internal")
        try:
            self.emos_chunk_size_mb = int(emos.get("chunk_size_mb", 50))
        except (TypeError, ValueError):
            self.emos_chunk_size_mb = 50

    def _build_emos_client(self) -> None:
        """构建 Emos 官方 API 客户端（用于在线识别）"""
        self.emos_client = None
        if not self.emos_auth_token:
            return
        try:
            self.emos_client = EmosClient(
                base_url=self.emos_base_url,
                auth_token=self.emos_auth_token,
            )
        except Exception as e:
            self.logger.error(f"初始化 Emos 客户端失败: {e}")

    def get_emos_client(self) -> Optional[EmosClient]:
        """获取 Emos 客户端（不存在时按当前配置重建）"""
        if self.emos_client is None:
            self._build_emos_client()
        return self.emos_client

    def apply_config(self, config: Optional[Dict[str, Any]]) -> None:
        """在线修改配置后立即生效（无需重启容器）"""
        if not config:
            return
        with self._queue_lock:
            self.config = dict(config)
            self.naming_rules = config.get("naming_rules") or self.naming_rules
            self.tmdb_config = config.get("tmdb") or self.tmdb_config
            self.processing_config = config.get("processing") or {}
            self.telegram_config = config.get("telegram") or {}
            self.emos_config = config.get("emos") or {}
            monitoring = config.get("monitoring") or {}
            if monitoring.get("path_mappings"):
                self.path_mappings = monitoring["path_mappings"]

            self._apply_processing_config()
            self._apply_emos_config()
            self._build_emos_client()

            # 同步 TMDB / 命名规则到重命名器
            try:
                api_key = self.tmdb_config.get("api_key") or ""
                tmdb_client = getattr(self.renamer, "tmdb_client", None)
                if api_key:
                    if tmdb_client is None:
                        self.renamer.tmdb_client = TMDBClient(api_key)
                    else:
                        tmdb_client.api_key = api_key
                if self.naming_rules and hasattr(self.renamer, "set_naming_rules"):
                    self.renamer.set_naming_rules(self.naming_rules)
            except Exception as e:
                self.logger.warning(f"热更新重命名器配置失败: {e}")

        # 日志级别热更新
        try:
            logging_config = config.get("logging")
            if logging_config:
                from ..utils.logging_utils import setup_logging

                setup_logging(logging_config)
        except Exception as e:
            self.logger.warning(f"热更新日志配置失败: {e}")

        # 同步到在线识别上传服务
        try:
            from .online_upload import OnlineUploadService

            OnlineUploadService.instance().configure(config)
        except Exception:
            pass

        self.logger.info("配置已在线热更新（无需重启容器）")

    @staticmethod
    def _pick_emos_match(
        payload: Dict[str, Any],
        media_type: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """从 Emos getVideoId 返回结果中挑选最合适的上传目标

        电视剧必须定位到具体某一集（ve）：季/集号对不上或接口未返回 episode_info 时返回 None，
        绝不退回整部剧（vl）/整季（vs），否则上传接口会返回 404。
        """
        if not isinstance(payload, dict):
            return None

        # 只要不是电影且能确定集号，就按电视剧处理，必须定位到具体某一集
        is_tv = payload.get("video_type") == "tv" or (
            media_type != "movie" and episode is not None
        )

        episode_info = payload.get("episode_info") or {}
        if isinstance(episode_info, dict) and episode_info.get("item_id"):
            if is_tv:
                try:
                    got_episode = int(episode_info.get("episode_number"))
                except (TypeError, ValueError):
                    got_episode = None
                try:
                    got_season = int(episode_info.get("season_number"))
                except (TypeError, ValueError):
                    got_season = None
                if episode is None or got_episode is None or got_episode != episode:
                    return None
                if season is not None and got_season is not None and got_season != season:
                    return None
                if str(episode_info.get("item_type") or "ve") != "ve":
                    return None
            return {
                "item_type": episode_info.get("item_type") or "ve",
                "item_id": str(episode_info.get("item_id")),
                "label": episode_info.get("episode_title") or payload.get("title") or "",
                "kind": "episode",
                "season_number": episode_info.get("season_number"),
                "episode_number": episode_info.get("episode_number"),
            }

        if media_type == "movie" or payload.get("video_type") == "movie":
            if payload.get("item_id"):
                return {
                    "item_type": payload.get("item_type") or "vl",
                    "item_id": str(payload.get("item_id")),
                    "label": payload.get("title") or payload.get("video_list_name") or "",
                    "kind": "movie",
                }

        # 电视剧找不到具体某一集时不再退回整季/整剧，避免上传到错误目标
        if is_tv:
            return None

        season_info = payload.get("season_info") or {}
        if isinstance(season_info, dict) and season_info.get("item_id"):
            return {
                "item_type": season_info.get("item_type") or "vs",
                "item_id": str(season_info.get("item_id")),
                "label": season_info.get("season_title") or payload.get("title") or "",
                "kind": "season",
                "season_number": season_info.get("season_number"),
            }

        if payload.get("item_id"):
            return {
                "item_type": payload.get("item_type") or "vl",
                "item_id": str(payload.get("item_id")),
                "label": payload.get("title") or "",
                "kind": "video",
            }
        return None

    def _notify_match_error(self, file_path, title, media_type, season, episode, reason):
        """识别不到 Emos 上传目标时推送 Telegram 报错，便于用户回复修正"""
        try:
            from .telegram_bot import TelegramBotService

            TelegramBotService.instance().notify_error(
                {
                    "file_path": str(file_path),
                    "file_name": os.path.basename(str(file_path)),
                    "title": title,
                    "media_type": media_type,
                    "season_number": season,
                    "episode_number": episode,
                    "storage": getattr(self, "emos_file_storage", None),
                },
                reason,
                header="未找到 Emos 上传目标",
            )
        except Exception as exc:
            self.logger.debug("推送 Telegram 报错信息失败: %s", exc)

    def add_downloader(self, downloader):
        """
        添加下载器实例

        Args:
            downloader: 下载器监控实例
        """
        if downloader not in self.downloaders:
            self.downloaders.append(downloader)

    def on_created(self, event):
        """
        当文件创建时被调用

        Args:
            event: 文件系统事件
        """
        if event.is_directory:
            return

        file_path = event.src_path
        if self._is_supported_file(file_path):
            # 检查是否是处理中的文件（避免重复处理）
            if file_path in self._processing_files:
                self.logger.debug(f"文件已在处理队列中: {file_path}")
                return

            self._processing_files.add(file_path)
            try:
                # 检查文件是否完整写入
                if self._is_file_complete(file_path):
                    self._process_file(file_path)
                else:
                    # 如果文件未完整写入，设置一个延迟处理
                    self.logger.debug(f"文件尚未完成写入，稍后处理: {file_path}")
                    # 在监控器的下一个轮询周期处理
                    if self._parent_monitor:
                        self._parent_monitor._pending_files.add(file_path)
            finally:
                # 无论处理结果如何，从处理队列中移除
                self._processing_files.discard(file_path)

    def on_modified(self, event):
        """
        当文件修改时被调用

        Args:
            event: 文件系统事件
        """
        if event.is_directory:
            return

        file_path = event.src_path
        if self._is_supported_file(file_path):
            # 检查文件是否已经在上传中或已上传，避免重复处理
            if file_path in self._uploading_files or file_path in self._uploaded_files:
                self.logger.debug(
                    f"文件已在上传中或已上传，跳过修改事件处理: {file_path}"
                )
                return

            # 对于修改事件，检查文件是否已完成写入
            if not file_path in self._processing_files and self._is_file_complete(
                file_path
            ):
                self._process_file(file_path)

    def _is_supported_file(self, file_path: str) -> bool:
        """
        检查文件是否为支持的视频或字幕文件

        Args:
            file_path: 文件路径

        Returns:
            是否为支持的文件
        """
        try:
            file_ext = os.path.splitext(file_path)[1].lower()
            return file_ext in self.supported_extensions
        except Exception as e:
            self.logger.error(f"检查文件类型时出错: {file_path}, 错误: {e}")
            return False

    def _is_subtitle_file(self, file_path: str) -> bool:
        """
        检查文件是否为字幕文件

        Args:
            file_path: 文件路径

        Returns:
            是否为字幕文件
        """
        if not self.subtitle_handler:
            return False
        return self.subtitle_handler.is_subtitle_file(Path(file_path))

    def _is_video_file(self, file_path: str) -> bool:
        """
        检查文件是否为视频文件

        Args:
            file_path: 文件路径

        Returns:
            是否为视频文件
        """
        subtitle_extensions = {'.srt', '.ass', '.ssa', '.sub', '.vtt'}
        try:
            file_ext = os.path.splitext(file_path)[1].lower()
            return file_ext in self.supported_extensions and file_ext not in subtitle_extensions
        except Exception as e:
            self.logger.error(f"检查文件类型时出错: {file_path}, 错误: {e}")
            return False

    def _is_file_complete(self, file_path: str) -> bool:
        """
        检查文件是否已完成写入

        Args:
            file_path: 文件路径

        Returns:
            文件是否完整
        """
        try:
            # 检查文件是否存在
            if not os.path.exists(file_path):
                return False

            # 尝试以只读方式打开文件，检查是否被锁定
            try:
                with open(file_path, "rb") as f:
                    # 读取文件的最后1KB，测试是否能正常访问
                    f.seek(0, 2)  # 移动到文件末尾
                    file_size = f.tell()

                    # 检查文件大小是否为0
                    if file_size == 0:
                        return False

                    # 等待一小段时间，检查文件大小是否变化
                    import time

                    time.sleep(0.5)  # 等待500ms，增加等待时间提高准确性

                    # 再次检查文件大小
                    current_size = os.path.getsize(file_path)

                    # 如果文件大小没有变化，认为文件已完成写入
                    return file_size == current_size
            except PermissionError:
                # 文件被锁定（可能正在下载），返回False
                self.logger.debug(f"文件被锁定，可能正在下载: {file_path}")
                return False
        except Exception as e:
            self.logger.error(f"检查文件完整性时出错: {file_path}, 错误: {e}")
            return False

    def _start_upload_queue(self):
        """启动上传队列处理线程（按当前配置的工作线程数）"""
        self._queue_running = True
        self._sync_upload_workers()

    def _sync_upload_workers(self) -> None:
        """
        使实际工作线程数与 max_upload_workers 保持一致。

        在线修改「同时上传数量」后立即生效：
        - 调大并发：补足缺少的工作线程
        - 调小并发：通知多余的工作线程在完成当前任务后退出
        """
        with self._worker_lock:
            # 回收已经退出的线程
            self._upload_workers = [
                (thread, stop_event)
                for thread, stop_event in self._upload_workers
                if thread.is_alive()
            ]

            try:
                target = max(1, int(self.max_upload_workers))
            except (TypeError, ValueError):
                target = 3
            self.max_upload_workers = target

            current = len(self._upload_workers)
            if current < target:
                for _ in range(target - current):
                    self._worker_seq += 1
                    stop_event = threading.Event()
                    thread = threading.Thread(
                        target=self._worker_process_queue,
                        args=(self._worker_seq, stop_event),
                        daemon=True,
                    )
                    thread.start()
                    self._upload_workers.append((thread, stop_event))
                self.logger.info(
                    f"上传工作线程已调整: {current} -> {len(self._upload_workers)}（并发上传数 {target}）"
                )
            elif current > target:
                for _ in range(current - target):
                    _, stop_event = self._upload_workers.pop()
                    stop_event.set()
                self.logger.info(
                    f"上传工作线程已调整: {current} -> {len(self._upload_workers)}（多余线程将在当前任务完成后退出）"
                )

    def _worker_process_queue(
        self, worker_id, stop_event: Optional[threading.Event] = None
    ):
        """
        上传队列消费者工作函数
        Args:
            worker_id: 工作线程ID
            stop_event: 通知该线程退出的信号（在线调小并发数时使用）
        """
        self.logger.debug(f"上传工作线程 #{worker_id} 进入主循环")
        while self._queue_running:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                # 从队列中获取文件路径，超时1秒
                file_path = self._upload_queue.get(timeout=1)
                if file_path is None:  # 退出信号
                    self._upload_queue.task_done()
                    self.logger.debug(f"上传工作线程 #{worker_id} 收到退出信号")
                    break

                self.logger.debug(f"上传工作线程 #{worker_id} 获取到任务: {file_path}")

                # 显示队列状态
                queue_size = self._upload_queue.qsize()
                console_log(f"\n{'='*80}")
                console_log(f"📋 工作线程 #{worker_id} 开始处理任务")
                console_log(f"当前任务: {os.path.basename(file_path)}")
                console_log(f"剩余任务: {queue_size}")
                console_log(f"{'='*80}")

                try:
                    self._process_file_internal(file_path, worker_id)
                except Exception as e:
                    self.logger.error(
                        f"工作线程 #{worker_id} 处理文件失败: {file_path}, 错误: {e}"
                    )
                finally:
                    # 清理状态（不持有锁，避免阻塞其他线程）
                    self._queued_files.discard(file_path)
                    self._processing_files.discard(file_path)

                    self._upload_queue.task_done()
                    # 显式垃圾回收
                    import gc

                    gc.collect()

            except Empty:
                continue
            except Exception as e:
                self.logger.error(f"工作线程 #{worker_id} 发生未捕获异常: {e}")

    def _process_file(self, file_path: str) -> bool:
        """
        处理视频或字幕文件

        Args:
            file_path: 文件路径
        """
        if not os.path.exists(file_path):
            self.logger.warning(f"文件不存在: {file_path}")
            return False

        # 检查文件类型
        is_subtitle = self._is_subtitle_file(file_path)
        is_video = self._is_video_file(file_path)

        if not is_subtitle and not is_video:
            self.logger.debug(f"文件不是支持的视频或字幕文件，跳过: {file_path}")
            return False

        # 对于字幕文件，使用特殊的处理逻辑
        if is_subtitle:
            return self._process_subtitle_file(file_path)

        # 快速检查：文件是否已经在上传中或已上传（不加锁，因为这些集合只在主线程修改）
        if file_path in self._uploading_files or file_path in self._uploaded_files:
            self.logger.debug(f"文件已在上传中或已上传，跳过处理: {file_path}")
            return True

        # 使用锁保护去重检查，防止竞态条件
        with self._queue_lock:
            # 检查文件是否正在处理中（API调用阶段）
            if file_path in self._processing_files:
                self.logger.debug(f"文件正在处理中，跳过: {file_path}")
                return True

            # 检查文件是否已在队列中
            if file_path in self._queued_files:
                self.logger.debug(f"文件已在队列中，跳过重复添加: {file_path}")
                return True

            # 立即标记为处理中并添加到队列追踪集合（在锁内完成）
            self._processing_files.add(file_path)
            self._queued_files.add(file_path)

        # 检查文件是否完整且可访问（在锁外进行，避免阻塞其他线程）
        if not self._is_file_complete(file_path):
            self.logger.debug(f"文件未完成或被锁定，跳过处理: {file_path}")
            # 清理状态
            with self._queue_lock:
                self._processing_files.discard(file_path)
                self._queued_files.discard(file_path)
            # 如果有父监控器，可以将文件添加到重试队列
            if self._parent_monitor:
                self._parent_monitor._pending_files.add(file_path)
            return False

        if self._use_queue:
            # 放入队列异步处理
            self._upload_queue.put(file_path)

            queue_size = self._upload_queue.qsize()
            console_log(f"\n✅ 已加入处理队列: {os.path.basename(file_path)}")
            console_log(f"   当前队列长度: {queue_size}")
            console_log(f"   工作线程数: {self.max_upload_workers}")

            return True
        else:
            # 同步直接处理
            return self._process_file_internal(file_path)

    def _process_file_internal(self, file_path, worker_id=0):
        """
        内部文件处理逻辑（包含元数据获取、API调用、上传）
        """
        console_log(f"\n🔍 [线程#{worker_id}] 开始深入处理文件: {file_path}")

        try:
            # 第一步：获取视频的tmdbid和media_type (使用本地 Renamer + TMDB Client)
            print(f"正在本地分析文件元数据: {os.path.basename(file_path)}")

            # 使用 VideoRenamer 提取元数据 (包含 Regex 解析和 TMDB 搜索)
            # 注意: 这里使用 extract_metadata 会自动调用 _enrich_with_tmdb
            metadata = self.renamer.extract_metadata(file_path)

            # 打印综合识别结果（类似 --comprehensive 模式）
            console_log(f"✓ [线程#{worker_id}] 本地识别完成")
            important_fields = [
                "show_name",
                "title",
                "tmdb_id",
                "media_type",
                "season",
                "episode",
                "year",
                "quality_tags",
                "release_group",
            ]
            for field in important_fields:
                value = metadata.get(field)
                if value:
                    print(f"  [{worker_id}] {field}: {value}")

            # 提取所需信息
            tmdb_id = str(metadata.get("tmdb_id", ""))

            # 检查是否成功获取到 TMDB ID
            if not tmdb_id or tmdb_id == "":
                console_log(f"\n❌ [线程#{worker_id}] 未找到 TMDB 匹配结果")
                tmdb_request_failed = bool(
                    self.renamer.tmdb_client
                    and getattr(self.renamer.tmdb_client, "last_request_failed", False)
                )
                if tmdb_request_failed:
                    error_msg = getattr(self.renamer.tmdb_client, "last_request_error", None)
                    reason = f"TMDB请求失败: {error_msg}" if error_msg else "TMDB请求失败"
                    console_log(f"⚠️  TMDB 请求异常，文件将加入自动重试: {reason}")
                    self._failed_files[file_path] = reason
                    record_task(file_path, "failed", error_message=reason, end_time=datetime.now())
                    if self._parent_monitor:
                        self._parent_monitor._retry_files.add(file_path)
                else:
                    console_log(f"⚠️  建议：请手动处理该文件或确认文件名是否正确")
                    console_log(f"⚠️  文件将跳过上传，等待手动处理\n")
                    # 记录失败原因，但不标记为已上传（以便后续可以重试）
                    self._failed_files[file_path] = "未找到 TMDB 匹配结果"
                    record_task(file_path, "failed", error_message="未找到 TMDB 匹配结果", end_time=datetime.now())
                self._uploading_files.discard(file_path)
                return False
            media_type = metadata.get(
                "media_type", "tv"
            )  # 默认为 tv, renamer 会返回 'tv' 或 'movie'

            # 标题处理：优先使用 title (电影) 或 show_name (剧集)
            title = (
                metadata.get("title")
                or metadata.get("show_name")
                or metadata.get("original_filename", "")
            )

            # 季集信息处理
            season = metadata.get("season")
            episode = metadata.get("episode")
            season_episode = ""

            # 添加特别篇检测逻辑，与renamer.py中的generate_new_path方法保持一致
            if season is None or episode is None:
                # 检查文件名是否包含特别篇标识
                filename = os.path.basename(file_path)
                special_keywords = [
                    "OVA",
                    "SP",
                    "Special",
                    "特别篇",
                    "番外篇",
                    "OVA01",
                    "OVA02",
                    "OVA03",
                    "OVA04",
                    "OVA05",
                    "OVA06",
                    "OVA07",
                    "OVA08",
                    "OVA09",
                    "OVA10",
                ]
                filename_upper = filename.upper()
                for keyword in special_keywords:
                    if keyword in filename_upper:
                        # 如果是特别篇，设置季数为0，集数从文件名提取
                        season = 0
                        # 尝试从文件名提取集数
                        import re

                        episode_match = re.search(r"(?:OVA|SP)(\d+)", filename_upper)
                        if episode_match:
                            episode = episode_match.group(1)
                        break
                if season is None or episode is None:
                    season = 1
                    episode = 1

            if season is not None and episode is not None:
                try:
                    # 尝试格式化为 SxxExx
                    s_num = int(season)
                    e_num = int(episode)
                    season_episode = f"S{s_num:02d}E{e_num:02d}"
                except:
                    # 如果转换整数失败，直接拼接
                    season_episode = f"S{season}E{episode}"
            else:
                # 如果季集信息缺失，保持为空字符串
                season_episode = ""

            # 输出获取到的信息
            console_log(f"\n[线程#{worker_id}] 文件信息 (本地识别):")
            print(f"  文件: {os.path.basename(file_path)}")
            print(f"  TMDB ID: {tmdb_id}")
            print(f"  媒体类型: {media_type}")
            print(f"  标题: {title}")
            print(f"  季集: {season_episode}")

            # 兼容性处理: 原有逻辑可能依赖 "电视剧" 这样的中文类型，但 Renamer 返回 "tv"/"movie"
            # 下面的逻辑原本是: media_type = "tv" if media_type == "电视剧" else "movie"
            # 现在 renamer 直接返回标准代码，所以我们只需确保它是 tv 或 movie
            if media_type not in ["tv", "movie"]:
                # 如果是 anime 或其他，归类为 tv
                media_type = "tv"

            # 初始化匹配结果
            matched_item_id = None
            matched_item_type = None
            match_error = ""

            # 第二步：通过官方 API 在线识别（TMDB ID -> Emos item_type/item_id）
            if tmdb_id and media_type and title:
                try:
                    season_num = int(season) if season else None
                except (ValueError, TypeError):
                    season_num = None
                try:
                    episode_num = int(episode) if episode else None
                except (ValueError, TypeError):
                    episode_num = None

                emos_client = self.get_emos_client()
                if emos_client is None:
                    console_log(f"✗ [线程#{worker_id}] 未配置 Emos auth_token，无法识别上传目标")
                else:
                    try:
                        result2 = emos_client.get_video_id(
                            tmdb_id,
                            "tmdb",
                            tmdb_type="movie" if media_type == "movie" else "tv",
                            season_number=season_num if media_type == "tv" else None,
                            episode_number=episode_num if media_type == "tv" else None,
                        )
                        print(f"[线程#{worker_id}] Emos 识别返回: {result2}")
                        match = self._pick_emos_match(result2, media_type, season_num, episode_num)
                        if not match and media_type == "tv":
                            # getVideoId 没给到具体某一集时，用该剧目录树兜底定位
                            from .online_upload import OnlineUploadService

                            match = OnlineUploadService.resolve_episode_from_tree(
                                emos_client, result2.get("item_id"), season_num, episode_num
                            )
                        if match:
                            matched_item_id = match["item_id"]
                            matched_item_type = match["item_type"]
                            console_log(
                                f"✓ [线程#{worker_id}] 在线识别成功: "
                                f"{matched_item_type}/{matched_item_id} {match.get('label') or ''}"
                            )
                        elif media_type == "tv":
                            if episode_num is None:
                                match_error = (
                                    f"未能从文件名解析出「{title}」的季/集号，无法定位到具体某一集，"
                                    "请在 Telegram 回复本条报错修正目标"
                                )
                            else:
                                match_error = (
                                    f"Emos 中没有「{title}」"
                                    f"S{season_num if season_num is not None else '?'}E{episode_num} 这一集，"
                                    "请先在 Emos 建集，或在 Telegram 回复本条报错修正目标"
                                )
                            console_log(f"✗ [线程#{worker_id}] {match_error}")
                            self._notify_match_error(
                                file_path, title, media_type, season_num, episode_num, match_error
                            )
                    except Exception as e:
                        match_error = f"Emos 在线识别失败: {e}"
                        console_log(f"✗ [线程#{worker_id}] {match_error}")
                        self._notify_match_error(
                            file_path, title, media_type, season_num, episode_num, match_error
                        )

            # 步骤4：决定是否需要上传
            if matched_item_id:
                self._execute_upload(
                    file_path,
                    matched_item_type,
                    matched_item_id,
                    worker_id,
                    tmdb_id,
                    media_type,
                    title,
                    season_episode,
                    metadata,
                )
            else:
                # 未识别到 Emos 条目：可在「在线识别上传」页面手动选择目标后上传
                reason = match_error or "未找到匹配的 Emos 条目（item_id），可在「在线识别上传」中手动选择目标"
                self._failed_files[file_path] = reason
                record_task(file_path, "failed", error_message=reason, end_time=datetime.now())
                log_success(
                    self.logger,
                    "文件元数据获取成功但未匹配到item_id",
                    {
                        "original_path": file_path,
                        "tmdb_id": tmdb_id,
                        "media_type": media_type,
                        "title": title,
                        "season_episode": season_episode,
                    },
                )

            return True

        except KeyboardInterrupt:
            # 让键盘中断正常传播
            raise
        except Exception as e:
            log_exception(self.logger, f"获取元数据时发生错误: {file_path}")
            console_log(f"\n✗ API请求失败: {e}")

            # 记录失败原因
            self._failed_files[file_path] = f"API请求失败: {str(e)}"
            record_task(file_path, "failed", error_message=f"API请求失败: {str(e)}", end_time=datetime.now())

            # 如果有父监控器，可以将文件添加到重试队列
            if self._parent_monitor:
                self.logger.info(f"将文件添加到重试队列: {file_path}")
                self._parent_monitor._retry_files.add(file_path)
        finally:
            # 从处理中集合移除
            self._processing_files.discard(file_path)

    def _execute_upload(
        self,
        file_path,
        matched_item_type,
        matched_item_id,
        worker_id,
        tmdb_id,
        media_type,
        title,
        season_episode,
        metadata,
    ):
        """执行上传到 Emos（官方 API）"""
        console_log(f"\n=== [线程#{worker_id}] 开始上传视频 ===")

        # 检查文件是否已经上传完成
        if file_path in self._uploaded_files:
            console_log(f"✗ [线程#{worker_id}] 文件已上传完成，跳过: {file_path}")
            return

        # 添加到上传中集合
        self._uploading_files.add(file_path)

        try:
            console_log(f"📤 [线程#{worker_id}] 上传到 Emos")
            console_log(f"类型: {matched_item_type}")
            console_log(f"项目ID: {matched_item_id}")

            if not self.emos_auth_token:
                reason = "未配置 Emos auth_token，跳过上传"
                console_log(f"✗ [线程#{worker_id}] {reason}")
                self._failed_files[file_path] = reason
                record_task(file_path, "failed", error_message=reason, end_time=datetime.now())
                self._uploading_files.discard(file_path)
                return

            # 用 ffprobe 提取 file_metadata（未安装 ffprobe 时自动跳过，不影响上传）
            file_metadata = None
            if getattr(self, "probe_enabled", True):
                try:
                    probe_result = probe_video(file_path, ffprobe_path=getattr(self, "ffprobe_path", None))
                    if probe_result.get("valid"):
                        file_metadata = probe_summary_for_upload(probe_result) or None
                    elif probe_result.get("error"):
                        self.logger.warning(f"ffprobe 校验未通过（继续上传）: {probe_result['error']}")
                except Exception as e:
                    self.logger.warning(f"ffprobe 探测异常（继续上传）: {e}")

            uploader = RobustEmosVideoUploader(
                auth_token=self.emos_auth_token,
                base_url=self.emos_base_url,
                chunk_size_mb=int(self.emos_chunk_size_mb),
                telegram_config=self.telegram_config,
            )
            upload_result = uploader.upload_video(
                file_path,
                matched_item_type,
                str(matched_item_id),
                self.emos_file_storage,
                metadata=file_metadata,
            )

            if not upload_result:
                reason = "Emos 上传失败"
                console_log(f"\n❌ [线程#{worker_id}] {reason}!")
                self._uploading_files.discard(file_path)
                self._failed_files[file_path] = reason
                record_task(file_path, "failed", error_message=reason, end_time=datetime.now())
                if self._parent_monitor:
                    self._parent_monitor._retry_files.add(file_path)
                log_success(
                    self.logger,
                    "文件元数据获取成功但上传失败",
                    {
                        "original_path": file_path,
                        "tmdb_id": tmdb_id,
                        "media_type": media_type,
                        "title": title,
                        "season_episode": season_episode,
                        "matched_item_id": matched_item_id,
                        "upload_success": False,
                    },
                )
                return

            console_log(f"\n🎉 [线程#{worker_id}] Emos 上传成功!")
            self._uploaded_files.add(file_path)
            record_task(file_path, "completed", end_time=datetime.now())
            self._uploading_files.discard(file_path)

            # 如果配置了上传后删除文件，执行删除操作
            #   - 文件来自下载器：先删下载任务，任务删掉后文件仍在（如 aria2）再兜底删除
            #   - 下载器里还有未完成任务：暂不删除，避免破坏正在做种的种子
            #   - 文件与下载器无关（例如手动放进媒体库）：直接删除
            deleted = False
            if self.delete_after_upload:
                try:
                    outcome = cleanup_uploaded_source(
                        file_path,
                        downloader_cleanup=self._downloader_cleanup_state,
                    )
                    deleted = bool(outcome.get("deleted"))
                    console_log(f"🗑️ [线程#{worker_id}] {outcome.get('reason')}: {file_path}")
                    self.logger.info(
                        f"上传后处理原文件: {outcome.get('reason')} - {file_path}"
                    )
                except Exception as e:
                    console_log(f"❌ [线程#{worker_id}] 删除原文件失败: {e}")
                    self.logger.error(f"删除原文件失败: {file_path}, 错误: {e}")

            # 更新日志
            log_success(
                self.logger,
                "文件元数据获取并上传成功",
                {
                    "original_path": file_path,
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "title": title,
                    "season_episode": season_episode,
                    "matched_item_id": matched_item_id,
                    "upload_success": True,
                    "upload_targets": self.upload_targets,
                    "emos_file_id": upload_result.get("file_id"),
                    "emos_media_id": upload_result.get("media_id"),
                    "deleted_after_upload": deleted,
                },
            )

            # 清理旧记录防止内存泄露
            self._cleanup_old_records()
        except Exception as e:
            console_log(f"\n❌ [线程#{worker_id}] 视频上传错误: {e}")
            self._uploading_files.discard(file_path)
            # 记录失败原因，以便 Web UI 显示和重试
            self._failed_files[file_path] = f"上传异常: {str(e)}"
            record_task(file_path, "failed", error_message=f"上传异常: {str(e)}", end_time=datetime.now())
            # 加入重试队列，自动重试
            if self._parent_monitor:
                self._parent_monitor._retry_files.add(file_path)
            log_success(
                self.logger,
                "文件元数据获取成功但上传出错",
                {
                    "original_path": file_path,
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "title": title,
                    "season_episode": season_episode,
                    "matched_item_id": matched_item_id,
                    "upload_success": False,
                    "error": str(e),
                },
            )

    def force_process_file(self, file_path: str) -> bool:
        """
        强制处理文件

        Args:
            file_path: 文件路径

        Returns:
            是否处理成功
        """
        try:
            if not os.path.exists(file_path):
                log_failure(self.logger, f"文件不存在: {file_path}")
                return False
        except Exception as e:
            log_failure(self.logger, f"检查文件是否存在时出错: {file_path}", error=e)
            return False

        print(f"检测到文件: {file_path}")

        # 即使文件已上传，强制模式可能希望重试，所以我们尝试从已上传集合中移除它
        if file_path in self._uploaded_files:
            self._uploaded_files.discard(file_path)
            self.logger.info(f"强制处理: 从已上传集合中移除 {file_path}")

        if file_path in self._failed_files:
            del self._failed_files[file_path]
            self.logger.info(f"强制处理: 从失败集合中移除 {file_path}")

        # 使用锁保护去重检查
        with self._queue_lock:
            # 检查文件是否已在队列中，避免重复添加
            if file_path in self._queued_files:
                self.logger.info(f"强制处理: 文件已在队列中，跳过重复添加: {file_path}")
                console_log(f"\n⚠️  文件已在队列中，跳过: {os.path.basename(file_path)}")
                return True

            # 标记为处理中并添加到队列追踪集合
            self._processing_files.add(file_path)
            self._queued_files.add(file_path)

        # 检查文件是否不支持
        if not self._is_supported_file(file_path):
            # 清理状态
            with self._queue_lock:
                self._processing_files.discard(file_path)
                self._queued_files.discard(file_path)
            # log_failure(self.logger, f"不支持的文件类型: {file_path}")
            # return False
            pass

        if self._use_queue:
            # 放入队列异步处理
            self._upload_queue.put(file_path)

            queue_size = self._upload_queue.qsize()
            console_log(f"\n✅ 已加入处理队列: {os.path.basename(file_path)}")
            console_log(f"   当前队列长度: {queue_size}")
            console_log(f"   工作线程数: {self.max_upload_workers}")

            return True
        else:
            # 同步直接处理
            return self._process_file_internal(file_path)

    def stop_upload_queue(self):
        """停止上传队列处理线程"""
        self._queue_running = False
        with self._worker_lock:
            workers = list(self._upload_workers)
            self._upload_workers = []
        for _, stop_event in workers:
            stop_event.set()
        # 兼容旧逻辑：发送 None 退出信号，唤醒阻塞在队列上的线程
        for _ in workers:
            self._upload_queue.put(None)
        for thread, _ in workers:
            if thread.is_alive():
                thread.join(timeout=5)
        if workers:
            self.logger.info("上传队列处理线程已停止")

    def get_upload_worker_count(self) -> int:
        """当前存活的上传工作线程数"""
        with self._worker_lock:
            self._upload_workers = [
                (thread, stop_event)
                for thread, stop_event in self._upload_workers
                if thread.is_alive()
            ]
            return len(self._upload_workers)

    def _reverse_apply_path_mapping(self, file_path: str) -> str:
        """
        反向应用路径映射：将本地文件路径转换为下载器路径

        Args:
            file_path: 本地文件路径

        Returns:
            下载器使用的文件路径
        """
        if not self.path_mappings:
            print("DEBUG: path_mappings 为空，跳过反向映射")
            return file_path

        # 规范化路径分隔符
        file_path = file_path.replace("\\", "/")

        print(f"DEBUG: 尝试反向映射路径: {file_path}")
        print(f"DEBUG: 当前映射配置: {self.path_mappings}")

        for downloader_path, local_path in self.path_mappings.items():
            # 规范化本地映射路径
            local_path = local_path.replace("\\", "/")

            # 如果文件路径以本地映射路径开头
            if file_path.startswith(local_path):
                # 替换为下载器路径
                rel_path = file_path[len(local_path) :].lstrip("/")
                # 拼接下载器路径 (注意 downloader_path 结尾可能有也可能没有 /)
                new_path = f"{downloader_path.rstrip('/')}/{rel_path}"
                self.logger.debug(f"反向路径映射: {file_path} -> {new_path}")
                print(f"DEBUG: 映射成功: {new_path}")
                return new_path

        print(f"DEBUG: 未找到匹配的映射路径")
        return file_path

    def _is_downloader_tracked(self, file_path: str) -> bool:
        """文件是否与某个下载任务建立了映射

        决定「上传后删除原文件」时是否走下载器逻辑：只有确实来自下载器的文件才需要
        先删任务，避免误判成「下载器里还有未完成任务」而永远不删。
        """
        if not self._file_downloader_map:
            return False
        try:
            downloader_file_path = self._reverse_apply_path_mapping(file_path)
        except Exception:
            downloader_file_path = file_path
        candidates = [
            file_path,
            decode_file_path(file_path),
            downloader_file_path,
            decode_file_path(downloader_file_path),
        ]
        return any(key and key in self._file_downloader_map for key in candidates)

    def _downloader_cleanup_state(self, file_path: str) -> Optional[bool]:
        """供 core.source_cleanup 使用的三态回调

        True  = 已从下载器删除任务
        False = 下载器里还有未完成的任务，暂不删除文件
        None  = 该文件与下载器无关，直接删除文件
        """
        if not self._is_downloader_tracked(file_path):
            return None
        return self._cleanup_download_task(file_path)

    def _cleanup_download_task(self, file_path) -> bool:
        """
        从下载器中删除对应的下载任务

        Args:
            file_path: 文件路径 (本地路径)

        Returns:
            bool: 是否成功从下载器中删除了任务
        """
        # 反向映射路径，因为下载器使用的是它自己的路径系统
        downloader_file_path = self._reverse_apply_path_mapping(file_path)
        
        # 解码路径用于查找 _file_downloader_map（因为 map 的 key 是解码形式）
        decoded_file_path = decode_file_path(file_path)
        decoded_downloader_path = decode_file_path(downloader_file_path)

        task_removed = False

        # 1. 尝试从映射中查找下载器（同时尝试编码和解码路径）
        map_key = None
        for key in [file_path, decoded_file_path, downloader_file_path, decoded_downloader_path]:
            if key in self._file_downloader_map:
                map_key = key
                break
        
        if map_key:
            try:
                downloader = self._file_downloader_map[map_key]
                if hasattr(downloader, "remove_download"):
                    # 尝试多种路径格式
                    paths_to_try = [
                        file_path,
                        decoded_file_path,
                        downloader_file_path,
                        decoded_downloader_path
                    ]
                    for path in paths_to_try:
                        if path and downloader.remove_download(path):
                            console_log(f"✅ 已从下载器中删除任务")
                            self.logger.info(f"已从下载器中删除任务: {path}")
                            task_removed = True
                            break
                # 清理映射
                del self._file_downloader_map[map_key]
            except Exception as e:
                self.logger.error(f"从映射的下载器删除任务失败: {e}")

        if task_removed:
            return True

        # 2. 如果映射中没有或删除失败，尝试遍历所有注册的下载器
        # 这在 --process 模式下很有用，因为那时文件可能没有被添加到映射中
        if self.downloaders:
            for downloader in self.downloaders:
                try:
                    if hasattr(downloader, "remove_download"):
                        # 尝试多种路径格式
                        paths_to_try = [
                            downloader_file_path,
                            decoded_downloader_path,
                            file_path,
                            decoded_file_path
                        ]
                        for path in paths_to_try:
                            if path and downloader.remove_download(path):
                                console_log(f"✅ 已从下载器中删除任务 (遍历查找)")
                                self.logger.info(
                                    f"已从下载器中删除任务 (遍历查找): {path}"
                                )
                                return True
                except Exception as e:
                    self.logger.warning(f"尝试从下载器删除任务时出错: {e}")

        console_log(f"⚠️ 未能从下载器删除任务 (未找到匹配任务): {downloader_file_path}")
        self.logger.debug(
            f"未能从下载器删除任务: {file_path} -> {downloader_file_path}"
        )
        return False

    def _force_cleanup_download_task(self, file_path: str) -> bool:
        """
        强制从下载器中删除任务及其文件 (用于文件删除失败时的清理)

        Args:
            file_path: 文件路径 (本地路径)

        Returns:
            bool: 是否成功从下载器中删除了任务
        """
        # 反向映射路径
        downloader_file_path = self._reverse_apply_path_mapping(file_path)
        
        # 解码路径用于查找
        decoded_file_path = decode_file_path(file_path)
        decoded_downloader_path = decode_file_path(downloader_file_path)

        # 1. 首先尝试从映射中查找下载器（同时尝试编码和解码路径）
        map_key = None
        for key in [file_path, decoded_file_path, downloader_file_path, decoded_downloader_path]:
            if key in self._file_downloader_map:
                map_key = key
                break
        
        if map_key:
            try:
                downloader = self._file_downloader_map[map_key]
                if hasattr(downloader, "force_remove_download"):
                    # 尝试多种路径格式
                    paths_to_try = [
                        file_path,
                        decoded_file_path,
                        downloader_file_path,
                        decoded_downloader_path
                    ]
                    for path in paths_to_try:
                        if path and downloader.force_remove_download(path):
                            console_log(f"✅ 已强制从下载器中删除任务及文件")
                            self.logger.info(f"已强制从下载器中删除任务及文件: {path}")
                            del self._file_downloader_map[map_key]
                            return True
            except Exception as e:
                self.logger.error(f"从映射的下载器强制删除失败: {e}")

        # 2. 尝试遍历所有注册的下载器
        if self.downloaders:
            for downloader in self.downloaders:
                try:
                    if hasattr(downloader, "force_remove_download"):
                        paths_to_try = [
                            downloader_file_path,
                            decoded_downloader_path,
                            file_path,
                            decoded_file_path
                        ]
                        for path in paths_to_try:
                            if path and downloader.force_remove_download(path):
                                console_log(f"✅ 已强制从下载器中删除任务及文件 (遍历查找)")
                                self.logger.info(
                                    f"已强制从下载器中删除任务及文件 (遍历查找): {path}"
                                )
                                return True
                except Exception as e:
                    self.logger.warning(f"尝试从下载器强制删除时出错: {e}")

        self.logger.debug(f"未能从下载器强制删除任务: {file_path}")
        return False

    def _process_subtitle_file(self, subtitle_path: str) -> bool:
        """
        处理字幕文件：查找匹配的视频文件并重命名整理

        Args:
            subtitle_path: 字幕文件路径

        Returns:
            是否处理成功
        """
        if not self.subtitle_handler:
            self.logger.warning("字幕处理器未初始化，跳过字幕文件处理")
            return False

        try:
            print(f"\n🎬 发现字幕文件: {os.path.basename(subtitle_path)}")

            # 解析字幕文件信息
            subtitle_info = self.subtitle_handler.parse_subtitle_filename(os.path.basename(subtitle_path))
            language = subtitle_info.get('language', 'Unknown')
            subtitle_type = subtitle_info.get('type', 'Normal')

            print(f"   语言: {language}")
            print(f"   类型: {subtitle_type}")

            # 查找匹配的视频文件
            video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.wmv')
            video_path = self.subtitle_handler.find_matching_video(Path(subtitle_path), video_extensions)

            if not video_path:
                console_log(f"   ⚠️  未找到匹配的视频文件，跳过处理")
                self.logger.warning(f"未找到匹配的视频文件: {subtitle_path}")
                return False

            console_log(f"   ✓ 找到匹配的视频文件: {os.path.basename(video_path)}")

            # 生成新的字幕文件名
            new_subtitle_name = self.subtitle_handler.generate_subtitle_name(
                video_path.name, subtitle_info
            )
            print(f"   新字幕文件名: {new_subtitle_name}")

            # 查找视频文件的输出目录（如果视频文件已经处理过）
            # 这里我们暂时将字幕文件放在与视频文件相同的目录
            video_dir = video_path.parent
            new_subtitle_path = video_dir / new_subtitle_name

            # 如果目标文件已存在，跳过
            if new_subtitle_path.exists():
                print(f"   ℹ️  目标字幕文件已存在，跳过")
                self.logger.info(f"目标字幕文件已存在: {new_subtitle_path}")
                return True

            # 移动字幕文件
            try:
                import shutil
                shutil.move(subtitle_path, new_subtitle_path)
                console_log(f"   ✅ 字幕文件已移动并重命名")
                self.logger.info(f"字幕文件已处理: {subtitle_path} -> {new_subtitle_path}")
                return True
            except Exception as e:
                console_log(f"   ❌ 移动字幕文件失败: {e}")
                self.logger.error(f"移动字幕文件失败: {subtitle_path} -> {new_subtitle_path}, 错误: {e}")
                return False

        except Exception as e:
            console_log(f"   ❌ 处理字幕文件时出错: {e}")
            self.logger.error(f"处理字幕文件时出错: {subtitle_path}, 错误: {e}")
            return False

    def _release_file_lock_via_downloader(self, file_path: str) -> bool:
        """
        通过暂停下载器中的种子来释放文件句柄

        Args:
            file_path: 文件路径

        Returns:
            bool: 是否成功释放了文件锁
        """
        downloader_file_path = self._reverse_apply_path_mapping(file_path)

        # 1. 首先尝试从映射中查找下载器
        if file_path in self._file_downloader_map:
            try:
                downloader = self._file_downloader_map[file_path]
                if hasattr(downloader, "pause_torrent_for_file"):
                    if downloader.pause_torrent_for_file(file_path) or (
                        file_path != downloader_file_path
                        and downloader.pause_torrent_for_file(downloader_file_path)
                    ):
                        self.logger.info(f"已通过下载器暂停种子释放文件锁: {file_path}")
                        return True
            except Exception as e:
                self.logger.warning(f"通过映射的下载器暂停种子失败: {e}")

        # 2. 尝试遍历所有注册的下载器
        if self.downloaders:
            for downloader in self.downloaders:
                try:
                    if hasattr(downloader, "pause_torrent_for_file"):
                        if downloader.pause_torrent_for_file(downloader_file_path):
                            self.logger.info(
                                f"已通过下载器暂停种子释放文件锁 (遍历): {downloader_file_path}"
                            )
                            return True
                except Exception as e:
                    self.logger.warning(f"尝试暂停种子时出错: {e}")

        return False

    def _delete_file_with_background_retry(
        self, file_path: str, max_retries: int = 20, retry_interval: int = 5
    ) -> bool:
        """
        使用 PowerShell 后台作业持续重试删除文件，即使主进程退出后也能继续

        Args:
            file_path: 文件路径
            max_retries: 最大重试次数
            retry_interval: 重试间隔（秒）

        Returns:
            bool: 是否成功启动了后台重试
        """
        # 删除实现统一收敛到 core.source_cleanup.delete_file_with_retry
        return delete_file_with_retry(file_path, max_retries, retry_interval)

    def _cleanup_old_records(self):
        """清理旧的处理记录，防止内存溢出"""
        try:
            # 清理已上传文件记录
            if len(self._uploaded_files) > self._max_set_size:
                old_size = len(self._uploaded_files)
                # 保留最近的一半
                self._uploaded_files = set(
                    list(self._uploaded_files)[-(self._max_set_size // 2) :]
                )
                self.logger.info(
                    f"清理已上传文件记录: {old_size} -> {len(self._uploaded_files)}"
                )

            # 清理失败文件记录
            if len(self._failed_files) > self._max_set_size:
                old_size = len(self._failed_files)
                items = list(self._failed_files.items())[-(self._max_set_size // 2) :]
                self._failed_files = dict(items)
                self.logger.info(
                    f"清理失败文件记录: {old_size} -> {len(self._failed_files)}"
                )

            # 清理处理中记录 (防止僵尸记录)
            # 注意：这里需要谨慎，因为正在处理的文件也在这个集合中
            # 一般不需要自动清理，除非确定它已经是僵尸了。这里暂时不自动清理 processing_files
        except Exception as e:
            self.logger.error(f"清理旧记录时出错: {e}")
