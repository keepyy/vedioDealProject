# 茶辑 · Coze 智能视频剪辑应用

<img width="861" height="677" alt="茶剪辑首页" src="https://github.com/user-attachments/assets/4eaee9b2-26a3-47db-b337-910b4e512777" />

<img width="2082" height="1017" alt="茶剪辑编辑页面" src="https://github.com/user-attachments/assets/0c4fcd42-8fcb-40af-ada9-d5937c4d5c6e" />

<img width="1654" height="1223" alt="茶剪辑处理页面" src="https://github.com/user-attachments/assets/19da0b69-2ae7-4f26-909a-2d217e9846ef" />

<img width="1695" height="1047" alt="茶剪辑任务页面" src="https://github.com/user-attachments/assets/86eeb1f5-58ba-4ad0-a822-212d50fbd305" />

<img width="818" height="674" alt="茶剪辑完成页面" src="https://github.com/user-attachments/assets/49a5ff1a-b8bd-4fa2-b383-53996e306ebb" />

这是一个基于 FastAPI、FFmpeg 和百度网盘导入能力的视频剪辑 Web 应用。用户可上传本地视频或从已授权的百度网盘选择视频，在统一编辑页完成片段、人物、文字 Logo、蒙版和成品合并设置。

## 主要功能

- 普通剪辑和人物识别剪辑。
- 所有窗口在同一页面编辑，并一次确认批量生成。
- 视频预览和最终成品保持 4:3，原画面等比例适配，不拉伸。
- 每个成品盒可从当前播放器时间位置自动生成默认封面帧。
- 用户可定位到其他时间并点击“使用当前画面”覆盖默认封面。
- 每个最终成品使用独立封面，封面作为 2 秒、3840×2880 的 4:3 高清片头写入视频。
- 支持文字 Logo、手动蒙版模糊、区间选择和多个区间合并。
- 任务进度页和编辑页支持自定义居中取消确认弹窗。

## 应用入口

- FastAPI 应用：`app.web.main:app`
- 首页：`/`
- 健康检查：`/health`
- API 文档：`/docs`
- 默认局域网地址：`http://localhost:8080`

## 本地 Python 启动

环境要求：

- Python 3.11
- FFmpeg 和 ffprobe
- aria2c
- 可选：`bdpan`

安装依赖并启动：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.web.main:app --host 0.0.0.0 --port 8000
```

## Docker Compose 部署

项目使用根目录的 `docker-compose.yml`：

- `app`：FastAPI 视频处理服务，容器内监听 `8000`。
- `gpu-detector`：人物检测服务，容器内监听 `8100`。
- `nginx`：反向代理，对外暴露 `LAN_PORT`，默认 `8080`。
- `./storage:/app/storage`：持久化上传视频、临时片段和最终成品。
- `./.env:/app/.env:ro`：向应用容器提供运行配置。

启动：

```powershell
docker compose up -d --build
```

查看状态：

```powershell
docker compose ps
```

查看日志：

```powershell
docker compose logs -f app
docker compose logs -f gpu-detector
```

重建并重启应用：

```powershell
docker compose build app
docker compose up -d --no-deps --force-recreate app
```

健康检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost:8080/health
```

预期返回 HTTP 200 和类似内容：

```json
{"status":"ok","service":"coze-video-editor"}
```

## 环境变量

