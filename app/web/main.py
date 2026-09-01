"""FastAPI Web 应用：视频智能处理 Agent。"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse
from typing import Any, Dict, List, Optional

import httpx
import psutil
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.agents.workflow import job_manager
from app.web.auth import (
    authenticate_phone, authenticate_user, bind_phone, create_user,
    change_password, current_user, init_auth_db, require_user,
    session_secret, update_profile, validate_phone_code, visitor_identity,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("web")

ALLOWED_VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v", ".rm", ".rmvb",
}

app = FastAPI(
    title="茶辑 · Coze 智能视频剪辑应用",
    description="面向 Coze 编程部署的单页视频剪辑应用，支持人物片段、文字 Logo、蒙版模糊和 4:3 批量成片。",
)
init_auth_db()
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    session_cookie="tea_session",
    max_age=30 * 24 * 60 * 60,
    same_site="lax",
    https_only=False,
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
    if subdir not in {"raw", "segments", "final", "frames", "portraits"}:
        raise HTTPException(404, "not found")
    base = settings.storage_path
    target = (base / subdir / file_name).resolve()
    if str(target).startswith(str(base / subdir)) and target.exists() and target.is_file():
        return FileResponse(target)
    raise HTTPException(404, "not found")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "coze-video-editor"}


@app.post("/feishu/events")
async def feishu_events(request: Request):
    if not settings.feishu_enabled:
        raise HTTPException(404, "飞书 Bot 未启用")
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise HTTPException(503, "飞书 Bot 凭证未配置")
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "请求体必须是有效 JSON") from exc
    if "encrypt" in body:
        raise HTTPException(400, "当前版本不支持加密事件，请在飞书后台关闭事件加密")
    from app.integrations.feishu_bot import handle_event
    try:
        return JSONResponse(handle_event(body))
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc


# ========== 用户账户 ==========
@app.post("/auth/sms-code")
async def sms_code(phone: str = Form(...)):
    try:
        validate_phone_code(phone, "123456")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"sent": True, "development_code": "123456"}


@app.post("/auth/register")
async def register(
    request: Request, username: str = Form(...), phone: str = Form(...),
    code: str = Form(...), password: str = Form(...),
):
    try:
        user = await asyncio.to_thread(create_user, username, phone, code, password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    request.session.clear()
    request.session["user_id"] = user["id"]
    return {"authenticated": True, "user": user}


@app.post("/auth/login")
async def login(
    request: Request, account: str = Form(...), password: str = Form(""),
    phone_code: str = Form(""),
):
    try:
        user = (
            await asyncio.to_thread(authenticate_phone, account, phone_code)
            if phone_code else await asyncio.to_thread(authenticate_user, account, password)
        )
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    if not user:
        raise HTTPException(401, "账号、密码或验证码不正确")
    request.session.clear()
    request.session["user_id"] = user["id"]
    return {"authenticated": True, "user": user}


@app.post("/auth/bind-phone")
async def bind_user_phone(request: Request, phone: str = Form(...), code: str = Form(...)):
    user = require_user(request)
    try:
        updated = await asyncio.to_thread(bind_phone, user["id"], phone, code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user": updated}


@app.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return {"authenticated": False}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = current_user(request)
    return {"authenticated": bool(user), "user": user}


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    user = require_user(request)
    return templates.TemplateResponse("account.html", {"request": request, "current_user": user})


@app.post("/api/account/profile")
async def account_profile_update(
    request: Request, username: str = Form(...), phone: str = Form(""), code: str = Form(""),
):
    user = require_user(request)
    try:
        updated = await asyncio.to_thread(update_profile, user["id"], username, phone, code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user": updated}


@app.post("/api/account/password")
async def account_password_update(
    request: Request, old_password: str = Form(...), new_password: str = Form(...),
):
    user = require_user(request)
    try:
        await asyncio.to_thread(change_password, user["id"], old_password, new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"updated": True}


# ========== 1. 首页 ==========
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    identity = visitor_identity(request)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "current_user": current_user(request),
        "identity": identity,
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
@app.post("/upload-stream")
async def upload_stream(request: Request):
    """直接把原始视频请求体写入存储，避免 multipart 临时文件的二次复制。"""
    if not job_manager.can_accept_job():
        raise HTTPException(503, "系统资源繁忙，请稍后再试")

    original_name = request.headers.get("X-Video-Filename", "video.mp4")
    ext = Path(unquote(original_name)).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTS:
        raise HTTPException(400, f"不支持的视频格式: {ext}")

    dest = settings.storage_path / "raw" / f"{uuid.uuid4().hex[:8]}{ext}"
    total = 0
    limit = settings.max_upload_mb * 1024 * 1024
    try:
        with dest.open("wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise HTTPException(400, f"视频超过最大限制 {settings.max_upload_mb}MB")
                output.write(chunk)
        if total == 0:
            raise HTTPException(400, "视频文件为空")
        state = job_manager.new_job(
            str(dest), 0,
            mode="portrait" if request.query_params.get("portrait_detection") == "true" else "manual",
            portrait_paths=[],
            ost_autocut=request.query_params.get("ost_autocut") == "true",
        )
        return {"job_id": state["job_id"], "progress_url": f"/job/{state['job_id']}/progress"}
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _validate_public_video_url(video_url: str) -> str:
    parsed = urlparse(video_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "请输入有效的 HTTP 或 HTTPS 视频链接")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(400, "视频链接域名无法解析") from exc
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise HTTPException(400, "不允许访问内网或本机地址")
    return video_url.strip()


def _run_checked(
    command: List[str], *, timeout: int,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, check=False, capture_output=True, text=True,
        timeout=timeout, env=env, input=input_text,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "命令执行失败")[-1200:].strip()
        raise RuntimeError(detail)
    return result


def _baidu_config_path(user_id: int) -> Path:
    path = settings.storage_path / "baidu-auth" / str(user_id) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _baidu_official_enabled() -> bool:
    return bool(settings.baidu_app_key and settings.baidu_app_secret)


def _baidu_token_path(user_id: int) -> Path:
    path = settings.storage_path / "baidu-auth" / str(user_id) / "oauth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_baidu_token(user_id: int, payload: Dict[str, Any]) -> None:
    expires_in = int(payload.get("expires_in") or 0)
    token = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or "",
        "expires_at": int(time.time()) + expires_in,
    }
    path = _baidu_token_path(user_id)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(token), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def _baidu_token_request(params: Dict[str, str]) -> Dict[str, Any]:
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.post("https://openapi.baidu.com/oauth/2.0/token", data=params)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("百度 OAuth 返回了无法识别的数据") from exc
    if response.is_error or payload.get("error"):
        raise RuntimeError(str(payload.get("error_description") or payload.get("error") or "百度 OAuth 请求失败"))
    return payload


def _baidu_access_token(user_id: int) -> str:
    path = _baidu_token_path(user_id)
    try:
        token = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("百度网盘尚未授权") from exc
    if int(token.get("expires_at") or 0) <= int(time.time()) + 300:
        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            path.unlink(missing_ok=True)
            raise RuntimeError("百度网盘授权已过期，请重新授权")
        try:
            payload = _baidu_token_request({
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.baidu_app_key,
                "client_secret": settings.baidu_app_secret,
            })
            _write_baidu_token(user_id, payload)
            token = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return str(token["access_token"])


def _baidu_api_get(user_id: int, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    query = dict(params)
    query["access_token"] = _baidu_access_token(user_id)
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(endpoint, params=query)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("百度网盘 API 返回了无法识别的数据") from exc
    errno = payload.get("errno", 0)
    if response.is_error or errno != 0:
        raise RuntimeError(f"百度网盘 API 请求失败（errno={errno}）")
    return payload


def _bdpan_command(user_id: int, *args: str) -> List[str]:
    return [
        "bdpan", *args,
        "--config-path", str(_baidu_config_path(user_id)),
        "--no-check-update",
    ]


def _bdpan_json(user_id: int, *args: str, timeout: int = 60) -> Any:
    result = _run_checked(_bdpan_command(user_id, *args, "--json"), timeout=timeout)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("百度网盘返回了无法识别的数据") from exc
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        message = str(payload.get("error") or "百度网盘操作失败")
        raise RuntimeError(message)
    return payload


def _baidu_auth_status(user_id: int) -> Dict[str, Any]:
    if _baidu_official_enabled():
        try:
            _baidu_access_token(user_id)
            token = json.loads(_baidu_token_path(user_id).read_text(encoding="utf-8"))
            return {"authenticated": True, "expires_at": token.get("expires_at")}
        except (RuntimeError, OSError, json.JSONDecodeError):
            return {"authenticated": False, "expires_at": None}
    try:
        result = _run_checked(
            _bdpan_command(user_id, "whoami", "--json"), timeout=30,
        )
        status = json.loads(result.stdout)
        return {
            "authenticated": bool(status.get("authenticated")),
            "expires_at": status.get("expires_at"),
        }
    except (RuntimeError, json.JSONDecodeError):
        return {"authenticated": False, "expires_at": None}


def _normalize_baidu_path(remote_path: str) -> str:
    path = remote_path.strip().replace("\\", "/").strip("/")
    if not path or ".." in Path(path).parts:
        return path
    if path == "apps/bdpan":
        return ""
    if path.startswith("apps/bdpan/"):
        return path[len("apps/bdpan/"):]
    return path


def _baidu_file_list(user_id: int, remote_path: str = "") -> List[Dict[str, Any]]:
    path = _normalize_baidu_path(remote_path)
    if _baidu_official_enabled():
        app_root = f"/apps/{settings.baidu_app_name.strip('/')}"
        remote_dir = f"{app_root}/{path}" if path else app_root
        payload = _baidu_api_get(
            user_id,
            "https://pan.baidu.com/rest/2.0/xpan/file",
            {"method": "list", "dir": remote_dir, "order": "name", "desc": 0, "start": 0, "limit": 1000},
        )
    else:
        payload = _bdpan_json(user_id, "ls", path) if path else _bdpan_json(user_id, "ls")
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("data") or payload.get("list") or []
    else:
        items = []
    if isinstance(items, dict):
        items = items.get("items") or items.get("list") or items.get("files") or []
    if not isinstance(items, list):
        raise RuntimeError("百度网盘文件列表格式不正确")
    result = []
    for item in items:
        name = str(item.get("name") or item.get("server_filename") or "")
        is_dir = bool(item.get("is_dir") or item.get("isdir"))
        item_path = "/".join(part for part in (path, name) if part)
        if is_dir or Path(name).suffix.lower() in ALLOWED_VIDEO_EXTS:
            result.append({
                "name": name, "path": item_path, "is_dir": is_dir,
                "size": int(item.get("size") or 0),
            })
    result.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    return result


def _baidu_share_list(
    user_id: int, video_url: str, code: str, source_dir: str = "",
) -> List[Dict[str, Any]]:
    command = _bdpan_command(
        user_id, "transfer", "list", video_url,
        "--page", "1", "--page-size", "100", "--json",
    )
    if source_dir:
        command.extend(["--source-dir", source_dir])
    if code and "pwd=" not in video_url:
        command.extend(["--pwd", code])
    result = _run_checked(command, timeout=60)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("无法识别该百度网盘分享中的文件") from exc
    if payload.get("code") not in (None, 0):
        raise RuntimeError(payload.get("error") or "无法查询百度网盘分享内容")
    return payload.get("items") or []


def _collect_baidu_share_videos(
    user_id: int, video_url: str, code: str, source_dir: str = "",
) -> List[Dict[str, Any]]:
    videos: List[Dict[str, Any]] = []
    for item in _baidu_share_list(user_id, video_url, code, source_dir):
        if item.get("is_dir"):
            videos.extend(_collect_baidu_share_videos(
                user_id, video_url, code, str(item.get("path") or ""),
            ))
        elif Path(str(item.get("name") or item.get("path") or "")).suffix.lower() in ALLOWED_VIDEO_EXTS:
            videos.append(item)
    return videos


def _find_owned_baidu_path(user_id: int, shared_video: Dict[str, Any]) -> Optional[str]:
    name = str(shared_video.get("name") or Path(str(shared_video.get("path") or "")).name)
    size = int(shared_video.get("size") or 0)
    result = _run_checked(
        _bdpan_command(
            user_id, "search", name, "--category", "1",
            "--page-size", "50", "--page", "1", "--json",
        ),
        timeout=60,
    )
    try:
        items = json.loads(result.stdout).get("items") or []
    except json.JSONDecodeError as exc:
        raise RuntimeError("无法定位当前账号中的分享源文件") from exc
    exact = [
        item for item in items
        if item.get("server_filename") == name and int(item.get("size") or 0) == size
    ]
    return str(exact[0]["path"]) if len(exact) == 1 else None


def _download_own_baidu_share(
    user_id: int, video_url: str, code: str, work_dir: Path,
) -> None:
    videos = _collect_baidu_share_videos(user_id, video_url, code)
    if not videos:
        raise RuntimeError("该分享由当前账号创建，但分享中没有支持的视频文件")
    if len(videos) > 1:
        raise RuntimeError("百度网盘分享包含多个视频，请创建仅包含一个视频的分享链接")
    remote_path = _find_owned_baidu_path(user_id, videos[0])
    if not remote_path:
        raise RuntimeError("无法在当前账号中唯一定位该分享的原始视频，请使用单独文件分享")
    authorized_prefix = "/apps/bdpan/"
    if not remote_path.startswith(authorized_prefix):
        raise RuntimeError(
            "这是当前百度账号自己的分享，原视频不在“我的应用数据/bdpan”授权目录内，"
            "百度禁止重复转存且不允许本应用直接下载该目录；请改用另一个百度账号授权，"
            "或先将原视频放入“我的应用数据/bdpan”后重新分享"
        )
    authorized_path = remote_path[len(authorized_prefix):]
    _run_checked(
        _bdpan_command(user_id, "download", authorized_path, str(work_dir)),
        timeout=3600,
    )


def _download_baidu_share(user_id: int, video_url: str, extraction_code: str, target: Path) -> Path:
    if not _baidu_auth_status(user_id)["authenticated"]:
        raise RuntimeError("百度网盘尚未登录，请先在首页完成百度授权")
    code = extraction_code.strip()
    if code and not re.fullmatch(r"[A-Za-z0-9]{4,8}", code):
        raise RuntimeError("百度网盘提取码格式不正确")
    work_dir = target.parent / f"baidu_{target.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    command = _bdpan_command(user_id, "download", video_url, str(work_dir))
    if code and "pwd=" not in video_url:
        command.extend(["--pwd", code])
    try:
        try:
            _run_checked(command, timeout=3600)
        except RuntimeError as exc:
            message = str(exc)
            if "13045" in message:
                _download_own_baidu_share(user_id, video_url, code, work_dir)
            elif "13003" in message:
                raise RuntimeError("百度网盘提取码缺失或不正确") from exc
            elif (
                "13000" in message or "13004" in message
                or "share link record not found" in message
                or "失效" in message or "不存在" in message
            ):
                raise RuntimeError("百度网盘分享链接已失效、已取消或不存在") from exc
            elif (
                "errno=-6" in message or "errno: -6" in message
                or "登录" in message or "token" in message.lower() or "认证" in message
            ):
                _baidu_config_path(user_id).unlink(missing_ok=True)
                raise RuntimeError(
                    "百度网盘授权已失效或当前授权账号无文件访问权限，请重新授权其他百度账号"
                ) from exc
            else:
                raise
        candidates = [
            path for path in work_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in ALLOWED_VIDEO_EXTS
        ]
        if not candidates:
            raise RuntimeError("百度网盘分享中没有找到支持的视频文件")
        if len(candidates) > 1:
            raise RuntimeError("百度网盘分享包含多个视频，请创建仅包含一个视频的分享链接")
        if candidates[0].stat().st_size > settings.max_upload_mb * 1024 * 1024:
            raise RuntimeError(f"视频超过最大限制 {settings.max_upload_mb}MB")
        shutil.move(str(candidates[0]), str(target))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/api/baidu-auth/status")
async def baidu_auth_status_api(request: Request):
    user = require_user(request)
    return await asyncio.to_thread(_baidu_auth_status, user["id"])


@app.post("/api/baidu-auth/start")
async def baidu_auth_start_api(request: Request, switch_account: bool = Form(False)):
    user = require_user(request)
    try:
        if _baidu_official_enabled():
            _baidu_token_path(user["id"]).unlink(missing_ok=True)
            auth_url = "https://openapi.baidu.com/oauth/2.0/authorize?" + urlencode({
                "response_type": "code",
                "client_id": settings.baidu_app_key,
                "redirect_uri": settings.baidu_oauth_redirect_uri,
                "scope": "basic,netdisk",
                "display": "popup",
            })
        else:
            config_path = _baidu_config_path(user["id"])
            config_path.unlink(missing_ok=True)
            result = await asyncio.to_thread(
                _run_checked,
                _bdpan_command(user["id"], "login", "--get-auth-url", "--accept-disclaimer"),
                timeout=30,
            )
            auth_url = result.stdout.strip()
        parsed = urlparse(auth_url)
        if parsed.scheme != "https" or parsed.hostname not in {"openapi.baidu.com", "wappass.baidu.com"}:
            raise RuntimeError("授权地址校验失败")
        request.session["baidu_auth_started_at"] = int(time.time())
        request.session["baidu_auth_submitted"] = False
        return {"auth_url": auth_url}
    except Exception as exc:
        logger.exception("获取百度网盘授权链接失败")
        raise HTTPException(502, f"无法获取百度网盘授权链接：{exc}") from exc


@app.post("/api/baidu-auth/complete")
async def baidu_auth_complete_api(request: Request, auth_code: str = Form(...)):
    user = require_user(request)
    code = auth_code.strip()
    started_at = int(request.session.get("baidu_auth_started_at") or 0)
    if not started_at or time.time() - started_at > 600:
        raise HTTPException(400, "授权会话已过期，请点击“重新获取授权链接”后再试")
    if request.session.get("baidu_auth_submitted"):
        raise HTTPException(409, "该授权码已经提交过，请重新获取授权链接和新授权码")
    code_pattern = r"[A-Za-z0-9._~-]{20,128}" if _baidu_official_enabled() else r"[A-Fa-f0-9]{32}"
    if not re.fullmatch(code_pattern, code):
        raise HTTPException(400, "授权码格式不正确")
    request.session["baidu_auth_submitted"] = True
    try:
        if _baidu_official_enabled():
            payload = await asyncio.to_thread(
                _baidu_token_request,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.baidu_app_key,
                    "client_secret": settings.baidu_app_secret,
                    "redirect_uri": settings.baidu_oauth_redirect_uri,
                },
            )
            await asyncio.to_thread(_write_baidu_token, user["id"], payload)
        else:
            await asyncio.to_thread(
                _run_checked,
                _bdpan_command(user["id"], "login", "--set-code-stdin", "--accept-disclaimer"),
                timeout=60,
                input_text=f"{code}\n",
            )
        status = await asyncio.to_thread(_baidu_auth_status, user["id"])
        if not status["authenticated"]:
            raise RuntimeError("授权码兑换完成，但登录状态验证失败")
        request.session.pop("baidu_auth_started_at", None)
        request.session.pop("baidu_auth_submitted", None)
        logger.info("百度网盘授权码兑换成功 user_id=%s", user["id"])
        return {"authenticated": True}
    except Exception as exc:
        (_baidu_token_path(user["id"]) if _baidu_official_enabled() else _baidu_config_path(user["id"])).unlink(missing_ok=True)
        detail = str(exc).split("\n", 1)[0].removeprefix("Error: ").strip()
        logger.warning("百度网盘授权码兑换失败 user_id=%s reason=%s", user["id"], detail)
        raise HTTPException(
            400,
            "授权码兑换失败：授权码无效、已使用或已过期。请确认复制的是百度页面最终显示的32位授权码",
        ) from exc


@app.post("/api/baidu-auth/logout")
async def baidu_auth_logout_api(request: Request):
    user = require_user(request)
    _baidu_config_path(user["id"]).unlink(missing_ok=True)
    _baidu_token_path(user["id"]).unlink(missing_ok=True)
    return {"authenticated": False}


@app.get("/api/baidu/files")
async def baidu_files_api(request: Request, path: str = ""):
    user = require_user(request)
    normalized = _normalize_baidu_path(path)
    if ".." in Path(normalized).parts:
        raise HTTPException(400, "网盘目录路径不正确")
    try:
        items = await asyncio.to_thread(_baidu_file_list, user["id"], normalized)
        return {"path": normalized, "items": items}
    except Exception as exc:
        detail = str(exc).split("\n", 1)[0].removeprefix("Error: ").strip()
        logger.warning("读取百度网盘文件失败 user_id=%s reason=%s", user["id"], detail)
        if "errno=-6" in str(exc) or "errno: -6" in str(exc):
            raise HTTPException(
                403,
                "百度账号已授权，但当前 bdpan 应用没有网盘文件访问权限。请确认百度授权页面已勾选网盘权限",
            ) from exc
        raise HTTPException(400, f"读取百度网盘文件失败：{detail}") from exc


_baidu_downloads: Dict[str, Dict[str, Any]] = {}
_baidu_downloads_lock = threading.Lock()
_baidu_download_processes: Dict[str, subprocess.Popen] = {}
_baidu_download_processes_lock = threading.Lock()


def _update_baidu_download(import_id: str, **values: Any) -> None:
    with _baidu_downloads_lock:
        if import_id in _baidu_downloads:
            _baidu_downloads[import_id].update(values)
    job_manager.update_import_job(import_id, **values)


def _baidu_download_command(user_id: int, normalized: str, target: Path) -> tuple[List[str], str]:
    if not _baidu_official_enabled():
        return _bdpan_command(user_id, "download", normalized, str(target)), ""
    parent, _, name = normalized.rpartition("/")
    app_root = f"/apps/{settings.baidu_app_name.strip('/')}"
    remote_dir = f"{app_root}/{parent}" if parent else app_root
    listing = _baidu_api_get(
        user_id,
        "https://pan.baidu.com/rest/2.0/xpan/file",
        {"method": "list", "dir": remote_dir, "start": 0, "limit": 1000},
    )
    matches = [item for item in listing.get("list", []) if item.get("server_filename") == name and not item.get("isdir")]
    if len(matches) != 1:
        raise RuntimeError("无法在百度网盘中唯一定位所选文件")
    access_token = _baidu_access_token(user_id)
    metadata = _baidu_api_get(
        user_id,
        "https://pan.baidu.com/rest/2.0/xpan/multimedia",
        {"method": "filemetas", "fsids": json.dumps([matches[0]["fs_id"]]), "dlink": 1},
    )
    files = metadata.get("list") or []
    if not files or not files[0].get("dlink"):
        raise RuntimeError("百度网盘未返回文件下载地址")
    separator = "&" if "?" in files[0]["dlink"] else "?"
    download_url = f"{files[0]['dlink']}{separator}access_token={access_token}"
    connections = min(4, max(1, settings.baidu_aria2_connections))
    splits = min(connections, max(1, settings.baidu_aria2_split))
    command = [
        "aria2c", "--allow-overwrite=true", "--auto-file-renaming=false", "--continue=true",
        f"--max-connection-per-server={connections}", f"--split={splits}",
        f"--min-split-size={settings.baidu_aria2_min_split_size}", "--file-allocation=none",
        "--max-tries=8", "--retry-wait=5", "--timeout=60", "--connect-timeout=30",
        "--lowest-speed-limit=1K", "--summary-interval=1",
        "--console-log-level=warn", "--user-agent=pan.baidu.com", "--input-file=-",
        f"--dir={target.parent}", f"--out={target.name}",
    ]
    return command, download_url


def _download_baidu_file_in_background(
    import_id: str,
    user_id: int,
    normalized: str,
    target: Path,
    total_bytes: int,
    portrait_detection: bool,
) -> None:
    log_path: Optional[Path] = None
    sensitive_value = ""
    try:
        command, sensitive_value = _baidu_download_command(user_id, normalized, target)
        with tempfile.NamedTemporaryFile(delete=False) as log_file:
            log_path = Path(log_file.name)
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if sensitive_value else None,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=bool(sensitive_value),
            )
            with _baidu_download_processes_lock:
                _baidu_download_processes[import_id] = process
            if sensitive_value and process.stdin:
                process.stdin.write(
                    f"{sensitive_value}\n"
                    f"  dir={target.parent}\n"
                    f"  out={target.name}\n"
                )
                process.stdin.close()
            last_bytes = target.stat().st_size if target.is_file() else 0
            last_sample_at = time.monotonic()
            speed = 0.0
            downloaded = 0 if sensitive_value else last_bytes
            aria2_log_offset = 0
            aria2_log_tail = ""
            aria2_progress_re = re.compile(
                r"\[#\w+\s+([\d.]+)(KiB|MiB|GiB)/(\d+(?:\.\d+)?)(KiB|MiB|GiB)\((\d+)%\)"
            )
            size_multipliers = {"KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
            while process.poll() is None:
                if not job_manager.get(import_id):
                    process.terminate()
                    process.wait(timeout=10)
                    return
                if sensitive_value and log_path and log_path.exists():
                    with log_path.open("r", encoding="utf-8", errors="replace") as aria2_log:
                        aria2_log.seek(aria2_log_offset)
                        chunk = aria2_log.read()
                        aria2_log_offset = aria2_log.tell()
                    progress_text = aria2_log_tail + chunk
                    matches = list(aria2_progress_re.finditer(progress_text))
                    aria2_log_tail = progress_text[-300:]
                    if matches:
                        match = matches[-1]
                        completed = int(float(match.group(1)) * size_multipliers[match.group(2)])
                        downloaded = max(downloaded, completed)
                else:
                    downloaded = max(downloaded, target.stat().st_size if target.is_file() else 0)
                if total_bytes:
                    downloaded = min(total_bytes, downloaded)
                now = time.monotonic()
                elapsed = now - last_sample_at
                if elapsed >= 1:
                    speed = max(0.0, (downloaded - last_bytes) / elapsed)
                    last_bytes = downloaded
                    last_sample_at = now
                percent = min(99, int(downloaded * 100 / total_bytes)) if total_bytes else None
                speed_text = f" · {speed / 1024 / 1024:.1f} MB/s" if speed > 0 else ""
                _update_baidu_download(
                    import_id,
                    downloaded_bytes=downloaded,
                    percent=percent,
                    message=(
                        f"正在下载视频… {percent}%{speed_text}" if percent is not None
                        else f"正在下载视频… 已下载 {downloaded / 1024 / 1024:.1f} MB{speed_text}"
                    ),
                )
                time.sleep(0.5)

        if process.returncode != 0:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:].strip()
            if sensitive_value:
                detail = detail.replace(sensitive_value, "[REDACTED]")
            raise RuntimeError(detail or "百度网盘下载失败")
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError("所选网盘视频下载结果不正确")
        if target.stat().st_size > settings.max_upload_mb * 1024 * 1024:
            raise RuntimeError(f"视频超过最大限制 {settings.max_upload_mb}MB")

        job_manager.finalize_import_job(import_id, str(target))
        with _baidu_downloads_lock:
            _baidu_downloads[import_id].update({
                "status": "completed",
                "downloaded_bytes": target.stat().st_size,
                "percent": 100,
                "message": "下载完成，正在进入视频处理…",
                "progress_url": f"/job/{import_id}/progress",
            })
    except Exception as exc:
        target.unlink(missing_ok=True)
        if job_manager.get(import_id):
            message = str(exc)
            if "errno=-6" in message or "errno: -6" in message:
                (_baidu_token_path(user_id) if _baidu_official_enabled() else _baidu_config_path(user_id)).unlink(missing_ok=True)
                message = "百度网盘授权已失效，请重新授权"
            logger.exception("百度网盘文件后台导入失败")
            _update_baidu_download(import_id, status="failed", message=f"无法导入所选网盘视频：{message}")
    finally:
        with _baidu_download_processes_lock:
            _baidu_download_processes.pop(import_id, None)
        if log_path:
            log_path.unlink(missing_ok=True)


@app.post("/upload-baidu-file")
async def upload_baidu_file(
    request: Request,
    remote_path: str = Form(...),
    portrait_detection: bool = Form(False),
    total_bytes: int = Form(0),
):
    user = require_user(request)
    if not job_manager.can_accept_job():
        raise HTTPException(503, "系统资源繁忙，请稍后再试")
    normalized = _normalize_baidu_path(remote_path)
    suffix = Path(normalized).suffix.lower()
    if not normalized or ".." in Path(normalized).parts or suffix not in ALLOWED_VIDEO_EXTS:
        raise HTTPException(400, "请选择有效的百度网盘视频文件")
    if total_bytes < 0 or total_bytes > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(400, f"视频超过最大限制 {settings.max_upload_mb}MB")
    if not _baidu_auth_status(user["id"])["authenticated"]:
        raise HTTPException(401, "百度网盘授权已失效，请重新授权")

    target = settings.storage_path / "raw" / f"{uuid.uuid4().hex[:8]}{suffix}"
    state = job_manager.create_import_job(
        str(target), "portrait" if portrait_detection else "manual",
        "downloading", "正在从百度网盘下载视频…", total_bytes,
    )
    import_id = state["job_id"]
    with _baidu_downloads_lock:
        _baidu_downloads[import_id] = {
            "user_id": user["id"],
            "status": "downloading",
            "message": "正在连接百度网盘…",
            "downloaded_bytes": 0,
            "total_bytes": total_bytes,
            "percent": 0 if total_bytes else None,
            "progress_url": None,
        }
    threading.Thread(
        target=_download_baidu_file_in_background,
        args=(import_id, user["id"], normalized, target, total_bytes, portrait_detection),
        daemon=True,
    ).start()
    return JSONResponse({"import_id": import_id, "status_url": f"/api/baidu-download/{import_id}"})


@app.get("/api/baidu-download/{import_id}")
async def baidu_download_status(request: Request, import_id: str):
    user = require_user(request)
    with _baidu_downloads_lock:
        state = _baidu_downloads.get(import_id)
        if not state or state["user_id"] != user["id"]:
            raise HTTPException(404, "下载任务不存在")
        return {key: value for key, value in state.items() if key != "user_id"}


def _download_url_in_background(job_id: str, video_url: str, target: Path) -> None:
    try:
        import yt_dlp
        host = (urlparse(video_url).hostname or "").lower()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        }
        if host == "bilibili.com" or host.endswith(".bilibili.com") or host == "b23.tv":
            headers.update({"Origin": "https://www.bilibili.com", "Referer": "https://www.bilibili.com/"})

        def progress_hook(data: Dict[str, Any]) -> None:
            if data.get("status") != "downloading":
                return
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            percent = min(99, int(downloaded * 100 / total)) if total else None
            job_manager.update_import_job(
                job_id, status="importing", downloaded_bytes=downloaded,
                total_bytes=total, percent=percent,
                message=f"正在下载链接视频… {percent}%" if percent is not None else "正在下载链接视频…",
            )

        options = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(target), "noplaylist": True,
            "max_filesize": settings.max_upload_mb * 1024 * 1024,
            "socket_timeout": 30, "retries": 2,
            "quiet": True, "no_warnings": True, "http_headers": headers,
            "progress_hooks": [progress_hook],
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([video_url])
        downloaded = target if target.exists() else next(target.parent.glob(f"{target.stem}.*"), None)
        if not downloaded or not downloaded.is_file():
            raise RuntimeError("链接中没有找到可下载的视频")
        if downloaded.stat().st_size > settings.max_upload_mb * 1024 * 1024:
            raise RuntimeError(f"视频超过最大限制 {settings.max_upload_mb}MB")
        job_manager.finalize_import_job(job_id, str(downloaded))
    except Exception as exc:
        for candidate in target.parent.glob(f"{target.stem}.*"):
            candidate.unlink(missing_ok=True)
        logger.exception("视频链接后台导入失败")
        job_manager.update_import_job(job_id, status="failed", message=f"无法从该链接导入视频：{exc}")


@app.post("/upload-url")
async def upload_url(
    request: Request,
    video_url: str = Form(...),
    extraction_code: str = Form(""),
    portrait_detection: bool = Form(False),
):
    user = require_user(request)
    if not job_manager.can_accept_job():
        raise HTTPException(503, "系统资源繁忙，请稍后再试")
    video_url = _validate_public_video_url(video_url)
    host = (urlparse(video_url).hostname or "").lower()
    if host == "pan.baidu.com" or host.endswith(".pan.baidu.com"):
        raise HTTPException(400, "百度网盘分享链接不支持直接导入，请授权百度网盘后从文件列表选择视频")
    target = settings.storage_path / "raw" / f"{uuid.uuid4().hex[:8]}.mp4"
    state = job_manager.create_import_job(
        str(target), "portrait" if portrait_detection else "manual",
        "importing", "正在后台下载链接视频…",
    )
    threading.Thread(
        target=_download_url_in_background,
        args=(state["job_id"], video_url, target),
        daemon=True,
        name=f"url-import-{state['job_id']}",
    ).start()
    return JSONResponse({
        "job_id": state["job_id"],
        "progress_url": f"/job/{state['job_id']}/progress",
    })


@app.post("/upload", response_class=RedirectResponse)
async def upload(
    video: UploadFile = File(...),
    portrait_detection: bool = Form(False),
    mode: str = Form("manual"),
    ost_autocut: bool = Form(False),
    portraits: List[UploadFile] = File(default=[]),
):
    # /upload 以新布尔开关为准；保留 mode/portraits 参数仅避免旧客户端请求报错。
    mode = "portrait" if portrait_detection else "manual"

    segment_seconds = 0

    # 检查系统资源
    if not job_manager.can_accept_job():
        raise HTTPException(503, "系统资源繁忙，请稍后再试")

    if not video.filename:
        raise HTTPException(400, "未选择视频文件")
    ext = Path(video.filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTS:
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


@app.post("/job/{job_id}/cancel", response_class=RedirectResponse)
async def cancel_job(job_id: str):
    try:
        with _baidu_download_processes_lock:
            process = _baidu_download_processes.get(job_id)
        if process and process.poll() is None:
            process.terminate()
        job_manager.cancel_job(job_id)
        with _baidu_downloads_lock:
            _baidu_downloads.pop(job_id, None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(url="/", status_code=303)


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
        "segment_minutes": int(st["segment_seconds"]) // 60 if st.get("segment_seconds") else None,
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
    covers_json: str = Form("[]"),
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
    try:
        raw_covers = json.loads(covers_json)
        if not isinstance(raw_covers, list):
            raise ValueError
        output_covers = {}
        duration = float(st.get("video_info", {}).get("duration", 0))
        for cover in raw_covers:
            group = int(cover["output_group"])
            cover_time = float(cover["cover_time_sec"])
            if group < 1 or not math.isfinite(cover_time) or cover_time < 0 or cover_time > duration:
                raise ValueError
            output_covers[group] = cover_time
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "封面画面数据无效") from exc
    st["logo_regions"] = logo_regions
    st["mask_regions"] = mask_regions
    st["output_covers"] = output_covers
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


def _job_target_url(job: Dict[str, Any]) -> str:
    job_id = job["job_id"]
    status = job.get("status")
    if status == "completed":
        return f"/job/{job_id}/done"
    if status == "reviewing_regions":
        return f"/job/{job_id}/review-regions"
    if status == "reviewing_windows":
        return f"/job/{job_id}/review-windows"
    if status in ("portrait_matching_done", "reviewing_portrait"):
        return f"/job/{job_id}/portrait-results"
    if status == "reviewing_segment":
        return f"/job/{job_id}/review-segment/{job.get('current_segment_idx', 0)}"
    return f"/job/{job_id}/progress"


@app.get("/api/tasks/summary")
async def tasks_summary_api():
    all_jobs = job_manager.get_all_jobs()
    jobs = all_jobs[:8]
    labels = {
        "uploading": "上传中", "downloading": "网盘下载中", "importing": "链接导入中",
        "initializing": "初始化中", "detecting": "人物识别中", "rendering": "生成中", "completed": "已完成",
        "failed": "失败", "reviewing_windows": "等待剪辑", "reviewing_regions": "等待区域审核",
        "reviewing_segment": "等待片段审核", "portrait_matching": "人物匹配中",
        "portrait_matching_done": "等待选择人物",
    }
    return {
        "processing_count": sum(job["status"] not in ("completed", "failed") for job in jobs),
        "completed_count": sum(job["status"] == "completed" for job in jobs),
        "failed_count": sum(job["status"] == "failed" for job in jobs),
        "jobs": [{
            "job_id": job["job_id"], "status": job["status"],
            "status_label": labels.get(job["status"], job["status"]),
            "outputs": job["final_outputs_count"],
            "url": _job_target_url(job),
        } for job in jobs],
    }


# ========== 6. 任务列表页 ==========
@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    all_jobs = job_manager.get_all_jobs()
    # 处理中：渲染中 / 检测中 / 等待区域审核 / 等待片段审核
    processing = [
        j for j in all_jobs
        if j["status"] in ("uploading", "downloading", "importing", "initializing", "rendering", "detecting",
                           "reviewing_regions", "reviewing_windows", "reviewing_segment",
                           "portrait_matching", "portrait_matching_done")
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
