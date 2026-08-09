"""FastAPI Web 应用：视频智能处理 Agent。"""
from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.agents.workflow import job_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("web")

app = FastAPI(
    title="茶辑 · Coze 智能视频剪辑应用",
    description="面向 Coze 编程部署的单页视频剪辑应用，支持人物片段、文字 Logo、蒙版模糊和 4:3 批量成片。",
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ========== 静态资源（Agent 图标 logo.png 等） ==========
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ========== 静态文件 ==========
@app.get("/storage/{subdir}/{file_name:path}")
async def get_storage_file(subdir: str, file_name: str):
    base = settings.storage_path
    target = (base / subdir / file_name).resolve()
    if str(target).startswith(str(base)) and target.exists() and target.is_file():
        return FileResponse(target)
    raise HTTPException(404, "not found")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "coze-video-editor"}


# ========== 1. 首页 ==========
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "default_segment": settings.default_segment_seconds // 60,
        "max_minutes": settings.max_video_seconds // 60,
        "max_mb": settings.max_upload_mb,
        "can_accept": job_manager.can_accept_job(),
        "processing_count": sum(
            1 for s in job_manager.jobs.values()
            if s.get("status") in ("rendering", "detecting", "reviewing_regions", "reviewing_windows",
                                    "reviewing_segment", "portrait_matching", "portrait_matching_done")
        ),
    })


# ========== 2. 上传 ==========
@app.post("/upload", response_class=RedirectResponse)
async def upload(
    video: UploadFile = File(...),
    segment_minutes: int = Form(0),
    portrait_detection: bool = Form(False),
    mode: str = Form("manual"),
    ost_autocut: bool = Form(False),
    portraits: List[UploadFile] = File(default=[]),
):
    # /upload 以新布尔开关为准；保留 mode/portraits 参数仅避免旧客户端请求报错。
    mode = "portrait" if portrait_detection else "manual"

    if segment_minutes < 1 or segment_minutes > 180:
        raise HTTPException(400, "分割时长必须为 1..180 的整数分钟")
    segment_seconds = segment_minutes * 60

    # 检查系统资源
    if not job_manager.can_accept_job():
        raise HTTPException(503, "系统资源繁忙，请稍后再试")

    if not video.filename:
        raise HTTPException(400, "未选择视频文件")
    ext = Path(video.filename).suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v"}:
        raise HTTPException(400, f"不支持的视频格式: {ext}")

    safe_name = f"{uuid.uuid4().hex[:8]}{ext}"
    dest = settings.storage_path / "raw" / safe_name
    with dest.open("wb") as f:
        total = 0
        while chunk := await video.read(1024 * 1024):
            total += len(chunk)
            if total > settings.max_upload_mb * 1024 * 1024:
                dest.unlink(missing_ok=True)
                raise HTTPException(400, f"视频超过最大限制 {settings.max_upload_mb}MB")
            f.write(chunk)

    # 自动人物发现无需参考图；portrait_paths 仅保留给 new_job 旧接口兼容。
    portrait_paths: List[str] = []

    try:
        st = job_manager.new_job(
            str(dest), segment_seconds,
            mode=mode, portrait_paths=portrait_paths,
            ost_autocut=ost_autocut,
        )
    except Exception as e:
        logger.exception("任务启动失败")
        raise HTTPException(400, f"任务启动失败: {e}")

    # 立即跳转到进度页，detect 在后台线程运行，不阻塞浏览器
    return RedirectResponse(url=f"/job/{st.get('job_id')}/progress", status_code=303)


# ========== 3. 区域审核 ==========
@app.get("/job/{job_id}/review-regions", response_class=HTMLResponse)
async def review_regions(request: Request, job_id: str):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")
    return templates.TemplateResponse("review_regions.html", {
        "request": request,
        "job_id": job_id,
        "state": st,
        "sample_frame_url": _abs_to_url(st.get("sample_frame", "")),
        "width": st.get("video_info", {}).get("width", 1920),
        "height": st.get("video_info", {}).get("height", 1080),
        "banner": st.get("banner_region"),
        "logos": st.get("logo_regions") or [],
        "masks": st.get("mask_regions") or [],
    })


