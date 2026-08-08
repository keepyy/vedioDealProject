# 主应用镜像：FastAPI Web 服务 + NVIDIA CUDA Runtime + YOLO GPU 推理 + ffmpeg
# CUDA 基础镜像已在本机缓存；Python/CUDA 推理依赖统一使用阿里云 PyPI 镜像安装
FROM nvidia/cuda:12.6.2-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV YOLO_CONFIG_DIR=/tmp/Ultralytics

RUN sed -i 's|http://archive.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g; s|http://security.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl ffmpeg fonts-noto-cjk \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3.11 python3.11-venv python3.11-dev \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && ( printf '/usr/lib/wsl/lib\n/usr/local/cuda/compat\n' > /etc/ld.so.conf.d/wsl-nvidia.conf ; ldconfig 2>/dev/null || true ) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python -m venv /opt/venv --upgrade-deps
ENV PATH="/opt/venv/bin:${PATH}"
ENV VIRTUAL_ENV="/opt/venv"

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt \
    && pip uninstall -y opencv-python \
    && pip install --force-reinstall --no-deps \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        opencv-python-headless==4.10.0.84 \
    && python -c "import cv2; assert cv2.__version__ == '4.10.0'; assert hasattr(cv2, 'CascadeClassifier'); print('OpenCV headless:', cv2.__version__)"

COPY ./app ./app

RUN mkdir -p /app/storage/raw /app/storage/segments /app/storage/final /app/storage/frames /tmp/Ultralytics

ENV SANDBOX_ENABLED=false
ENV STORAGE_DIR=/app/storage

EXPOSE 8000
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
