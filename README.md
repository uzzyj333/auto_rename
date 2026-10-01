# Video Organizer — 视频文件自动重命名与组织工具

![版本](https://img.shields.io/badge/version-1.0.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.12%2B-green.svg)
![平台](https://img.shields.io/badge/平台-Windows%20%7C%20Linux%20%7C%20macOS-blue.svg)
![构建](https://img.shields.io/github/actions/workflow/status/uzzyj333/auto_rename/build.yml?label=构建)

自动识别、重命名、刮削、上传视频文件。支持 TMDB 元数据、多字幕组分类、云盘多端上传、Web 管理后台。

---

## 功能一览

| 功能 | 说明 |
|------|------|
| **智能文件名解析** | 正则 + GuessIt + LLM 三级兜底，覆盖 50+ 种文件名格式 |
| **TMDB 元数据** | 自动刮削中英文元数据，生成 NFO（Emby/Jellyfin 兼容）|
| **字幕组分类** | 内置 300+ 字幕组映射，自动区分动漫 / 日韩剧 / 电影 |
| **手动规则 DSL** | 自定义屏蔽词、替换、定位截取、内嵌 TMDB ID |
| **Web 管理后台** | FastAPI + SPA，实时仪表盘、配置管理、手动处理、日志查看 |
| **Emos 上传** | 官方 API 上传 + 在线识别上传（浏览服务器视频、识别条目、手动选择目标）|
| **下载器监控** | 自动监控 aria2 / qBittorrent 下载完成事件 |
| **文件监控** | 目录轮询 + 文件稳定性检测，自动整理归档 |
| **LLM 兜底** | 多 Provider 负载均衡（OpenAI / DeepSeek / 智谱等）|
| **认证系统** | PBKDF2 密码、HMAC 令牌、首次运行随机密码 |

---

## 快速开始

### 使用 Docker（推荐）

```bash
# 克隆仓库
git clone https://github.com/uzzyj333/auto_rename.git
cd auto_rename

# 创建数据目录（容器内以 uid 1000 运行，需要确保目录可写）
mkdir -p data && chown -R 1000:1000 data

# 编辑 docker-compose.yml 修改挂载路径后启动
docker compose up -d

# 查看日志获取管理员密码
docker compose logs | grep "管理员密码"

# 访问 Web 界面：http://localhost:8080
```

完整 Docker 部署文档：[DOCKER.md](DOCKER.md) | [快速开始](DOCKER_QUICKSTART.md)

> 说明：`./data` 是唯一的持久化目录（`config.ini`、日志、SQLite 数据库都在里面），日志不再映射宿主机路径；
> 在 Web「配置管理」里修改的配置会立即生效，无需重启容器。

### 下载可执行文件

从 [GitHub Releases](https://github.com/uzzyj333/auto_rename/releases) 下载对应平台的单文件可执行程序，解压即可运行。

### 从源码运行

```bash
# 克隆仓库
git clone https://github.com/uzzyj333/auto_rename.git
cd auto_rename

# 安装依赖
pip install -r requirements.txt

# 启动 Web 管理后台
python -m src.video_organizer.main --web-only --web-port 8080
```

首次运行会自动生成随机密码，打印在控制台并显示在登录页面上。

---

## 使用方式

### Web 管理后台

最推荐的使用方式，浏览器操作所有功能：

```bash
# 仅启动 Web 服务（不带文件监控）
python -m src.video_organizer.main --web-only --config config.ini

# 同时启动文件监控和 Web 服务
python -m src.video_organizer.main --web --config config.ini
```

打开 `http://localhost:8080` 访问管理页面。首次运行自动生成随机管理员密码，显示在控制台和登录页。

### 命令行

```bash
# 查看帮助
python -m src.video_organizer.main --help

# 使用自定义配置文件
python -m src.video_organizer.main --config /path/to/config.ini

# 监控指定目录
python -m src.video_organizer.main --monitor-dir "D:\Downloads" --web --web-port 8081

# 手动处理单个文件
python -m src.video_organizer.main --process "D:\video.mkv"

```

---

## Web 管理后台

### 页面

| 页面 | 功能 |
|------|------|
| **仪表盘** | 系统状态概览，最近活动（分页+搜索），上传进度实时监控 |
| **任务管理** | 队列/处理中/已完成/失败四标签，搜索、重试、清除 |
| **配置管理** | 广播控制台风格，分节配置所有参数，支持搜索和实时保存 |
| **日志查看** | 选择日志文件、tail 模式、实时日志流（WebSocket）|
| **手动处理** | 输入/浏览文件路径，预览重命名、验证刮削、批量处理 |
| **下载器管理** | 下载器状态查看，配置管理（添加/编辑/删除）|
| **手动处理** | 输入/浏览文件路径，预览重命名、验证刮削、批量处理 |
| **在线识别上传** | 浏览/扫描视频根目录、识别 Emos 条目、手动选择目标并上传，实时进度 |

### API 文档

启用了 FastAPI 自动文档：
- Swagger UI: `http://localhost:8080/api/docs`
- ReDoc: `http://localhost:8080/api/redoc`

### 认证

默认使用首次运行生成的随机密码登录。支持多用户、角色管理，通过 PBKDF2-HMAC-SHA256 加密存储，HMAC-SHA256 令牌（7天有效期）。

---

## 配置

项目使用 INI 配置文件（`config.ini`），首次运行自动生成模板。完整配置节如下：

| 配置节 | 说明 |
|--------|------|
| `[monitoring]` | 监控目录、输出目录、文件扩展名、轮询间隔、路径映射 |
| `[naming]` | 电视剧/电影/动漫命名模板（Jinja2）|
| `[tmdb]` | TMDB API 密钥、语言、地区、重试次数、超时、代理 base_url |
| `[guessit]` | GuessIt 增强解析开关 |
| `[llm_fallback]` | LLM 兜底解析开关和并发控制 |
| `[llm_provider_N]` | LLM 提供商（名称/地址/密钥/模型/权重/超时），支持多个 |
| `[processing]` | 上传目标（固定 `emos`）、上传后删除源文件、最大上传并发数 |
| `[emos]` | Emos 认证令牌、API 地址（默认 `https://emos.best`）、存储类型、分片大小 |
| `[online_upload]` | 在线识别上传：视频根目录、ffprobe 开关与路径、path_type |
| `[downloader.aria2]` | aria2 RPC 地址、密钥、监控模式（轮询/WebSocket/Webhook）|
| `[downloader.qbittorrent]` | qBittorrent 地址、用户名、密码 |
| `[telegram]` | Telegram 推送 Bot Token、Chat ID |
| `[manual_rules]` | 手动规则列表（DSL 语法）|
| `[logging]` | 日志级别、文件、控制台开关 |

### 命名模板变量

| 变量 | 说明 | 适用 |
|------|------|------|
| `{show_name}` / `{movie_name}` / `{anime_name}` | 显示名称 | 所有 |
| `{season}` | 季号（两位数）| 电视剧/动漫 |
| `{episode}` | 集号（两位数）| 电视剧/动漫 |
| `{season_episode}` | `S01E01` 格式 | 电视剧 |
| `{year}` / `{year_suffix}` | 年份 / `(2024)` 格式 | 所有 |
| `{tmdb_id}` / `{tmdbid_suffix}` | TMDB ID / `{tmdbid=123}`（在线识别会读取该标记） | 所有 |
| `{title}` | 综合标题 | 简单模式 |
| `{quality_tags}` / `{quality_tags_suffix}` | 画质标签 / `-1080p` | 所有 |
| `{release_group}` / `{release_group_suffix}` | 发布组 / `-[ANi]` | 所有 |
| `{en_title}` / `{en_title_suffix}` | 英文标题 | 电影 |

---

## 高级功能

### 文件名解析引擎

三级解析兜底，确保最大识别率：

1. **正则解析**（50+ 模式）— SxxExx、`[字幕组]剧名`、中文"第X集"、罗马数字、PT 命名法、紧凑格式等
2. **GuessIt 增强** — 专业视频文件名解析，中文预处理，季目录识别，流媒体误判防护
3. **LLM 兜底** — 正则和 GuessIt 都失败时调用 LLM 解析，支持多 Provider 负载均衡

### TMDB 刮削

- 中英文双语搜索（先中文后英文）
- 搜索结果缓存（同剧多集共享）
- 年份降级重试
- 可配置代理 base_url
- 附带图片下载（海报/背景图/剧照）

### NFO 元数据生成

兼容 Emby / Jellyfin / Kodi：
- `tvshow.nfo` — 剧集级别
- `episodedetails.nfo` — 逐集
- `movie.nfo` — 电影
- 完整字段：标题、原名、年份、首播日期、评分、剧情简介、制片公司、演员角色、工作人员、TMDB/IMDb/TVDB ID

### 手动规则 DSL

在文件名解析前干预，解决 TMDB 重名/错判：

```
block: 预告,生肉
replace: 超级英雄 -> 正义联盟
position: start=3,end=-4,offset=-2
{[tmdbid=1418;type=tv;s=1;e=12]}
when: 包含"1080p" => block: 4K
```

### 云盘上传（Emos 官方 API）

上传完成后自动推送到 Emos，全部使用官方 API：
- **在线识别上传** — 在 Web 页面浏览服务器视频 → 自动识别 Emos 条目（TMDB ID / 标题搜索）→ 选择目标一次性入队上传
- **自动上传** — 文件监控/下载器完成后自动识别 `getVideoId` 并分片上传
- **断点续传** — google_drive 存储走 resumable 协议，其他存储走 multipart 分片 + 校验
- **ffprobe 校验** — 上传前校验视频并提取分辨率/编码/时长等元数据

### 下载器集成

自动监控下载器完成事件，完成后自动刮削+上传：
- **aria2** — 轮询 / WebSocket / Webhook 三种监控模式
- **qBittorrent** — 轮询监控，自动登录续期

支持路径映射（Docker 场景）、文件完整性检查、Sample 跳过、去重、下载任务自动清理。

### 字幕组映射

内置 300+ 知名字幕组/发布组，映射三种内容类型：
- `anime` — 动漫（日番/国漫）
- `drama` — 电视剧
- `movie` — 电影

通过 Web UI 或 API 可以自定义映射。

---

## 构建

### 使用 build.sh（推荐，Linux/macOS）

```bash
bash build.sh
```

输出在 `dist/` 目录。

### 使用 package.py（Windows）

```powershell
python package.py
```

### GitHub Actions

推送至 `main` / `feature/llm-tmdb-fallback` 分支或打 `v*` tag 时自动构建三平台可执行文件：
- Ubuntu (Linux)
- Windows (.exe)
- macOS

构建产物上传至 Action Artifacts，tag 推送自动发布到 GitHub Releases。

### Docker

```bash
docker compose up -d
```

详细文档：[DOCKER.md](DOCKER.md)

---

## 目录结构

```
src/video_organizer/
├── main.py                       # 程序入口
├── core/
│   ├── config_loader.py          # INI 配置加载
│   ├── renamer.py               # 核心：元数据提取 + TMDB 丰富 + 命名生成
│   ├── tmdb_client.py           # TMDB API v3 客户端
│   ├── guessit_parser.py        # GuessIt 增强解析
│   ├── manual_rule_engine.py    # 手动规则 DSL
│   ├── filesystem_monitor.py    # 文件系统监控
│   ├── video_file_handler.py    # 视频文件处理器
│   ├── file_mover.py            # 文件移动器
│   ├── subtitle_handler.py      # 字幕处理
│   ├── downloader_monitor.py    # 下载器监控（aria2/qBittorrent）
│   ├── emos_client.py           # Emos 官方 API 客户端
│   ├── probe.py                 # ffprobe 视频校验
│   └── online_upload.py         # 在线识别上传服务
├── web/
│   ├── app.py                   # FastAPI 应用
│   ├── auth.py                  # 认证系统
│   ├── routers/                 # API 路由
│   └── static/                  # SPA 前端
├── database/                    # SQLite ORM
├── upload/                      # Emos 上传器
└── utils/                       # 工具模块
```

---

## 许可证

MIT
