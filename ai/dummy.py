"""더미 세션 진행 — 질문 텍스트와 음성만 가짜고, 세션 구성은 실제 로직으로 돌린다.

가짜   질문 텍스트, 음성 파일 URL, 발화 길이
진짜   문항 수, 난이도 배분, 주제 구조, 되묻기, 예비 주제, 재연습

LLM · STT · TTS를 호출하지 않는다. ai/session_plan.py는 사용만 하고 수정하지 않는다.
"""
import itertools
import os
import uuid
from typing import Optional, Union

from pydantic import BaseModel

from ai.redis_store import RedisCounter, RedisDict
from ai.schemas import (
    Persona,
    TaskProcessingResponse,
    QuestionResult,
    ReplayLogItem,
    SessionEndResult,
    TaskDoneResponse,
    TaskErrorResponse,
)
from ai.session_plan import (
    RetryRunner,
    SessionRunner,
    build_plan,
    topics_from_log,
)

# ---------------------------------------------------------------------------
# 고정 응답 재료
# ---------------------------------------------------------------------------

# TTS를 붙이기 전까지 모든 질문이 이 파일 하나를 가리킨다.
SAMPLE_AUDIO_URL = "https://cue-dummy-assets.s3.ap-northeast-2.amazonaws.com/tts/sample.mp3"

# 주질문 — 카테고리 8종에 하나씩. 문장은 docs/질문 유형.md 13장에서 가져왔다.
MAIN_QUESTIONS: dict[str, str] = {
    "지원동기": "백엔드 개발 직무에 지원하신 이유를 말씀해 주세요.",
    "직무역량": "가장 자신 있는 기술 스택과 그 이유는 무엇인가요?",
    "프로젝트경험": "프로젝트 중 가장 기억에 남는 것을 소개해 주세요.",
    "문제해결": "예상치 못한 문제를 만나면 어떻게 접근하시나요?",
    "협업·갈등": "팀원과 의견이 갈렸던 경험을 말씀해 주세요.",
    "실패·성장": "실패했던 경험을 말씀해 주세요.",
    "가치관·인성": "개발자로서 가장 중요한 가치는 무엇인가요?",
    "미래계획": "입사 후 어떤 업무를 맡고 싶으신가요?",
}

# 꼬리질문 — 실제로는 직전 답변을 읽고 만든다. 더미는 난이도별 고정 문장을 쓴다.
FOLLOWUP_QUESTIONS: dict[str, str] = {
    "L1": "방금 말씀하신 것 중 본인이 직접 담당한 부분은 무엇이었나요?",
    "L2": "그 방식을 선택하신 이유는 무엇인가요?",
    "L3": "그 방식은 요청이 몰릴 때 비용이 커지는데, 적절한 선택이었나요?",
}

# 되묻기 — 실제로는 무엇이 빠졌는지에 따라 매번 다르게 만든다.
REASK_QUESTION = "어떤 내용이었는지 조금 더 자세히 말씀해 주시겠어요?"

# ---------------------------------------------------------------------------
# 발화 길이 — 더미에는 STT가 없으므로 audio_url에서 지어낸다
# ---------------------------------------------------------------------------

# 1단계 게이트(10초 미만 또는 25어절 미만)를 기준으로 양쪽에 넉넉히 떨어뜨린다.
SUFFICIENT_LENGTH = (45, 60)   # duration_sec, word_count
INSUFFICIENT_LENGTH = (5, 10)

# audio_url에 이 조각이 들어 있으면 부실한 답변으로 본다.
SHORT_ANSWER_MARKERS = ("short", "insufficient")


def answer_length(audio_url: str) -> tuple[int, int]:
    """audio_url에서 발화 길이를 지어낸다.

    실제 서버는 STT 결과에서 계산한다. 더미에는 STT가 없고 계약서의 답변 제출
    요청에도 길이 정보가 없으므로, 계약서에 필드를 추가하지 않고 파일명으로 받는다.

        .../ans_2.webm         → 충분한 답변
        .../ans_2_short.webm   → 부실한 답변

    실제 STT를 붙일 때 이 함수 하나만 교체하면 된다.
    """
    lowered = audio_url.lower()
    if any(marker in lowered for marker in SHORT_ANSWER_MARKERS):
        return INSUFFICIENT_LENGTH
    return SUFFICIENT_LENGTH