复制 `.env.example` 为 `.env` 后按部署环境填写：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `APP_HOST` | `0.0.0.0` | 应用监听地址 |
| `APP_PORT` | `8000` | FastAPI 容器监听端口 |
| `LAN_PORT` | `8080` | Nginx 对外访问端口 |
| `STORAGE_DIR` | `./storage` | 视频和成品存储目录 |
| `PUBLIC_BASE_URL` | 空 | 需要公网访问时配置，不以 `/` 结尾 |
| `BAIDU_APP_KEY` | 空 | 百度开放平台 App Key；与 Secret 同时配置才启用官方 API |
| `BAIDU_APP_SECRET` | 空 | 百度开放平台 Secret Key |
| `BAIDU_APP_NAME` | `bdpan` | 百度开放平台应用目录名 |
| `BAIDU_OAUTH_REDIRECT_URI` | `oob` | 百度 OAuth 回调地址 |
| `BAIDU_ARIA2_CONNECTIONS` | `16` | 百度官方 dlink 直连连接数；应用当前最多使用 16 个 |
| `BAIDU_ARIA2_SPLIT` | `16` | 百度官方直连分片数；应用当前不超过连接数；过高可能触发限流 |
| `BAIDU_ARIA2_MIN_SPLIT_SIZE` | `1M` | aria2 最小分片大小 |
| `GPU_DETECTOR_URL` | Docker 内部地址 | 人物识别服务地址 |
| `DISABLE_HWACCEL` | `0` | 设置为 `1` 强制 FFmpeg 使用 CPU |
| `SANDBOX_ENABLED` | `false` | 是否启用 FFmpeg 沙箱；Docker 调试时通常关闭 |
| `SANDBOX_CPU_LIMIT` | `2.0` | 单任务 CPU 限额 |
| `SANDBOX_MEM_LIMIT` | `4g` | 单任务内存限额 |
| `SANDBOX_TIMEOUT` | `1800` | 单任务超时时间，单位秒 |
| `DEFAULT_SEGMENT_SECONDS` | `900` | 默认窗口时长 |
| `MAX_VIDEO_SECONDS` | `9000` | 单视频最大时长 |
| `MAX_UPLOAD_MB` | `8192` | 单视频最大体积 |

## 百度网盘下载说明

### 官方 API 模式

同时配置 `BAIDU_APP_KEY` 和 `BAIDU_APP_SECRET` 时：

1. 应用通过百度开放 API 获取文件信息和 `dlink`。
2. 应用服务器启动 `aria2c`，直接连接百度下载节点/CDN。
3. 视频下载到服务器的 `storage/raw` 后再交给 FFmpeg 处理。
4. 浏览器不会直接下载百度链接，视频处理必须先在服务器落盘。
5. 单个文件默认使用 16 个连接和分片（代码上限 16），并启用断点续传、连接复用、重试和禁用预分配；如果出现 EOF、超时或限流，可将并发降回 4-8。

### 回退模式

未配置官方 API 凭证时使用 `bdpan` CLI 下载。并发和速度由 `bdpan` 自身配置及百度账号状态决定，应用不会把浏览器作为百度下载代理。

## 封面片头规则

- 成品组首次出现在合并选择区时，系统自动选择当前播放器时间；如果当前时间不在该成品片段内，则默认使用该成品第一个片段的起始帧。
- 用户可以在播放器或时间条定位后，点击对应成品盒的“使用当前画面”覆盖默认值。
- 每个成品组必须有且只能有一个封面时间，且封面时间必须来自该成品包含的片段。
- 最终封面片头为 `3840×2880`、4:3、2 秒静帧，并写入视频开头。
- 封面片头使用静音，之后接入正常视频内容。

## GPU 与 CPU 部署

### GPU 电脑配置要求

人物识别模式需要独立的 `gpu-detector` 服务。当前 GPU 镜像基于 CUDA 12.6，并安装 PyTorch `2.7.1+cu126`、YOLO11n、YuNet 和 SFace 模型。宿主电脑建议使用以下配置：

| 项目 | 最低要求 | 推荐配置 | 说明 |
|---|---|---|---|
| 操作系统 | 64 位 Windows 10/11、Ubuntu 22.04 或兼容 Linux | Ubuntu 22.04/24.04 或 Windows 11 + WSL2 | Docker Desktop 用户建议启用 WSL2 后端 |
| CPU | 4 核 | 8 核或以上 | 负责视频解码、文件处理和 Web 服务 |
| 内存 | 16 GB | 32 GB 或以上 | 人物检测、FFmpeg 和 Docker 同时运行时需要较大余量 |
| NVIDIA GPU | 支持 CUDA 的 NVIDIA GPU，显存至少 4 GB | NVIDIA RTX 系列，显存 8 GB 或以上 | 显存越大越适合高分辨率视频和多任务处理 |
| NVIDIA 驱动 | 支持 CUDA 12.6 的驱动 | 使用 NVIDIA 官方最新稳定驱动 | 宿主驱动版本必须兼容 CUDA 12.6 Runtime |
| Docker | Docker Engine 或 Docker Desktop，支持 Compose v2 | Docker Desktop + WSL2 或原生 Linux Docker | 必须支持 NVIDIA Container Runtime |
| 磁盘 | 可用空间至少 30 GB | SSD，可用空间 100 GB 以上 | 模型、镜像、原视频、临时片段和成品都会占用空间 |
| 网络 | 可访问镜像和模型下载地址 | 稳定宽带或局域网 | 首次构建需下载 CUDA、PyTorch 和模型层 |

