"""ffmpeg 视频处理器：探测、分割、裁剪横幅、模糊蒙版、拼接透明模糊背景。

所有外部命令统一通过 Sandbox 执行，确保容器隔离。
"""
from __future__ import annotations

import itertools
import json
import logging
import math
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.core.sandbox import SandboxError, sandbox

logger = logging.getLogger(__name__)

_idx_counter = itertools.count(0)


def _new_id(prefix: str = "n") -> str:
    return f"{prefix}{next(_idx_counter)}"


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    duration: float  # seconds
    fps: float
    codec: str = ""


# -------------------- 探测与基本信息 --------------------

def probe_video(video_path: Path) -> VideoInfo:
    """调用 ffprobe 读取视频基础信息。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(video_path),
    ]
    try:
        raw = sandbox.run(cmd)
    except SandboxError as e:
        raise RuntimeError(f"视频探测失败: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe 输出非 JSON: {e}") from e

    v_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v_stream:
        raise RuntimeError("文件中不存在视频流")

    def _parse_fps(s: str) -> float:
        if not s or "/" not in s:
            return 30.0
        n, d = s.split("/", 1)
        try:
            return float(n) / float(d) if float(d) != 0 else 30.0
        except ValueError:
            return 30.0

    dur = float(data.get("format", {}).get("duration", v_stream.get("duration", 0)))
    return VideoInfo(
        path=str(video_path),
        width=int(v_stream["width"]),
        height=int(v_stream["height"]),
        duration=dur,
        fps=_parse_fps(v_stream.get("r_frame_rate", "30/1")),
        codec=v_stream.get("codec_name", ""),
    )


# -------------------- 截取指定帧图片 --------------------

def extract_frame(video_path: Path, out_png: Path, t_second: float) -> Path:
    """在 t_second 秒处截取一帧为 PNG，返回输出文件路径。

    说明：
    - 使用"混合 seek 策略"：先在 -i 之前用 `-ss` 粗略 seek（定位到 t-30s 前的关键帧），
      然后在输出端再精确 seek 到目标时间。这比纯 input-seek 更稳健，
      能避免 rv40/wmv3/mpeg4 等老编解码器在 B 帧 seek 时出现
      "Invalid decoder state: B-frame without reference data" 之类的解码器状态错误。
    - 同时增加 `-err_detect ignore_err` / `-fflags +genpts+discardcorrupt` 容错，
      面对局部损坏帧时不会直接抛错导致任务失败。
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    # input seek：目标时间向前最多回退 30 秒（覆盖一个 GOP 长度），保证落在关键帧之前
    coarse = max(0.0, float(t_second) - 30.0)
    fine = max(0.0, float(t_second) - coarse)
    cmd = [
        "ffmpeg", "-y",
        "-err_detect", "ignore_err",
        "-fflags", "+genpts+discardcorrupt",
        "-ss", f"{coarse:.3f}",
        "-i", str(video_path),
        "-ss", f"{fine:.3f}",
        "-frames:v", "1",
        "-q:v", "2",
        str(out_png),
    ]
    sandbox.run(cmd)
    return out_png


# -------------------- 分割列表计算 --------------------

def plan_segments(total_seconds: float, segment_seconds: int) -> List[Tuple[float, float]]:
    """把 [0, total_seconds) 按 segment_seconds 切分，返回 [(start,end), ...]。"""
    if segment_seconds <= 0:
        raise ValueError("segment_seconds 必须为正整数")
    if total_seconds <= 0:
        return []
    n = math.ceil(total_seconds / segment_seconds)
    segs: List[Tuple[float, float]] = []
    for i in range(n):
        s = i * segment_seconds
        e = min((i + 1) * segment_seconds, total_seconds)
        segs.append((float(s), float(e)))
    return segs


# -------------------- 编码加速（GPU/CPU 自动降级） --------------------