@app.post("/job/{job_id}/review-regions", response_class=RedirectResponse)
async def submit_regions(
    job_id: str,
    banner_x: int = Form(0), banner_y: int = Form(0),
    banner_w: int = Form(0), banner_h: int = Form(0),
    logos_json: str = Form("[]"),
    masks_json: str = Form("[]"),
    skip_detect: bool = Form(False),
):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")

    if skip_detect:
        banner_region = None
        logo_regions = []
        mask_regions = []
    else:
        # 横幅处理已完全移除：无论前端填什么都强制 None
        banner_region = None
        logo_regions = _parse_rect_json(logos_json)
        mask_regions = _parse_rect_json(masks_json)

    try:
        job_manager.confirm_regions(job_id, banner_region, logo_regions, mask_regions)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # 所有模式都跳进度页：后台线程正在生成第一段预览（manual）或执行人脸匹配（portrait）
    return RedirectResponse(url=f"/job/{job_id}/progress", status_code=303)


# ========== 4. 片段审核 ==========
@app.get("/job/{job_id}/review-segment/{seg_idx}", response_class=HTMLResponse)
async def review_segment(request: Request, job_id: str, seg_idx: int):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")
    segs: List[Dict[str, Any]] = st.get("segments") or []
    if seg_idx >= len(segs):
        return RedirectResponse(url=f"/job/{job_id}/done", status_code=303)
    seg = segs[seg_idx]
    return templates.TemplateResponse("review_segment.html", {
        "request": request,
        "job_id": job_id,
        "seg_idx": seg_idx,
        "seg_total": len(segs),
        "seg": seg,
        "preview_url": _abs_to_url(seg.get("preview_path", "")),
        "video_duration": st.get("video_info", {}).get("duration", 0),
    })


@app.post("/job/{job_id}/review-segment/{seg_idx}", response_class=RedirectResponse)
async def submit_segment_end(
    job_id: str,
    seg_idx: int,
    subsegments_json: str = Form("[]"),
    action: str = Form("confirm"),
):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")
    segs: List[Dict[str, Any]] = st.get("segments") or []
    if seg_idx < 0 or seg_idx >= len(segs):
        raise HTTPException(400, "片段索引无效")

    subsegments = []
    if action == "confirm":
        try:
            subsegments = json.loads(subsegments_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "子片段 JSON 格式无效") from exc
        if not isinstance(subsegments, list) or not subsegments:
            raise HTTPException(400, "确认时至少需要一个有效子片段")

    # 启动后台渲染线程，立即返回
    try:
        job_manager.confirm_segment_end(job_id, seg_idx, subsegments=subsegments, action=action)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # 跳转到进度页，用户可继续其他业务
    return RedirectResponse(url=f"/job/{job_id}/progress", status_code=303)


# ========== 4'. 全部固定窗口统一编辑 ==========
@app.get("/job/{job_id}/review-windows", response_class=HTMLResponse)
@app.get("/job/{job_id}/portrait-results", response_class=HTMLResponse)
async def portrait_results(request: Request, job_id: str):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")
    detected_people = []
    for person in (st.get("detected_people") or [])[:3]:
        item = dict(person)
        item["thumbnail_url"] = _abs_to_url(item.get("thumbnail_path", ""))
        detected_people.append(item)
    return templates.TemplateResponse("portrait_results.html", {
        "request": request,
        "job_id": job_id,
        "mode": st.get("mode", "manual"),
        "matched_segments": st.get("matched_segments") or [],
        "detected_people": detected_people,
        "video_url": _abs_to_url(st.get("upload_path", "")),
        "video_duration": st.get("video_info", {}).get("duration", 0),
        "video_width": st.get("video_info", {}).get("width", 1920),
        "video_height": st.get("video_info", {}).get("height", 1080),
        "logos": st.get("logo_regions") or [],
        "masks": st.get("mask_regions") or [],
        "segment_minutes": int(st.get("segment_seconds") or settings.default_segment_seconds) // 60,
        "analysis_segments": st.get("analysis_segments") or [],
    })