### NVIDIA 软件环境

GPU 部署还需要：

1. 安装与 CUDA 12.6 Runtime 兼容的 NVIDIA 显卡驱动。
2. 安装 Docker Desktop 或 Docker Engine。
3. Linux 安装 NVIDIA Container Toolkit；Windows 使用 Docker Desktop 时启用 WSL2 和 GPU 支持。
4. 确认 Docker 可以识别 GPU：

```powershell
docker run --rm --gpus all nvidia/cuda:12.6.2-runtime-ubuntu22.04 nvidia-smi
```

如果命令能够列出显卡型号、驱动版本和显存，说明 Docker GPU 通道基本可用。

Compose 配置会为 `app` 和 `gpu-detector` 使用 NVIDIA Runtime，并设置：

- `NVIDIA_VISIBLE_DEVICES=all`
- `NVIDIA_DRIVER_CAPABILITIES=all`（应用容器）
- `NVIDIA_DRIVER_CAPABILITIES=compute,utility`（检测容器）
- CUDA Runtime：`12.6.2`
- GPU 检测端口：容器内 `8100`

启动 GPU 服务：

```powershell
docker compose up -d --build gpu-detector app nginx
docker compose ps
docker compose logs --tail 100 gpu-detector
```

### GPU 模式适用范围

- 人物识别由 `gpu-detector` 调用 YOLO、YuNet 和 SFace 完成。
- FFmpeg 视频编码优先尝试 NVIDIA NVENC；如果 NVENC 不可用，会自动降级到 CPU `libx264`。
- `app` 与 `gpu-detector` 都需要使用同一套可用的 NVIDIA Docker GPU 配置。
- GPU 识别主要降低人物分析耗时，不代表百度网盘下载速度会自动提升。
- 高分辨率视频、4K 封面片头和多个并行任务会明显增加显存、内存和磁盘压力。

### 无 GPU 时的 CPU 方案

没有 NVIDIA GPU 或无法使用 NVIDIA Container Runtime 时：

- 普通手动剪辑仍可使用。
- 人物识别需要改为连接外部可用的 GPU 检测服务，并配置 `GPU_DETECTOR_URL`。
- 可设置 `DISABLE_HWACCEL=1` 强制 FFmpeg 使用 CPU 编码和软解。
- CPU 建议至少 8 核、16 GB 内存，并使用 SSD 临时目录。
- 关闭 GPU 服务时，不建议直接按当前 Compose 配置启动人物识别流程；应先确认业务只使用手动剪辑模式。

## 存储目录

- `storage/raw`：原始上传或百度网盘下载的视频。
- `storage/segments`：中间片段和预览文件。
- `storage/final`：最终独立 MP4 成品。
- `storage/frames`：分析帧和人物检测素材。
- `storage/portraits`：人物识别相关素材。
- `storage/baidu-auth`：百度授权配置和令牌，不应提交到 Git 或暴露给静态文件服务。

## 注意事项

- 不要把百度 App Secret、OAuth Token、飞书 Secret 或其他真实凭证写入源码、README 或 Git。
- `.env` 只保存在部署环境，提交代码时使用 `.env.example`。
- 生产环境建议限制 `storage` 访问权限，并为用户任务做归属校验。
- 视频处理需要足够磁盘空间，4K 封面片头会增加每个最终视频的编码时间和文件体积。