# NVIDIA NVDEC/CUVID 官方支持的解码 codec 白名单；其他格式（rv40/wmv3/indeo 等）只能软解
# 参考：https://developer.nvidia.com/video-codec-sdk-encoders-decoders
_CUDA_DECODE_WHITELIST = {
    "h264", "av1", "hevc", "vp9", "mpeg2video", "mpeg4",
    "h263", "vc1", "wmv3", "mjpeg",
}


def _probe_ffmpeg_capabilities() -> Dict[str, bool]:
    """探测当前 ffmpeg 支持的编码器/硬件加速，返回能力字典。

    相比"仅看 help/列表输出"，额外引入两层动态校验：
    1) DISABLE_HWACCEL 环境变量强制全部 GPU 特性关闭
    2) 对 h264_nvenc 和 hwaccel_cuda 跑 1 帧冒烟测试：
       - h264_nvenc 需要能成功创建 CUDA context 才能返回 True（否则 libcuda.so 缺失即判否）
       - hwaccel cuda 必须能创建 HWDeviceContext（否则 libcuda 未加载即判否）
    这保证"容器 ffmpeg 虽然编译了 NVENC/CUDA，但宿主机没挂载 NVIDIA runtime 时自动降级"。
    """
    caps = {"h264_nvenc": False, "h264_cuvid": False, "hwaccel_cuda": False, "libx264": True}
    # D) 最高优先级：DISABLE_HWACCEL=1 一键关闭所有 GPU 特性
    if os.environ.get("DISABLE_HWACCEL", "") in ("1", "true", "TRUE", "yes", "on"):
        logger.info("DISABLE_HWACCEL=1 已设置，强制使用纯 CPU 编码/解码")
        return caps
    try:
        out = sandbox.run(["ffmpeg", "-hide_banner", "-encoders"])
        want_nvenc = "h264_nvenc" in out
        out2 = sandbox.run(["ffmpeg", "-hide_banner", "-decoders"])
        want_cuvid = "h264_cuvid" in out2
        out3 = sandbox.run(["ffmpeg", "-hide_banner", "-hwaccels"])
        want_hw_cuda = "cuda" in out3

        # A) 冒烟测试 h264_nvenc：保证至少编码 1 帧（加 -frames:v 1 + -t 2）
        # 这样 CUDA 编码器初始化一定会触发（不会因 duration<1帧 导致 no packets 误判）
        if want_nvenc:
            try:
                sandbox.run([
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=1",
                    "-frames:v", "1", "-t", "2", "-an",
                    "-c:v", "h264_nvenc",
                    "-f", "null", "-",
                ])
                caps["h264_nvenc"] = True
            except SandboxError as e:
                logger.warning("h264_nvenc 冒烟失败，降级 CPU 编码：%s",
                               str(e).splitlines()[-1] if str(e) else "未知错误")
                caps["h264_nvenc"] = False
        caps["h264_cuvid"] = want_cuvid and caps["h264_nvenc"]

        # A) 冒烟测试 hwaccel=cuda：至少解码/传递 1 帧，确保 HWDeviceContext 创建真的成功
        if want_hw_cuda and caps["h264_nvenc"]:
            try:
                sandbox.run([
                    "ffmpeg", "-y", "-v", "error",
                    "-hwaccel", "cuda",
                    "-f", "lavfi", "-i", "testsrc2=size=32x32:rate=1",
                    "-frames:v", "1", "-t", "2", "-an",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-f", "null", "-",
                ])
                caps["hwaccel_cuda"] = True
            except SandboxError as e:
                logger.warning("hwaccel=cuda 冒烟失败，禁用 CUDA 解码加速：%s",
                               str(e).splitlines()[-1] if str(e) else "未知错误")
                caps["hwaccel_cuda"] = False
    except Exception as e:
        logger.warning("ffmpeg 能力探测失败，默认使用 CPU: %s", e)
    logger.info("ffmpeg 能力探测结果: %s", caps)
    return caps


