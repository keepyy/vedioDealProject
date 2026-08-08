"""OpenCV 检测器：自动识别视频中的横幅（字幕条）和静态标识（台标/水印）。

策略：
- 横幅检测：每隔若干帧采样，在画面上方/下方 15% 区域寻找灰度高方差+边缘密集的水平条带；
- 台标/标识检测：对多张采样帧做差分，统计长期稳定低方差区域，聚类后得到矩形。

识别结果允许前端人工调整（前端拿到的是一个矩形列表）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from app.config import settings
from app.core.ffmpeg_proc import probe_video, extract_frame

logger = logging.getLogger(__name__)


@dataclass
class DetectionResult:
    banner_region: Optional[Tuple[int, int, int, int]]  # x,y,w,h
    logo_regions: List[Tuple[int, int, int, int]]
    sample_frame: str  # 用于前端展示的帧路径
    width: int
    height: int


def _read_image(png: Path) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(str(png), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"无法读取图片: {png}")
    return img


# -------------------- 横幅检测 --------------------

def detect_banner(frame: np.ndarray, scan_ratio: float = 0.15) -> Optional[Tuple[int, int, int, int]]:
    """在单帧中检测横幅（顶部或底部带文字的水平条带）。

    返回 (x, y, w, h)，若没检测到则返回 None。
    """
    h, w = frame.shape[:2]
    scan_h = max(20, int(h * scan_ratio))
    candidates = []

    for region_name, y_start in (("top", 0), ("bottom", h - scan_h)):
        roi = frame[y_start:y_start + scan_h, 0:w]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        # 横幅/字幕条里通常有横向排列的文字 → 文字有竖直边缘（Sobel x）+ 水平边缘（Sobel y）
        # 两者取 L2 作为边缘强度，鲁棒性更强
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        mag = cv2.convertScaleAbs(cv2.magnitude(gx, gy))
        # 对每行求和，文字密集的行会明显高于背景
        row_profile = mag.sum(axis=1).astype(np.float64)
        if row_profile.max() < 500:
            continue
        row_profile = cv2.GaussianBlur(row_profile.reshape(-1, 1), (1, 7), 0).flatten()
        mean, std = row_profile.mean(), row_profile.std()
        # 用均值 + 0.3 std 作为弱阈值：字幕条边缘密度通常明显高于噪点
        threshold = mean + 0.3 * max(1e-6, std)
        mask = (row_profile > threshold).astype(np.uint8)

        # 找到最长连续段；最小长度放宽到 4
        best_len, best_start, cur_len, cur_start = 0, 0, 0, 0
        for i, v in enumerate(mask):
            if v:
                if cur_len == 0:
                    cur_start = i
                cur_len += 1
                if cur_len > best_len:
                    best_len, best_start = cur_len, cur_start
            else:
                cur_len = 0
        if best_len >= 4:
            end = best_start + best_len
            band_y = y_start + best_start
            band_h = max(best_len, 10)
            # 向外各扩展 3 像素做安全边界
            band_y = max(0, band_y - 3)
            band_h = min(h - band_y, band_h + 6)
            candidates.append((0, band_y, w, band_h))

    if not candidates:
        return None
    # 选取"每行边缘强度总和 × 高度"加权最大的候选，即显著度最高
    def _score(c):
        x, y, ww, hh = c
        roi = frame[y:y + hh, x:x + ww]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        mag = cv2.magnitude(gx, gy)
        return float(mag.sum()) * hh
    candidates.sort(key=_score, reverse=True)
    return candidates[0]


# -------------------- 台标 / 水印检测 --------------------

def detect_logos(
    sample_frames: List[np.ndarray],
    static_threshold: float = 8.0,
    exclude_region: Optional[Tuple[int, int, int, int]] = None,
) -> List[Tuple[int, int, int, int]]:
    """对多帧做差分，返回长期稳定的矩形区域（候选台标/水印）。"""
    if len(sample_frames) < 2:
        return []
    h, w = sample_frames[0].shape[:2]
    # 统一尺寸（取第一张尺寸）并转为 float 灰度
    stack = np.stack([
        cv2.cvtColor(cv2.resize(f, (w, h)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        for f in sample_frames
    ], axis=0)
    var_map = stack.var(axis=0)

    # 低方差区域 => 静态
    static_mask = (var_map < static_threshold).astype(np.uint8)

    # 去除文本扫描线噪声：先做形态学闭运算再开运算
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3))
    static_mask = cv2.morphologyEx(static_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    static_mask = cv2.morphologyEx(static_mask, cv2.MORPH_OPEN, kernel, iterations=1)

    # 排除横幅区域，避免和 logo 区域合并导致 logo 被误判为大横条
    if exclude_region:
        rx, ry, rw, rh = exclude_region
        if rw > 0 and rh > 0:
            # 判定 banner 在顶部还是底部，只朝该方向扩展到画面边缘（避免覆盖掉另一侧的台标）
            is_top = ry + rh / 2 < h / 2
            pad = int(h * 0.05)
            if is_top:
                y0 = 0
                y1 = min(h, ry + rh + pad)
            else:
                y0 = max(0, ry - pad)
                y1 = h
            static_mask[y0:y1, :] = 0
            # exclude 后再做一次小开运算，断开残余尾巴
            kernel2 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            static_mask = cv2.morphologyEx(static_mask, cv2.MORPH_OPEN, kernel2, iterations=1)

    # 画面中央大片区域（例如场景背景）也要被排除：移除占比过大的连通域
    contours, _ = cv2.findContours(static_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    results: List[Tuple[int, int, int, int]] = []
    img_area = w * h
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        area = bw * bh
        # 过滤：面积过小/过大，或极度扁平/细长，或几乎横跨整屏（通常是字幕带）
        if area < 80 or area > img_area * 0.2:
            continue
        if bw >= w * 0.7:  # 宽度超过 70% 画面宽度 → 字幕带/扫描带，排除
            continue
        ratio = max(bw, bh) / max(1, min(bw, bh))
        if ratio > 15:
            continue
        results.append((x, y, bw, bh))

    # 按面积降序，最多返回 4 个
    results.sort(key=lambda r: r[2] * r[3], reverse=True)
    return results[:4]


# -------------------- 主入口 --------------------

def detect_video(video_path: Path) -> DetectionResult:
    info = probe_video(video_path)
    storage = settings.storage_path
    frames_dir = storage / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    sample_n = settings.logo_sample_frames
    sample_points = [
        max(0.0, min(info.duration - 0.1, (i + 1) * info.duration / (sample_n + 1)))
        for i in range(sample_n)
    ]
    images: List[np.ndarray] = []
    for i, t in enumerate(sample_points):
        p = frames_dir / f"{video_path.stem}_{i:03d}.png"
        if not p.exists():
            try:
                extract_frame(video_path, p, t)
            except Exception as e:  # pragma: no cover
                logger.warning("取样帧失败 t=%.1fs: %s", t, e)
                continue
        try:
            images.append(_read_image(p))
        except Exception:
            continue

    if not images:
        raise RuntimeError("视频抽帧失败，无法检测横幅和标识")

    main_frame = images[len(images) // 2]
    banner = detect_banner(main_frame, scan_ratio=settings.banner_scan_ratio)
    logos = detect_logos(images, static_threshold=settings.logo_static_threshold, exclude_region=banner)

    main_png = frames_dir / f"{video_path.stem}_main.png"
    cv2.imwrite(str(main_png), main_frame)

    return DetectionResult(
        banner_region=banner,
        logo_regions=logos,
        sample_frame=str(main_png),
        width=info.width,
        height=info.height,
    )
