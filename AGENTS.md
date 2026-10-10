# AGENTS.md

## 构建/测试/格式化

- **运行全部测试:** `pytest`
- **运行单个测试文件:** `pytest tests/test_renamer.py`
- **运行单个测试:** `pytest tests/test_renamer.py::TestVideoRenamer::test_extract_metadata_basic`
- **格式化:** `black .`
- **类型检查:** `mypy src/`
- **Lint:** `flake8 src/`
- `pyproject.toml` 已配置 `pythonpath = ["src"]`，测试会自动识别包路径

## 项目结构要点

- **入口:**
  - 主入口: `src/video_organizer/main.py` — `main()`
  - 包运行: `python -m src.video_organizer.main [--web/--web-only/--process]`
  - CLI 启动器: `run_organizer.py` — 无参数时自动追加 `--web`（同时启动监控 + Web）
- **核心模块** (`src/video_organizer/core/`):
  - `renamer.py` — 文件识别/重命名核心
  - `config_loader.py` — 配置加载/保存/验证（支持 frozen 打包环境路径）
  - `video_file_handler.py` — 文件处理主循环（「未找到 TMDB / Emos 目标」的失败文件会加入重试队列，由 `filesystem_monitor._retry_loop` 每 1 分钟自动重跑识别 + 上传，命中后自动完成，无需手动重传）
  - `filesystem_monitor.py` — 目录/下载器监控（`_retry_files` 重试队列：每 60 秒处理一次）
  - `tmdb_client.py` — TMDB API 客户端
  - `guessit_parser.py` — GuessIt 集成 + 中文文件名预处理
  - `emos_client.py` / `probe.py` / `online_upload.py` — Emos 官方 API 客户端、ffprobe 校验、在线识别上传服务（`search_targets` 搜索结果里电视剧没带季/集时，会用 Emos 季/集接口补齐前 3 个候选，网页端才能选到手动新增的集）
  - `telegram_bot.py` — Telegram 机器人：上传报错通知（未解决每 5 分钟提醒；回复「删除」即停止提醒，并连带删除上传任务与 aria2/qB 下载任务），本地文件已不存在则不再提醒）+ 回复修正上传目标（发片名关键词 → 搜索候选 → 点选作品/季/集，可用年份区分同名作品；也可直接写「剧名SxxExx」）+ Telegram 原生命令菜单（`setMyCommands` + 菜单按钮：输入框左侧「菜单」/输入 `/` 列出指令，启动、绑定、测试消息时自动注册；不再使用底部快捷键盘，绑定时会顺手收起旧键盘）+ `/upload` 浏览本地文件（每个条目名称按显示宽度折成多行整行按钮（按钮里显示完整文件名），名称不带序号，最后一行才是「📤 N 上传 / 🗑️ 删除」（序号只挂在上传按钮上）；正文只列目录与数量）+ `/config` 快捷配置（布尔项开关、其余输入）（长轮询 getUpdates，含 inline 按钮）
  - `mapping_store.py` — 目标映射表（文字映射 / 剧集集数映射），命中后跳过 TMDB/Emos 搜索直接上传
  - `incomplete_downloads.py` — 汇总 qb/aria2 未完成下载，手动识别本地文件时排除半成品
- **Web 后端** (`web/`):
  - `app.py` — FastAPI 应用创建，`create_app()`
  - `auth.py` — HMAC-SHA256 令牌认证（非标准 JWT），服务重启所有 token 失效
  - `routers/` — `config.py`, `tasks.py`, `logs.py`, `manual.py`, `auth.py`, `downloaders.py`, `online_upload.py`
  - `services/state.py` — `StateManager` 单例
- **上传模块** (`upload/`): `upload_emos.py` — Emos 官方 API 上传（分片/直传 + save）
- **数据库** (`database/`): SQLAlchemy，用于任务/配置持久化（默认 SQLite）
- **配置文件:** `config.ini`（实际）、`config_template.ini`（模板），首次运行自动生成
  - 下载器配置节 `downloader.<标识>` 支持同类型多实例：`downloader.aria2`、`downloader.aria2_2` 等，用 `type` 字段指定真实类型（aria2 / qbittorrent）
  - 下载器可在「下载器管理」或「配置管理 → 下载器配置」里在线增删改，保存后立即生效（无需重启容器）
  - `[logging]` 只有 `log_level` 需要在界面配置：日志文件固定写入统一日志目录 `video-organizer.log`（控制台 + 文件始终开启），「日志查看」页面只需选等级并自动展示最新日志