# ---------------------------------------------------------------------------
# 비동기 폴링 흉내
# ---------------------------------------------------------------------------

# 실제 서버는 STT + LLM + TTS가 순차로 돌아 5~15초가 걸리므로, 폴링하면
# processing을 여러 번 거친 뒤 done이 된다. 더미는 즉시 계산해두기 때문에
# 그대로 두면 백엔드의 processing 분기가 한 번도 실행되지 않는다.
#
# DUMMY_POLL_TICKS를 켜면 지정한 횟수만큼 processing을 돌려준 뒤 done이 된다.
# 기본값 0이면 예전처럼 즉시 done이다.
POLL_TICKS_ENV = "DUMMY_POLL_TICKS"

# 계약서 4장 (질문 생성)
QUESTION_STAGES = ("stt", "generating", "tts")

# 리포트 계약 3장 — 값이 다르다
REPORT_STAGES = (
    "transcribing",
    "analyzing_speech",
    "analyzing_gaze",
    "analyzing_content",
    "composing",
)


def poll_ticks() -> int:
    """done이 되기 전까지 processing을 몇 번 돌려줄 것인가. 0이면 즉시 done."""
    try:
        return max(0, int(os.environ.get(POLL_TICKS_ENV, "0")))
    except ValueError:
        return 0


class PendingTask:
    """폴링할 때마다 단계가 진행되다가 마지막에 최종 응답을 내놓는다.

    더미 전용 장치다. 실제 서버에서는 작업이 진짜로 진행되는 동안 processing이 나간다.
    """

    def __init__(self, final, stages: tuple[str, ...], ticks: int, with_progress: bool):
        self.final = final
        self.stages = stages
        self.ticks = ticks
        self.with_progress = with_progress
        self.polled = 0

    def poll(self):
        if self.polled >= self.ticks:
            return self.final

        idx = min(self.polled * len(self.stages) // self.ticks, len(self.stages) - 1)
        stage = self.stages[idx]
        self.polled += 1

        if self.with_progress:
            # 리포트 폴링에만 progress가 있다 (리포트 계약 3장)
            from ai.report_schemas import ReportTaskProcessing

            return ReportTaskProcessing(
                status="processing",
                stage=stage,
                progress=round(self.polled / (self.ticks + 1), 2),
            )
        return TaskProcessingResponse(status="processing", stage=stage)


def pending(final, stages: tuple[str, ...], with_progress: bool = False):
    """DUMMY_POLL_TICKS가 0이면 최종 응답을 그대로, 아니면 PendingTask로 감싼다."""
    ticks = poll_ticks()
    if ticks <= 0:
        return final
    return PendingTask(final, stages, ticks, with_progress)


def read_task(task_id: str):
    """폴링 한 번. 진행 중이면 processing을, 끝났으면 최종 응답을 돌려준다."""
    task = TASKS.get(task_id)
    if task is not None and hasattr(task, "poll"):
        return task.poll()
    return task


# ---------------------------------------------------------------------------
# 세션
# ---------------------------------------------------------------------------

# 워커마다 따로 노는 itertools.count 대신 Redis INCR로 워커 간 원자적으로 증가.
# 안 바꾸면 워커 A와 B가 동시에 task_001을 만들어 서로 덮어쓴다.
_TASK_SEQ = RedisCounter("dummy_task_seq")


def _new_task_id() -> str:
    return f"task_{_TASK_SEQ.next():03d}"


def _new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:6]}"


