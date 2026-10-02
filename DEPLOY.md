# 部署（Docker Compose）

```bash
# 1) 创建目录 + 拉代码（compose 文件在仓库里，就是 docker-compose.yml）
sudo mkdir -p /opt/auto-rename && sudo chown "$(id -u)":"$(id -g)" /opt/auto-rename
git clone https://github.com/uzzyj333/auto_rename.git /opt/auto-rename
cd /opt/auto-rename

# 2) 建目录并授权（容器内以 uid 1000 运行）
mkdir -p data downloads media
sudo chown -R 1000:1000 data downloads

# 3) 环境变量：端口 / 挂载路径（默认 ./data ./downloads ./media）
cp .env.example .env

# 4) 构建并启动
docker compose up -d --build
docker compose ps

# 5) 首次登录密码 + 打开 Web
curl -s http://127.0.0.1:8080/api/auth/first-run-credentials   # 用户名 admin
# 浏览器访问 http://<服务器IP>:8080
```

启动后在 Web 里配置（**保存即生效，不用重启容器**）：

- 配置管理：`emos.auth_token`、`tmdb.api_key`、`online_upload.video_root = /media`、`processing.max_upload_workers`
- 下载器：添加 aria2 / qBittorrent（可多个实例，节名 `downloader.aria2_2`）；路径不一致时填 `path_mappings = 下载器路径:容器内路径`
- Telegram：填 `bot_token`，在 TG 里发 `/bind`；上传失败可直接回复 `剧名S04E09` 修正目标

其他：

- `./data` 是唯一持久化目录（config.ini、日志、数据库都在里面），日志不再单独映射
- 升级：`git pull && docker compose up -d --build`；日志：`docker compose logs -f` 或 `./data/logs/video-organizer.log`

## 和旧版本并存（不冲突）

旧版是 `container_name: auto_rename` / `8083:8080` / `/opt/media_center/auto_rename/data`，只要下面几项都换掉就能同时跑：

| 项 | 旧版 | 新版 |
|----|------|------|
| 项目目录 | `/opt/media_center/auto_rename` | `/opt/media_center/auto_rename_v2`（compose 项目名跟目录走） |
| 容器名 | `auto_rename` | `auto-rename-v2` |
| 镜像名 | `auto_rename` | `video-organizer-v2:latest` |
| 宿主端口 | 8083 | 8084 |
| 数据目录 | `.../auto_rename/data` | `.../auto_rename_v2/data`（**必须换新目录**，旧库表结构不同） |

```bash
git clone https://github.com/uzzyj333/auto_rename.git /opt/media_center/auto_rename_v2
mkdir -p /opt/media_center/auto_rename_v2/data
```

```yaml
# /opt/media_center/auto_rename_v2/docker-compose.yml
services:
  auto-rename-v2:
    build: .
    image: video-organizer-v2:latest
    container_name: auto-rename-v2
    restart: unless-stopped
    user: "0:0"                  # 目录属主是 root 就留着；chown 1000:1000 后可以删掉
    command: ["python", "run_organizer.py", "--web", "--web-host", "0.0.0.0", "--web-port", "8080", "--config", "/app/data/config.ini"]
    ports:
      - "8084:8080"
    volumes:
      - /opt/media_center/auto_rename_v2/data:/app/data
      - /media:/media
      - /cd2:/cd2:shared         # 需要就保留
    environment:
      TZ: Asia/Shanghai
      VIDEO_ORGANIZER_LOG_DIR: /app/data/logs
      VIDEO_ORGANIZER_DB_PATH: /app/data/video_organizer.db
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
```

注意：

- 新版只需要 `data` 一个卷，`logs` / `strm` / `/app/src/video_organizer/data` 这几个旧映射都不用再写；
- `command` 里的 `--config /app/data/config.ini` 不能省，否则配置会写进镜像里，重建容器就丢；
- 两个版本不要同时监控同一个下载目录并都开「删除原文件」，会互相删文件；只并行用「在线识别上传」没问题；
- 换端口后记得防火墙放行 8084。