_FFMPEG_CAPS: Dict[str, bool] = {}


def _get_encoder() -> Tuple[List[str], str]:
    """返回 (编码参数列表, 简短描述)。优先 NVENC，降级 libx264(速度优先 preset)。"""
    global _FFMPEG_CAPS
    if not _FFMPEG_CAPS:
        _FFMPEG_CAPS = _probe_ffmpeg_capabilities()
    if _FFMPEG_CAPS.get("h264_nvenc"):
        # NVIDIA 硬件编码：p4 是速度优先，cq 相当于 crf，bframes 和 rc-lookahead 提升质量
        return (
            [
                "-c:v", "h264_nvenc",
                "-preset", "p2",
                "-tune", "ll",
                "-rc", "vbr",
                "-cq", "25",
                "-b:v", "0",
                "-rc-lookahead", "0",
                "-bf", "0",
                "-pix_fmt", "yuv420p",
            ],
            "h264_nvenc(GPU)",
        )
    # CPU 编码：-preset faster（比 veryfast 更快 30%+），crf 略提升保持体积质量平衡
    return (
        [
            "-c:v", "libx264",
            "-preset", "faster",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-tune", "zerolatency",
            "-threads", "0",
        ],
        "libx264(CPU faster)",
    )


def _get_decode_hwaccel(video_codec: Optional[str] = None) -> List[str]:
    """解码加速参数。

    B) 只有同时满足 3 个条件才会返回 [-hwaccel cuda]：
    1) 冒烟测试通过（hwaccel_cuda=True）
    2) video_codec 非空且在 _CUDA_DECODE_WHITELIST 中（防止 rv40/wmv 等无 CUDA 解码器的格式误开）
    3) DISABLE_HWACCEL 未设置
    否则返回空列表（纯 CPU 软解，安全底线）。
    """
    global _FFMPEG_CAPS
    if not _FFMPEG_CAPS:
        _FFMPEG_CAPS = _probe_ffmpeg_capabilities()
    if not _FFMPEG_CAPS.get("hwaccel_cuda"):
        return []
    if video_codec:
        normalized = video_codec.strip().lower()
        if normalized not in _CUDA_DECODE_WHITELIST:
            logger.info("codec=%r 不在 CUDA 解码白名单，使用纯 CPU 软解", video_codec)
            return []
        return ["-hwaccel", "cuda"]
    # 未知 codec → 宁可保守不用 hwaccel，避免"No device available for decoder: device type cuda needed"
    return []


# CUDA/device 相关错误关键词（用于失败时自动降级重试）
_CUDA_ERR_RE = re.compile(
    r"(Cannot load libcuda|Could not dynamically load CUDA|Device creation failed|"
    r"No device available for decoder.*device type cuda|Hardware device setup failed|"
    r"Operation not permitted|failed to set CUDA|CUDA_ERROR_NO_DEVICE)",
    re.IGNORECASE,
)


def _is_cuda_related_error(err: str) -> bool:
    """判断错误输出里是否出现 CUDA/硬件加速初始化失败等关键词。"""
    if not err:
        return False
    return bool(_CUDA_ERR_RE.search(err))


# -------------------- 真正的 ffmpeg 视频剪辑（带处理链） --------------------

