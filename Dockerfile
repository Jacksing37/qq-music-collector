# QQ 音乐收集机器人 · 运行镜像
# 构建：docker compose build
# 说明：本镜像只跑机器人本体，QQ 协议端（NapCat）是独立容器。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# fonts-noto-cjk：长图里的中文，不装会渲染成方块
# tzdata：容器默认 UTC，不设时区所有定时任务会差 8 小时
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层单独 COPY，改代码时不会重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.example.yaml ./
COPY src ./src
COPY scripts ./scripts

# data/ 由 volume 挂载，这里只保证目录存在
RUN mkdir -p data/cache

EXPOSE 8080

# 容器内必须监听 0.0.0.0，否则 NapCat 容器连不进来
ENV HOST=0.0.0.0 \
    PORT=8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import socket,sys; socket.create_connection(('127.0.0.1',8080),3)" || exit 1

CMD ["python", "bot.py"]
