"""飞书 Bot 处理器：接收消息事件，自动处理视频，发送成品。

飞书应用配置步骤：
1. 在 https://open.feishu.cn 创建企业自建应用
2. 启用机器人能力
3. 权限管理 → 添加权限：
   - im:message（读取消息）
   - im:message:send_as_bot（发送消息）
   - im:resource（下载消息中的资源）
   - drive:drive + drive:file:write（上传文件到云盘）
4. 事件订阅 → 请求地址填写：https://你的公网域名/feishu/webhook
5. 事件订阅 → 添加事件：im.message.receive_v1（接收消息）
6. 记下「验证 token」和「加密 key」填入 .env

用户使用方式：
- 发送"帮助" → 显示可用命令
- 发送"任务列表" → 显示最近任务状态
- 发送视频文件 → 自动下载并处理（检测→分割→渲染），完成后发送成品
- 发送"状态 {job_id}" → 查询指定任务进度
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.agents.workflow import job_manager, nodes

logger = logging.getLogger(__name__)

HELP_TEXT = """茶辑 · 视频处理助手

可用操作：
1. 直接发送视频文件 → 自动处理（检测横幅/Logo → 按默认时长分割 → 渲染 → 发送成品）
2. 发送"任务列表" → 查看所有任务状态
3. 发送"状态 任务ID" → 查询指定任务进度
4. 发送"帮助" → 显示此帮助信息

处理流程：
- 自动识别画面顶部横幅并裁剪，用模糊背景填充
- 自动识别台标/水印并用蒙版模糊
- 按默认15分钟分割（可发送"分割 N"指定N分钟）
- 每段渲染完成后自动发送

