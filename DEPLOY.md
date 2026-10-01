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