- **打包:** `build.sh` — PyInstaller 构建，spec 内嵌生成
- **部署文档:** `DEPLOY.md` — 从创建目录到 `docker compose up -d --build` 的精简部署步骤（compose 文件即仓库根目录 `docker-compose.yml`）

## 代码约定

- 使用 `pathlib.Path`，禁用字符串路径
- 使用 `from typing import Dict, List, Optional, Union`
- 绝对导入: `from src.video_organizer.core.renamer import VideoRenamer`
- 异常用 `try/except` + 日志 `logger = logging.getLogger(__name__)`
- 文档和注释使用中文

## Docker

- 正式镜像: `Dockerfile`（多阶段构建，内置 ffmpeg，默认 `python run_organizer.py --web`）
- 单卷持久化: `./data:/app/data`（config.ini / 日志 / 数据库），不再映射 logs/strm 路径
- `docker-compose.yml` 只有一个 `video-organizer` service

## 调试

### 启动测试服务器（避免卡住）
```powershell
$p = Start-Process -WindowStyle Hidden -PassThru -FilePath "python" -ArgumentList "-m", "src.video_organizer.main", "--web-only", "--web-port", "8095"; Write-Output $p.Id
```

### 停止测试服务器
```powershell
Get-Process -Id (Get-NetTCPConnection -LocalPort 8095 -ErrorAction SilentlyContinue).OwningProcess -ErrorAction SilentlyContinue | Stop-Process -Force
```

### 查看日志
```powershell
python -c "import sys; sys.path.insert(0,'src'); from pathlib import Path; p=Path('logs'); [print(f.read_text()[:2000]) for f in sorted(p.glob('*.log'))[-3:]]"
```

## Web API 快速参考

- Web 服务默认 `0.0.0.0:8080`（`--web-port` 可改）
- 认证: `POST /api/auth/login` → `access_token`，后续 `Authorization: Bearer <token>`
- `POST /api/manual/validate` — 文件名识别验证（传 `{"file_path": "..."}`）
- `GET /api/auth/first-run-credentials` — 首次运行随机密码
- `/api/auth/`、`/static/`、`/api/health` 无需认证
- WebSocket: `/api/tasks/ws/progress`, `/api/tasks/ws/dashboard`, `/api/logs/ws/{filename}`
- 在线识别上传: `GET /api/online-upload/config|roots|search|tasks`、`POST /api/online-upload/browse|scan|probe|recognize|recognize-batch|tasks|save-internal`、`POST /api/online-upload/tasks/{id}/retry`、`DELETE /api/online-upload/tasks/{id}|tasks/clear`（`recognize-batch` 并发识别；创建任务按文件去重，重复提交返回 `duplicates`）
- 目标映射表: `GET|POST /api/config/db/target-mappings`、`PUT|DELETE /api/config/db/target-mappings/{id}`（`match_type` 为 `title`（文字映射）或 `episode`（剧集集数映射）；TG 修正过的目标自动写入；网页端「搜索目标」会展开电视剧的季/集，选中某一集自动填好季/集并指向 `ve` 目标）
- Telegram 机器人: `GET /api/config/telegram/status`、`POST /api/config/telegram/test`（绑定 / 测试消息；TG 内发送 `/upload` 选择服务器本地文件上传）
- 下载器: `GET /api/downloaders` 返回每个实例的 `id`（配置节标识）与 `type`，`/api/downloaders/{id}/{status|tasks|remove|pause|resume|processed}` 按实例标识访问
- 配置在线修改后会自动热更新到视频处理器、在线识别上传服务、Telegram 机器人与下载器监控（新增/删除 aria2 实例、调整上传并发数均立即生效），无需重启容器
