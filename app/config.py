"""集中配置：从环境变量 / .env 读取所有可调参数。"""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    storage_dir: str = "./storage"

    # 沙箱
    sandbox_image: str = "vedio-agent-worker:latest"
    sandbox_cpu_limit: str = "2.0"
    sandbox_mem_limit: str = "4g"
    sandbox_timeout: int = 1800
    sandbox_enabled: bool = True

    # 视频
    default_segment_seconds: int = 900
    max_video_seconds: int = 9000
    max_upload_mb: int = 8192

    # 检测
    banner_scan_ratio: float = 0.15
    logo_sample_frames: int = 30
    logo_static_threshold: float = 8.0

    # 人像检测
    face_match_threshold: float = 0.4
    face_sample_interval_sec: float = 5.0
    face_merge_gap_sec: float = 10.0
    yolo_model: str = "yolo11n.pt"
    gpu_detector_url: str = "http://gpu-detector:8100"
    gpu_detector_timeout_sec: float = 30.0
    person_confidence: float = 0.45
    face_detector_confidence: float = 0.85
    face_embedding_threshold: float = 0.48
    face_embedding_size: int = 128
    person_cluster_threshold: float = 0.72
    person_face_min_size: int = 48
    person_face_min_sharpness: float = 45.0
    person_min_hits: int = 2

    # OST 检测
    ost_detect_enabled: bool = False
    ost_black_threshold: int = 15
    ost_scene_threshold: float = 30.0

    # 飞书
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_receive_open_id: str = ""
    feishu_enabled: bool = False
    # 飞书 - 手机号自动解析open_id
    feishu_receive_mobile: str = ""
    # 飞书 - 群聊（如果配了群聊ID，发到群里而非个人）
    feishu_chat_id: str = ""
    # 飞书 - Bot webhook 验证 token（事件订阅时配置）
    feishu_verification_token: str = ""
    # 飞书 - 事件加密 key（可选，事件订阅时配置）
    feishu_encrypt_key: str = ""

    @property
    def storage_path(self) -> Path:
        p = Path(self.storage_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        (p / "raw").mkdir(exist_ok=True)
        (p / "segments").mkdir(exist_ok=True)
        (p / "final").mkdir(exist_ok=True)
        (p / "frames").mkdir(exist_ok=True)
        return p


settings = Settings()