class DummySession:
    """세션 하나의 진행 상태.

    session_plan.py의 러너가 내는 항목에는 계약서가 요구하는 question_id,
    question_number, topic_index, topic_total이 없다. 여기서 세어 붙인다.
    """

    def __init__(
        self,
        session_id: str,
        question_total: int,
        persona: Persona,
        runner: Union[SessionRunner, RetryRunner],
        planned_topic_count: int,
        job_role: str = "",
    ):
        self.session_id = session_id
        # 꼬리질문을 만들 때 필요하다. 계약서 필드를 세션이 들고 있는 것뿐이다.
        self.job_role = job_role
        self.question_total = question_total
        self.persona = persona
        self.runner = runner
        # 계획된 주제 수. 예비 주제가 투입되면 topic_total이 이 값을 넘어 늘어난다.
        self.planned_topic_count = planned_topic_count

        self.question_number = 0        # 되묻기에서는 올라가지 않는다
        self.topic_index = 0            # type이 "question"일 때마다 1씩 증가
        self.current_topic_is_spare = False
        self.current_question_id: Optional[str] = None
        self.ended = False
        self.aborted = False

        # 카테고리별 주질문 문장. 기본은 고정 문장이고, AI_MODE가 dummy가 아니면
        # 세션 시작 작업이 이력서를 읽고 만든 문장으로 덮어쓴다.
        self.main_questions: dict[str, str] = dict(MAIN_QUESTIONS)

        # 지금 주제에서 오간 질문과 답변. 꼬리질문을 만들 때 근거가 된다.
        # 주질문이 나올 때마다 비운다. 꼬리질문은 직전 답변을 파고드는 것이라
        # 이전 주제의 대화를 끌고 가면 안 되기 때문이다.
        # 더미 모드에서는 답변 텍스트가 없어 계속 빈 채로 남는다.
        self.topic_history: list[dict[str, str]] = []

    def record_answer_text(self, text: str) -> None:
        """직전 질문에 대한 답변 텍스트를 붙인다. STT가 있을 때만 값이 온다."""
        if text and self.topic_history:
            self.topic_history[-1]["answer"] = text

    def answered_history(self) -> list[dict[str, str]]:
        """답변까지 채워진 것만. 꼬리질문 생성에 넘긴다."""
        return [e for e in self.topic_history if e["answer"]]

    def attach_main_questions(self, generated: dict[str, str]) -> None:
        """생성된 주질문을 붙인다. 빠진 카테고리는 고정 문장이 남는다."""
        self.main_questions.update({k: v for k, v in generated.items() if v})

    # ---------- 내부 ----------
    @property
    def topic_total(self) -> int:
        """계획된 주제 수와 지금까지 열린 주제 수 중 큰 값.

        계획대로 끝나면 계획된 주제 수가 그대로 나가고,
        예비 주제가 투입되면 그만큼 늘어난다. 참고용이며 진행률에는 쓰지 않는다.
        """
        return max(self.planned_topic_count, self.topic_index)

    def _text_for(self, item: dict) -> str:
        # 재연습 주질문은 1회차 텍스트를 그대로 쓴다
        if item.get("text"):
            return item["text"]
        if item["type"] == "reask":
            return REASK_QUESTION
        if item["type"] == "question":
            # 생성에 실패했거나 예상 못 한 카테고리면 고정 문장으로 버틴다.
            # 세션 중간에 KeyError로 죽는 것보다 낫다.
            category = item["category"]
            return self.main_questions.get(category) or MAIN_QUESTIONS[category]
        return FOLLOWUP_QUESTIONS[item["difficulty"]]

    def _to_result(self, item: dict) -> Union[QuestionResult, SessionEndResult]:
        if item["type"] == "session_end":
            self.ended = True
            self.current_question_id = None
            return SessionEndResult(type="session_end", total_questions=item["total"])

        kind = item["type"]

        if kind == "question":
            self.topic_index += 1
            # RetryRunner는 항목에 직접 담아준다. SessionRunner는 계획된 주제를
            # 다 쓴 뒤에 열린 주제가 예비 주제다.
            if "is_spare_topic" in item:
                self.current_topic_is_spare = bool(item["is_spare_topic"])
            else:
                self.current_topic_is_spare = self.topic_index > self.planned_topic_count

        if kind == "reask":
            # 되묻기는 새 질문이 아니라 같은 질문의 재요청이다.
            # question_number가 올라가지 않고, 문항 수에도 세지 않는다.
            question_id = f"q_{self.question_number}r"
            reask_of = f"q_{self.question_number}"
        else:
            self.question_number += 1
            question_id = f"q_{self.question_number}"
            reask_of = None

        self.current_question_id = question_id

        text = self._text_for(item)
        if kind == "question":
            # 새 주제가 열렸다. 이전 주제의 대화는 여기서 끊는다.
            self.topic_history = []
        self.topic_history.append({"question": text, "answer": ""})

        # 되묻기는 is_spare_topic과 is_replay가 항상 false다 (계약서 4장)
        if kind == "reask":
            is_spare_topic = False
            is_replay = False
        else:
            is_spare_topic = self.current_topic_is_spare
            is_replay = bool(item.get("is_replay", False))

        return QuestionResult(
            type=kind,
            question_id=question_id,
            reask_of=reask_of,
            text=text,
            audio_url=SAMPLE_AUDIO_URL,
            category=item["category"] if kind == "question" else None,
            difficulty=None if kind == "reask" else item["difficulty"],
            question_number=self.question_number,
            question_total=self.question_total,
            topic_index=self.topic_index,
            topic_total=self.topic_total,
            is_spare_topic=is_spare_topic,
            is_replay=is_replay,
        )

    # ---------- 외부 ----------
    def start(self) -> QuestionResult:
        return self._to_result(self.runner.start())

    def answer(
        self, audio_url: str, is_timeout: bool
    ) -> Union[QuestionResult, SessionEndResult]:
        """더미 경로. 발화 길이를 audio_url에서 지어낸다."""
        duration_sec, word_count = answer_length(audio_url)
        return self.advance(duration_sec, word_count, is_timeout=is_timeout)

    def advance(
        self,
        duration_sec: int,
        word_count: int,
        *,
        is_timeout: bool,
        answer_text: str = "",
    ) -> Union[QuestionResult, SessionEndResult]:
        """실측 발화 길이로 다음 항목을 낸다. STT가 붙으면 이 경로를 쓴다.

        answer_text가 있으면 대화 기록에 남는다. 꼬리질문 생성이 이것을 읽는다.
        """
        self.record_answer_text(answer_text)

        # verdict는 LLM 판정 결과다. 2단계 판정을 켜기 전까지는 항상 None이다.
        # is_timeout이 true면 러너가 되묻기 경로 자체를 타지 않는다 (계약서 3장).
        # 발화 길이는 실제 값 그대로 넘어가고 되묻기 한도도 소모되지 않는다.
        item = self.runner.next(
            duration_sec, word_count, verdict=None, is_timeout=is_timeout
        )
        return self._to_result(item)


