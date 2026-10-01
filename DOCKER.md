# Docker 部署指南

## 快速开始

### 1. 使用 Docker Compose（推荐）

```bash
# 克隆仓库
git clone https://github.com/uzzyj333/auto_rename.git
cd auto_rename

# 创建数据目录
mkdir -p data

# 编辑 docker-compose.yml，修改挂载路径：
# - /path/to/downloads:/downloads  （你的下载目录）
# - /path/to/media:/media           （你的媒体库目录）

# 启动服务（Web + 文件监控）
docker compose up -d

# 查看日志
docker compose logs -f

# 访问 Web 管理界面
# http://localhost:8080
# 首次启动会在日志中显示随机管理员密码
```

## 使用 Docker 命令

### 构建镜像

```bash
docker build -t video-organizer:latest .
```

### 运行容器

```bash
docker run -d \
  --name video-organizer \
  --restart unless-stopped \
  -p 8080:8080 \
  -v ./data:/app/data \
  -e VIDEO_ORGANIZER_LOG_DIR=/app/data/logs \
  -e TZ=Asia/Shanghai \
  -v /path/to/downloads:/downloads \
  -v /path/to/media:/media \
  video-organizer:latest
```

### 查看日志

```bash
docker logs -f video-organizer
```

### 进入容器

```bash
docker exec -it video-organizer sh
```

### 停止和删除

```bash
docker stop video-organizer
docker rm video-organizer
```

## 配置说明

### 目录挂载

| 容器路径 | 宿主机路径 | 说明 |
|---------|-----------|------|
| `/app/data` | `./data` | 唯一持久化目录：config.ini、日志、SQLite 数据库 |
| `/app/data/logs` | `./data/logs` | 日志目录（容器内固定路径，无需单独挂载）|
| `/downloads` | 你的下载目录 | 监控的下载目录 |
| `/media` | 你的媒体库目录 | 整理后的输出目录 / 在线识别上传的视频根目录 |

### 环境变量

| 变量 | 默认值 | 说明 |
|-----|--------|------|
| `TZ` | `Asia/Shanghai` | 时区 |
| `PYTHONUNBUFFERED` | `1` | Python 输出不缓冲 |
| `VIDEO_ORGANIZER_LOG_DIR` | `/app/data/logs` | 日志目录（容器内固定路径，不再映射宿主机日志路径）|
| `VIDEO_ORGANIZER_DB_PATH` | `/app/data/video_organizer.db` | SQLite 数据库路径（默认与 config.ini 同一个持久化目录）|
| `LOG_LEVEL` | `INFO` | 日志级别（DEBUG/INFO/WARNING/ERROR）|

### 端口

- `8080` - Web 管理界面

> 容器内以非 root 用户（uid 1000）运行，首次部署请先创建并授权数据目录：
>
> ```bash
> mkdir -p data && chown -R 1000:1000 data
> ```
>
> 若目录不可写，程序会自动回退到可写位置并在日志中给出警告，不会直接崩溃。

## 首次配置

### 1. 获取管理员密码

首次启动时，容器会生成随机管理员密码并输出到日志：

```bash
docker compose logs | grep "管理员密码"
```

### 2. 修改配置

访问 `http://localhost:8080`，使用管理员密码登录后，在"配置管理"页面修改：

- `[monitoring]` - 监控目录设置为 `/downloads`
- `[monitoring]` - 输出目录设置为 `/media`
- `[tmdb]` - 填入你的 TMDB API Key
- `[emos]` - 配置 Emos auth_token 与 base_url (必须)
- `[online_upload]` - 配置在线识别上传的视频根目录 video_root

或直接编辑 `./data/config.ini` 文件后重启容器（在 Web 界面修改则立即生效，无需重启）：

```bash
docker compose restart
```

### 3. 配置下载器监控（可选）

如果使用 aria2 或 qBittorrent：

```ini
[downloader.aria2]
enabled = True
url = http://aria2:6800/jsonrpc
secret = your_secret

[downloader.qbittorrent]
enabled = True
url = http://qbittorrent:8080
username = admin
password = admin
```

**注意**：如果下载器也在 Docker 中运行，使用容器名或网络 IP，而不是 `localhost`。

## 路径映射

如果你的下载器在 Docker 中运行，需要配置路径映射：

```ini
[monitoring]
path_mappings = {
    "/downloads": "/media/downloads"
}
```

例如，qBittorrent 容器中的路径是 `/downloads/video.mkv`，在宿主机是 `/media/downloads/video.mkv`。

## 常见问题

### 1. 权限问题

容器使用 `appuser` (UID 1000) 运行，确保挂载目录有读写权限：

```bash
sudo chown -R 1000:1000 data
```

或修改 Dockerfile 中的 UID：

```dockerfile
RUN useradd -m -u YOUR_UID appuser
```

### 2. 配置文件不生效

- 确认挂在 `./data` 的 `config.ini` 已保存
- Web「配置管理」里修改的配置会立即热生效，无需重启容器；直接改文件才需要 `docker compose restart`

### 3. 无法访问 Web 界面

- 检查防火墙是否开放 8080 端口
- 检查容器是否正常运行：`docker compose ps`
- 查看日志：`docker compose logs -f`

### 4. 找不到下载的文件

- 检查目录挂载是否正确
- 如果下载器在 Docker 中，检查路径映射配置
- 查看日志：`docker compose logs | grep "找不到"`

### 5. 云盘上传失败

- 检查网络连接
- 验证云盘账号配置是否正确
- 查看详细错误：`docker compose logs | grep "上传"`

## 更新

### 更新镜像

```bash
# 拉取最新代码
git pull

# 重新构建镜像
docker compose build

# 重启服务
docker compose up -d
```

### 备份数据

```bash
# 备份配置和数据
tar -czf backup-$(date +%Y%m%d).tar.gz data/ logs/
```

## 性能优化

### 调整资源限制

在 `docker-compose.yml` 中添加：

```yaml
services:
  video-organizer:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          memory: 512M
```

### 使用 tmpfs 加速（可选）

```yaml
services:
  video-organizer:
    tmpfs:
      - /tmp:size=1G
```

## 多实例部署

如果需要同时管理多个下载目录：

```bash
# 实例 1
docker compose -p organizer1 -f docker-compose.yml up -d

# 实例 2 - 修改端口和挂载目录
docker compose -p organizer2 -f docker-compose-2.yml up -d
```

## 监控和维护

### 健康检查

```bash
docker inspect video-organizer | grep -A 10 Health
```

### 资源使用

```bash
docker stats video-organizer
```

### 日志轮转

建议配置日志轮转，在 `docker-compose.yml` 中添加：

```yaml
services:
  video-organizer:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```
