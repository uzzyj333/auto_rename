# Multi-stage build for optimized production image
FROM python:3.12-slim AS builder

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先安装依赖（利用缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 生产环境镜像
FROM python:3.12-slim

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    ffmpeg \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从 builder 复制已安装的包
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制项目文件
COPY src/ ./src/
COPY run_organizer.py .
COPY config_template.ini ./config.ini

# 创建必要目录，并预置一份带注释的配置模板到数据目录
# 日志固定写入 /app/data/logs（env: VIDEO_ORGANIZER_LOG_DIR），不再映射宿主机日志路径
RUN mkdir -p /app/data/logs && \
    cp /app/config.ini /app/data/config.ini && \
    chmod -R 755 /app

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/api/health || exit 1

EXPOSE 8080

# 使用非 root 用户运行
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# 默认启动 Web 管理界面（带文件监控）
CMD ["python", "run_organizer.py", "--web", "--web-host", "0.0.0.0", "--web-port", "8080"]
