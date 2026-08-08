# 独立 GPU 人物检测服务：CUDA Runtime + 固化的 PyTorch/YOLO 推理依赖
FROM nvidia/cuda:12.6.2-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV YOLO_CONFIG_DIR=/models/.config
ENV YOLO_MODEL=/models/yolo11n.pt
ENV YUNET_MODEL=/models/face_detection_yunet_2023mar.onnx
ENV SFACE_MODEL=/models/face_recognition_sface_2021dec.onnx

RUN sed -i 's|http://archive.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g; s|http://security.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3.11 python3.11-venv python3.11-dev \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN python -m venv /opt/venv --upgrade-deps
ENV PATH="/opt/venv/bin:${PATH}"
ENV VIRTUAL_ENV="/opt/venv"

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 1000 --retries 10 \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        --extra-index-url https://download.pytorch.org/whl/cu126 \
        --trusted-host mirrors.aliyun.com \
        --trusted-host download.pytorch.org \
        torch==2.7.1+cu126 torchvision==0.22.1+cu126

COPY requirements-gpu.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-gpu.txt \
    && pip uninstall -y opencv-python \
    && pip install --force-reinstall --no-deps \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        opencv-python-headless==4.10.0.84 \
    && python -c "import torch; assert torch.version.cuda == '12.6'; import cv2; print('PyTorch:', torch.__version__, 'OpenCV:', cv2.__version__)"

COPY ./gpu_service ./gpu_service
RUN mkdir -p /models/.config \
    && curl -fL --retry 5 --retry-delay 3 \
        https://hf-mirror.com/Ultralytics/YOLO11/resolve/main/yolo11n.pt \
        -o /opt/yolo11n.pt \
    && curl -fL --retry 5 --retry-delay 3 \
        https://hf-mirror.com/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx \
        -o /opt/face_detection_yunet_2023mar.onnx \
    && curl -fL --retry 5 --retry-delay 3 \
        https://hf-mirror.com/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx \
        -o /opt/face_recognition_sface_2021dec.onnx \
    && test "$(stat -c%s /opt/yolo11n.pt)" -gt 1000000 \
    && test "$(stat -c%s /opt/face_detection_yunet_2023mar.onnx)" -gt 200000 \
    && test "$(stat -c%s /opt/face_recognition_sface_2021dec.onnx)" -gt 30000000

EXPOSE 8100
CMD ["uvicorn", "gpu_service.main:app", "--host", "0.0.0.0", "--port", "8100"]
