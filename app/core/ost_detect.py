"""OST（片头片尾 / Opening / Ending Theme）检测模块。

检测策略：
1. 黑帧检测：全画面亮度均值 < 阈值的帧，通常是 OP/ED 的分隔标记；
2. 场景切换检测：用相邻帧灰度直方图差异（chi-square）找突变点，OP/ED 通常有明显边界；
3. 启发式：视频开头 5% 和结尾 5% 区域内的场景切换密集区，标记为 OP/ED 候选；
4. 移动横条检测：对画面上下 15% 区域计算水平方向光流，持续水平位移的条带
   （滚动字幕 / 新闻跑马灯）标记为 scrolling_bar。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)


def detect_scene_changes(video_path: Path, threshold: float = 30.0) -> List[float]:
    """检测视频中的场景切换时间点。

    对采样帧计算灰度直方图并归一化，用 chi-square 距离衡量相邻帧差异，
    差异超过 threshold 的位置视为场景切换。返回切换时间点（秒）列表。
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    # 每 0.5 秒采样一帧，兼顾精度与速度
    sample_step = max(1, int(round(fps * 0.5)))

    scene_times: List[float] = []
    prev_hist: Optional[np.ndarray] = None
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % sample_step == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
            cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
            if prev_hist is not None:
                diff = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CHISQR))
                if diff > threshold:
                    scene_times.append(frame_idx / fps if fps > 0 else 0.0)
            prev_hist = hist
        frame_idx += 1
    cap.release()
    return scene_times


def _detect_black_frames(video_path: Path, black_threshold: int, sample_step: int) -> List[float]:
    """检测黑帧（全画面亮度均值 < black_threshold）的时间点列表。"""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    black_times: List[float] = []
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % sample_step == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if float(gray.mean()) < black_threshold:
                black_times.append(frame_idx / fps if fps > 0 else 0.0)
        frame_idx += 1
    cap.release()
    return black_times


def detect_moving_bars(video_path: Path, max_samples: int = 30) -> List[dict]:
    """检测视频中的移动横条（滚动字幕 / 新闻跑马灯）。

    检测策略：均匀采样多帧，对画面上下 15% 区域分别计算相邻帧之间的
    Farneback 密集光流，取水平位移分量（u）的均值；若超过半数采样对在
    该区域出现持续水平位移（|mean_u| > 阈值），则判定为移动横条。

    返回 [{"y": int, "height": int, "type": "scrolling_bar", "confidence": float}, ...]
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0 or fps <= 0:
        cap.release()
        return []

    # 均匀采样 max_samples 帧，覆盖整个视频
    sample_count = min(max_samples, max(2, total_frames))
    sample_step = max(1, total_frames // sample_count)

    frames: List[np.ndarray] = []
    for i in range(sample_count):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i * sample_step)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue
        frames.append(frame)
    cap.release()

    if len(frames) < 2:
        return []

    h, w = frames[0].shape[:2]
    bar_h = max(1, int(h * 0.15))

    results: List[dict] = []
    # 顶部 15% 与底部 15% 两个候选区域
    regions = [("top", 0, bar_h), ("bottom", h - bar_h, h)]
    # 单帧对水平位移阈值（像素）：超过即视为该帧对存在水平移动
    move_pixel_threshold = 1.0
    # 持续移动比例阈值：超过该比例才判定为滚动横条
    move_ratio_threshold = 0.5

    for _name, y_start, y_end in regions:
        move_count = 0
        pair_count = 0
        for prev, curr in zip(frames, frames[1:]):
            prev_gray = cv2.cvtColor(prev[y_start:y_end], cv2.COLOR_BGR2GRAY)
            curr_gray = cv2.cvtColor(curr[y_start:y_end], cv2.COLOR_BGR2GRAY)
            # Farneback 密集光流：返回 (H, W, 2)，最后一维 0=水平 1=垂直
            flow = cv2.calcOpticalFlowFarneback(
                prev_gray, curr_gray, None,
                pyr_scale=0.5, levels=3, winsize=15,
                iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
            )
            mean_u = float(flow[..., 0].mean())
            pair_count += 1
            if abs(mean_u) > move_pixel_threshold:
                move_count += 1

        if pair_count == 0:
            continue
        move_ratio = move_count / pair_count
        if move_ratio >= move_ratio_threshold:
            confidence = min(0.95, 0.4 + move_ratio * 0.5)
            results.append({
                "y": int(y_start),
                "height": int(bar_h),
                "type": "scrolling_bar",
                "confidence": float(confidence),
            })

    return results


def detect_ost(video_path: Path) -> List[dict]:
    """检测视频中的片头（OP）/片尾（ED）段落。

    返回 [{"start_sec": float, "end_sec": float, "type": "op"|"ed", "confidence": float}, ...]

    检测流程：
    1. 场景切换检测 + 黑帧检测；
    2. 开头 5% 区域场景切换密集 → OP 候选；
    3. 结尾 5% 区域场景切换密集 → ED 候选；
    4. 用黑帧作为分隔标记修正边界。
    """
    if not settings.ost_detect_enabled:
        return []

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0.0
    cap.release()

    if duration <= 0:
        return []

    black_threshold = settings.ost_black_threshold
    scene_threshold = settings.ost_scene_threshold
    sample_step = max(1, int(round(fps * 0.5)))

    scene_changes = detect_scene_changes(video_path, threshold=scene_threshold)
    black_times = _detect_black_frames(video_path, black_threshold, sample_step)

    results: List[dict] = []
    opening_cutoff = duration * 0.05
    ending_start = duration * 0.95

    # ---------- OP 候选：开头 5% 区域 ----------
    op_scenes = [t for t in scene_changes if t <= opening_cutoff]
    if len(op_scenes) >= 2:
        # 场景切换越密集，置信度越高
        confidence = min(0.95, 0.4 + 0.15 * len(op_scenes))
        start = 0.0
        # OP 结束边界：优先用该区域内的黑帧（分隔标记），否则用最后一个场景切换
        early_blacks = [t for t in black_times if t <= opening_cutoff]
        if early_blacks and early_blacks[-1] > op_scenes[0]:
            end = early_blacks[-1]
        else:
            end = op_scenes[-1]
        if end > start:
            results.append({
                "start_sec": float(start),
                "end_sec": float(end),
                "type": "op",
                "confidence": float(confidence),
            })

    # ---------- ED 候选：结尾 5% 区域 ----------
    ed_scenes = [t for t in scene_changes if t >= ending_start]
    if len(ed_scenes) >= 2:
        confidence = min(0.95, 0.4 + 0.15 * len(ed_scenes))
        # ED 起始边界：优先用该区域内的黑帧（分隔标记），否则用第一个场景切换
        late_blacks = [t for t in black_times if t >= ending_start]
        if late_blacks and late_blacks[0] < ed_scenes[-1]:
            start = late_blacks[0]
        else:
            start = ed_scenes[0]
        end = duration
        if end > start:
            results.append({
                "start_sec": float(start),
                "end_sec": float(end),
                "type": "ed",
                "confidence": float(confidence),
            })

    # 合并移动横条（滚动字幕 / 新闻跑马灯）检测结果
    try:
        results.extend(detect_moving_bars(video_path))
    except Exception as e:
        logger.warning("移动横条检测失败: %s", e)

    return results
