"""人像检测与匹配模块。

使用 OpenCV Haar Cascade 检测视频中的人脸，并与参考人像图片匹配。
特征融合：HSV 颜色直方图 + LBP（局部二值模式）纹理直方图，两者加权平均，
相比单一颜色直方图对人脸的区分度更高、对光照变化更鲁棒。

⚠ 关于视频帧读取：
  历史上使用 `cv2.VideoCapture(str(video))` 直接读取视频，
  但对于 rv40 / wmv3 / mpeg4 等老编解码器会频繁出现
  "Invalid decoder state: B-frame without reference data"，
  导致大量帧读取失败，最终匹配段数量为 0 或明显偏少。
  因此重构为：调用 app.core.ffmpeg_proc.extract_frame（已内置
  混合 seek + 容错参数，经 rv40 大规模验证通过）逐帧抽取为
  PNG，再由 OpenCV 读 PNG 做人脸检测与匹配，稳定性大幅提升。
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
import uuid
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import cv2
import httpx
import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# 人脸特征统一到固定尺寸，避免距离受检测框尺寸影响。
_FACE_SIZE = (96, 96)
_COLOR_WEIGHT: float = 0.25
_LBP_WEIGHT: float = 0.75
_COLOR_BINS: int = 32
_LBP_BINS: int = 32
_LBP_GRID = (4, 4)
_MAX_FACE_FEATURES = 3
_FACE_THUMBNAIL_SCALE = 1.7

# Haar Cascade 分类器（懒加载，避免每次调用都重新读取 xml）
_face_cascade: Optional[cv2.CascadeClassifier] = None


def _get_cascade() -> cv2.CascadeClassifier:
    """懒加载 Haar Cascade 正面人脸分类器。"""
    global _face_cascade
    if _face_cascade is None:
        xml_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(xml_path)
        if _face_cascade.empty():
            raise RuntimeError(f"无法加载 Haar Cascade 模型: {xml_path}")
    return _face_cascade


# -------------------- 人脸检测 --------------------

def detect_faces(frame: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """用 Haar Cascade 检测单帧中的人脸。

    返回 [(x, y, w, h), ...]，未检测到时返回空列表。
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # 直方图均衡化增强对比度，提升暗光下检测率
    gray = cv2.equalizeHist(gray)
    cascade = _get_cascade()
    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(30, 30),
    )
    if faces is None:
        return []
    return [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in faces]


# -------------------- 特征提取 --------------------

def _color_histogram(roi: np.ndarray) -> np.ndarray:
    """计算 ROI 的 HSV 颜色直方图并归一化（H-S 二维联合直方图）。"""
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [_COLOR_BINS, _COLOR_BINS], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return hist.flatten().astype(np.float32)


def _lbp_histogram(gray: np.ndarray) -> np.ndarray:
    """计算空间分块 LBP，保留五官纹理的大致位置。"""
    h, w = gray.shape[:2]
    feature_len = _LBP_BINS * _LBP_GRID[0] * _LBP_GRID[1]
    if h < 3 or w < 3:
        return np.zeros(feature_len, dtype=np.float32)
    center = gray[1:-1, 1:-1].astype(np.int32)
    lbp = np.zeros_like(center)
    neighbors = [
        gray[:-2, :-2], gray[:-2, 1:-1], gray[:-2, 2:], gray[1:-1, 2:],
        gray[2:, 2:], gray[2:, 1:-1], gray[2:, :-2], gray[1:-1, :-2],
    ]
    for bit, neighbor in enumerate(neighbors):
        lbp += (neighbor.astype(np.int32) >= center) * (1 << bit)
    parts = []
    for row in np.array_split(lbp, _LBP_GRID[0], axis=0):
        for cell in np.array_split(row, _LBP_GRID[1], axis=1):
            hist = cv2.calcHist([cell.astype(np.uint8)], [0], None,
                                [_LBP_BINS], [0, 256]).flatten().astype(np.float32)
            norm = float(np.linalg.norm(hist))
            parts.append(hist / norm if norm > 0 else hist)
    return np.concatenate(parts).astype(np.float32)


