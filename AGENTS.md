# 茶辑 · Coze 智能视频剪辑应用

## 项目定位

这是一个可从 GitHub 导入扣子编程（Coze Code）的 Python Web 智能视频剪辑应用。用户通过网页上传视频，在统一编辑页完成人物片段选择、文字 Logo、蒙版模糊和分割区间编辑，最终一次确认并批量生成独立的 4:3 MP4 成品。

本项目不依赖飞书，不包含消息机器人、Webhook 或飞书凭证。

## 应用入口

- FastAPI 应用：`app.web.main:app`
- 本地启动：`uvicorn app.web.main:app --host 0.0.0.0 --port 8000`
- 部署启动：`uvicorn app.web.main:app --host 0.0.0.0 --port $PORT`
- Coze 默认构建入口：根目录 `Dockerfile`（CPU 兼容部署）
- 首页：`/`
- 健康检查：`/health`
- API 文档：`/docs`

## 核心流程

1. 用户上传视频并选择普通剪辑或人物识别模式。
2. 后台读取视频信息并按固定时长规划窗口。
3. 普通模式直接进入统一编辑页；人物模式先发现人物，再进入同一编辑页。
4. 用户在视频画面上添加文字 Logo、手动蒙版模糊区域，并编辑每个窗口的保留区间。
5. 点击片段播放时，共用的 4:3 播放器悬浮在当前操作区域上方。
6. 最终确认后一次性批量渲染所有有效区间，每个区间输出一个独立 4:3 MP4。
7. 成品列表使用 4:3 播放器展示和下载视频。

## 主要模块

- `app/web/main.py`：FastAPI 路由和页面流程
- `app/agents/workflow.py`：任务状态、人物分析和批量渲染编排
- `app/core/ffmpeg_proc.py`：FFmpeg 剪辑、文字 Logo、蒙版和 4:3 输出
- `app/core/face_match.py`：人物发现和匹配
- `app/web/templates/portrait_results.html`：统一单页编辑器
- `app/web/templates/done.html`：成品列表

## 运行依赖

- Python 3.11
- FFmpeg 和 ffprobe
- Python 依赖见 `requirements.txt`
- 存储目录由 `STORAGE_DIR` 指定，默认 `./storage`
- 服务端口优先读取平台提供的 `PORT`

## GPU 与 Coze 部署

人物识别可通过 `GPU_DETECTOR_URL` 调用独立 GPU 检测服务。本地完整 GPU 部署使用 `docker-compose.yml`。如果 Coze 运行环境没有 NVIDIA GPU 或无法启动 Compose 辅助服务，应将普通手动剪辑作为默认可用流程；人物识别需要额外部署 `gpu_service` 并配置 `GPU_DETECTOR_URL`。

FFmpeg 渲染可设置 `DISABLE_HWACCEL=1` 强制使用 CPU，适用于没有 NVIDIA 编码器的 Coze 运行环境。

## 修改约束

- 保持 Logo 为文字叠加，不恢复自动 Logo 识别。
- 蒙版区域由用户手动添加，并必须写入最终视频。
- 预览和成品均保持 4:3；原画面等比例适配，不拉伸。
- 所有窗口在同一页面编辑，只进行一次最终确认。
- 每个有效区间生成独立视频，不合并成一个文件。
- 不添加飞书配置、飞书 SDK、飞书路由或飞书推送。