@app.post("/job/{job_id}/confirm-windows", response_class=RedirectResponse)
@app.post("/job/{job_id}/confirm-portrait", response_class=RedirectResponse)
async def confirm_portrait(
    job_id: str,
    windows_json: Optional[str] = Form(None),
    selected_person_ids: Optional[str] = Form(None),
    segments_json: str = Form("[]"),
    logos_json: str = Form("[]"),
    masks_json: str = Form("[]"),
):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")
    window_selections = None
    if windows_json is not None:
        try:
            window_selections = json.loads(windows_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "固定窗口 JSON 格式无效") from exc
        if not isinstance(window_selections, list):
            raise HTTPException(400, "windows_json 必须为列表")

    person_ids = None
    if selected_person_ids is not None:
        try:
            person_ids = json.loads(selected_person_ids)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "人物 ID JSON 格式无效") from exc
        if not isinstance(person_ids, list):
            raise HTTPException(400, "selected_person_ids 必须为列表")
    try:
        selected = json.loads(segments_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "片段 JSON 格式无效") from exc
    if not isinstance(selected, list):
        raise HTTPException(400, "片段必须为列表")
    logo_regions = _parse_text_logo_json(logos_json)
    mask_regions = _parse_rect_json(masks_json)
    st["logo_regions"] = logo_regions
    st["mask_regions"] = mask_regions
    try:
        if st.get("mode") == "manual":
            job_manager.confirm_windows(job_id, window_selections or [])
        else:
            job_manager.confirm_portrait(
                job_id,
                selected_segments=selected,
                selected_person_ids=person_ids,
                window_selections=window_selections,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(url=f"/job/{job_id}/progress", status_code=303)


# ========== 5. 完成页 ==========
@app.get("/job/{job_id}/done", response_class=HTMLResponse)
async def job_done(request: Request, job_id: str):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")
    outputs: List[str] = st.get("final_outputs") or []
    output_urls = [(Path(p).name, _abs_to_url(p)) for p in outputs]
    return templates.TemplateResponse("done.html", {
        "request": request,
        "job_id": job_id,
        "state": st,
        "output_urls": output_urls,
    })


# ========== 6. 任务列表页 ==========
@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    all_jobs = job_manager.get_all_jobs()
    # 处理中：渲染中 / 检测中 / 等待区域审核 / 等待片段审核
    processing = [
        j for j in all_jobs
        if j["status"] in ("rendering", "detecting", "reviewing_regions", "reviewing_windows",
                           "reviewing_segment", "portrait_matching", "portrait_matching_done")
    ]
    # 已完成：完成 / 失败
    completed = [j for j in all_jobs if j["status"] in ("completed", "failed")]
    can_accept = job_manager.can_accept_job()
    return templates.TemplateResponse("tasks.html", {
        "request": request,
        "processing": processing,
        "completed": completed,
        "can_accept": can_accept,
    })


# ========== 7. 进度页 ==========
@app.get("/job/{job_id}/progress", response_class=HTMLResponse)
async def progress_page(request: Request, job_id: str):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")
    segs = st.get("segments") or []
    cur = int(st.get("current_segment_idx", 0))
    outputs_total = sum(
        len(s.get("subsegments") or []) for s in segs
        if s.get("status") != "skipped"
    ) or len([s for s in segs if s.get("status") != "skipped"])
    # 标记每段状态用于UI展示
    seg_status_list = [
        {"idx": i, "status": s.get("status", "pending"), "preview_url": _abs_to_url(s.get("preview_path", ""))}
        for i, s in enumerate(segs)
    ]
    return templates.TemplateResponse("progress.html", {
        "request": request,
        "job_id": job_id,
        "state": st,
        "segments_total": len(segs),
        "outputs_total": outputs_total,
        "current_segment_idx": cur,
        "final_outputs_count": len(st.get("final_outputs") or []),
        "seg_status_list": seg_status_list,
    })


# ========== 8. JSON API：轮询任务状态 ==========
@app.get("/api/job/{job_id}/status")
async def job_status_api(job_id: str):
    st = job_manager.get(job_id)
    if not st:
        raise HTTPException(404, "任务不存在")
    rp = st.get("render_progress") or {}
    segs = st.get("segments") or []
    outputs_total = sum(
        len(s.get("subsegments") or []) for s in segs
        if s.get("status") != "skipped"
    ) or len([s for s in segs if s.get("status") != "skipped"])
    # 每段状态概览
    seg_brief = [
        {"idx": i, "status": s.get("status", "pending"),
         "output_url": _abs_to_url(s.get("output_path", ""))}
        for i, s in enumerate(segs)
    ]
    return JSONResponse({
        "status": st.get("status"),
        "mode": st.get("mode", "manual"),
        "render_progress": rp,
        "current_segment_idx": st.get("current_segment_idx", 0),
        "segments_total": len(segs),
        "outputs_total": outputs_total,
        "final_outputs_count": len(st.get("final_outputs") or []),
        "segments": seg_brief,
    })


# ========== 9. JSON API：系统资源 ==========
@app.get("/api/system/resources")
async def system_resources():
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory().percent
    except Exception as e:
        logger.warning("psutil 读取资源失败: %s", e)
        cpu, mem = 0.0, 0.0
    rendering_count = sum(
        1 for s in job_manager.jobs.values() if s.get("status") == "rendering"
    )
    return JSONResponse({
        "cpu_percent": cpu,
        "memory_percent": mem,
        "can_accept": job_manager.can_accept_job(),
        "rendering_count": rendering_count,
    })


# ========== 工具函数 ==========

def _parse_text_logo_json(json_str: str) -> List[dict]:
    try:
        arr = json.loads(json_str)
        result = []
        for item in arr:
            text = str(item.get("text", "")).strip()[:80]
            if not text:
                continue
            x = max(0, int(item.get("x", 0)))
            y = max(0, int(item.get("y", 0)))
            font_size = max(12, min(160, int(item.get("font_size", 36))))
            color = str(item.get("color", "white"))
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                color = "#ffffff"
            result.append({"text": text, "x": x, "y": y, "font_size": font_size, "color": color})
        return result
    except Exception:
        return []


def _parse_rect_json(json_str: str) -> List[tuple]:
    """解析区域 JSON，兼容 4 种格式（向后兼容 + 新字段 mode/strength）：

    旧格式（Logo 蒙版和旧前端，默认 blur+medium）：
      [{"x":..,"y":..,"w":..,"h":..}, ...]                     → 4-tuple (x,y,w,h)
    新格式（手动蒙版区域，带模式 + 强度）：
      [{"x":..,"y":..,"w":..,"h":..,"mode":"blur","strength":"medium"}, ...]   → 6-tuple
    """
    try:
        arr = json.loads(json_str)
        result = []
        for r in arr:
            x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
            if w <= 0 or h <= 0:
                continue
            mode = str(r.get("mode", "blur")).lower()
            strength = str(r.get("strength", "medium")).lower()
            if mode not in ("blur", "cover"):
                mode = "blur"
            if strength not in ("light", "medium", "strong"):
                strength = "medium"
            result.append((x, y, w, h, mode, strength))
        return result
    except Exception:
        return []


def _abs_to_url(abs_path: Optional[str]) -> str:
    if not abs_path:
        return ""
    p = Path(abs_path).resolve()
    base = settings.storage_path.resolve()
    try:
        rel = p.relative_to(base)
    except ValueError:
        return ""
    parts = rel.parts
    if len(parts) >= 2:
        subdir = parts[0]
        file_path = "/".join(parts[1:])
        return f"/storage/{subdir}/{file_path}"
    return ""
