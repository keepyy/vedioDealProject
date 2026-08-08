# Debug Session: trailing-segment-corruption
- **Status**: [OPEN]
- **Issue**: 固定时长批量剪辑时，前段正常，最后不足固定时长的尾段可能生成乱码视频或失败。
- **Debug Server**: 待启动
- **Log File**: .dbg/trae-debug-log-trailing-segment-corruption.ndjson

## Reproduction Steps
1. 上传约 7 分钟视频。
2. 选择固定 3 分钟分割。
3. 在统一剪辑页确认三个固定窗口并批量生成。
4. 检查前两个三分钟输出与最后约一分钟输出。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | 尾段终点超过真实视频时长 | High | Low | Pending |
| B | 尾段 seek/duration 计算导致空文件或缺流 | High | Low | Pending |
| C | 无效输出仍被加入 final_outputs | Medium | Low | Pending |
| D | 源视频尾部时间戳或解码损坏 | Medium | Medium | Pending |
| E | GPU 编解码降级或音视频差异触发尾段失败 | Low | Medium | Pending |

## Log Evidence
- pre-fix 日志 1/3/5：三个窗口边界分别为 0-180、180-360、360-420，均未超过源时长 420 秒。
- pre-fix 日志 2/4/6：标准源生成的三个文件均为 H.264、320x240，时长分别为 180、180、60 秒。
- 历史成品扫描发现多个 0 字节 MP4，以及一个 45,875,248 字节但缺少 moov atom 的无效 MP4。

## Verification Conclusion
- A：Rejected，固定窗口边界算法正确。
- B：Rejected，标准源尾段 seek 正常。
- C：Confirmed，未完成或损坏的 MP4 会残留在最终输出路径，现有流程缺少发布前有效性校验和原子写入。
- D：Inconclusive/likely contributing，特定源尾部异常或处理中断可产生半成品。
- E：Rejected，CPU 路径同样参与复现验证，GPU 不是必要条件。
- 最小修复：临时文件编码、源时长边界钳制、时间戳容错、ffprobe 有效性校验、原子替换。

## Post-fix Evidence
- 同一 7 分钟源、3 分钟窗口再次完整批量生成，最终状态 completed，共 3 个输出。
- 输出时长分别为 180、180、60 秒，文件大小分别为 5,012,582、5,420,096、1,816,798 字节。
- 三个输出均通过 ffprobe 视频流与时长检查。
- 最终目录不存在 `.part.mp4`，说明只有校验成功的文件被原子发布。
- 专项回归测试 20 项全部通过，app 容器重建后 healthy。

## Status
等待用户确认实际问题视频是否已恢复；确认后清理调试采集代码、调试服务及会话文件。