# ---------------------------------------------------------------------------
# 재연습
# ---------------------------------------------------------------------------


def replay_question_total(replay_log: list[ReplayLogItem]) -> int:
    """replay_log가 나타내는 1회차 문항 수. reask는 로그에 담기지 않는다."""
    return len(replay_log)


def is_replayable(replay_log: Optional[list[ReplayLogItem]], question_count: int) -> bool:
    """1회차 구조를 재생할 수 있는가.

    문항 수를 바꿔 재연습하면 1회차 구조를 재생할 수 없으므로 일반 세션으로 처리한다.
    페르소나 변경은 AI가 알 수 없다. 백엔드가 replay_log를 보내지 않으면 된다.
    """
    if not replay_log:
        return False
    if not any(item.type == "question" for item in replay_log):
        return False
    return replay_question_total(replay_log) == question_count


def _runner_from_replay_log(replay_log: list[ReplayLogItem]) -> RetryRunner:
    log = [item.model_dump() for item in replay_log]
    main_texts = [item.text for item in replay_log if item.type == "question"]
    return RetryRunner(topics_from_log(log, main_texts))


# ---------------------------------------------------------------------------
# 메모리 보관소 — 세션이 끝나면 정리되고, 재배포하면 사라진다 (계약서 0장)
# ---------------------------------------------------------------------------

# 보관소 상한. 넘으면 오래된 것부터 지운다.
#
# 계약서 0장이 "세션 상태는 임시 보관"이라고 못 박고 있어 지워도 되지만,
# 상한이 없으면 오래 띄워둔 서버의 메모리가 계속 늘어난다.
# 지워진 세션에 답변을 보내면 SESSION_NOT_FOUND가 나는데, 이는 재배포했을 때와
# 같은 상황이라 백엔드 처리 방식이 동일하다.
MAX_SESSIONS = 500
MAX_TASKS = 5000


