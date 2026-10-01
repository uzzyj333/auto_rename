# Docker 快速开始

> 从创建目录开始的完整部署步骤（含 compose 文件、下载器/Telegram 配置、升级备份、排障）请直接看 **[DEPLOY.md](DEPLOY.md)**。
> 本文只是最短路径的速查。

## 最短部署流程

```bash
# 1. 创建部署目录并拉取代码
sudo mkdir -p /opt/auto-rename && sudo chown "$(id -u)":"$(id -g)" /opt/auto-rename
git clone https://github.com/uzzyj333/auto_rename.git /opt/auto-rename
cd /opt/auto-rename

# 2. 创建数据 / 下载 / 媒体目录，并授权给容器内的 uid 1000
mkdir -p data downloads media
sudo chown -R 1000:1000 data downloads
sudo chmod -R a+rX media

# 3. 环境变量（端口、挂载路径，按需修改）
cp .env.example .env

# 4. 构建并启动
docker compose up -d --build
docker compose ps

# 5. 获取首次登录密码
curl -s http://127.0.0.1:8080/api/auth/first-run-credentials
#    或：docker compose logs video-organizer | grep -A3 "首次运行"

# 6. 浏览器打开 http://<服务器IP>:8080 （用户名 admin）
```

## 启动后在 Web 界面配置

| 页面 | 要做的事 |
|------|----------|
| 配置管理 | `[emos] auth_token`、`[tmdb] api_key`、`[online_upload] video_root = /media`、`[processing] max_upload_workers` / `delete_after_upload` |
| 在线识别上传 | 选视频 → 识别 → 确认目标 → 上传（并发数 = `max_upload_workers`） |
| 下载器 | 添加 aria2 / qBittorrent（可多个实例），自动监控下载完成后上传 |
| Telegram | 填 `bot_token` 后在 Telegram 发 `/bind`；上传失败会推送报错，直接回复 `剧名S04E09` 即可修正目标并重新上传 |

> 以上配置**保存即生效，无需重启容器**。

## 镜像特点

- ✅ 基于 Python 3.12 官方镜像，多阶段构建
- ✅ 非 root 用户（uid 1000）运行
- ✅ 内置健康检查（`/api/health`）
- ✅ 内置 ffmpeg / ffprobe，支持上传前视频校验
- ✅ 单卷持久化：只需挂载 `./data`（配置 / 日志 / 数据库），不再映射日志与 strm 路径
- ✅ 同一类型可配置多个下载器实例（多 aria2 / aria2 + qBittorrent）

## 常用命令

```bash
docker compose ps                    # 状态
docker compose logs -f               # 日志
docker compose restart               # 重启
docker compose down                  # 停止并删除容器
git pull && docker compose up -d --build   # 升级
docker exec -it video-organizer sh   # 进入容器
```

## 下一步

- [DEPLOY.md](DEPLOY.md) —— 完整部署步骤与排障
- [DOCKER.md](DOCKER.md) —— Docker 命令与配置项参考
- [README.md](README.md) —— 功能特性
- [config_template.ini](config_template.ini) —— 完整配置模板（含多 aria2 示例）
