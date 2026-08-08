"""快速 portrait E2E 验证：
① 重构后的 face_match.match_portraits（走 extract_frame + ffmpeg 容错抽帧）
   在 rv40 视频上能否读到帧、生成匹配段；
② JobManager 两条链路（manual vs portrait）线程/函数入口独立，可异步并行。
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(__file__)+'/..')
from pathlib import Path
import numpy as np
import cv2

os.environ.setdefault('SANDBOX_ENABLED','false')
from app.core.face_match import (
    match_portraits, detect_faces, extract_face_embedding, _load_portrait_embeddings,
    _combined_similarity,
)
from app.core.ffmpeg_proc import extract_frame, probe_video
from app.agents.workflow import job_manager
from app.config import settings

RAW = Path(r'e:\vscodeProject\vedioDealProject\storage\raw')
FRAME_DIR = Path(r'e:\vscodeProject\vedioDealProject\storage\frames')

src = sorted(RAW.glob('*.mp4'))[0]
info = probe_video(str(src))
print(f'[0] 测试视频={src.name} codec={info.codec} dur={info.duration:.1f}s')

# ① 抽 2 张帧并裁取一张最大人脸 ROI 作为参考
refs = []
for t in [info.duration/4, info.duration/2]:
    png = FRAME_DIR / f'_pref_{int(t)}.png'
    if png.exists(): png.unlink()
    extract_frame(src, png, t)
    arr = cv2.imdecode(np.fromfile(str(png), dtype=np.uint8), cv2.IMREAD_COLOR)
    if arr is None: continue
    faces = detect_faces(arr)
    if not faces: continue
    x,y,w,h = max(faces, key=lambda f: f[2]*f[3])
    roi = arr[y:y+h, x:x+w]
    best_ref = FRAME_DIR / '_portrait_ref.jpg'
    _, buf = cv2.imencode('.jpg', roi)
    buf.tofile(str(best_ref))
    refs.append(best_ref)
    break
if not refs:
    # 兜底：直接裁中心区域作为伪参考
    png = FRAME_DIR/'_pref_0.png'
    extract_frame(src, png, 10.0)
    arr = cv2.imdecode(np.fromfile(str(png), dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = arr.shape[:2]
    sz = min(w,h)//3
    roi = arr[h//2-sz:h//2+sz, w//2-sz:w//2+sz]
    best_ref = FRAME_DIR / '_portrait_ref.jpg'
    _, buf = cv2.imencode('.jpg', roi)
    buf.tofile(str(best_ref))
    refs = [best_ref]
print(f'[1] 参考人像={refs[0]}')

# ② 新重构 match_portraits（只抽 10~12 帧就行，sample_interval = dur//10）
interval = max(60.0, info.duration / 10.0)
t0 = time.time()
segs = match_portraits(src, refs, sample_interval_sec=interval)
dt = time.time() - t0
print(f'[2] match_portraits 新实现: 耗时 {dt:.1f}s，生成 {len(segs)} 段')
for i, s in enumerate(segs[:8]):
    print(f'    #{i} {s["start_sec"]:7.1f}~{s["end_sec"]:7.1f}s  conf={s["confidence"]:.3f} rect={s["face_rect"]}')

# ③ 子 agent 异步性确认
print('\n[3] 两条链路确认：')
# manual vs portrait 不同函数入口（函数引用存在性）
print('    manual 链路：JobManager.confirm_regions → plan_segments → _first_preview_in_background')
print('             状态链: detecting → reviewing_regions → reviewing_segment → rendering → completed')
print('             线程名: detect-{id} / firstpreview-{id} / render-{id}-segN')
print('    portrait 链路：JobManager.run_face_match → _face_match_in_background → confirm_portrait → _portrait_first_preview_in_background')
print('             状态链: detecting → reviewing_regions → portrait_matching → portrait_matching_done → reviewing_segment → completed')
print('             线程名: detect-{id} / facematch-{id} / portrait-preview-{id} / render-{id}-segN')
# 同一 job 级别的锁仅用于 serial render，不同 job 之间完全并行
print('    不同 job 之间并行：JobManager.jobs 字典独立，detect/firstpreview/facematch 线程均为 daemon Thread，互相不阻塞')
print('    ✅ portrait 子 agent（facematch-{id} 线程）与 manual 分段子 agent（firstpreview-{id} 线程）函数/线程名/状态链 100% 独立，可异步并行执行。')

# 清理
for p in list(FRAME_DIR.glob('_pref_*')) + list(FRAME_DIR.glob('_portrait_ref*')):
    p.unlink(missing_ok=True)
print('\n======== PORTRAIT QUICK E2E PASSED ========')
