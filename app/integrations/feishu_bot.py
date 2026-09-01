"""飞书 Bot：接收视频消息，创建人工剪辑任务并回传成品。"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import httpx

from app.agents.workflow import job_manager
from app.config import settings

logger = logging.getLogger(__name__)
_BASE_URL = "https://open.feishu.cn/open-apis"
_ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v", ".rm", ".rmvb"}
_seen_events: set[str] = set()
_seen_lock = threading.Lock()


class FeishuClient:
    def __init__(self) -> None:
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    def _tenant_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            response = httpx.post(
                f"{_BASE_URL}/auth/v3/tenant_access_token/internal",
                json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
                timeout=settings.feishu_request_timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code", 0) != 0:
                raise RuntimeError(f"获取飞书 tenant token 失败: {payload}")
            self._token = payload["tenant_access_token"]
            self._token_expires_at = time.time() + max(60, int(payload.get("expire", 7200)) - 120)
            return self._token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._tenant_token()}"}

    def reply_text(self, message_id: str, text: str) -> None:
        response = httpx.post(
            f"{_BASE_URL}/im/v1/messages/{message_id}/reply",
            headers=self._headers(),
            json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
            timeout=settings.feishu_request_timeout_sec,
        )
        self._ensure_success(response, "回复飞书消息")

    def download_message_file(self, message_id: str, file_key: str, destination: Path) -> None:
        url = f"{_BASE_URL}/im/v1/messages/{message_id}/resources/{file_key}"
        limit = settings.max_upload_mb * 1024 * 1024
        part_path = destination.with_suffix(destination.suffix + ".part")
        part_path.unlink(missing_ok=True)
        total = 0
        try:
            with httpx.stream(
                "GET", url, params={"type": "file"}, headers=self._headers(),
                timeout=httpx.Timeout(settings.feishu_download_timeout_sec),
            ) as response:
                response.raise_for_status()
                with part_path.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise ValueError(f"视频超过最大限制 {settings.max_upload_mb}MB")
                        output.write(chunk)
            if total == 0:
                raise ValueError("飞书视频文件为空")
            part_path.replace(destination)
        except Exception:
            part_path.unlink(missing_ok=True)
            raise

    def send_file(self, chat_id: str, file_path: Path) -> None:
        with file_path.open("rb") as stream:
            response = httpx.post(
                f"{_BASE_URL}/im/v1/files",
                headers=self._headers(),
                data={"file_type": "mp4", "file_name": file_path.name},
                files={"file": (file_path.name, stream, "video/mp4")},
                timeout=httpx.Timeout(settings.feishu_download_timeout_sec),
            )
        self._ensure_success(response, "上传飞书成品")
        file_key = response.json()["data"]["file_key"]
        response = httpx.post(
            f"{_BASE_URL}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers=self._headers(),
            json={
                "receive_id": chat_id,
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
            },
            timeout=settings.feishu_request_timeout_sec,
        )
        self._ensure_success(response, "发送飞书成品")

    @staticmethod
    def _ensure_success(response: httpx.Response, action: str) -> None:
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise RuntimeError(f"{action}失败: {payload}")


client = FeishuClient()


def handle_event(body: Dict[str, Any]) -> Dict[str, Any]:
    if body.get("type") == "url_verification":
        if settings.feishu_verification_token and body.get("token") != settings.feishu_verification_token:
            raise ValueError("飞书 Verification Token 无效")
        return {"challenge": body.get("challenge", "")}

    header = body.get("header") or {}
    if settings.feishu_verification_token and header.get("token") != settings.feishu_verification_token:
        raise ValueError("飞书 Verification Token 无效")
    if header.get("event_type") != "im.message.receive_v1":
        return {"code": 0}

    event_id = str(header.get("event_id") or "")
    with _seen_lock:
        if event_id and event_id in _seen_events:
            return {"code": 0}
        if event_id:
            _seen_events.add(event_id)

    event = body.get("event") or {}
    message = event.get("message") or {}
    message_type = message.get("message_type")
    if message_type not in {"file", "media"}:
        threading.Thread(
            target=_safe_reply,
            args=(str(message.get("message_id") or ""), "请发送视频文件。收到后我会创建网页剪辑任务。"),
            daemon=True,
        ).start()
        return {"code": 0}

    threading.Thread(target=_create_job, args=(event,), daemon=True).start()
    return {"code": 0}


def _create_job(event: Dict[str, Any]) -> None:
    message = event.get("message") or {}
    message_id = str(message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    try:
        content = json.loads(message.get("content") or "{}")
        file_key = str(content.get("file_key") or "")
        original_name = Path(str(content.get("file_name") or "video.mp4")).name
        extension = Path(original_name).suffix.lower()
        if not file_key:
            raise ValueError("消息中缺少视频 file_key")
        if extension not in _ALLOWED_EXTENSIONS:
            raise ValueError(f"不支持的视频格式: {extension or '未知'}")
        if not job_manager.can_accept_job():
            raise RuntimeError("系统资源繁忙，请稍后重新发送视频")

        destination = settings.storage_path / "raw" / f"{uuid.uuid4().hex}{extension}"
        client.download_message_file(message_id, file_key, destination)
        try:
            state = job_manager.new_job(
                str(destination), settings.default_segment_seconds,
                mode="manual", portrait_paths=[], ost_autocut=False,
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        job_id = state["job_id"]
        state["feishu_context"] = {"chat_id": chat_id, "message_id": message_id}
        threading.Thread(target=_watch_job, args=(job_id,), daemon=True).start()
        edit_url = _public_url(f"/job/{job_id}/progress")
        client.reply_text(
            message_id,
            f"视频已接收，任务号：{job_id}\n请打开以下链接进行剪辑：\n{edit_url}\n完成剪辑后，机器人会把成品发送到当前会话。",
        )
    except Exception as exc:
        logger.exception("创建飞书视频任务失败")
        _safe_reply(message_id, f"视频任务创建失败：{exc}")


def _watch_job(job_id: str) -> None:
    while True:
        state = job_manager.get(job_id)
        if not state:
            return
        status = state.get("status")
        if status in {"completed", "failed"}:
            break
        time.sleep(3)

    context = state.get("feishu_context") or {}
    message_id = str(context.get("message_id") or "")
    chat_id = str(context.get("chat_id") or "")
    try:
        if status == "failed":
            message = (state.get("render_progress") or {}).get("message") or "未知错误"
            client.reply_text(message_id, f"任务 {job_id} 处理失败：{message}")
            return
        outputs = [Path(path) for path in state.get("final_outputs") or [] if Path(path).is_file()]
        if not outputs:
            client.reply_text(message_id, f"任务 {job_id} 已结束，但没有生成成品。")
            return
        client.reply_text(message_id, f"任务 {job_id} 已完成，共生成 {len(outputs)} 个视频，正在发送成品。")
        failed = 0
        for output in outputs:
            try:
                client.send_file(chat_id, output)
            except Exception:
                failed += 1
                logger.exception("飞书成品发送失败: %s", output)
        result_url = _public_url(f"/job/{job_id}/done")
        suffix = f"，其中 {failed} 个发送失败，可从结果页下载" if failed else ""
        client.reply_text(message_id, f"成品发送完成{suffix}：\n{result_url}")
    except Exception:
        logger.exception("飞书任务完成通知失败: %s", job_id)


def _safe_reply(message_id: str, text: str) -> None:
    if not message_id:
        return
    try:
        client.reply_text(message_id, text)
    except Exception:
        logger.exception("回复飞书消息失败")


def _public_url(path: str) -> str:
    base = settings.public_base_url.rstrip("/")
    if not base:
        raise RuntimeError("尚未配置 PUBLIC_BASE_URL，无法生成飞书可访问的剪辑链接")
    return f"{base}/{path.lstrip('/')}"
