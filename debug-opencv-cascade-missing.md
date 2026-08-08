# Debug Session: opencv-cascade-missing

Status: [OPEN]

## Symptom

人物匹配失败：`module 'cv2' has no attribute 'CascadeClassifier'`。

## Hypotheses

1. 容器安装了错误或不完整的 OpenCV wheel。
2. 项目文件遮蔽了官方 `cv2` 模块。
3. OpenCV 循环导入导致模块未完整初始化。
4. 镜像缓存或依赖冲突造成运行版本偏差。
5. OpenCV 安装文件损坏或被后续依赖覆盖。

## Evidence

- 用户提供运行时错误：`module 'cv2' has no attribute 'CascadeClassifier'`。

## Progress

- [x] 建立调试会话
- [ ] 添加最小运行时探针
- [ ] 复现并收集证据
- [ ] 最小修复
- [ ] 修复后验证
- [ ] 用户确认
- [ ] 清理调试产物
