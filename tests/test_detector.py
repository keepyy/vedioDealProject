import sys, os
sys.path.insert(0, 'e:/vscodeProject/vedioDealProject')

# 把 opencvstudy 的 numpy/cv2 也用上，同时手动 import 自定义模块
import numpy as np, cv2

# 准备一张假的 1920x1080 画面：
# - 底部模拟横幅条（980-1080 行），填充 240 加竖黑条（模拟字幕边缘）
# - 左上角模拟固定矩形 logo (30-90 行, 30-250 列)
# - 右上角模拟固定矩形 logo (40-100 行, 1600-1900 列)
rng = np.random.RandomState(42)
H, W = 1080, 1920
img = rng.randint(30, 180, (H, W, 3), dtype=np.uint8)
# 底部横幅（980..1080 行：填充 240 底色 + 竖黑条作为文字边缘）
img[980:1080, :] = 240
for x in range(0, W, 8):
    img[1000:1070, x:x+3] = 20
# 左上角 logo 矩形（恒定灰色 128）
img[30:90, 30:250] = 128
# 右上角 logo
img[40:100, 1600:1900] = 200

# 保存到临时图片（不调用 ffmpeg 抽帧，直接喂给 detect_banner）
tmp_dir = 'e:/vscodeProject/vedioDealProject/storage/frames'
os.makedirs(tmp_dir, exist_ok=True)
tmp_path = os.path.join(tmp_dir, 'synthetic_test.png')
cv2.imwrite(tmp_path, img)

# 直接导入 detector （去掉对 settings/probe_video 的引用，仅测试算法函数）
raw = open('app/core/detector.py', encoding='utf-8').read()
for rem in ['from __future__ import annotations',
            'from app.config import settings',
            'from app.core.ffmpeg_proc import probe_video, extract_frame']:
    raw = raw.replace(rem, '')
import importlib.util
spec = importlib.util.spec_from_loader('detector_test', loader=None)
m = importlib.util.module_from_spec(spec)
exec(raw, m.__dict__)

frame_read = cv2.imread(tmp_path)
# Debug: print row profiles for top/bottom scan regions
import math
scan_h = max(20, int(frame_read.shape[0] * 0.15))
import numpy as np2
for region_name, y_start in (('top', 0), ('bottom', frame_read.shape[0] - scan_h)):
    roi = frame_read[y_start:y_start+scan_h]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    mag = cv2.convertScaleAbs(cv2.magnitude(gx, gy))
    rp = mag.sum(axis=1)
    thr = rp.mean() + rp.std() * 0.8
    print(f'{region_name}[y={y_start}:{y_start+scan_h}] max={rp.max()} mean={rp.mean():.1f} std={rp.std():.1f} thr={thr:.1f}')
    # Show rows above threshold with consecutive count >=8
    mask = (rp > thr).astype(np2.uint8)
    # find consecutive 1 runs
    runs=[]; cur=0; cs=0
    for i,v in enumerate(mask):
        if v:
            if cur==0: cs=i
            cur+=1
        else:
            if cur>0: runs.append((cs,cur)); cur=0
    if cur>0: runs.append((cs,cur))
    runs8 = [r for r in runs if r[1]>=8]
    print(f'  runs>=8: {runs8[:5]}')

banner = m.detect_banner(frame_read, scan_ratio=0.15)
print('BANNER =', banner)
assert banner is not None, '底部横幅未检测到'
x, y, w, h = banner
assert w == W
# 真实底部横幅在 y=980 开始，高 100
assert y >= 900 and h >= 50, f'banner 区域范围不对 {banner}'
# overlap 检查：真实带 y=[980,1080]，识别带 [y, y+h]
overlap = max(0, min(y+h, H) - max(y, 980))
real_h = 100
assert overlap / real_h >= 0.4, f'banner 与真实带重叠不够 {overlap}'

sample_frames = []
for i in range(15):
    # 每帧：背景重新随机，左上角 logo (30:90, 30:250) 和右上角 logo (40:100,1600:1900) 保持不变
    f = rng.randint(30, 180, (H, W, 3), dtype=np.uint8)
    f[30:90, 30:250] = 128
    f[40:100, 1600:1900] = 200
    # 底部横幅（不同帧略有变化）
    f[980:1080, :] = 240
    for x0 in range(0, W, 8 + (i % 3)):
        f[1000:1070, x0:x0+3] = 20 + (i % 7) * 5
    sample_frames.append(f)
logos = m.detect_logos(sample_frames, static_threshold=8.0, exclude_region=banner)
print('LOGOS =', logos)
assert len(logos) >= 1, f'至少应检测到 1 个 logo，实际 {len(logos)}'

print('DETECTOR_OK')