def _safe_boxblur(w: int, h: int, strength: str = "medium") -> str:
    """根据小色块尺寸生成安全的 boxblur 参数（避免 YUV420P chroma 半径超限）。

    修正：boxblur 参数名使用短名 lr/lp/cr/cp/ar/ap（长名 luma_radius/chroma_radius/alpha_radius 也可）。
    注意：**FFmpeg 7.x boxblur 选项名是 luma_radius / luma_power，不是 luma_r / luma_p**！
    之前的错误写法 `luma_r=3` 会导致 "Error applying option 'luma_r' to filter 'boxblur': Option not found"。

    模糊半径需要随区域尺寸增长。旧实现把中度半径限制为 6px，在 2K/4K 视频上
    几乎不可见，看起来像蒙版没有写入成品。这里按区域短边计算半径，并使用 FFmpeg
    表达式限制其不超过当前色度平面的安全范围。
    """
    short_edge = max(2, min(int(w), int(h)))
    if strength == "light":
        divisor, r_min, power = 18, 3, 2
    elif strength == "strong":
        divisor, r_min, power = 5, 12, 3
    else:
        divisor, r_min, power = 9, 7, 3
    luma_limit = max(1, short_edge // 2)
    chroma_limit = max(1, short_edge // 4)
    radius = min(luma_limit, max(r_min, min(40, int(round(short_edge / divisor)))))
    chroma_radius = min(chroma_limit, max(1, min(20, radius // 2)))
    return f"boxblur=lr={radius}:lp={power}:cr={chroma_radius}:cp={power}:ar=0:ap=0"


def _solid_cover(w: int, h: int, color: str = "black") -> str:
    """生成纯色块覆盖滤镜链（用于「消除/遮挡」模式）。

    等价于：
      - 先 crop 出对应区域（尺寸 w x h）
      - 用 drawbox fill=color 整块填色（简单稳定，不需要 split / nullsrc）
    返回可以直接接在 crop=... 之后的滤镜片段字符串。
    """
    # drawbox: 0-based coords within the cropped piece → 填满就是 (0,0,w,h)
    # thickness=fill 或 t=fill（ffmpeg 新版支持 "fill" 作为 thickness）
    return f"drawbox=x=0:y=0:w={w}:h={h}:color={color}:t=fill"


def _mask_mode_chain(
    prev: str,
    x: int, y: int, w: int, h: int,
    mode: str,
    strength: str = "medium",
) -> Tuple[str, str]:
    """生成单个蒙版区域的 filter 链片段，返回 (blur_tag, appended_chain)。

    支持 mode:
      - "blur"   : 高斯式 boxblur 模糊（强度可调）
      - "cover"  : 纯色块覆盖（黑色填充，直接消除内容，用于"消除遮挡"选项）
    """
    base_tag = f"[{_new_id('bs')}]"
    crop_tag = f"[{_new_id('cp')}]"
    blur_tag = f"[{_new_id('mb')}]"
    out_tag  = f"[{_new_id('mk')}]"
    if mode == "cover":
        chain = (
            f"{prev}split=2{base_tag}{crop_tag};"
            f"{crop_tag}crop={w}:{h}:{x}:{y},{_solid_cover(w, h, color='black')}{blur_tag};"
            f"{base_tag}{blur_tag}overlay=x={x}:y={y}:format=auto:eof_action=repeat{out_tag}"
        )
    else:
        chain = (
            f"{prev}split=2{base_tag}{crop_tag};"
            f"{crop_tag}crop={w}:{h}:{x}:{y},{_safe_boxblur(w, h, strength)}{blur_tag};"
            f"{base_tag}{blur_tag}overlay=x={x}:y={y}:format=auto:eof_action=repeat{out_tag}"
        )
    return out_tag, chain


# 蒙版 region 元组支持两种格式（向后兼容）：
#   旧格式（4 项）: (x, y, w, h)                          → 默认为 blur + medium
#   新格式（6 项）: (x, y, w, h, mode, strength)           → mode ∈ {"blur", "cover"}, strength ∈ {"light","medium","strong"}
# 也支持 dict 格式：{"x":..,"y":..,"w":..,"h":..,"mode":..,"strength":..}
MaskRegion = Any
TextLogo = Any


def _escape_drawtext(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace("%", "\\%"))


def _normalize_text_logo(logo: TextLogo) -> Dict[str, Any]:
    if not isinstance(logo, dict):
        raise ValueError("文字 Logo 必须为对象格式")
    color = str(logo.get("color", "#ffffff"))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = "#ffffff"
    return {
        "text": str(logo.get("text", "")).strip()[:80],
        "x": max(0, int(logo.get("x", 0))),
        "y": max(0, int(logo.get("y", 0))),
        "font_size": max(12, min(160, int(logo.get("font_size", 36)))),
        "color": color,
    }


def _normalize_mask(region: MaskRegion) -> Dict[str, Any]:
    """把 4-tuple / 6-tuple / dict 蒙版区域统一为 dict，便于按 mode/strength 渲染。"""
    if isinstance(region, dict):
        x = int(region.get("x", 0)); y = int(region.get("y", 0))
        w = int(region.get("w", 0)); h = int(region.get("h", 0))
        mode = str(region.get("mode", "blur")).lower()
        strength = str(region.get("strength", "medium")).lower()
    elif isinstance(region, (tuple, list)):
        n = len(region)
        if n >= 6:
            x, y, w, h = int(region[0]), int(region[1]), int(region[2]), int(region[3])
            mode = str(region[4]).lower()
            strength = str(region[5]).lower()
        elif n == 5:
            x, y, w, h = int(region[0]), int(region[1]), int(region[2]), int(region[3])
            mode = str(region[4]).lower()
            strength = "medium"
        else:
            x, y, w, h = int(region[0]), int(region[1]), int(region[2]), int(region[3])
            mode = "blur"
            strength = "medium"
    else:
        raise ValueError(f"不支持的蒙版 region 格式: {region!r}")
    if mode not in ("blur", "cover"):
        mode = "blur"
    if strength not in ("light", "medium", "strong"):
        strength = "medium"
    return {"x": x, "y": y, "w": w, "h": h, "mode": mode, "strength": strength}


def build_process_filter(
    info: VideoInfo,
    *,
    banner_region: Optional[Tuple[int, int, int, int]] = None,  # x,y,w,h（已废弃，保留签名兼容）
    logos: Optional[List[TextLogo]] = None,                    # 多个文字 Logo
    mask_regions: Optional[List[MaskRegion]] = None,           # 手动蒙版区域（支持 4/6-tuple/dict）
    crop_banner: bool = False,                                 # 已废弃：不再处理横幅
    fill_blur: bool = False,                                   # 已废弃：不再处理横幅
) -> Tuple[str, int, int]:
    """构造 ffmpeg -filter_complex 过滤图；返回 (filter_str, out_w, out_h)。

    支持两种蒙版模式：
      - blur  : boxblur 模糊（强度 light/medium/strong 3 档可调）
      - cover : 纯色块填充（黑色，直接消除选中区域内容，用于"消除遮挡"）
    Logo 始终用 medium 强度 blur（保持历史行为）。
    输出分辨率保持原视频尺寸不变。
    """
    chain: List[str] = []
    W, H = info.width, info.height

    # ---------- 1) 文字 Logo ----------
    prev = "[0:v]"
    if logos:
        for raw_logo in logos:
            logo = _normalize_text_logo(raw_logo)
            if not logo["text"]:
                continue
            out_tag = f"[{_new_id('lg')}]"
            color = logo["color"].lstrip("#")
            chain.append(
                f"{prev}drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:"
                f"text='{_escape_drawtext(logo['text'])}':x={min(logo['x'], W - 1)}:"
                f"y={min(logo['y'], H - 1)}:fontsize={logo['font_size']}:fontcolor=0x{color}:"
                f"borderw=2:bordercolor=black@0.65{out_tag}"
            )
            prev = out_tag

    # ---------- 2) 手动蒙版区域（支持 blur/cover 双模式 + 3 档强度）----------
    # 注意：坐标始终以原视频尺寸为基准，每一段渲染都会收到相同的 mask_regions，
    #       所以蒙版在"整个视频时长内"保持一致的区域处理。
    if mask_regions:
        for idx, region in enumerate(mask_regions):
            m = _normalize_mask(region)
            x = max(0, min(m["x"], W - 1))
            y = max(0, min(m["y"], H - 1))
            w = max(1, min(m["w"], W - x))
            h = max(1, min(m["h"], H - y))
            new_prev, segment = _mask_mode_chain(prev, x, y, w, h, m["mode"], m["strength"])
            chain.append(segment)
            prev = new_prev

    # ---------- 3) 最终画布统一为 4:3，不拉伸原画面 ----------
    if W / H > 4 / 3:
        out_w = W + (W % 2)
        out_h = int(math.ceil(out_w * 3 / 4 / 2) * 2)
    else:
        out_h = H + (H % 2)
        out_w = int(math.ceil(out_h * 4 / 3 / 2) * 2)
    chain.append(
        f"{prev}pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[prefinal]"
    )

    return ";".join(chain), out_w, out_h


# -------------------- 主剪辑函数 --------------------

def cut_and_process_segment(
    *,
    video_path: Path,
    start_sec: float,
    end_sec: float,
    output_path: Path,
    banner_region: Optional[Tuple[int, int, int, int]],
    logos: Optional[List[TextLogo]],
    mask_regions: Optional[List[MaskRegion]] = None,
) -> Path:
    """剪辑 [start_sec, end_sec) 片段并应用 logo模糊+蒙版（blur或cover），输出到 output_path。

    注意：横幅/banner 处理已完全移除；蒙版支持 blur/cover 双模式及强度参数。
    """
    info = probe_video(video_path)
    start_sec = max(0.0, float(start_sec))
    end_sec = min(float(end_sec), info.duration)
    if start_sec >= info.duration or end_sec - start_sec < 0.1:
        raise RuntimeError(
            f"剪辑区间超出视频有效时长: {start_sec:.3f}-{end_sec:.3f}s，"
            f"源视频时长 {info.duration:.3f}s"
        )
    duration = end_sec - start_sec
    final_path = output_path
    output_path = final_path.with_name(f"{final_path.stem}.part{final_path.suffix}")
    output_path.unlink(missing_ok=True)
    final_path.unlink(missing_ok=True)

    def _publish_output() -> Path:
        rendered = probe_video(output_path)
        if output_path.stat().st_size <= 0 or rendered.duration < 0.1:
            raise RuntimeError("生成的视频为空或缺少有效视频流")
        if rendered.duration < min(duration * 0.8, duration - 0.5):
            raise RuntimeError(
                f"生成视频时长异常: 期望 {duration:.3f}s，实际 {rendered.duration:.3f}s"
            )
        output_path.replace(final_path)
        return final_path

    # #region debug-point A:B:segment-boundary
    try:
        _payload = json.dumps({"sessionId":"trailing-segment-corruption","runId":os.environ.get("TRAE_DEBUG_RUN_ID","pre-fix"),"hypothesisId":"A,B","location":"ffmpeg_proc.py:cut_and_process_segment","msg":"[DEBUG] segment input boundary","data":{"video":str(video_path),"sourceDuration":info.duration,"start":start_sec,"end":end_sec,"requestedDuration":duration,"remaining":info.duration-start_sec,"output":str(output_path)},"ts":int(time.time()*1000)}).encode(); urllib.request.urlopen(urllib.request.Request(os.environ.get("TRAE_DEBUG_SERVER_URL","http://host.docker.internal:7777/event"),data=_payload,headers={"Content-Type":"application/json"}),timeout=1).read()
    except Exception:
        pass
    # #endregion
    # 最终输出始终经过 4:3 画布滤镜；文字 Logo 和蒙版在同一滤镜链中生效。
    need_filter = True
    output_path.parent.mkdir(parents=True, exist_ok=True)
    venc, venc_desc = _get_encoder()
    logger.info("视频编码使用: %s", venc_desc)
    # B) 传入 video_codec，白名单内才开 CUDA 解码；rv40 等老格式强制软解
    hwaccel_args = _get_decode_hwaccel(info.codec)
    if hwaccel_args:
        logger.info("解码使用: hwaccel cuda (codec=%s)", info.codec)
    else:
        logger.info("解码使用: 纯 CPU 软解 (codec=%s)", info.codec or "unknown")

    base = [
        "ffmpeg", "-y",
        "-err_detect", "ignore_err",
        "-fflags", "+genpts+discardcorrupt",
        *hwaccel_args,
        "-ss", f"{start_sec:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(video_path),
    ]
    if not need_filter:
        cmd = base + list(venc) + [
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ]
        try:
            sandbox.run(cmd)
        except SandboxError as e:
            # C) 简单版失败时：若检测到 CUDA/device 错误，自动去掉 hwaccel 重试一次
            if hwaccel_args and _is_cuda_related_error(str(e)):
                logger.warning("检测到 CUDA/硬件加速错误，自动用纯 CPU 软解重试...")
                cmd_nohw = [
                    "ffmpeg", "-y",
                    "-ss", f"{start_sec:.3f}",
                    "-t", f"{duration:.3f}",
                    "-i", str(video_path),
                    *venc,
                    "-c:a", "aac", "-b:a", "128k",
                    str(output_path),
                ]
                sandbox.run(cmd_nohw)
            else:
                raise
        return _publish_output()


    filter_str, _, _ = build_process_filter(
        info, banner_region=None, logos=logos, mask_regions=mask_regions,
        crop_banner=False, fill_blur=False,
    )
    logger.info("filter_complex = %s", filter_str)
    base_full = base + [
        "-filter_complex", filter_str,
        "-map", "[prefinal]",
        "-map", "0:a?",
        *venc,
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(output_path),
    ]
    try:
        sandbox.run(base_full)
    except SandboxError as e:
        # C) 先尝试"去掉 hwaccel + 保留同款复杂滤镜"，避免 hwaccel 失败被误判为 filter 失败
        if hwaccel_args and _is_cuda_related_error(str(e)):
            logger.warning("检测到 CUDA/硬件加速错误，自动用纯 CPU 软解 + 原滤镜重试一次...")
            base_nohw_full = [
                "ffmpeg", "-y",
                "-ss", f"{start_sec:.3f}",
                "-t", f"{duration:.3f}",
                "-i", str(video_path),
                "-filter_complex", filter_str,
                "-map", "[prefinal]",
                "-map", "0:a?",
                *venc,
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                str(output_path),
            ]
            try:
                sandbox.run(base_nohw_full)
                return _publish_output()
            except SandboxError:
                # 若纯 CPU 版滤镜本身也有问题，继续走简化版 fallback
                logger.warning("纯 CPU 版原滤镜仍失败，继续回退简化版...")
        # 回退：逐区域逐个应用，简化逻辑（简化版从不使用 hwaccel，纯 CPU）
        logger.warning("复杂滤镜失败，回退简化版：%s", e)
        simple: List[str] = []
        prev = "[0:v]"
        if logos:
            for idx, raw_logo in enumerate(logos):
                logo = _normalize_text_logo(raw_logo)
                if not logo["text"]:
                    continue
                out = f"[lg{idx}]"
                color = logo["color"].lstrip("#")
                simple.append(
                    f"{prev}drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:"
                    f"text='{_escape_drawtext(logo['text'])}':x={min(logo['x'], info.width - 1)}:"
                    f"y={min(logo['y'], info.height - 1)}:fontsize={logo['font_size']}:fontcolor=0x{color}:"
                    f"borderw=2:bordercolor=black@0.65{out}"
                )
                prev = out
        if mask_regions:
            for idx, region in enumerate(mask_regions):
                m = _normalize_mask(region)
                x = max(0, min(m["x"], info.width - 1))
                y = max(0, min(m["y"], info.height - 1))
                w = max(1, min(m["w"], info.width - x))
                h = max(1, min(m["h"], info.height - y))
                prev, segment = _mask_mode_chain(
                    prev, x, y, w, h, m["mode"], m["strength"]
                )
                simple.append(segment)
        if info.width / info.height > 4 / 3:
            fallback_w = info.width + (info.width % 2)
            fallback_h = int(math.ceil(fallback_w * 3 / 4 / 2) * 2)
        else:
            fallback_h = info.height + (info.height % 2)
            fallback_w = int(math.ceil(fallback_h * 4 / 3 / 2) * 2)
        simple.append(
            f"{prev}pad={fallback_w}:{fallback_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[prefinal]"
        )
        # 简化版 fallback 不使用 hwaccel，纯 CPU
        base2 = [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.3f}",
            "-t", f"{duration:.3f}",
            "-i", str(video_path),
            "-filter_complex", ";".join(simple),
            "-map", "[prefinal]", "-map", "0:a?",
            *venc,
            "-c:a", "aac", "-b:a", "128k", "-shortest",
            str(output_path),
        ]
        sandbox.run(base2)
    return _publish_output()


def prepend_cover_intro(
    video_path: Path, source_path: Path, cover_time_sec: float, duration_sec: float = 2.0,
) -> Path:
    """截取指定画面，增强为 3840×2880，并作为静音片头写入成品视频。"""
    output_path = video_path.with_name(f"{video_path.stem}.with-cover.mp4")
    encoder, _ = _get_encoder()
    filters = (
        f"[0:v]select='eq(n,0)',scale=3840:2880:force_original_aspect_ratio=decrease,"
        f"pad=3840:2880:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        f"fps=30,tpad=stop_mode=clone:stop_duration={duration_sec:.3f},"
        f"trim=duration={duration_sec:.3f},setpts=PTS-STARTPTS[cover];"
        f"[1:a]atrim=duration={duration_sec:.3f},asetpts=PTS-STARTPTS[covera];"
        "[2:v]scale=3840:2880:force_original_aspect_ratio=decrease,"
        "pad=3840:2880:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,setpts=PTS-STARTPTS[main];"
        "[2:a]aresample=48000,asetpts=PTS-STARTPTS[maina];"
        "[cover][covera][main][maina]concat=n=2:v=1:a=1[v][a]"
    )
    try:
        sandbox.run([
            "ffmpeg", "-y", "-ss", f"{cover_time_sec:.3f}", "-i", str(source_path),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-i", str(video_path), "-filter_complex", filters,
            "-map", "[v]", "-map", "[a]", *encoder,
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_path),
        ])
        rendered = probe_video(output_path)
        if rendered.width != 3840 or rendered.height != 2880 or rendered.duration <= duration_sec:
            raise RuntimeError("4K 封面片头生成结果不正确")
        video_path.unlink(missing_ok=True)
        output_path.replace(video_path)
        return video_path
    finally:
        output_path.unlink(missing_ok=True)


def concat_processed_segments(input_paths: List[Path], output_path: Path) -> Path:
    """按给定顺序无损拼接已经统一编码和规格的 4:3 片段。"""
    if not input_paths:
        raise ValueError("至少需要一个待合成片段")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if len(input_paths) == 1:
        output_path.unlink(missing_ok=True)
        input_paths[0].replace(output_path)
        return output_path
    list_path = output_path.with_suffix(".concat.txt")
    part_path = output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")
    try:
        lines = [f"file '{path.resolve().as_posix()}'" for path in input_paths]
        list_path.write_text("\n".join(lines), encoding="utf-8")
        sandbox.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart", str(part_path),
        ])
        rendered = probe_video(part_path)
        if part_path.stat().st_size <= 0 or rendered.duration < 0.1:
            raise RuntimeError("合成视频为空")
        output_path.unlink(missing_ok=True)
        part_path.replace(output_path)
        return output_path
    finally:
        list_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)
        for path in input_paths:
            path.unlink(missing_ok=True)
