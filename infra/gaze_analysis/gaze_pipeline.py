"""
presigned URL로 영상을 받아 시선 분석하고, 성공/실패 관계없이 로컬 파일을
즉시 삭제하는 프로덕션 경로. extract_l2cs_warm.py, gaze_metrics.py를 묶는다.

흐름: presigned URL 다운로드 -> 5fps 시선 분석 -> 구간 지표 변환 -> 로컬 파일 삭제
"""
import os
import uuid

import requests

from .extract_l2cs_warm import extract_l2cs_gaze_warm
from .gaze_metrics import compute_gaze_metrics

DOWNLOAD_DIR = "/tmp/gaze_downloads"

# 5fps 확정값 (원본 30fps 기준 6프레임당 1개). remeasure_fps.py 실측으로 검증됨.
FRAME_SKIP = 6


def _download_to_temp(presigned_url: str) -> str:
    """presigned URL에서 영상을 로컬 임시 파일로 스트리밍 다운로드."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp4")

    with requests.get(presigned_url, stream=True, timeout=30) as resp:
        resp.raise_for_status()  # 만료된 URL이나 접근 실패 시 여기서 예외
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    return local_path


def _delete_local_file(local_path: str) -> None:
    if local_path and os.path.exists(local_path):
        os.remove(local_path)


def analyze_gaze_from_presigned_url(
    presigned_url: str,
    weights_path: str,
    question_id: str = "",
    frame_skip: int = FRAME_SKIP,
    device: str = "cuda",
) -> dict:
    """
    presigned URL 하나를 받아 다운로드 -> 분석 -> 구간 지표 변환까지 끝내고,
    성공하든 실패하든 로컬에 받았던 영상 파일은 finally에서 무조건 삭제한다.

    반환값은 report_dummy.py의 Evidence 스키마와 바로 맞물리는 형태.
    """
    local_path = None
    try:
        local_path = _download_to_temp(presigned_url)

        raw = extract_l2cs_gaze_warm(
            local_path, weights_path, device=device, frame_skip=frame_skip
        )
        metrics = compute_gaze_metrics(
            raw["frames"], total_duration_sec=raw["video_duration_sec"]
        )

        return {
            "question_id": question_id,
            "status": "done",
            "gaze_maintain_ratio": metrics.gaze_maintain_ratio,
            "aversion_frequency": metrics.aversion_frequency,
            "dropout_rate": metrics.dropout_rate,
            "evidence": [e.to_evidence_dict(question_id) for e in metrics.aversion_events],
            "processed_frame_count": raw["processed_frame_count"],
            "elapsed_sec": raw["elapsed_sec"],
        }

    finally:
        # 분석이 성공하든, 위에서 예외가 나든 이 블록은 반드시 실행된다.
        # 영상은 민감정보라 서버에 남으면 안 된다.
        _delete_local_file(local_path)