def extract_face_embedding(img: np.ndarray, face_rect: Tuple[int, int, int, int]) -> np.ndarray:
    """提取人脸区域特征向量（HSV 颜色直方图 + LBP 纹理直方图拼接）。

    face_rect 为 (x, y, w, h)。返回一维 float32 向量。
    """
    x, y, w, h = face_rect
    H, W = img.shape[:2]
    # 边界保护，确保 ROI 落在画面内
    x = max(0, min(x, W - 1))
    y = max(0, min(y, H - 1))
    w = max(1, min(w, W - x))
    h = max(1, min(h, H - y))
    roi = img[y:y + h, x:x + w]
    roi = cv2.resize(roi, _FACE_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.equalizeHist(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY))
    color_hist = _color_histogram(roi)
    lbp_hist = _lbp_histogram(gray)
    return np.concatenate([color_hist, lbp_hist]).astype(np.float32)


def _split_embedding(emb: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """把拼接的 embedding 拆成 (颜色直方图, LBP 直方图)。"""
    color_len = _COLOR_BINS * _COLOR_BINS
    lbp_len = _LBP_BINS * _LBP_GRID[0] * _LBP_GRID[1]
    flat = np.asarray(emb, dtype=np.float32).reshape(-1)
    if flat.size != color_len + lbp_len:
        raise ValueError(
            f"人脸特征长度无效: 期望 {color_len + lbp_len}，实际 {flat.size}"
        )
    return flat[:color_len], flat[color_len:]


def _hist_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """计算两个直方图的相关性相似度，结果裁剪到 [0, 1]。"""
    if a.size == 0 or b.size == 0:
        return 0.0
    # compareHist 要求两直方图形状一致，统一重塑为列向量
    sim = float(cv2.compareHist(
        a.reshape(-1, 1).astype(np.float32),
        b.reshape(-1, 1).astype(np.float32),
        cv2.HISTCMP_CORREL,
    ))
    # HISTCMP_CORREL 范围 [-1, 1]，负相关视为不相似
    return max(0.0, min(1.0, sim))


def _combined_similarity(emb_a: np.ndarray, emb_b: np.ndarray) -> float:
    """颜色直方图与 LBP 直方图加权平均相似度。"""
    color_a, lbp_a = _split_embedding(emb_a)
    color_b, lbp_b = _split_embedding(emb_b)
    color_sim = _hist_correlation(color_a, color_b)
    lbp_sim = _hist_correlation(lbp_a, lbp_b)
    return _COLOR_WEIGHT * color_sim + _LBP_WEIGHT * lbp_sim


# -------------------- 参考人像特征 --------------------

def _load_portrait_embeddings(portrait_images: List[Path]) -> List[np.ndarray]:
    """读取参考人像图片并提取特征。

    对每张人像图：优先检测其中最大的人脸并提取特征；若检测不到人脸，
    则把整张图视为人脸区域提取特征（兼容纯头像图）。
    """
    embeddings: List[np.ndarray] = []
    for pp in portrait_images:
        # 用 imdecode 避免中文路径问题
        img = cv2.imdecode(np.fromfile(str(pp), dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            logger.warning("无法读取人像图片: %s", pp)
            continue
        faces = detect_faces(img)
        if faces:
            # 选择面积最大的人脸作为参考
            face_rect = max(faces, key=lambda f: f[2] * f[3])
        else:
            H, W = img.shape[:2]
            face_rect = (0, 0, W, H)
        embeddings.append(extract_face_embedding(img, face_rect))
    return embeddings


# -------------------- 匹配结果合并 --------------------

def _merge_matches(
    match_points: List[Tuple[float, List[int], float]],
    duration: float,
    sample_interval_sec: float,
) -> List[dict]:
    """把按时间排序的匹配点合并成片段。

    相邻匹配点间隔小于 face_merge_gap_sec 视为同一段；每个匹配点覆盖
    [t, t + sample_interval_sec) 的时间范围，避免单点产生 0 长度片段。
    返回 [{"start_sec", "end_sec", "confidence", "face_rect"}, ...]。
    """
    if not match_points:
        return []
    match_points.sort(key=lambda x: x[0])
    gap = settings.face_merge_gap_sec

    segments: List[dict] = []
    cur_start = match_points[0][0]
    # 单个匹配点覆盖到下一个采样点
    cur_end = min(duration, match_points[0][0] + sample_interval_sec)
    cur_rect = match_points[0][1]
    confs: List[float] = [match_points[0][2]]

    for t, rect, conf in match_points[1:]:
        if t - cur_end <= gap:
            # 仍在合并范围内，延长当前片段
            cur_end = min(duration, t + sample_interval_sec)
            confs.append(conf)
            # 保留置信度最高的人脸框作为代表
            if conf >= max(confs):
                cur_rect = rect
        else:
            # 间隔过大，结算当前片段并开启新片段
            segments.append({
                "start_sec": float(cur_start),
                "end_sec": float(cur_end),
                "confidence": float(sum(confs) / len(confs)),
                "face_rect": [int(v) for v in cur_rect],
            })
            cur_start = t
            cur_end = min(duration, t + sample_interval_sec)
            cur_rect = rect
            confs = [conf]

    segments.append({
        "start_sec": float(cur_start),
        "end_sec": float(cur_end),
        "confidence": float(sum(confs) / len(confs)),
        "face_rect": [int(v) for v in cur_rect],
    })
    return segments


# -------------------- 辅助：安全抽取 PNG 帧 --------------------

def _iter_sampled_frames(video_path: Path, sample_interval_sec: float
                         ) -> Tuple[float, Optional[np.ndarray]]:
    """按 sample_interval_sec 间隔从视频中抽帧，yield (t秒, BGR ndarray)。

    说明：
    - 底层走 ffmpeg_proc.extract_frame（混合 seek + 容错），
      避免 cv2.VideoCapture 对 rv40/wmv3 等 codec 反复报 B-frame 错。
    - 抽出的 PNG 临时写到 settings.storage_path/frames 下，处理完统一删除。
    - 某一帧抽失败时 yield (t, None)，由调用方决定如何处理。
    """
    # 懒加载，避免循环 import（ffmpeg_proc → settings → 本文件）
    from app.core.ffmpeg_proc import extract_frame, probe_video

    info = probe_video(str(video_path))
    duration = float(info.duration)
    if duration <= 0:
        return

    step = max(0.5, float(sample_interval_sec))
    frames_dir = settings.storage_path / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    temp_files: List[Path] = []

    t = 0.0
    idx = 0
    try:
        while t < duration:
            png = frames_dir / f"facematch_{tag}_{idx:05d}.png"
            try:
                extract_frame(video_path, png, t)
                temp_files.append(png)
                arr = cv2.imdecode(
                    np.fromfile(str(png), dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
            except Exception as e:
                logger.debug("抽帧失败 t=%.1fs: %s", t, e)
                arr = None
            yield t, arr
            t += step
            idx += 1
    finally:
        for p in temp_files:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


# -------------------- 自动人物发现 --------------------

def _detect_gpu_faces(frames: List[np.ndarray]) -> List[List[dict]]:
    """批量调用专用人脸服务，并严格校验坐标、关键点和归一化特征。"""
    if not 1 <= len(frames) <= 8:
        raise ValueError("GPU 人脸检测批次必须为 1-8 帧")
    files = []
    for index, frame in enumerate(frames):
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise ValueError(f"第 {index} 帧 JPEG 编码失败")
        files.append(("images", (f"frame-{index}.jpg", encoded.tobytes(), "image/jpeg")))
    response = httpx.post(
        f"{settings.gpu_detector_url.rstrip('/')}/v1/faces:batch",
        files=files,
        data={"face_confidence": str(settings.face_detector_confidence),
              "min_face_size": str(settings.person_face_min_size)},
        timeout=settings.gpu_detector_timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or set(payload) != {"faces"}:
        raise ValueError("GPU 人脸响应根结构无效")
    raw_batches = payload["faces"]
    if not isinstance(raw_batches, list) or len(raw_batches) != len(frames):
        raise ValueError("GPU 人脸响应长度与输入不一致")
    batches: List[List[dict]] = []
    for frame, raw_faces in zip(frames, raw_batches):
        if not isinstance(raw_faces, list) or len(raw_faces) > 20:
            raise ValueError("GPU 人脸单帧结果无效")
        height, width = frame.shape[:2]
        current = []
        for raw in raw_faces:
            if not isinstance(raw, dict) or set(raw) != {"xyxy", "landmarks", "confidence", "embedding"}:
                raise ValueError("GPU 人脸结构无效")
            box, landmarks = raw["xyxy"], raw["landmarks"]
            confidence, embedding = raw["confidence"], raw["embedding"]
            if (not isinstance(box, list) or len(box) != 4
                    or any(isinstance(v, bool) or not isinstance(v, int) for v in box)
                    or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                raise ValueError("GPU 人脸坐标无效")
            if (not isinstance(landmarks, list) or len(landmarks) != 5
                    or any(not isinstance(p, list) or len(p) != 2 for p in landmarks)):
                raise ValueError("GPU 人脸关键点无效")
            flat_points = [v for point in landmarks for v in point]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   or v < 0 or (i % 2 == 0 and v > width) or (i % 2 == 1 and v > height)
                   for i, v in enumerate(flat_points)):
                raise ValueError("GPU 人脸关键点坐标无效")
            if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError("GPU 人脸置信度无效")
            if (not isinstance(embedding, list) or len(embedding) != settings.face_embedding_size
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) for v in embedding)):
                raise ValueError("GPU 人脸特征无效")
            emb = np.asarray(embedding, dtype=np.float32)
            norm = float(np.linalg.norm(emb))
            if not 0.99 <= norm <= 1.01:
                raise ValueError("GPU 人脸特征未 L2 归一化")
            current.append({"xyxy": box.copy(), "landmarks": landmarks,
                            "confidence": float(confidence), "embedding": emb / norm})
        batches.append(current)
    return batches


def _iter_discovery_frames(video_path: Path, duration: float, step: float):
    """优先一次顺序 VideoCapture 采样；失败后从失败时间起安全抽帧。"""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        yield from _iter_sampled_frames(video_path, step)
        return

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        cap.release()
        yield from _iter_sampled_frames(video_path, step)
        return

    next_t = 0.0
    frame_idx = 0
    failed_at: Optional[float] = None
    try:
        while next_t < duration:
            target_idx = max(frame_idx, int(round(next_t * fps)))
            while frame_idx < target_idx:
                if not cap.grab():
                    failed_at = next_t
                    break
                frame_idx += 1
            if failed_at is not None:
                break
            ok, frame = cap.read()
            frame_idx += 1
            if not ok or frame is None:
                failed_at = next_t
                break
            yield next_t, frame
            next_t += step
    finally:
        cap.release()

    if failed_at is not None:
        logger.warning("VideoCapture 在 %.1fs 读取失败，剩余采样改用安全抽帧", failed_at)
        for t, frame in _iter_sampled_frames(video_path, step):
            if t + 1e-6 >= failed_at:
                yield t, frame


def _face_thumbnail(frame: np.ndarray, box: List[int]) -> np.ndarray:
    """从原帧按脸框扩展为边界安全的头肩方形头像。"""
    x1, y1, x2, y2 = box
    face_w, face_h = x2 - x1, y2 - y1
    side = max(face_w, face_h) * 2.4
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0 + face_h * 0.35
    height, width = frame.shape[:2]
    side = min(side, width, height)
    left = max(0, min(width - int(side), int(round(cx - side / 2))))
    top = max(0, min(height - int(side), int(round(cy - side / 2))))
    size = int(side)
    return frame[top:top + size, left:left + size].copy()



def _face_quality(frame: np.ndarray, box: List[int]) -> float:
    x1, y1, x2, y2 = box
    face = frame[y1:y2, x1:x2]
    if face.size == 0:
        return 0.0
    gray = cv2.cvtColor(face, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean_light = float(gray.mean())
    exposure = 1.0 - min(abs(mean_light - 127.5) / 127.5, 1.0)
    return math.log1p(min(sharpness, 2000.0)) * math.sqrt((x2 - x1) * (y2 - y1)) * (0.7 + 0.3 * exposure)



def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def _merge_person_hits(hits: List[Tuple[float, float]], duration: float,
                       step: float) -> List[dict]:
    if not hits:
        return []
    by_time: dict[float, float] = {}
    for t, conf in hits:
        by_time[t] = max(by_time.get(t, 0.0), conf)
    ordered = sorted(by_time.items())
    segments: List[dict] = []
    start = ordered[0][0]
    end = min(duration, start + step)
    confs = [ordered[0][1]]
    for t, conf in ordered[1:]:
        if t <= end + 1e-6:
            end = min(duration, t + step)
            confs.append(conf)
        else:
            if end > start:
                segments.append({"start_sec": float(start), "end_sec": float(end),
                                 "confidence": float(sum(confs) / len(confs))})
            start, end, confs = t, min(duration, t + step), [conf]
    if end > start:
        segments.append({"start_sec": float(start), "end_sec": float(end),
                         "confidence": float(sum(confs) / len(confs))})
    return segments


def _cluster_centroid(cluster: dict) -> np.ndarray:
    centroid = np.mean(cluster["embeddings"], axis=0)
    norm = float(np.linalg.norm(centroid))
    return centroid / norm


def discover_people(
    video_path: Path,
    thumbnail_dir: Path,
    sample_interval: float = 5.0,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[dict]:
    """自动发现视频主要人物，返回人物缩略图及不连续可见片段。"""
    from app.core.ffmpeg_proc import probe_video

    video_path = Path(video_path)
    thumbnail_dir = Path(thumbnail_dir)
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    try:
        duration = float(probe_video(str(video_path)).duration)
    except Exception as e:
        logger.warning("无法读取视频时长，人物发现返回空结果: %s", e)
        return []
    if duration <= 0:
        return []

    step = max(0.5, float(sample_interval))
    total = max(1, int(math.ceil(duration / step)))
    clusters: List[dict] = []
    batch: List[Tuple[float, np.ndarray]] = []
    gpu_available = True
    scanned = 0
    valid_scanned = 0

    def add_detection(t: float, frame: np.ndarray, face: dict,
                      used_clusters: set[int]) -> None:
        box = face["xyxy"]
        embedding = face["embedding"]
        quality = _face_quality(frame, box)
        thumbnail = _face_thumbnail(frame, box)
        if thumbnail.size == 0:
            return
        best_idx, best_sim = -1, -1.0
        for idx, cluster in enumerate(clusters):
            if idx in used_clusters:
                continue
            sim = _cosine_similarity(embedding, _cluster_centroid(cluster))
            if sim > best_sim:
                best_idx, best_sim = idx, sim
        if best_idx < 0 or best_sim < settings.face_embedding_threshold:
            clusters.append({"embeddings": [], "hits": [], "quality_sum": 0.0,
                             "best_thumbnail": None, "best_quality": -1.0})
            best_idx = len(clusters) - 1
        cluster = clusters[best_idx]
        used_clusters.add(best_idx)
        cluster["hits"].append((t, face["confidence"]))
        cluster["quality_sum"] += quality
        cluster["embeddings"].append(embedding)
        if len(cluster["embeddings"]) > _MAX_FACE_FEATURES:
            cluster["embeddings"] = cluster["embeddings"][-_MAX_FACE_FEATURES:]
        if quality > cluster["best_quality"]:
            cluster["best_quality"] = quality
            cluster["best_thumbnail"] = thumbnail

    def process_batch(items: List[Tuple[float, np.ndarray]]) -> None:
        nonlocal gpu_available
        if not gpu_available:
            return
        try:
            detections = _detect_gpu_faces([frame for _, frame in items])
        except Exception as e:
            logger.warning("GPU 人脸服务不可用或响应无效，本次任务跳过人物发现且不回退 Haar: %s", e)
            gpu_available = False
            clusters.clear()
            return
        for (t, frame), frame_detections in zip(items, detections):
            used_clusters: set[int] = set()
            for face in frame_detections:
                add_detection(t, frame, face, used_clusters)

    for t, frame in _iter_discovery_frames(video_path, duration, step):
        scanned += 1
        if frame is not None:
            valid_scanned += 1
            batch.append((t, frame))
        if len(batch) >= 8:
            process_batch(batch)
            batch = []
        if progress_cb and (scanned == 1 or scanned % 5 == 0 or scanned >= total):
            try:
                progress_cb(min(scanned, total), total,
                            f"正在自动发现人物 {min(scanned, total)}/{total}")
            except Exception:
                pass
    if batch:
        process_batch(batch)

    candidates = []
    min_hits = 1 if valid_scanned <= 1 else settings.person_min_hits
    for cluster in clusters:
        unique_hits = {float(t): float(conf) for t, conf in cluster["hits"]}
        hit_count = len(unique_hits)
        thumbnail = cluster["best_thumbnail"]
        if hit_count < min_hits or thumbnail is None:
            continue
        segments = _merge_person_hits(list(unique_hits.items()), duration, step)
        if not segments:
            continue
        total_visible = sum(s["end_sec"] - s["start_sec"] for s in segments)
        avg_conf = sum(unique_hits.values()) / hit_count
        avg_quality = cluster["quality_sum"] / max(len(cluster["hits"]), 1)
        candidates.append({
            "hit_count": hit_count,
            "confidence": float(avg_conf),
            "total_visible_sec": float(total_visible),
            "average_face_quality": float(avg_quality),
            "segments": segments,
            "thumbnail": thumbnail,
        })
    candidates.sort(key=lambda item: (
        item["hit_count"], item["total_visible_sec"],
        item["average_face_quality"], item["confidence"]), reverse=True)

    people = []
    for index, candidate in enumerate(candidates[:3], start=1):
        person_id = f"person_{index:03d}"
        thumbnail_path = thumbnail_dir / f"{person_id}.jpg"
        ok, encoded = cv2.imencode(".jpg", candidate.pop("thumbnail"),
                                   [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            continue
        encoded.tofile(str(thumbnail_path))
        candidate.pop("average_face_quality", None)
        candidate["person_id"] = person_id
        candidate["thumbnail_path"] = str(thumbnail_path)
        people.append(candidate)
    if progress_cb:
        try:
            progress_cb(total, total, f"人物发现完成，共发现 {len(people)} 位主要人物")
        except Exception:
            pass
    return people


# -------------------- 主入口 --------------------

def match_portraits(
    video_path: Path,
    portrait_images: List[Path],
    sample_interval_sec: float = 5.0,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[dict]:
    """视频按间隔采样，检测人脸，与参考人像匹配。

    流程（重构后）：
      1. 读取参考人像图片并提取特征（颜色直方图 + LBP）；
      2. 按 sample_interval_sec 间隔用 ffmpeg extract_frame 抽 PNG；
      3. cv2.imdecode 读 PNG，对每帧检测人脸 → 特征 → 与参考 max 相似度；
      4. 相似度 >= face_match_threshold 的位置记为匹配点；
      5. 把间隔 < face_merge_gap_sec 的匹配点合并为片段。

    progress_cb(i, total, message) 可选：每扫描一帧回调一次，
    用于上层刷新进度条（i 从 0 开始，扫描完 i 帧后会增加到 i+1）。

    返回 [{"start_sec", "end_sec", "confidence", "face_rect": [x,y,w,h]}, ...]，
    按时间升序排列。
    """
    if not portrait_images:
        if progress_cb:
            progress_cb(0, 0, "未提供参考人像")
        return []

    # 懒加载 video 时长，用于片段合并
    from app.core.ffmpeg_proc import probe_video
    try:
        duration = float(probe_video(str(video_path)).duration)
    except Exception:
        duration = 0.0
    if duration <= 0:
        if progress_cb:
            progress_cb(0, 0, "视频时长为 0，无法扫描")
        return []

    step = max(0.5, float(sample_interval_sec))
    total_frames = max(1, int(math.ceil(duration / step)))

    # 提取参考人像特征
    if progress_cb:
        progress_cb(0, total_frames, "正在提取参考人像特征…")
    ref_embeddings = _load_portrait_embeddings(portrait_images)
    if not ref_embeddings:
        if progress_cb:
            progress_cb(total_frames, total_frames, "没有可用的参考人像特征，无法匹配")
        logger.warning("没有可用的参考人像特征，无法匹配")
        return []

    threshold = settings.face_match_threshold

    match_points: List[Tuple[float, List[int], float]] = []
    scanned = 0
    matched_hits = 0
    failed_frames = 0
    for t, frame in _iter_sampled_frames(video_path, step):
        scanned += 1
        if frame is None:
            failed_frames += 1
        else:
            faces = detect_faces(frame)
            best_this_frame = 0.0
            for face_rect in faces:
                emb = extract_face_embedding(frame, face_rect)
                sim = max(
                    (_combined_similarity(emb, ref) for ref in ref_embeddings),
                    default=0.0,
                )
                if sim >= threshold and sim >= best_this_frame:
                    best_this_frame = sim
                    match_points.append((t, [int(v) for v in face_rect], sim))
                    matched_hits += 1
        if progress_cb and (scanned == 1 or scanned == total_frames or scanned % 5 == 0):
            pct = scanned / max(total_frames, 1) * 100
            msg = (
                f"扫描视频帧 {scanned}/{total_frames}（{pct:.0f}%）"
                f" · 已找到匹配 {matched_hits} 处"
                + (f" · 抽帧失败 {failed_frames}" if failed_frames else "")
            )
            try:
                progress_cb(scanned, total_frames, msg)
            except Exception:
                pass

    segments = _merge_matches(match_points, duration, step)
    if progress_cb:
        try:
            progress_cb(total_frames, total_frames,
                        f"扫描完成：共 {len(segments)} 段人像片段")
        except Exception:
            pass
    logger.info(
        "人像匹配完成: 视频时长 %.1fs, 扫描帧 %d (ffmpeg 抽帧失败 %d), "
        "匹配点 %d 个, 合并后 %d 段",
        duration, scanned, failed_frames, len(match_points), len(segments),
    )
    return segments
