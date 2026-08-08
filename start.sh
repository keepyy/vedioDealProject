#!/usr/bin/env bash
# 启动脚本（Linux / macOS / Git-Bash）：在 conda 环境 vedioDealEnv 中启动
set -e
ENV_NAME="vedioDealEnv"

# 定位 conda
if command -v conda >/dev/null 2>&1; then
    CONDA_BIN="$(command -v conda)"
else
    for p in "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" "$HOME/miniforge3/bin/conda" /opt/conda/bin/conda; do
        [ -x "$p" ] && CONDA_BIN="$p" && break
    done
fi
[ -z "$CONDA_BIN" ] && { echo "❌ 找不到 conda"; exit 1; }

# 检查环境
if ! $CONDA_BIN env list | grep -q "^${ENV_NAME}\s"; then
    echo "▶ 创建 conda 环境 $ENV_NAME (python 3.11)"
    $CONDA_BIN create -n "$ENV_NAME" -y python=3.11
fi

# 激活
CONDA_BASE="$($CONDA_BIN info --base)"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"

[ ! -f .env ] && cp .env.example .env

if [ "${1:-}" = "install" ]; then
    echo "▶ 安装依赖"
    pip install -r requirements.txt
fi

if [ "${1:-}" = "build-worker" ]; then
    echo "▶ 构建沙箱 worker 镜像"
    docker build -t vedio-agent-worker:latest -f docker/worker.Dockerfile .
fi

echo "▶ 启动 Web 服务 http://localhost:8000"
exec uvicorn app.web.main:app --host 0.0.0.0 --port 8000 --reload
