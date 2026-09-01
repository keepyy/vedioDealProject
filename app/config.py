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
    public_base_url: str = ""

    # 百度网盘开放平台（未配置时回退 bdpan）
    baidu_app_key: str = ""
    baidu_app_secret: str = ""
    baidu_app_name: str = "bdpan"
    baidu_oauth_redirect_uri: str = "oob"
    baidu_aria2_connections: int = 8
    baidu_aria2_split: int = 8
    baidu_aria2_min_split_size: str = "1M"

    # 飞书 Bot
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_request_timeout_sec: float = 30.0
    feishu_download_timeout_sec: float = 1800.0

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
