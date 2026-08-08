from __future__ import annotations

import math
import os
import shutil
import threading
from pathlib import Path
from typing import Annotated

import cv2
import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from ultralytics import YOLO

MODEL_PATH = Path(os.getenv("YOLO_MODEL", "/models/yolo11n.pt"))
YUNET_PATH = Path(os.getenv("YUNET_MODEL", "/models/face_detection_yunet_2023mar.onnx"))
SFACE_PATH = Path(os.getenv("SFACE_MODEL", "/models/face_recognition_sface_2021dec.onnx"))
MAX_BATCH_SIZE = 8
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(15 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(40_000_000)))
MAX_DETECTIONS = int(os.getenv("MAX_DETECTIONS", "30"))
MAX_FACES = 20
SFACE_EMBEDDING_SIZE = 128

app = FastAPI(title="GPU vision detector")
_model: YOLO | None = None
_face_detector = None
_face_recognizer = None
_model_lock = threading.Lock()
_face_model_lock = threading.Lock()


def _copy_bundled_model(target: Path, bundled: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        if not bundled.exists():
            raise RuntimeError(f"bundled model is missing: {bundled.name}")
        shutil.copyfile(bundled, target)


def _get_model() -> YOLO:
    global _model
    with _model_lock:
        if _model is None:
            _copy_bundled_model(MODEL_PATH, Path("/opt/yolo11n.pt"))
            _model = YOLO(str(MODEL_PATH))
        return _model


def _get_face_models():
    global _face_detector, _face_recognizer
    with _face_model_lock:
        if _face_detector is None or _face_recognizer is None:
            _copy_bundled_model(YUNET_PATH, Path("/opt/face_detection_yunet_2023mar.onnx"))
            _copy_bundled_model(SFACE_PATH, Path("/opt/face_recognition_sface_2021dec.onnx"))
            _face_detector = cv2.FaceDetectorYN.create(
                str(YUNET_PATH), "", (320, 320), 0.85, 0.3, 5000
            )
            _face_recognizer = cv2.FaceRecognizerSF.create(str(SFACE_PATH), "")
        return _face_detector, _face_recognizer


def _decode_image(upload: UploadFile) -> np.ndarray:
    data = upload.file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image exceeds size limit")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise HTTPException(status_code=400, detail="invalid image")
    height, width = image.shape[:2]
    if height <= 0 or width <= 0 or height * width > MAX_IMAGE_PIXELS:
        raise HTTPException(status_code=413, detail="decoded image exceeds pixel limit")
    return image


def _valid_landmark_geometry(points: np.ndarray, face_w: float, face_h: float) -> bool:
    right_eye, left_eye, nose, right_mouth, left_mouth = points
    eye_distance = float(np.linalg.norm(left_eye - right_eye))
    eye_y = float((right_eye[1] + left_eye[1]) / 2.0)
    mouth_y = float((right_mouth[1] + left_mouth[1]) / 2.0)
    return (
        right_eye[0] < left_eye[0]
        and right_mouth[0] < left_mouth[0]
        and eye_y < nose[1] < mouth_y
        and eye_y < right_mouth[1] and eye_y < left_mouth[1]
        and eye_distance >= max(8.0, face_w * 0.18)
        and eye_distance <= face_w * 0.75
        and mouth_y - eye_y >= face_h * 0.16
        and abs(right_eye[1] - left_eye[1]) <= face_h * 0.3
    )


def _detect_faces(image: np.ndarray, confidence: float, min_face_size: int) -> list[dict]:
    detector, recognizer = _get_face_models()
    height, width = image.shape[:2]
    with _face_model_lock:
        detector.setInputSize((width, height))
        detector.setScoreThreshold(confidence)
        _, raw_faces = detector.detect(image)
        if raw_faces is None:
            return []
        results = []
        for raw in raw_faces[:MAX_FACES]:
            x, y, face_w, face_h = (float(value) for value in raw[:4])
            score = float(raw[14])
            if not math.isfinite(score) or score < confidence:
                continue
            if face_w < min_face_size or face_h < min_face_size:
                continue
            points = np.asarray(raw[4:14], dtype=np.float32).reshape(5, 2)
            if not np.isfinite(points).all() or not _valid_landmark_geometry(points, face_w, face_h):
                continue
            x1 = max(0, min(width, int(math.floor(x))))
            y1 = max(0, min(height, int(math.floor(y))))
            x2 = max(0, min(width, int(math.ceil(x + face_w))))
            y2 = max(0, min(height, int(math.ceil(y + face_h))))
            if x2 <= x1 or y2 <= y1:
                continue
            aligned = recognizer.alignCrop(image, raw)
            feature = np.asarray(recognizer.feature(aligned), dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(feature))
            if feature.size != SFACE_EMBEDDING_SIZE or not np.isfinite(feature).all() or norm <= 0:
                continue
            feature /= norm
            results.append({
                "xyxy": [x1, y1, x2, y2],
                "landmarks": [[float(p[0]), float(p[1])] for p in points],
                "confidence": score,
                "embedding": feature.tolist(),
            })
        return results


@app.get("/health")
def health() -> dict:
    cuda_available = torch.cuda.is_available()
    device = torch.cuda.get_device_name(0) if cuda_available else "cpu"
    return {
        "status": "ok",
        "model": str(MODEL_PATH),
        "model_loaded": _model is not None,
        "face_models_loaded": _face_detector is not None and _face_recognizer is not None,
        "cuda_available": cuda_available,
        "device": device,
    }


@app.post("/v1/person-detections:batch")
def detect_people_batch(
    images: Annotated[list[UploadFile], File()],
    confidence: Annotated[float, Form()] = 0.45,
) -> dict:
    if not 1 <= len(images) <= MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail="images batch size must be between 1 and 8")
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise HTTPException(status_code=400, detail="confidence must be finite and between 0 and 1")
    decoded = [_decode_image(image) for image in images]
    model = _get_model()
    device: int | str = 0 if torch.cuda.is_available() else "cpu"
    with _model_lock:
        results = model.predict(decoded, classes=[0], conf=confidence, device=device,
                                verbose=False, batch=len(decoded), max_det=MAX_DETECTIONS)
    if len(results) != len(decoded):
        raise RuntimeError("model result count does not match input count")
    detections: list[list[dict]] = []
    for result, image in zip(results, decoded):
        current: list[dict] = []
        height, width = image.shape[:2]
        boxes = getattr(result, "boxes", None)
        if boxes is not None:
            for coords, score in zip(boxes.xyxy.detach().cpu().numpy(),
                                     boxes.conf.detach().cpu().numpy()):
                x1 = max(0, min(width, int(coords[0])))
                y1 = max(0, min(height, int(coords[1])))
                x2 = max(0, min(width, int(coords[2])))
                y2 = max(0, min(height, int(coords[3])))
                if x2 > x1 and y2 > y1:
                    current.append({"xyxy": [x1, y1, x2, y2], "confidence": float(score)})
        detections.append(current)
    return {"detections": detections}


@app.post("/v1/faces:batch")
def detect_faces_batch(
    images: Annotated[list[UploadFile], File()],
    face_confidence: Annotated[float, Form()] = 0.85,
    min_face_size: Annotated[int, Form()] = 48,
) -> dict:
    if not 1 <= len(images) <= MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail="images batch size must be between 1 and 8")
    if not math.isfinite(face_confidence) or not 0.0 <= face_confidence <= 1.0:
        raise HTTPException(status_code=400, detail="face_confidence must be finite and between 0 and 1")
    if not 16 <= min_face_size <= 4096:
        raise HTTPException(status_code=400, detail="min_face_size must be between 16 and 4096")
    decoded = [_decode_image(image) for image in images]
    return {"faces": [_detect_faces(image, face_confidence, min_face_size) for image in decoded]}
