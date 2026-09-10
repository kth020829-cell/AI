"""
gpu0(Whisper): 아직 B가 설치 전이라 더미로 대체.
gpu1(시선): 실제 프로덕션 파이프라인(gaze_analysis.gaze_pipeline)을 그대로 호출.
            presigned URL 다운로드 -> 5fps 분석 -> 구간 지표 변환 -> 삭제까지 전부 포함.
"""
import time
from celery_app import app


@app.task(name="tasks.transcribe_dummy")
def transcribe_dummy(audio_path: str) -> dict:
    """gpu0 큐 확인용 더미. Whisper 붙으면 이 안을 교체."""
    time.sleep(1)
    return {"audio_path": audio_path, "text": "(더미 전사)", "status": "done", "queue": "gpu0"}


@app.task(name="tasks.analyze_gaze_video")
def analyze_gaze_video(presigned_url: str, weights_path: str, question_id: str = "") -> dict:
    """
    gpu1 큐. presigned URL 하나를 받아 실제 프로덕션 파이프라인을 그대로 돌린다.
    모델은 이 워커 프로세스 안에서 캐시되어 재사용된다 (extract_l2cs_warm.py의
    _pipeline_cache). 즉 같은 워커가 두 번째 요청부터는 콜드스타트 없이 빠르게 처리한다.
    """
    from gaze_analysis.gaze_pipeline import analyze_gaze_from_presigned_url

    result = analyze_gaze_from_presigned_url(
        presigned_url=presigned_url,
        weights_path=weights_path,
        question_id=question_id,
        device="cuda",
    )
    result["queue"] = "gpu1"
    return result
