# Debug Session: video-compose-exit

- Status: [OPEN]
- Symptom: 选择时间片段合成视频时任务失败，FFmpeg 命令退出码 1，页面仅显示编码进度尾部。
- Session ID: `video-compose-exit`

## Hypotheses

1. 并行 FFmpeg 任务争用资源导致进程失败。
2. 选区边界无效或超出实际视频时长。
3. Logo/蒙版滤镜参数在特定视频上失败。
4. 临时片段参数不一致导致 concat 失败。
5. SandboxError 截断 stderr，真实错误被进度文本覆盖。

## Evidence

- 失败任务：`08d9ae008265`。
- FFmpeg 错误：`av_interleaved_write_frame(): Input/output error`。
- 失败文件：`seg00_group02_part00.part.mp4` 与 `seg00_group02_part01.part.mp4`。
- 容器 `df -h /app/storage`：E 盘 241G 已用 241G，可用 52K，使用率 100%。
- 结论：磁盘空间耗尽导致 FFmpeg 无法写入，不是时间边界、滤镜或 concat 错误。

## Fix

1. 渲染前检查存储空间并返回明确错误。
2. 渲染失败时清理当前任务的 `.part.mp4` 和临时分段。
3. 清理此次失败遗留的两个 `.part.mp4`。
4. 首页上传来源改为标签切换。

## Verification

- 清理历史 `raw`、`segments`、`frames` 后，E 盘可用空间由 52KB 增至约 58.5GB。
- 保留 `storage/final` 全部最终成品。
- Python 编译与 12 项工作流测试通过。
- Docker 镜像重新构建成功，app healthy，首页 HTTP 200。
- 首页已确认包含 `上传视频 / 视频链接` 标签切换。
- 渲染前空间检查与失败临时文件清理已部署。
- 等待用户使用新视频进行最终确认。
