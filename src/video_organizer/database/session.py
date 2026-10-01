import os
import sys
import logging
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from .models import Base

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def get_db_path() -> Path:
    """获取数据库文件路径

    优先级：

    1. 环境变量 ``VIDEO_ORGANIZER_DB_PATH``
    2. 容器内的持久化目录 ``/app/data``（与 config.ini、日志同一个挂载卷，
       重建容器后用户/规则不会丢失）
    3. 旧路径（项目内 ``src/video_organizer/data`` 或 exe 同级 ``data``）

    如果旧路径已经存在数据库，则继续沿用，避免升级后丢数据。
    """
    env_path = os.environ.get("VIDEO_ORGANIZER_DB_PATH", "").strip()
    if env_path:
        path = Path(env_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        except OSError as exc:
            logger.warning(f"无法使用数据库路径 {path}: {exc}")

    if getattr(sys, "frozen", False):
        legacy_dir = Path(sys.executable).resolve().parent / "data"
    else:
        legacy_dir = Path(__file__).resolve().parent.parent / "data"
    legacy = legacy_dir / "video_organizer.db"

    container_dir = Path("/app/data")
    if os.name != "nt" and container_dir.is_dir() and not legacy.exists():
        try:
            container_dir.mkdir(parents=True, exist_ok=True)
            return container_dir / "video_organizer.db"
        except OSError as exc:
            logger.warning(f"无法使用 {container_dir} 存放数据库，回退旧路径: {exc}")

    legacy_dir.mkdir(parents=True, exist_ok=True)
    return legacy


def init_db(db_path: str = None) -> str:
    """
    初始化数据库引擎和表结构
    
    Args:
        db_path: 数据库文件路径，None 则使用默认路径
        
    Returns:
        实际使用的数据库路径
    """
    global _engine, _SessionLocal

    path = db_path or str(get_db_path())
    _engine = create_engine(
        f"sqlite:///{path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

    Base.metadata.create_all(bind=_engine)
    logger.info(f"数据库已初始化: {path}")
    return path


def get_engine():
    global _engine
    if _engine is None:
        init_db()
    return _engine


def get_session_local():
    global _SessionLocal
    if _SessionLocal is None:
        init_db()
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：获取数据库会话"""
    db = get_session_local()()
    try:
        yield db
    finally:
        db.close()
