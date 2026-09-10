"""
L2CS-Net을 프로세스당 한 번만 로딩해서 재사용한다.

기존 문제: extract_l2cs_gaze()가 호출될 때마다 Pipeline()을 새로 만들면
CUDA 초기화 + 가중치 로딩(수십 초)이 매번 반복된다. 실제 배포에서는
Celery 워커가 계속 떠있으니 이 비용은 최초 1회만 내면 된다.

이 모듈은 process-level 캐시로 Pipeline 인스턴스를 하나만 유지한다.
"""
import time
from typing import Optional

import cv2
import torch
from l2cs import Pipeline

_pipeline_cache: dict[str, Pipeline] = {}


def get_pipeline(weights_path: str, device: str = "cuda") -> Pipeline:
    """이미 로딩된 게 있으면 재사용, 없으면 한 번만 로딩."""
    key = f"{weights_path}:{device}"
    if key not in _pipeline_cache:
        _pipeline_cache[key] = Pipeline(
            weights=weights_path, arch="ResNet50", device=torch.device(device)
        )
    return _pipeline_cache[key]


def extract_l2cs_gaze_warm(
    video_path: str,
    weights_path: str,
    device: str = "cuda",
    frame_skip: int = 1,
) -> dict:
    """
    모델은 재사용(캐시)하고, frame_skip 프레임마다 하나씩만 처리한다.
    frame_skip=1이면 전체 프레임(원본 fps), frame_skip=6이면 30fps 영상 기준 5fps.

    Returns:
        {"frames": [...], "video_duration_sec": float, "elapsed_sec": float,
         "processed_frame_count": int, "total_frame_count": int}
    """
    pipeline = get_pipeline(weights_path, device)  # 캐시 히트면 사실상 0초

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    video_duration_sec = total_frame_count / fps if fps > 0 else 0.0

    frames = []
    frame_idx = 0
    start = time.time()

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if frame_idx % frame_skip == 0:
            try:
                result = pipeline.step(frame)
                if result is not None and len(result.pitch) > 0:
                    yaw = float(result.yaw[0])
                    pitch = float(result.pitch[0])
                else:
                    yaw, pitch = None, None
            except Exception:
                yaw, pitch = None, None

            frames.append({
                "frame_idx": frame_idx,
                "timestamp": round(frame_idx / fps, 3),
                "yaw": yaw,
                "pitch": pitch,
            })

        frame_idx += 1

    cap.release()
    elapsed_sec = time.time() - start

    return {
        "frames": frames,
        "video_duration_sec": video_duration_sec,
        "elapsed_sec": elapsed_sec,
        "processed_frame_count": len(frames),
        "total_frame_count": total_frame_count,
    }
