import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskHistory(Base):
    __tablename__ = "task_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_time: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)


class ManualRule(Base):
    __tablename__ = "config_manual_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


class ReleaseGroupMapping(Base):
    __tablename__ = "config_release_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)  # anime, drama, movie
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


class LlmProvider(Base):
    __tablename__ = "config_llm_provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    api_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    timeout: Mapped[int] = mapped_column(Integer, default=30)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


class AuthUser(Base):
    __tablename__ = "config_auth_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="admin")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


class TargetMapping(Base):
    """上传目标映射表

    - ``match_type = "title"``：文字映射，标题/文件名包含 ``keyword`` 即命中；
    - ``match_type = "episode"``：剧集映射，标题匹配且季/集号一致才命中。

    命中后直接使用 ``item_type`` / ``item_id`` 上传，跳过 TMDB + Emos 搜索，
    实现「手动指定过一次，以后类似文件直接上传」。
    """

    __tablename__ = "config_target_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_type: Mapped[str] = mapped_column(String(16), nullable=False, default="title")
    keyword: Mapped[str] = mapped_column(String(256), nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), default="")
    season_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    item_type: Mapped[str] = mapped_column(String(16), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    storage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="web")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
