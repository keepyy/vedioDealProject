# 沙箱 worker 镜像：用于在 Docker 沙箱里跑 ffmpeg / Python 脚本
# 构建：docker build -t vedio-agent-worker:latest -f docker/worker.Dockerfile .
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1-mesa-glx \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
# 仅在 worker 内需要 opencv（主应用也有，但 worker 可以单独跑检测脚本）
RUN pip install --no-cache-dir numpy opencv-python-headless Pillow

CMD ["/bin/bash"]