def evict_oldest(store: dict, limit: int) -> None:
    """삽입 순서가 오래된 것부터 지워 상한을 지킨다."""
    while len(store) > limit:
        store.pop(next(iter(store)))


# Redis로 이전 (2026-09-08). 워커가 여러 개일 때 요청이 다른 프로세스로 가면
# 메모리 dict에는 없어서 SESSION_NOT_FOUND가 났다. 재배포하면 전부 사라지는
# 것도 같은 원인. RedisDict가 dict와 같은 인터페이스를 흉내내므로 아래
# register_task/save_task/create_session/reset은 전혀 손대지 않았다.
# (RedisDict가 돌려주는 객체는 메서드를 호출하면 자동으로 Redis에 재저장되므로
#  PendingTask.poll()이나 DummySession.answer() 같은 변형 메서드도 그대로 동작한다.
#  자세한 내용은 ai/redis_store.py 상단 설명 참고)
SESSIONS = RedisDict("sessions")
# 질문 생성 결과와 리포트 생성 결과가 같은 보관소를 쓴다.
# GET /ai/tasks/{task_id}가 두 계약에서 공유되는 엔드포인트이기 때문이다.
TASKS = RedisDict("tasks")


def reset() -> None:
    """테스트에서 보관소를 비운다."""
    SESSIONS.clear()
    TASKS.clear()


def register_task(pollable) -> str:
    """폴링 가능한 것을 보관소에 넣고 task_id를 돌려준다.

    pollable은 최종 응답 모델이거나 poll()을 가진 객체다.
      PendingTask     더미의 processing 흉내 (DUMMY_POLL_TICKS)
      BackgroundTask  실제로 백그라운드에서 도는 작업 (ai/tasks.py)
    """
    task_id = _new_task_id()
    TASKS[task_id] = pollable
    evict_oldest(TASKS, MAX_TASKS)
    return task_id


def save_task(result: Union[QuestionResult, SessionEndResult]) -> str:
    """즉시 계산한 결과를 done 상태로 저장해 두고 task_id만 돌려준다."""
    return register_task(
        pending(TaskDoneResponse(status="done", result=result), QUESTION_STAGES)
    )


def create_session(
    *,
    question_count: int,
    persona: Persona,
    replay_log: Optional[list[ReplayLogItem]] = None,
    job_role: str = "",
) -> DummySession:
    """세션을 만든다. 첫 주질문은 뽑지 않는다.

    주질문 생성이 LLM을 타면 10~30초가 걸리므로 세션만 먼저 만들어 두고,
    첫 질문은 백그라운드 작업이 뽑는다. 흐름은 ai/pipeline.py에 있다.
    """
    session_id = _new_session_id()

    if is_replayable(replay_log, question_count):
        runner: Union[SessionRunner, RetryRunner] = _runner_from_replay_log(replay_log)
        planned_topic_count = len(runner.topics)
        question_total = runner.target
    else:
        plan = build_plan(question_count=question_count, persona=persona)
        runner = SessionRunner(plan)
        planned_topic_count = plan["topic_count"]
        question_total = question_count

    session = DummySession(
        session_id=session_id,
        question_total=question_total,
        persona=persona,
        runner=runner,
        planned_topic_count=planned_topic_count,
        job_role=job_role,
    )
    SESSIONS[session_id] = session
    evict_oldest(SESSIONS, MAX_SESSIONS)

    # 원본 session이 아니라 SESSIONS[session_id]로 다시 꺼내서 반환한다.
    # 원본을 그대로 반환하면, 호출부(ai/pipeline.py)가 곧바로 session.start()를
    # 불러도 그 변화가 Redis에 반영되지 않는다 (원본은 Redis를 거치지 않은
    # 로컬 참조라서). 다시 꺼내면 RedisProxy로 감싸져서, 이후 이 반환값에 대고
    # 부르는 모든 메서드가 자동으로 Redis에 재저장된다.
    return SESSIONS[session_id]