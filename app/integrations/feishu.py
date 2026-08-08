"""飞书集成：个人机器人推送消息 + 云盘上传视频。

使用飞书开放平台的「企业自建应用」能力：
- 发送消息: im/v1/messages （个人 open_id 接收，即用户说的"发布到个人飞书"）
- 云盘上传: drive/v1/medias/upload_all（上传视频文件）→ 返回 file_token → 用于消息中附带文件卡片
- open_id 解析: contact/v3/users/batch_get_id（通过手机号查询用户 open_id）

权限要求（应用后台启用）：
- im:message          发送消息
- im:message:send_as_bot  以机器人身份发消息
- drive:drive         获取云文档/云盘
- drive:file:write    上传文件到云盘
- contact:user.id:readonly  通过手机号查询用户 open_id
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from app.config import settings

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        if not app_id or not app_secret:
            raise RuntimeError("飞书 app_id / app_secret 未配置")
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_token: str = ""
        self._token_expire: float = 0.0

    # ------------ token ------------
    def tenant_access_token(self) -> str:
        import time
        if self._tenant_token and time.time() < self._token_expire - 60:
            return self._tenant_token
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            r.raise_for_status()
            body = r.json()
            if body.get("code") != 0:
                raise RuntimeError(f"飞书 token 获取失败: {body}")
            self._tenant_token = body["tenant_access_token"]
            self._token_expire = time.time() + int(body.get("expire", 7200))
            return self._tenant_token

    def _headers(self, extra: Dict[str, str] | None = None) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.tenant_access_token()}"}
        if extra:
            h.update(extra)
        return h

    # ------------ 上传视频到云盘 ------------
    def upload_video(self, video_path: Path) -> Dict[str, Any]:
        """upload_all 单请求上传（适合 2GB 以内）。返回 {"file_token":...,"file_url":...}."""
        if not video_path.exists():
            raise RuntimeError(f"文件不存在: {video_path}")
        size = video_path.stat().st_size
        params = {
            "file_name": video_path.name,
            "parent_type": "explorer",
            "parent_node": "",   # 根目录
            "size": str(size),
        }
        with video_path.open("rb") as f:
            files = {"file": (video_path.name, f, "video/mp4")}
            with httpx.Client(timeout=600) as c:
                r = c.post(
                    f"{FEISHU_BASE}/drive/v1/medias/upload_all",
                    headers=self._headers(),
                    data=params,
                    files=files,
                )
        try:
            body = r.json()
        except Exception:
            raise RuntimeError(f"飞书上传失败 HTTP{r.status_code}: {r.text[:500]}")
        if body.get("code") != 0:
            raise RuntimeError(f"飞书上传失败: {body}")
        data = body.get("data", {})
        token = data.get("file_token", "")
        return {
            "file_token": token,
            "file_url": f"https://fs.feishu.cn/file/{token}",
        }

    # ------------ 下载飞书消息中的文件 ------------
    def download_message_file(self, message_id: str, file_key: str, save_path: Path) -> Path:
        """下载飞书消息中的文件（视频/图片/普通文件）。

        API: GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file
        """
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=600) as c:
            r = c.get(
                f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": "file"},
                headers=self._headers(),
            )
            if r.status_code != 200:
                raise RuntimeError(f"下载飞书文件失败 HTTP{r.status_code}: {r.text[:300]}")
            save_path.write_bytes(r.content)
        logger.info("飞书文件下载完成: %s (%.1f MB)", save_path.name, save_path.stat().st_size / 1024 / 1024)
        return save_path

    # ------------ 发送消息到群聊 ------------
    def send_text_to_chat(self, chat_id: str, text: str) -> Dict[str, Any]:
        """发送文本消息到群聊。"""
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=self._headers({"Content-Type": "application/json"}),
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": '{"text":%s}' % __import__("json").dumps(text, ensure_ascii=False),
                },
            )
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书群聊消息发送失败: {body}")
        return body.get("data", {})

    def send_media_card_to_chat(self, chat_id: str, video_path: Path, summary: str) -> Dict[str, Any]:
        """发送视频到群聊。"""
        meta = self.upload_video(video_path)
        content = {"file_key": meta["file_token"]}
        import json as _json
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=self._headers({"Content-Type": "application/json"}),
                json={
                    "receive_id": chat_id,
                    "msg_type": "media",
                    "content": _json.dumps(content, ensure_ascii=False),
                },
            )
        body = r.json()
        if body.get("code") != 0:
            logger.warning("飞书群聊发 media 失败（%s），回退文本", body)
            self.send_text_to_chat(chat_id, f"{summary}\n视频链接：{meta['file_url']}")
            return {"fallback": True, "file_url": meta["file_url"]}
        return body.get("data", {})

    # ------------ 发送个人消息 ------------
    def send_text(self, open_id: str, text: str) -> Dict[str, Any]:
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers=self._headers({"Content-Type": "application/json"}),
                json={
                    "receive_id": open_id,
                    "msg_type": "text",
                    "content": '{"text":%s}' % __import__("json").dumps(text, ensure_ascii=False),
                },
            )
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书消息发送失败: {body}")
        return body.get("data", {})

    def send_media_card(self, open_id: str, video_path: Path, summary: str) -> Dict[str, Any]:
        meta = self.upload_video(video_path)
        content = {
            "file_key": meta["file_token"],
        }
        import json as _json
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers=self._headers({"Content-Type": "application/json"}),
                json={
                    "receive_id": open_id,
                    "msg_type": "media",
                    "content": _json.dumps(content, ensure_ascii=False),
                },
            )
        body = r.json()
        if body.get("code") != 0:
            # 回退：发送"文件链接 + 描述"文本消息
            logger.warning("飞书发 media 消息失败（%s），回退文本消息", body)
            self.send_text(open_id, f"{summary}\n视频链接：{meta['file_url']}")
            return {"fallback": True, "file_url": meta["file_url"]}
        body["data"] = dict(body.get("data", {}), file_url=meta["file_url"])
        return body.get("data", {})


# ------------ open_id 解析 ------------
def resolve_open_id_by_mobile(mobile: str) -> Optional[str]:
    """通过手机号查询飞书用户 open_id。

    使用飞书 API POST /open-apis/contact/v3/users/batch_get_id，
    需要应用具备 contact:user.id:readonly 权限。

    mobile: 手机号（建议带国际区号，如 +8613800138000；若不带 + 默认补 +86）。
    返回 open_id 字符串，查询失败返回 None。
    """
    if not mobile:
        return None
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("飞书 app_id / app_secret 未配置，无法解析 open_id")
    # 飞书 batch_get_id 的 mobiles 字段要求带国际区号
    num = mobile.strip()
    if not num.startswith("+"):
        num = f"+86{num}"
    client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{FEISHU_BASE}/contact/v3/users/batch_get_id",
            params={"user_id_type": "open_id"},
            headers=client._headers({"Content-Type": "application/json"}),
            json={"mobiles": [num]},
        )
    try:
        body = r.json()
    except Exception:
        raise RuntimeError(f"飞书 open_id 解析失败 HTTP{r.status_code}: {r.text[:500]}")
    if body.get("code") != 0:
        raise RuntimeError(f"飞书 open_id 解析失败: {body}")
    user_list = body.get("data", {}).get("user_list", []) or []
    # user_list 每项含 open_id / user_id / mobile / email / status；status=0 表示查到
    for u in user_list:
        if u.get("open_id"):
            return u["open_id"]
    return None


# ------------ 工作流入口 ------------
def publish_to_feishu(state: Dict[str, Any]) -> Dict[str, Any]:
    if not settings.feishu_enabled:
        logger.info("飞书推送未启用（FEISHU_ENABLED=false），跳过")
        return {"ok": True, "skipped": True, "reason": "disabled"}
    client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    open_id = settings.feishu_receive_open_id
    # 若未直接配置 open_id 但配置了手机号，则自动解析
    if not open_id and settings.feishu_receive_mobile:
        try:
            open_id = resolve_open_id_by_mobile(settings.feishu_receive_mobile)
            if open_id:
                logger.info("通过手机号 %s 解析到 open_id: %s",
                            settings.feishu_receive_mobile, open_id)
        except Exception as e:
            logger.warning("通过手机号解析 open_id 失败: %s", e)
    if not open_id:
        raise RuntimeError("FEISHU_RECEIVE_OPEN_ID 未配置且无法通过手机号解析，无法推送个人消息")
    outputs: List[str] = state.get("final_outputs") or []
    if not outputs:
        client.send_text(open_id, f"[视频Agent] 任务 {state.get('job_id')} 完成，但未生成任何成品。")
        return {"ok": True, "segments": 0}

    client.send_text(
        open_id,
        f"[视频Agent] 任务 {state.get('job_id')} 已完成，共生成 {len(outputs)} 段视频，正在上传...",
    )
    results: List[Dict[str, Any]] = []
    for p in outputs:
        try:
            r = client.send_media_card(open_id, Path(p), f"{state.get('job_id')} | {os.path.basename(p)}")
            results.append({"file": p, "ok": True, "detail": r})
        except Exception as e:
            logger.exception("上传 %s 失败: %s", p, e)
            results.append({"file": p, "ok": False, "error": str(e)})
    client.send_text(
        open_id,
        f"[视频Agent] 上传完毕：成功 {sum(1 for x in results if x['ok'])} / {len(results)}。",
    )
    return {"ok": True, "segments": len(results), "details": results}
