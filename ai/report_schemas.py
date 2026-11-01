"""리포트 생성 API — 요청·응답 Pydantic 모델

원본 스펙: docs/리포트생성_API계약_백엔드전달용.md

공통 타입(Category, Difficulty, Persona, ErrorCode)은 ai/schemas.py에서 가져온다.
카테고리 8종과 페르소나 값은 질문 생성 계약과 동일하다.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_serializer

from ai.schemas import Category, Difficulty, Persona

# ---------------------------------------------------------------------------
# 공통 리터럴
# ---------------------------------------------------------------------------

# 점수 축 3개. 마무리는 독립 축이 아니라 speech의 하위 지표다.
Axis = Literal["content", "speech", "gaze"]

AxisStatus = Literal["ok", "failed", "skipped"]

ReportStatus = Literal["complete", "partial"]

EvidenceKind = Literal["strength", "weakness"]

# 계약서 3장. 질문 생성의 stage(stt · generating · tts)와 값이 다르다.
ReportStage = Literal[
    "transcribing",
    "analyzing_speech",
    "analyzing_gaze",
    "analyzing_content",
    "composing",
]

# 정상 가중치. 축이 실패하면 남은 축에 비례 배분한다. (계약서 6장)
AXIS_WEIGHTS: dict[str, float] = {"content": 0.5, "speech": 0.25, "gaze": 0.25}


def display_of(score: int) -> int:
    """내부 0~100을 표시용 1~5로 바꾼다. (계약서 0장)

    0~19 → 1   20~39 → 2   40~59 → 3   60~79 → 4   80~100 → 5
    """
    return min(5, score // 20 + 1)


# ---------------------------------------------------------------------------
# 2. 리포트 생성 요청
# ---------------------------------------------------------------------------


class ReportAnswer(BaseModel):
    """세션에서 실제로 나간 질문과 답변 하나. 계약서 2장.

    is_replay와 is_spare_topic은 계약서 초안에 없던 필드다.
    응답 questions[]가 두 값을 담고 회차 비교가 is_replay로 걸러지는데,
    AI 서버는 세션을 보관하지 않아 요청으로 받지 않으면 알 수 없다.
    기본값이 false이므로 두 필드가 없는 기존 요청도 그대로 파싱된다.
    """

    question_id: str
    type: Literal["question", "followup", "reask"]
    text: str
    category: Optional[Category] = None      # 되묻기는 null
    difficulty: Optional[Difficulty] = None  # 되묻기는 null
    question_number: int
    audio_url: str
    video_url: Optional[str] = None          # null이면 시선 축을 건너뛴다
    is_timeout: bool
    reask_of: Optional[str] = None           # 되묻기인 경우 원 질문의 question_id
    is_replay: bool = False
    is_spare_topic: bool = False


class ReportCreateRequest(BaseModel):
    """POST /ai/sessions/{session_id}/report 요청. 계약서 2장."""

    persona: Persona
    job_role: str
    company_id: Optional[str] = None
    company_profile_override: Optional[str] = None
    answers: list[ReportAnswer]


class ReportRetryRequest(ReportCreateRequest):
    """POST /ai/sessions/{session_id}/report/retry 요청. 계약서 7장.

    리포트 생성 요청과 같은 본문에 다시 계산할 축(axes)을 더한다.
    응답은 생성과 같은 전체 리포트다. 백엔드는 기존 리포트를 통째로 교체한다.

    축만 돌려주면 총점 · 게이트 · 부분 실패 여부가 옛 값으로 남는다.
    그것을 다시 계산하려면 가중치와 게이트 규칙이 필요한데, 그건 AI 로직이라
    백엔드가 따라 만들면 안 된다. 그래서 전체를 다시 조립해 준다.

    요청하지 않은 축은 첫 리포트 때 계산한 값을 재사용한다(시선 분석, 내용 채점).
    서버가 재시작돼 남은 값이 없으면 그 축도 다시 계산한다.
    """

    axes: list[Axis]


class TaskAccepted(BaseModel):
    """202 Accepted."""

    task_id: str


# ---------------------------------------------------------------------------
# 4. 리포트 응답
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """감점·강점 근거. t_start와 t_end는 필수다. (계약서 4장)"""

    question_id: str
    t_start: float
    t_end: float
    kind: EvidenceKind
    label: str
    comment: str


class AxisResult(BaseModel):
    """축 하나의 결과.

    ok       score · display · metrics · evidence에 값이 있다
    failed   score · display · metrics가 null이고 error_code가 붙는다
    skipped  카메라 미사용 등 정상 케이스. reason이 붙는다
    """

    status: AxisStatus
    score: Optional[int] = None
    display: Optional[int] = None
    # 축별 세부 지표. 이번 주차에는 빈 객체이며 비어 있는 것이 오류가 아니다.
    metrics: Optional[dict] = None
    evidence: list[Evidence] = Field(default_factory=list)
    error_code: Optional[str] = None  # failed일 때
    reason: Optional[str] = None      # skipped일 때 "no_video"

    @model_serializer(mode="wrap")
    def _drop_unused(self, handler):
        """error_code와 reason은 해당 상태일 때만 내보낸다.

        계약서 4장 · 6장 예시에서 ok 축에는 두 필드가 아예 없다.
        score · display · metrics의 null은 failed 예시에 그대로 있으므로 남긴다.
        """
        data = handler(self)
        for field in ("error_code", "reason"):
            if data.get(field) is None:
                data.pop(field, None)
        return data


class Axes(BaseModel):
    content: AxisResult
    speech: AxisResult
    gaze: AxisResult


class Overall(BaseModel):
    score: int          # 0~100. 게이트 적용 후 최종값
    display: int        # 1~5
    gated: bool         # 적절성 게이트가 발동했는가
    gate_reason: Optional[str] = None
    partial: bool       # 실패한 축이 있는가
    axes_used: list[Axis]
    axes_failed: list[Axis]


class AxisScores(BaseModel):
    """문항별 축 점수. 실패·건너뛴 축은 null."""

    content: Optional[int] = None
    speech: Optional[int] = None
    gaze: Optional[int] = None


class QuestionScore(BaseModel):
    """문항 하나의 채점 결과.

    되묻기는 독립 문항으로 세지 않는다. 원 질문에 합산하고 had_reask를 true로 둔다.
    """

    question_id: str
    question_number: int
    category: Optional[Category] = None
    difficulty: Optional[Difficulty] = None
    is_replay: bool
    is_spare_topic: bool
    score: int
    display: int
    axes: AxisScores
    transcript: str
    duration_sec: float
    word_count: int
    was_timeout: bool
    had_reask: bool
    comment: Optional[str] = None      # 문항 한 줄 코멘트. 생성 실패면 null


class Resilience(BaseModel):
    score: int
    display: int
    comment: str


class ImprovedAnswer(BaseModel):
    question_id: str
    original_excerpt: str
    suggestion: str
    t_start: float
    t_end: float


class ReportResult(BaseModel):
    """완성된 리포트. AI는 저장하지 않으므로 백엔드가 보관한다."""

    session_id: str
    generated_at: str                     # ISO8601 UTC, 예: 2026-09-05T14:22:31Z
    report_status: ReportStatus
    summary: Optional[str] = None         # 한 줄 총평. 생성 실패면 null
    overall: Overall
    axes: Axes
    questions: list[QuestionScore]
    resilience: Optional[Resilience] = None   # 친절형은 항상 null
    company_comment: Optional[str] = None     # 회사 미선택이면 null
    improved_answers: list[ImprovedAnswer] = Field(default_factory=list)


class ReportTaskDone(BaseModel):
    status: Literal["done"]
    result: ReportResult


class ReportTaskProcessing(BaseModel):
    """계약서 3장. 질문 생성 폴링과 달리 progress가 있다."""

    status: Literal["processing"]
    stage: ReportStage
    progress: float


# ---------------------------------------------------------------------------
# 7. 실패한 축만 재시도 — 응답은 리포트 생성과 같다 (ReportTaskDone)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 8. 회차 비교
# ---------------------------------------------------------------------------


class CompareReportItem(BaseModel):
    session_id: str
    round: int          # 1부터 시작. 배열은 오름차순으로 보낸다
    report: ReportResult


class CompareRequest(BaseModel):
    """POST /ai/reports/compare 요청. 회차 수 제한은 없다."""

    reports: list[CompareReportItem]


class AxisDelta(BaseModel):
    content: Optional[int] = None
    speech: Optional[int] = None
    gaze: Optional[int] = None


class VsPrevious(BaseModel):
    """직전 회차 대비. 성장 추이 화면의 기본 표시."""

    from_round: int
    to_round: int
    overall_delta: Optional[int]      # 두 회차 중 하나라도 부분 리포트면 null
    axis_delta: AxisDelta
    improved: list[str]
    declined: list[str]
    unchanged: list[str]
    comment: str


class TrendAxes(BaseModel):
    content: list[Optional[int]]
    speech: list[Optional[int]]
    gaze: list[Optional[int]]


class Trend(BaseModel):
    """전체 회차 흐름. 배열 길이는 compared_rounds와 같다.

    부분 리포트였던 회차는 overall이 null이 된다. 재정규화된 총점은
    다른 회차와 스케일이 달라 비교할 수 없기 때문이다.
    """

    overall: list[Optional[int]]
    axes: TrendAxes
    comment: str
    stalled_axes: list[Axis]   # 최근 3회차 변화가 임계 미만인 축. 없으면 빈 배열
    best_round: int


class ByQuestion(BaseModel):
    question_id: str
    text: str
    category: Optional[Category] = None
    scores: list[int]            # 회차별 점수. 그대로 꺾은선 그래프에 쓸 수 있다
    delta_from_previous: int
    delta_from_first: int
    comment: str


class CompareResponse(BaseModel):
    latest_round: int
    compared_rounds: list[int]
    vs_previous: Optional[VsPrevious] = None  # reports가 1개면 null
    trend: Trend
    by_question: list[ByQuestion]
    partial_rounds: list[int] = Field(default_factory=list)
