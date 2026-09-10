from celery import Celery

app = Celery(
    "gaze_infra",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
    include=["tasks"],
)

app.conf.result_expires = 3600

# GPU 큐 라우팅: task 이름 패턴에 따라 어느 큐로 보낼지 결정
# gpu0 = Whisper(B 담당), gpu1 = 시선 분석(C 담당), gpu2 = 예비/모델비교용
app.conf.task_routes = {
    "tasks.transcribe_*": {"queue": "gpu0"},
    "tasks.analyze_gaze_*": {"queue": "gpu1"},
    "tasks.compare_gaze_*": {"queue": "gpu2"},
}