提示：发送视频前先发送"分割 10"可将分割时长改为10分钟。"""


def get_feishu_client():
    """获取飞书客户端实例。"""
    from app.integrations.feishu import FeishuClient
    return FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)


def handle_webhook(body: Dict[str, Any]) -> Dict[str, Any]:
    """处理飞书 webhook 事件。返回 JSON 响应。"""
    # URL 验证
    if body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        logger.info("飞书 URL 验证: challenge=%s", challenge[:20])
        return {"challenge": challenge}

    # 事件消息（v2 schema）
    header = body.get("header", {})
    event_type = header.get("event_type", "")

    if event_type == "im.message.receive_v1":
        event = body.get("event", {})
        # 异步处理消息，快速返回 200
        thread = threading.Thread(target=_handle_message_event, args=(event,), daemon=True)
        thread.start()
        return {"code": 0, "msg": "ok"}

    logger.info("飞书事件未处理: type=%s", event_type)
    return {"code": 0, "msg": "ignored"}


def _handle_message_event(event: Dict[str, Any]) -> None:
    """处理收到的消息事件。"""
    try:
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {}).get("open_id", "")
        message = event.get("message", {})
        message_id = message.get("message_id", "")
        msg_type = message.get("message_type", "")
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "p2p")
        content_str = message.get("content", "{}")

        try:
            content = json.loads(content_str)
        except Exception:
            content = {}

        logger.info("飞书消息: type=%s chat=%s sender=%s", msg_type, chat_type, sender_id)

        # 群聊中只有 @机器人 的消息才处理
        if chat_type == "group":
            mentions = message.get("mentions", [])
            bot_mentioned = any(m.get("id", {}).get("open_id") == settings.feishu_app_id
                                or m.get("name") == "tea_agent" for m in mentions)
            if not bot_mentioned:
                return

        # 根据消息类型处理
        if msg_type == "text":
            _handle_text_message(content.get("text", ""), sender_id, chat_id, chat_type)
        elif msg_type in ("file", "video", "media"):
            _handle_video_message(content, message_id, sender_id, chat_id, chat_type)
        else:
            _send_reply(sender_id, chat_id, chat_type,
                        f"暂不支持 {msg_type} 类型的消息。发送『帮助』查看可用操作。")

    except Exception as e:
        logger.exception("飞书消息处理失败: %s", e)


def _handle_text_message(text: str, sender_id: str, chat_id: str, chat_type: str) -> None:
    """处理文本消息。"""
    text = text.strip()
    low = text.lower()

    if low in ("帮助", "help", "?", "？", "功能"):
        _send_reply(sender_id, chat_id, chat_type, HELP_TEXT)

    elif low in ("任务列表", "任务", "tasks", "list"):
        jobs = job_manager.get_all_jobs()
        if not jobs:
            _send_reply(sender_id, chat_id, chat_type, "暂无任务记录。发送视频文件开始处理。")
            return
        lines = ["任务列表：\n"]
        for j in jobs[:10]:
            status_map = {
                "detecting": "检测中", "reviewing_regions": "待审核",
                "reviewing_segment": "待审核", "rendering": "渲染中",
                "completed": "已完成", "failed": "失败"
            }
            st = status_map.get(j.get("status", ""), j.get("status", ""))
            outputs = j.get("final_outputs_count", 0)
            total = j.get("segments_total", 0)
            mode = "人像选片" if j.get("mode") == "portrait" else "时间分割"
            lines.append(f"  [{st}] {j['job_id']} | {mode} | {outputs}/{total} 段")
        _send_reply(sender_id, chat_id, chat_type, "\n".join(lines))

    elif low.startswith("状态 ") or low.startswith("status "):
        job_id = text.split(" ", 1)[1].strip() if " " in text else ""
        st = job_manager.get(job_id)
        if not st:
            _send_reply(sender_id, chat_id, chat_type, f"任务 {job_id} 不存在。")
            return
        rp = st.get("render_progress", {})
        _send_reply(sender_id, chat_id, chat_type,
                    f"任务 {job_id}\n状态: {st.get('status')}\n进度: {rp.get('message', '')}\n成品: {len(st.get('final_outputs', []))} 段")

    elif low.startswith("分割 "):
        # 设置分割时长（仅对当前会话的下一条视频生效）
        try:
            minutes = int(text.split(" ", 1)[1].strip())
            _send_reply(sender_id, chat_id, chat_type,
                        f"已设置分割时长为 {minutes} 分钟。请发送视频文件开始处理。")
            # 存储在内存中，key 为 sender_id
            _user_segment_pref[sender_id] = minutes
        except ValueError:
            _send_reply(sender_id, chat_id, chat_type, "格式错误，请发送『分割 10』设置10分钟。")

    else:
        _send_reply(sender_id, chat_id, chat_type,
                    f"收到：{text}\n\n发送『帮助』查看可用操作，或直接发送视频文件开始处理。")


# 用户分割时长偏好（内存临时存储）
_user_segment_pref: Dict[str, int] = {}


def _handle_video_message(
    content: Dict[str, Any], message_id: str,
    sender_id: str, chat_id: str, chat_type: str,
) -> None:
    """处理视频/文件消息：下载 → 自动处理 → 发送成品。"""
    file_key = content.get("file_key", "")
    file_name = content.get("file_name", "video.mp4")

    if not file_key:
        _send_reply(sender_id, chat_id, chat_type, "无法获取文件信息，请重新发送。")
        return

    _send_reply(sender_id, chat_id, chat_type, f"收到视频：{file_name}\n正在下载...")

    # 下载文件
    try:
        client = get_feishu_client()
        ext = Path(file_name).suffix.lower() or ".mp4"
        safe_name = f"{uuid.uuid4().hex[:8]}{ext}"
        dest = settings.storage_path / "raw" / safe_name
        client.download_message_file(message_id, file_key, dest)
    except Exception as e:
        logger.exception("飞书文件下载失败: %s", e)
        _send_reply(sender_id, chat_id, chat_type, f"文件下载失败：{e}")
        return

    _send_reply(sender_id, chat_id, chat_type, "下载完成，正在启动处理...")

    # 获取分割时长偏好
    segment_minutes = _user_segment_pref.pop(sender_id, 15)
    segment_seconds = segment_minutes * 60

    # 创建任务
    try:
        st = job_manager.new_job(str(dest), segment_seconds, mode="manual")
        job_id = st.get("job_id")
    except Exception as e:
        logger.exception("任务创建失败: %s", e)
        _send_reply(sender_id, chat_id, chat_type, f"任务创建失败：{e}")
        return

    _send_reply(sender_id, chat_id, chat_type,
                f"任务已创建：{job_id}\n分割时长：{segment_minutes} 分钟\n正在自动处理（检测→分割→渲染）...")

    # 启动后台自动处理线程，传入回调在完成时发送成品
    thread = threading.Thread(
        target=_auto_process_and_notify,
        args=(job_id, sender_id, chat_id, chat_type),
        daemon=True,
    )
    thread.start()


def _auto_process_and_notify(job_id: str, sender_id: str, chat_id: str, chat_type: str) -> None:
    """自动处理视频并在完成/失败时通知用户。"""
    job_manager.auto_process_job(job_id)

    # 轮询等待完成
    max_wait = 7200  # 最多等2小时
    waited = 0
    while waited < max_wait:
        time.sleep(10)
        waited += 10
        st = job_manager.get(job_id)
        if not st:
            break
        status = st.get("status", "")
        if status in ("completed", "failed"):
            break

    # 发送结果
    st = job_manager.get(job_id)
    if not st:
        return

    if st.get("status") == "failed":
        rp = st.get("render_progress", {})
        _send_reply(sender_id, chat_id, chat_type,
                    f"任务 {job_id} 处理失败：{rp.get('message', '未知错误')}")
        return

    outputs = st.get("final_outputs", [])
    if not outputs:
        _send_reply(sender_id, chat_id, chat_type, f"任务 {job_id} 完成，但未生成成品。")
        return

    _send_reply(sender_id, chat_id, chat_type,
                f"任务 {job_id} 处理完成！共 {len(outputs)} 段成品，正在上传...")

    # 逐个发送成品视频
    client = get_feishu_client()
    success = 0
    for i, path in enumerate(outputs):
        try:
            p = Path(path)
            if chat_type == "group" and chat_id:
                client.send_media_card_to_chat(chat_id, p, f"成品 {i+1}/{len(outputs)} | {p.name}")
            else:
                client.send_media_card(sender_id, p, f"成品 {i+1}/{len(outputs)} | {p.name}")
            success += 1
        except Exception as e:
            logger.exception("发送成品 %s 失败: %s", path, e)

    _send_reply(sender_id, chat_id, chat_type,
                f"上传完毕：成功 {success}/{len(outputs)} 段。\nWeb 查看结果：http://localhost:8000/job/{job_id}/done")


def _send_reply(sender_id: str, chat_id: str, chat_type: str, text: str) -> None:
    """发送回复消息。群聊用 chat_id，单聊用 open_id。"""
    try:
        client = get_feishu_client()
        if chat_type == "group" and chat_id:
            client.send_text_to_chat(chat_id, text)
        else:
            client.send_text(sender_id, text)
    except Exception as e:
        logger.exception("飞书回复失败: %s", e)
