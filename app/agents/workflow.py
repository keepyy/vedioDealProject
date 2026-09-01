"""工作流（Web 层编排模式，支持两种模式）。

模式 manual（默认）：
  upload → init → detect → review_regions → plan_segments
        → 逐段 cut_preview → review_segment → 后台 render → 循环 → 完成
模式 portrait（人像选片）：
  upload → init → detect → review_regions → run_face_match
        → portrait-results → confirm_portrait → 后台逐段 cut_preview/render → 完成

confirm_regions 后的跳转由 main.py 根据 state["mode"] 决定。
渲染采用后台线程异步执行，用户确认片段后立即跳转进度页，可继续其他业务。
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

from app.config import settings
from app.core import ffmpeg_proc, detector

logger = logging.getLogger(__name__)


class WorkflowState(dict):
    """工作流共享状态。"""
    pass


class WorkflowNodes:
    """节点逻辑，由 JobManager 分步调用。"""

    @property
    def storage(self) -> Path:
        return settings.storage_path

    def init_job(self, state: WorkflowState) -> WorkflowState:
        if not state.get("job_id"):
            state["job_id"] = uuid.uuid4().hex[:12]
        up = Path(state["upload_path"])
        if not up.exists():
            raise RuntimeError(f"上传文件不存在: {up}")
        info = ffmpeg_proc.probe_video(up)
        if info.duration > settings.max_video_seconds:
            raise RuntimeError(
                f"视频时长 {info.duration:.1f}s 超过上限 {settings.max_video_seconds}s"
            )
        state["video_info"] = {
            "width": info.width, "height": info.height,
            "duration": info.duration, "fps": info.fps, "codec": info.codec,
        }
        state.setdefault("final_outputs", [])
        state.setdefault("mask_regions", [])
        logger.info("[%s] 初始化完成 时长=%.1fs",
                    state["job_id"], info.duration)
        return state

    def detect_overlay(self, state: WorkflowState) -> WorkflowState:
        res = detector.detect_video(Path(state["upload_path"]))
        state["banner_region"] = res.banner_region
        state["logo_regions"] = res.logo_regions
        state["sample_frame"] = res.sample_frame
        logger.info("[%s] 检测完成: banner=%s, logos=%s",
                    state["job_id"], state["banner_region"], state["logo_regions"])
        return state

    def plan_segments(self, state: WorkflowState) -> WorkflowState:
        dur = float(state["video_info"]["duration"])
        seg_secs = int(state.get("segment_seconds") or settings.default_segment_seconds)
        segs = ffmpeg_proc.plan_segments(dur, seg_secs)
        state["segments"] = [
            {"idx": i, "start_sec": s, "end_sec": e, "final_end_sec": 0.0,
             "preview_path": "", "output_path": "", "status": "pending", "error": ""}
            for i, (s, e) in enumerate(segs)
        ]
        state["current_segment_idx"] = 0
        logger.info("[%s] 分割计划: %d 段", state["job_id"], len(state["segments"]))
        return state

    def cut_preview(self, state: WorkflowState) -> WorkflowState:
        segs = state["segments"]
        cur = int(state["current_segment_idx"])
        if cur >= len(segs):
            return state
        seg = segs[cur]
        seg["status"] = "previewing"
        out_dir = self.storage / "segments" / state["job_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        preview_file = out_dir / f"seg{cur:02d}_preview.mp4"
        try:
            ffmpeg_proc.cut_and_process_segment(
                video_path=Path(state["upload_path"]),
                start_sec=float(seg["start_sec"]),
                end_sec=float(seg["end_sec"]),
                output_path=preview_file,
                banner_region=None, logos=None, mask_regions=None,
            )
            seg["preview_path"] = str(preview_file)
            seg["final_end_sec"] = float(seg["end_sec"])
            seg["status"] = "human_review"
            logger.info("[%s] 预览生成 %s", state["job_id"], preview_file.name)
        except Exception as e:
            seg["status"] = "failed"
            seg["error"] = str(e)
            logger.exception("[%s] 预览失败 %s", state["job_id"], e)
        return state

    def render_segment(self, state: WorkflowState) -> WorkflowState:
        cur = int(state["current_segment_idx"])
        segs = state["segments"]
        if cur >= len(segs):
            return state
        seg = segs[cur]
        out_dir = self.storage / "final" / state["job_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"seg{cur:02d}.mp4"
        subsegments = seg.get("subsegments")
        try:
            output_files = []
            if subsegments:
                groups: Dict[int, List[Dict]] = {}
                for index, sub in enumerate(subsegments):
                    group = max(1, int(sub.get("output_group", index + 1)))
                    groups.setdefault(group, []).append(sub)
                for group, grouped_segments in sorted(groups.items()):
                    ordered_segments = sorted(grouped_segments, key=lambda item: item["start_sec"])
                    grouped_file = out_dir / f"output_group_{group:02d}.mp4"
                    parts = [
                        grouped_file if len(ordered_segments) == 1 else out_dir / f"seg{cur:02d}_group{group:02d}_part{i:02d}.mp4"
                        for i in range(len(ordered_segments))
                    ]
                    def render_part(index: int) -> None:
                        sub = ordered_segments[index]
                        ffmpeg_proc.cut_and_process_segment(
                            video_path=Path(state["upload_path"]),
                            start_sec=float(sub["start_sec"]),
                            end_sec=float(sub["end_sec"]),
                            output_path=parts[index],
                            banner_region=None,
                            logos=state.get("logo_regions") or [],
                            mask_regions=state.get("mask_regions") or [],
                        )
                    if len(parts) > 1:
                        with ThreadPoolExecutor(max_workers=min(3, len(parts))) as executor:
                            list(executor.map(render_part, range(len(parts))))
                    else:
                        render_part(0)
                    if len(parts) > 1:
                        ffmpeg_proc.concat_processed_segments(parts, grouped_file)
                    cover_time = (state.get("output_covers") or {}).get(group)
                    if cover_time is None:
                        raise RuntimeError(f"成品组 {group} 未选择封面画面")
                    ffmpeg_proc.prepend_cover_intro(
                        grouped_file, Path(state["upload_path"]), float(cover_time), 2.0,
                    )
                    output_files.append(str(grouped_file))
            else:
                ffmpeg_proc.cut_and_process_segment(
                    video_path=Path(state["upload_path"]),
                    start_sec=float(seg["start_sec"]),
                    end_sec=float(seg["final_end_sec"] or seg["end_sec"]),
                    output_path=out_file,
                    banner_region=None,
                    logos=state.get("logo_regions") or [],
                    mask_regions=state.get("mask_regions") or [],
                )
                output_files.append(str(out_file))
            seg["output_paths"] = output_files
            seg["output_path"] = output_files[0]
            seg["status"] = "rendered"
            state["final_outputs"].extend(output_files)
            logger.info("[%s] 渲染完成 seg%d，共 %d 个独立视频",
                        state["job_id"], cur, len(output_files))
        except Exception as e:
            seg["status"] = "failed"
            seg["error"] = str(e)
            logger.exception("[%s] 渲染失败 seg%d: %s", state["job_id"], cur, e)
        return state


nodes = WorkflowNodes()


class JobManager:
    """内存中持有每个 job 的状态，Web 层分步调用。

    渲染采用后台线程异步执行，每个 job 通过 status 字段表示当前阶段：
      detecting / reviewing_regions / reviewing_segment / rendering / completed / failed
    """

    def __init__(self) -> None:
        self.jobs: Dict[str, WorkflowState] = {}
        # 保护 jobs 字典并发读写（后台线程会修改 state）
        self._lock = threading.Lock()
        # 每个job独立的渲染锁，避免同一job多段并发渲染
        self._job_locks: Dict[str, threading.Lock] = {}

    # ---- 工具：确保 job 级别锁存在 ----
    def _get_job_lock(self, job_id: str) -> threading.Lock:
        with self._lock:
            lk = self._job_locks.get(job_id)
            if lk is None:
                lk = threading.Lock()
                self._job_locks[job_id] = lk
            return lk

    @staticmethod
    def _normalize_intervals(intervals: List[Dict], duration: float) -> List[Dict[str, float]]:
        if not isinstance(intervals, list) or len(intervals) > 1000:
            raise ValueError("时间区间必须为不超过 1000 项的列表")
        normalized = []
        for interval in intervals:
            if not isinstance(interval, dict):
                raise ValueError("时间区间格式无效")
            try:
                start = float(interval["start_sec"])
                end = float(interval["end_sec"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("时间区间必须包含有效的 start_sec 和 end_sec") from exc
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError("时间必须为有限数值")
            if start < 0 or end > duration or end <= start:
                raise ValueError("时间区间必须在视频范围内且结束时间晚于开始时间")
            normalized.append({"start_sec": start, "end_sec": end})
        normalized.sort(key=lambda item: (item["start_sec"], item["end_sec"]))
        merged: List[Dict[str, float]] = []
        for interval in normalized:
            if merged and interval["start_sec"] <= merged[-1]["end_sec"] + 1e-6:
                merged[-1]["end_sec"] = max(merged[-1]["end_sec"], interval["end_sec"])
            else:
                merged.append(dict(interval))
        return merged

    @staticmethod
    def _plan_analysis_segments(state: WorkflowState) -> List[Dict[str, float]]:
        duration = float(state["video_info"]["duration"])
        segment_seconds = state.get("segment_seconds")
        if segment_seconds is None:
            windows = [{"start_sec": 0.0, "end_sec": duration}]
        else:
            windows = [
                {"start_sec": float(start), "end_sec": float(end)}
                for start, end in ffmpeg_proc.plan_segments(duration, int(segment_seconds))
            ]
        state["analysis_segments"] = windows
        return windows

    # ---- 系统资源检查 ----
    def can_accept_job(self) -> bool:
        """检查系统资源是否允许接受新任务。

        规则：CPU使用率<80% 且 内存<85% 且 当前渲染中任务<2。
        """
        try:
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory().percent
        except Exception as e:
            logger.warning("psutil 获取资源失败: %s，保守拒绝", e)
            return False
        with self._lock:
            rendering_count = sum(
                1 for s in self.jobs.values() if s.get("status") == "rendering"
            )
        ok = cpu < 80 and mem < 85 and rendering_count < 2
        logger.info("can_accept_job: cpu=%.1f%% mem=%.1f%% rendering=%d -> %s",
                    cpu, mem, rendering_count, ok)
        return ok

    def create_import_job(
        self, upload_path: str, mode: str, status: str,
        message: str, total_bytes: int = 0,
    ) -> WorkflowState:
        if mode not in ("manual", "portrait"):
            raise ValueError("不支持的任务模式")
        job_id = uuid.uuid4().hex[:12]
        state: WorkflowState = WorkflowState({
            "job_id": job_id,
            "upload_path": upload_path,
            "segment_seconds": None,
            "mode": mode,
            "portrait_paths": [],
            "ost_autocut": False,
            "final_outputs": [],
            "segments": [],
            "mask_regions": [],
            "logo_regions": [],
            "banner_region": None,
            "status": status,
            "created_at": time.time(),
            "render_progress": {
                "current_seg": 0,
                "total_segs": 0,
                "status": status,
                "message": message,
                "downloaded_bytes": 0,
                "total_bytes": total_bytes,
                "percent": 0 if total_bytes else None,
            },
        })
        with self._lock:
            self.jobs[job_id] = state
        return state

    def update_import_job(self, job_id: str, **progress: Any) -> None:
        with self._lock:
            state = self.jobs.get(job_id)
            if not state:
                return
            state["render_progress"].update(progress)
            if "status" in progress:
                state["status"] = progress["status"]

    def finalize_import_job(self, job_id: str, upload_path: str) -> WorkflowState:
        with self._lock:
            state = self.jobs.get(job_id)
            if not state:
                raise ValueError("任务不存在")
            state["upload_path"] = upload_path
            state["status"] = "initializing"
            state["render_progress"] = {
                "current_seg": 0, "total_segs": 0,
                "status": "initializing", "message": "正在初始化视频剪辑…",
            }
        nodes.init_job(state)
        threading.Thread(
            target=self._prepare_editing_in_background,
            args=(job_id,), daemon=True, name=f"prepare-{job_id}",
        ).start()
        return state

    # ---- 阶段1：上传 → init（同步） → detect（后台线程） ----
    def new_job(
        self, upload_path: str, segment_seconds: Optional[int],
        mode: str = "manual", portrait_paths: Optional[List[str]] = None,
        ost_autocut: bool = False,
    ) -> WorkflowState:
        if mode not in ("manual", "portrait"):
            raise ValueError("不支持的任务模式")
        seg_secs = None
        if segment_seconds != 0:
            raw_segment_seconds = settings.default_segment_seconds if segment_seconds is None else segment_seconds
            try:
                numeric_segment_seconds = float(raw_segment_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError("分割时长必须为 1..180 的整数分钟") from exc
            if (not math.isfinite(numeric_segment_seconds)
                    or not numeric_segment_seconds.is_integer()):
                raise ValueError("分割时长必须为 1..180 的整数分钟")
            seg_secs = int(numeric_segment_seconds)
            if seg_secs < 60 or seg_secs > 180 * 60 or seg_secs % 60:
                raise ValueError("分割时长必须为 1..180 的整数分钟")
        job_id = uuid.uuid4().hex[:12]
        state: WorkflowState = WorkflowState({
            "job_id": job_id,
            "upload_path": upload_path,
            "segment_seconds": seg_secs,
            "mode": mode,
            "portrait_paths": portrait_paths or [],
            "ost_autocut": ost_autocut,
            "final_outputs": [],
            "mask_regions": [],
            "logo_regions": [],
            "banner_region": None,
            # 任务状态字段
            "status": "initializing",
            "created_at": time.time(),
            "render_progress": {
                "current_seg": 0,
                "total_segs": 0,
                "status": "initializing",
                "message": "正在初始化视频剪辑…",
            },
        })
        # init_job 只做 ffprobe（毫秒级），立即返回跳进度页
        state = nodes.init_job(state)

        with self._lock:
            self.jobs[job_id] = state

        # 仅规划固定窗口；Logo 和蒙版由用户在统一剪辑页面手动框选。
        t = threading.Thread(
            target=self._prepare_editing_in_background,
            args=(job_id,),
            daemon=True,
            name=f"prepare-{job_id}",
        )
        t.start()
        return state

    def _prepare_editing_in_background(self, job_id: str) -> None:
        """直接准备统一剪辑页，不执行 Logo/蒙版自动识别。"""
        try:
            with self._lock:
                state = self.jobs[job_id]
            self._plan_analysis_segments(state)
            if state.get("mode") == "portrait":
                state["status"] = "reviewing_regions"
                self.run_face_match(job_id)
                return
            total = len(state.get("analysis_segments") or [])
            state["status"] = "reviewing_windows"
            state["render_progress"] = {
                "current_seg": 0,
                "total_segs": total,
                "status": "pending",
                "message": "固定窗口已规划完成，等待统一剪辑确认",
            }
        except Exception as e:
            logger.exception("[%s] 剪辑页面准备失败", job_id)
            with self._lock:
                state = self.jobs.get(job_id)
            if state:
                state["status"] = "failed"
                state["render_progress"] = {
                    "current_seg": 0, "total_segs": 0,
                    "status": "failed", "message": f"剪辑页面准备失败：{e}",
                }

    def _detect_in_background(self, job_id: str) -> None:
        """兼容旧任务：后台执行历史区域自动检测流程。"""
        try:
            with self._lock:
                state = self.jobs[job_id]
            st = nodes.detect_overlay(state)
            state["banner_region"] = st.get("banner_region")
            state["logo_regions"] = st.get("logo_regions") or []
            state["sample_frame"] = st.get("sample_frame")
            state["mask_regions"] = st.get("mask_regions") or []

            # OST 自动检测
            if state.get("ost_autocut"):
                try:
                    from app.core.ost_detect import detect_ost
                    ost_segs = detect_ost(Path(state["upload_path"]))
                    state["ost_segments"] = ost_segs
                    logger.info("[%s] OST检测: %d 段", job_id, len(ost_segs))
                except Exception as e:
                    logger.warning("[%s] OST检测失败: %s", job_id, e)
                    state["ost_segments"] = []
            else:
                state["ost_segments"] = []

            # 检测完成，进入区域审核阶段
            state["status"] = "reviewing_regions"
            state["render_progress"] = {
                "current_seg": 0, "total_segs": 0,
                "status": "pending", "message": "检测完成，等待你进入区域审核页面",
            }
            logger.info("[%s] 画面检测完成，进入区域审核阶段", job_id)
        except Exception as e:
            logger.exception("[%s] 画面检测失败", job_id)
            with self._lock:
                state = self.jobs.get(job_id)
            if state:
                state["status"] = "failed"
                rp = state.get("render_progress") or {}
                rp["message"] = f"画面检测失败：{e}"
                state["render_progress"] = rp

    # ---- 阶段2：确认区域 → 生成基础分割 ----
    def confirm_regions(
        self, job_id: str,
        banner_region: Optional[Tuple[int, int, int, int]],
        logo_regions: List[Tuple[int, int, int, int]],
        mask_regions: List[Tuple[int, int, int, int]],
    ) -> WorkflowState:
        with self._lock:
            state = self.jobs[job_id]
        if state.get("status") != "reviewing_regions":
            raise ValueError("当前任务状态不允许确认区域")
        mode = state.get("mode")
        if mode not in ("manual", "portrait"):
            raise ValueError("不支持的任务模式")
        state["banner_region"] = None
        state["logo_regions"] = logo_regions
        state["mask_regions"] = mask_regions
        logger.info("[%s] 区域审核通过: banner=%s(已禁用) logos=%d masks=%d",
                    job_id, None, len(logo_regions), len(mask_regions))
        self._plan_analysis_segments(state)
        if mode == "manual":
            total = len(state.get("analysis_segments") or [])
            state["status"] = "reviewing_windows"
            state["render_progress"] = {
                "current_seg": 0,
                "total_segs": total,
                "status": "pending",
                "message": "固定窗口已规划完成，等待统一剪辑确认",
            }
        else:
            # portrait 模式：启动人像匹配（后台线程），完成后由 progress 页跳 portrait-results
            return self.run_face_match(job_id)
        return state

    def _first_preview_in_background(self, job_id: str) -> None:
        """manual 模式：后台线程生成第一段预览。"""
        try:
            with self._lock:
                state = self.jobs[job_id]
            nodes.cut_preview(state)
            current = state["segments"][int(state.get("current_segment_idx", 0))]
            if current.get("status") == "failed":
                raise RuntimeError(current.get("error") or "预览生成失败")
            state["status"] = "reviewing_segment"
            total = len(state.get("segments") or [])
            state["render_progress"] = {
                "current_seg": 0,
                "total_segs": total,
                "status": "reviewing",
                "message": "第1段预览已就绪，等待审核",
            }
            logger.info("[%s] 第一段预览生成完成", job_id)
        except Exception as e:
            logger.exception("[%s] 第一段预览失败", job_id)
            with self._lock:
                state = self.jobs.get(job_id)
            if state:
                state["status"] = "failed"
                rp = state.get("render_progress") or {}
                rp["message"] = f"第1段预览失败：{e}"
                state["render_progress"] = rp

    @staticmethod
    def _next_pending_segment(segments: List[Dict], after_idx: int = -1) -> Optional[int]:
        return next(
            (idx for idx in range(after_idx + 1, len(segments))
             if segments[idx].get("status") == "pending"),
            None,
        )

    # ---- 阶段3：片段审核 → 异步渲染 ----
    def confirm_segment_end(
        self, job_id: str, seg_idx: int, final_end_sec: float = 0, action: str = "confirm",
        subsegments: Optional[List[Dict]] = None,
    ) -> WorkflowState:
        """用户确认/跳过当前段。

        改为异步：启动后台线程执行 render_segment + 下一段 cut_preview，
        立即返回 state，由 Web 层重定向到进度页。

        subsegments: 不连续子片段列表 [{"start_sec": float, "end_sec": float}, ...]，
                     若提供则 render_segment 会将每个子片段渲染为独立视频，不做合并。
        """
        with self._lock:
            state = self.jobs[job_id]
        if state.get("mode") not in ("manual", "portrait"):
            raise ValueError("不支持的任务模式")
        if state.get("status") != "reviewing_segment":
            raise ValueError("当前任务状态不允许确认片段")
        if action not in ("confirm", "skip"):
            raise ValueError("不支持的片段操作")
        segs = state["segments"]
        if seg_idx < 0 or seg_idx >= len(segs):
            raise ValueError("片段索引无效")
        if seg_idx != int(state.get("current_segment_idx", -1)):
            raise ValueError("只能确认当前片段")

        if action == "skip":
            segs[seg_idx]["status"] = "skipped"
            segs[seg_idx]["output_path"] = ""
            logger.info("[%s] 跳过 seg%d", job_id, seg_idx)
        else:
            start = float(segs[seg_idx]["start_sec"])
            segment_end = float(segs[seg_idx]["end_sec"])
            duration = float(state["video_info"]["duration"])
            if subsegments:
                normalized_subsegments = self._normalize_intervals(subsegments, duration)
                if any(s["start_sec"] < start or s["end_sec"] > segment_end
                       for s in normalized_subsegments):
                    raise ValueError("子片段必须位于当前固定窗口内")
                end = segment_end
                segs[seg_idx]["subsegments"] = normalized_subsegments
            else:
                raw_end = final_end_sec or segment_end
                try:
                    end = float(raw_end)
                except (TypeError, ValueError) as exc:
                    raise ValueError("结束时间无效") from exc
                if not math.isfinite(end) or end <= start or end > segment_end or end > duration:
                    raise ValueError("结束时间必须在当前片段及视频范围内")
                segs[seg_idx].pop("subsegments", None)
            segs[seg_idx]["final_end_sec"] = end
            state["current_segment_idx"] = seg_idx
            logger.info("[%s] 审核通过 seg%d: %.1f-%.1f subsegments=%d",
                        job_id, seg_idx, start, end, len(subsegments or []))

        # 在启动线程前原子切换状态，阻止重复提交启动多个渲染线程。
        with self._lock:
            if state.get("status") != "reviewing_segment":
                raise ValueError("当前任务状态不允许确认片段")
            state["status"] = "rendering"
            state["render_progress"] = {
                "current_seg": seg_idx,
                "total_segs": len(segs),
                "status": "rendering",
                "message": f"正在渲染第{seg_idx+1}段...",
            }

        # 启动后台线程执行渲染 + 下一段预览
        thread = threading.Thread(
            target=self._render_in_background,
            args=(job_id, seg_idx, action),
            daemon=True,
            name=f"render-{job_id}-seg{seg_idx}",
        )
        thread.start()
        return state

    def _render_in_background(self, job_id: str, seg_idx: int, action: str) -> None:
        """后台线程：渲染当前段 + 生成下一段预览。

        流程：
          1. 设置 status=rendering，更新 render_progress
          2. 执行 nodes.render_segment（如 action==confirm）
          3. 生成下一段预览 nodes.cut_preview（如果还有下一段）
          4. 完成则 status=completed
        异常则 status=failed。
        """
        # 同一 job 串行渲染，避免多段并发冲突
        job_lock = self._get_job_lock(job_id)
        with job_lock:
            with self._lock:
                state = self.jobs[job_id]
                state["status"] = "rendering"
                state["render_progress"] = {
                    "current_seg": seg_idx,
                    "total_segs": len(state.get("segments") or []),
                    "status": "rendering",
                    "message": f"正在渲染第{seg_idx+1}段...",
                }

            try:
                if action != "skip":
                    nodes.render_segment(state)
                    if state["segments"][seg_idx].get("status") == "failed":
                        raise RuntimeError(state["segments"][seg_idx].get("error") or "片段渲染失败")
                if state.get("cancel_requested"):
                    self._finish_cancel(job_id, state)
                    return

                # 生成下一个待处理窗口的预览，跨过预先跳过的窗口。
                segs = state.get("segments") or []
                next_idx = self._next_pending_segment(segs, seg_idx)
                if next_idx is not None:
                    state["current_segment_idx"] = next_idx
                    nodes.cut_preview(state)
                    if segs[next_idx].get("status") == "failed":
                        raise RuntimeError(segs[next_idx].get("error") or "下一段预览生成失败")
                    with self._lock:
                        state["status"] = "reviewing_segment"
                        state["render_progress"] = {
                            "current_seg": next_idx,
                            "total_segs": len(segs),
                            "status": "reviewing",
                            "message": f"第{next_idx+1}段预览已就绪，等待审核",
                        }
                    logger.info("[%s] 后台渲染完成 seg%d，下一段预览就绪", job_id, seg_idx)
                else:
                    with self._lock:
                        state["status"] = "completed"
                        state["render_progress"] = {
                            "current_seg": seg_idx,
                            "total_segs": len(segs),
                            "status": "done",
                            "message": "全部渲染完成",
                        }
                    logger.info("[%s] 后台渲染完成，全部片段处理结束", job_id)
            except Exception as e:
                logger.exception("[%s] 后台渲染异常 seg%d: %s", job_id, seg_idx, e)
                with self._lock:
                    state["status"] = "failed"
                    state["render_progress"] = {
                        "current_seg": seg_idx,
                        "total_segs": len(state.get("segments") or []),
                        "status": "error",
                        "message": str(e),
                    }

    # ---- 阶段3'：人像选片（portrait 模式） ----
    def run_face_match(self, job_id: str) -> WorkflowState:
        """启动人像匹配（后台线程），立即返回 state。

        状态变化：reviewing_regions → portrait_matching → portrait_matching_done
        progress 页监测到 portrait_matching_done 后自动跳 /portrait-results
        """
        with self._lock:
            state = self.jobs[job_id]
        if state.get("mode") != "portrait":
            raise ValueError("仅人像识别任务可启动人物发现")
        if state.get("status") != "reviewing_regions":
            raise ValueError("当前任务状态不允许启动人物发现")
        state["status"] = "portrait_matching"
        state["render_progress"] = {
            "current_seg": 0, "total_segs": 0,
            "status": "matching",
            "message": "正在扫描视频并执行人脸匹配，可能需要 1~5 分钟…",
        }
        t = threading.Thread(
            target=self._face_match_in_background,
            args=(job_id,),
            daemon=True,
            name=f"facematch-{job_id}",
        )
        t.start()
        return state

    def _face_match_in_background(self, job_id: str) -> None:
        try:
            with self._lock:
                state = self.jobs[job_id]
            from app.core import face_match
            video_path = Path(state["upload_path"])
            sample_interval = settings.face_sample_interval_sec
            thumbnail_dir = settings.storage_path / "portraits" / job_id

            # 人像匹配进度回调：每扫一帧更新 render_progress，UI 进度条会动起来
            def _progress_cb(i: int, total: int, msg: str) -> None:
                try:
                    with self._lock:
                        s = self.jobs.get(job_id)
                    if not s:
                        return
                    rp = dict(s.get("render_progress") or {})
                    rp["current_seg"] = int(i)
                    rp["total_segs"] = int(total)
                    rp["status"] = "matching"
                    rp["message"] = msg
                    s["render_progress"] = rp
                except Exception:
                    logger.debug("[%s] 进度回调写入失败", job_id, exc_info=True)

            detected_people = face_match.discover_people(
                video_path, thumbnail_dir, sample_interval,
                progress_cb=_progress_cb,
            )
            state["detected_people"] = detected_people
            all_segments = sorted(
                (dict(segment) for person in detected_people for segment in person["segments"]),
                key=lambda s: (float(s["start_sec"]), float(s["end_sec"])),
            )
            matched = []
            for segment in all_segments:
                start, end = float(segment["start_sec"]), float(segment["end_sec"])
                confidence = float(segment["confidence"])
                if matched and start <= matched[-1]["end_sec"] + 1e-6:
                    matched[-1]["end_sec"] = max(matched[-1]["end_sec"], end)
                    matched[-1]["confidence"] = max(matched[-1]["confidence"], confidence)
                elif end > start:
                    matched.append({"start_sec": start, "end_sec": end,
                                    "confidence": confidence})
            state["matched_segments"] = matched
            self._plan_analysis_segments(state)
            # 标记完成：progress 页检测到此状态后自动跳 portrait-results
            state["status"] = "portrait_matching_done"
            state["render_progress"] = {
                "current_seg": 0, "total_segs": 0,
                "status": "done",
                "message": f"人物发现完成，共发现 {len(detected_people)} 位主要人物、{len(matched)} 个片段",
            }
            logger.info("[%s] 人物发现完成: %d 人 %d 段", job_id, len(detected_people), len(matched))
        except Exception as e:
            logger.exception("[%s] 人像匹配失败", job_id)
            with self._lock:
                state = self.jobs.get(job_id)
            if state:
                state["status"] = "failed"
                rp = state.get("render_progress") or {}
                rp["message"] = f"人像匹配失败：{e}"
                state["render_progress"] = rp

    def confirm_portrait(
        self, job_id: str, selected_segments: Optional[List[Dict]] = None,
        selected_person_ids: Optional[List[str]] = None,
        window_selections: Optional[List[Dict]] = None,
    ) -> WorkflowState:
        """确认人物模式的全部固定窗口并批量生成。"""
        return self._confirm_window_selections(
            job_id, window_selections, selected_segments, selected_person_ids
        )

    def confirm_windows(
        self, job_id: str, window_selections: List[Dict]
    ) -> WorkflowState:
        """确认普通模式的全部固定窗口并批量生成。"""
        return self._confirm_window_selections(job_id, window_selections)

    def _confirm_window_selections(
        self, job_id: str, window_selections: Optional[List[Dict]],
        selected_segments: Optional[List[Dict]] = None,
        selected_person_ids: Optional[List[str]] = None,
    ) -> WorkflowState:
        with self._lock:
            state = self.jobs[job_id]
        mode = state.get("mode")
        expected_status = "portrait_matching_done" if mode == "portrait" else "reviewing_windows"
        if mode not in ("manual", "portrait") or state.get("status") != expected_status:
            raise ValueError("当前任务状态不允许确认固定窗口")

        duration = float(state["video_info"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("视频时长无效")
        windows = state.get("analysis_segments") or self._plan_analysis_segments(state)
        people = {
            str(person.get("person_id")): person
            for person in state.get("detected_people") or []
        }

        # 兼容旧调用：把全局人物和区间投影到全部权威窗口。
        if window_selections is None:
            if not isinstance(selected_person_ids, list) or len(selected_person_ids) != 1:
                raise ValueError("每次必须且只能选择 1 位人物")
            legacy_person_id = selected_person_ids[0]
            if legacy_person_id not in people:
                raise ValueError(f"人物 ID 不存在: {legacy_person_id}")
            legacy_intervals = self._normalize_intervals(selected_segments or [], duration)
            if not legacy_intervals:
                raise ValueError("至少需要一个有效时间区间")
            window_selections = []
            for window_idx, window in enumerate(windows):
                start, end = float(window["start_sec"]), float(window["end_sec"])
                intersections = [
                    {"start_sec": max(start, item["start_sec"]),
                     "end_sec": min(end, item["end_sec"])}
                    for item in legacy_intervals
                    if item["start_sec"] < end and item["end_sec"] > start
                ]
                window_selections.append({
                    "window_idx": window_idx,
                    "enabled": bool(intersections),
                    "person_id": legacy_person_id if intersections else None,
                    "subsegments": intersections,
                })

        if not isinstance(window_selections, list):
            raise ValueError("windows_json 必须为列表")
        submitted = {}
        for selection in window_selections:
            if not isinstance(selection, dict):
                raise ValueError("固定窗口选择格式无效")
            window_idx = selection.get("window_idx")
            if isinstance(window_idx, bool) or not isinstance(window_idx, int):
                raise ValueError("window_idx 必须为整数")
            if window_idx < 0 or window_idx >= len(windows):
                raise ValueError("window_idx 超出固定窗口范围")
            if window_idx in submitted:
                raise ValueError(f"window_idx 重复: {window_idx}")
            submitted[window_idx] = selection
        if set(submitted) != set(range(len(windows))):
            raise ValueError("必须提交全部固定窗口且每个窗口恰好一次")

        # 固定时长仅用于初始切分；编辑页可移动相邻窗口的共享边界。
        adjusted_windows = []
        for window_idx, original in enumerate(windows):
            selection = submitted[window_idx]
            try:
                start = float(selection.get("window_start_sec", original["start_sec"]))
                end = float(selection.get("window_end_sec", original["end_sec"]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"窗口 {window_idx + 1} 的边界格式无效") from exc
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end > duration or end <= start:
                raise ValueError(f"窗口 {window_idx + 1} 的边界无效")
            if adjusted_windows and abs(start - adjusted_windows[-1]["end_sec"]) > 0.01:
                raise ValueError("相邻窗口边界必须连续")
            adjusted_windows.append({"start_sec": start, "end_sec": end})
        if abs(adjusted_windows[0]["start_sec"]) > 0.01 or abs(adjusted_windows[-1]["end_sec"] - duration) > 0.01:
            raise ValueError("窗口必须完整覆盖原视频")
        windows = adjusted_windows
        state["analysis_segments"] = windows

        segs = []
        selected_ids = set()
        for window_idx, window in enumerate(windows):
            selection = submitted[window_idx]
            enabled = selection.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError(f"窗口 {window_idx + 1} 的 enabled 必须为布尔值")
            person_id = selection.get("person_id") if mode == "portrait" else None
            if person_id is not None:
                if not isinstance(person_id, str) or not person_id:
                    raise ValueError(f"窗口 {window_idx + 1} 的人物 ID 格式无效")
                if person_id not in people:
                    raise ValueError(f"人物 ID 不存在: {person_id}")
                selected_ids.add(person_id)
            start, end = float(window["start_sec"]), float(window["end_sec"])
            raw_subsegments = selection.get("subsegments", [])
            subsegments = self._normalize_intervals(raw_subsegments, duration)
            for index, item in enumerate(subsegments):
                raw_item = next((raw for raw in raw_subsegments
                                 if abs(float(raw.get("start_sec", -1)) - item["start_sec"]) < 1e-6
                                 and abs(float(raw.get("end_sec", -1)) - item["end_sec"]) < 1e-6), {})
                try:
                    item["output_group"] = max(1, int(raw_item.get("output_group", index + 1)))
                except (TypeError, ValueError):
                    item["output_group"] = index + 1
            if enabled and not subsegments:
                raise ValueError(f"窗口 {window_idx + 1} 启用时至少需要一个有效子片段")
            if any(item["start_sec"] < start or item["end_sec"] > end
                   for item in subsegments):
                raise ValueError(f"窗口 {window_idx + 1} 的子片段必须位于当前固定窗口内")
            segs.append({
                "idx": window_idx, "window_idx": window_idx,
                "start_sec": start, "end_sec": end,
                "final_end_sec": end,
                "person_id": person_id,
                "enabled": enabled,
                "subsegments": subsegments if enabled else [],
                "preview_path": "", "output_path": "",
                "status": "pending" if enabled else "skipped", "error": "",
            })

        all_subsegments = [item for seg in segs for item in seg.get("subsegments") or []]
        output_groups = {int(item["output_group"]) for item in all_subsegments}
        output_covers = {
            int(group): float(value)
            for group, value in (state.get("output_covers") or {}).items()
            if int(group) in output_groups
        }
        if output_groups != set(output_covers):
            raise ValueError("每个成品盒必须且只能选择一个封面画面")
        state["output_covers"] = output_covers
        for group in output_groups:
            cover_time = float(output_covers[group])
            group_segments = [item for item in all_subsegments if int(item["output_group"]) == group]
            if not any(item["start_sec"] <= cover_time <= item["end_sec"] for item in group_segments):
                raise ValueError(f"成品盒 {group} 的封面必须来自该成品包含的片段")

        # 跨窗口的同一成品组统一交给一个渲染批次，避免后续窗口覆盖同名成品。
        enabled_indices = [idx for idx, seg in enumerate(segs) if seg["status"] == "pending"]
        if enabled_indices:
            primary = enabled_indices[0]
            segs[primary]["subsegments"] = sorted(all_subsegments, key=lambda item: item["start_sec"])
            for idx in enabled_indices[1:]:
                segs[idx]["subsegments"] = []
                segs[idx]["status"] = "skipped"

        state["selected_person_ids"] = sorted(selected_ids)
        state["window_selections"] = window_selections
        state["segments"] = segs
        first_pending = self._next_pending_segment(segs)
        state["current_segment_idx"] = first_pending if first_pending is not None else 0
        logger.info("[%s] 固定窗口剪辑确认: mode=%s %d 个窗口，%d 个启用",
                    job_id, mode, len(segs), sum(seg["status"] == "pending" for seg in segs))

        if first_pending is None:
            with self._lock:
                state["status"] = "completed"
                state["render_progress"] = {
                    "current_seg": 0, "total_segs": len(segs),
                    "status": "done", "message": "全部固定窗口均已跳过",
                }
            return state

        pending_count = sum(seg["status"] == "pending" for seg in segs)
        with self._lock:
            state["status"] = "rendering"
            state["render_progress"] = {
                "current_seg": 0,
                "total_segs": pending_count,
                "status": "rendering",
                "message": f"正在批量生成 {pending_count} 个固定窗口的视频...",
            }
        thread = threading.Thread(
            target=self._render_all_windows_in_background,
            args=(job_id,),
            daemon=True,
            name=f"window-render-{job_id}",
        )
        thread.start()
        return state

    def _render_all_windows_in_background(self, job_id: str) -> None:
        """后台批量渲染所有已确认窗口，每个子区间生成独立视频。"""
        job_lock = self._get_job_lock(job_id)
        with job_lock:
            with self._lock:
                state = self.jobs[job_id]
            try:
                upload_path = Path(state.get("upload_path", ""))
                upload_size = upload_path.stat().st_size if upload_path.is_file() else 0
                free_space = shutil.disk_usage(settings.storage_path).free
                required_space = max(1024 * 1024 * 1024, upload_size * 2)
                if free_space < required_space:
                    raise RuntimeError(
                        f"存储空间不足：当前可用 {free_space / 1024**3:.2f}GB，"
                        f"生成该视频至少需要约 {required_space / 1024**3:.2f}GB，请清理磁盘后重试"
                    )
                segs = state.get("segments") or []
                pending_indices = [
                    idx for idx, seg in enumerate(segs)
                    if seg.get("status") == "pending"
                ]
                total = len(pending_indices)
                for position, seg_idx in enumerate(pending_indices, start=1):
                    with self._lock:
                        state["current_segment_idx"] = seg_idx
                        state["render_progress"] = {
                            "current_seg": position - 1,
                            "total_segs": total,
                            "status": "rendering",
                            "message": f"正在生成第 {position}/{total} 个固定窗口的视频...",
                        }
                    nodes.render_segment(state)
                    if segs[seg_idx].get("status") == "failed":
                        raise RuntimeError(
                            segs[seg_idx].get("error") or f"第 {seg_idx + 1} 个窗口渲染失败"
                        )
                    if state.get("cancel_requested"):
                        self._finish_cancel(job_id, state)
                        return

                with self._lock:
                    state["status"] = "completed"
                    state["render_progress"] = {
                        "current_seg": total,
                        "total_segs": total,
                        "status": "done",
                        "message": f"全部完成，共生成 {len(state.get('final_outputs') or [])} 个独立视频",
                    }
                logger.info("[%s] 固定窗口批量生成完成，共 %d 个独立视频",
                            job_id, len(state.get("final_outputs") or []))
            except Exception as e:
                logger.exception("[%s] 人像模式批量生成失败: %s", job_id, e)
                output_dir = settings.storage_path / "final" / job_id
                for pattern in ("*.part.mp4", "*_part*.mp4", "*.concat.txt"):
                    for temporary in output_dir.glob(pattern):
                        temporary.unlink(missing_ok=True)
                with self._lock:
                    state["status"] = "failed"
                    state["render_progress"] = {
                        "current_seg": int(state.get("current_segment_idx", 0)),
                        "total_segs": len([
                            seg for seg in state.get("segments") or []
                            if seg.get("status") != "skipped"
                        ]),
                        "status": "error",
                        "message": str(e),
                    }

    def _remove_job_files(self, job_id: str, state: WorkflowState) -> None:
        upload_path = Path(state.get("upload_path", ""))
        if upload_path.is_file():
            upload_path.unlink(missing_ok=True)
        for subdir in ("segments", "final", "frames"):
            shutil.rmtree(settings.storage_path / subdir / job_id, ignore_errors=True)

    def _finish_cancel(self, job_id: str, state: WorkflowState) -> None:
        with self._lock:
            self.jobs.pop(job_id, None)
            self._job_locks.pop(job_id, None)
        self._remove_job_files(job_id, state)

    def cancel_job(self, job_id: str) -> None:
        """取消任务；渲染中的任务在当前 FFmpeg 操作结束后停止。"""
        with self._lock:
            state = self.jobs.get(job_id)
            if not state:
                raise ValueError("任务不存在")
            if state.get("status") == "rendering":
                state["cancel_requested"] = True
                state["status"] = "cancelling"
                state["render_progress"] = {
                    "status": "cancelling", "message": "正在取消任务并清理文件…",
                }
                return
        self._finish_cancel(job_id, state)

    # ---- 任务列表 ----
    def get_all_jobs(self) -> List[Dict[str, Any]]:
        """返回所有任务列表（按创建时间倒序，最新的在前）。"""
        with self._lock:
            items = list(self.jobs.items())
        result = []
        for job_id, state in items:
            result.append({
                "job_id": job_id,
                "status": state.get("status", "unknown"),
                "mode": state.get("mode", "manual"),
                "created_at": state.get("created_at", 0),
                "video_info": state.get("video_info", {}),
                "render_progress": state.get("render_progress", {}),
                "final_outputs_count": len(state.get("final_outputs", [])),
                "segments_total": len(state.get("segments", [])),
                "current_segment_idx": state.get("current_segment_idx", 0),
            })
        # 按创建时间倒序
        result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return result

    def get(self, job_id: str) -> Optional[WorkflowState]:
        with self._lock:
            return self.jobs.get(job_id)


job_manager = JobManager()
