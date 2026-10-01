# Docker 快速开始

将项目打包成 Docker 镜像，简化部署和使用。

## 一键启动

```bash
# 1. 克隆项目
git clone https://github.com/uzzyj333/auto_rename.git
cd auto_rename

# 2. 创建数据目录（容器内以 uid 1000 运行，需要确保目录可写）
mkdir -p data && chown -R 1000:1000 data

# 3. 编辑 docker-compose.yml，修改挂载路径
# /path/to/downloads -> 你的下载目录
# /path/to/media -> 你的媒体库目录

# 4. 启动服务
docker compose up -d

# 5. 查看日志获取管理员密码
docker compose logs | grep "管理员密码"

# 6. 访问 Web 界面
# http://localhost:8080
```

## 详细文档

完整的 Docker 部署指南请查看 [DOCKER.md](DOCKER.md)

## 镜像特点

- ✅ 基于 Python 3.12 官方镜像
- ✅ Multi-stage 构建，镜像体积优化
- ✅ 非 root 用户运行，安全可靠
- ✅ 自动健康检查
- ✅ 内置 ffmpeg / ffprobe，支持上传前视频校验
- ✅ 完整的数据持久化

## 运行模式

```bash
docker compose up -d
```

自动监控下载目录，完成后刮削并上传到 Emos，同时提供 Web 管理界面（含「在线识别上传」页面）。

## 配置说明

首次启动后：

1. 查看日志获取随机管理员密码
2. 访问 `http://localhost:8080` 登录
3. 在`配置管理`页面填写 TMDB API Key 与 Emos auth_token（修改后立即生效，无需重启容器）
4. 在`在线识别上传`页面选择视频根目录并上传；也可直接编辑 `./data/config.ini` 后重启容器

## 常用命令

```bash
# 查看运行状态
docker compose ps

# 查看日志
docker compose logs -f

# 重启服务
docker compose restart

# 停止服务
docker compose down

# 更新镜像
git pull
docker compose build
docker compose up -d

# 进入容器
docker exec -it video-organizer sh
```

## 推送到镜像仓库

```bash
# 使用脚本构建和推送
chmod +x docker-build.sh
./docker-build.sh v1.0.0

# 或手动推送
docker build -t your-registry/video-organizer:latest .
docker push your-registry/video-organizer:latest
```

## 下一步

- 查看 [DOCKER.md](DOCKER.md) 了解详细配置
- 查看 [README.md](README.md) 了解功能特性
- 查看 [docs/usage_guide.md](docs/usage_guide.md) 了解使用方法
