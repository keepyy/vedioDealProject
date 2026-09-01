# Debug: Aria2 stuck at 99%

Status: [OPEN]
Session: aria2-stuck-99

## Symptom

任务 3f423fb19fb8 的目标文件字节数达到总大小，但 Aria2 进程与 `.aria2` 控制文件持续存在；后端显示 99%，前端进度条显示 0%。

## Hypotheses

1. `.aria2` 位图仍有未完成分片，目标文件可能是预分配文件。
2. 百度服务器对 8 路 Range 请求支持不稳定，部分连接持续重试。
3. Aria2 数据已完成，但输入队列或连接未结束，进程无法退出。
4. 前端读取了错误的进度字段，导致后端 99% 而进度条 0%。
5. Aria2 错误仅写入匿名临时日志，Docker 日志无法显示根因。

## Evidence

- Aria2 临时日志显示真实进度为 `197MiB/522MiB (37%)`，仍以约 70–100 KiB/s 下载，并有 6 条活动连接。
- 目标文件 `st_size=547917325` 是 Aria2 预分配后的最终长度，不能表示已下载字节数；`.aria2` 控制文件仍存在。
- 后端按 `target.stat().st_size` 计算，因预分配立即得到 100%，再被限制为 99%。
- 前端只按输出片段数量计算进度；下载阶段输出片段为 0，所以始终显示 0%。

## Fix

- 后端官方 Aria2 模式改为解析 Aria2 自身进度日志中的已完成字节数，不再使用预分配文件长度。
- 前端上传、百度下载、链接导入阶段优先显示 `render_progress.percent`。

## Verification

- Pending post-fix deployment and task verification.
