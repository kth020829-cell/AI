"""더미 리포트 생성 — 점수와 코멘트는 가짜, 구조와 계산 규칙은 진짜.

가짜   점수 값, 전사 텍스트, 코멘트, 근거 문장
진짜   표시 점수 변환, 축 재정규화, 되묻기 합산, 부분 실패 처리, 회차 비교 계산

LLM · STT · 시선 분석을 호출하지 않는다.
"""
import hashlib
import itertools
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ai.dummy import answer_length
from ai.redis_store import RedisCounter, RedisDict
from ai.report_schemas import (
    AXIS_WEIGHTS,
    Axes,
    AxisDelta,
    AxisResult,
    AxisScores,
    ByQuestion,
    CompareRequest,
    CompareResponse,
    Evidence,
    ImprovedAnswer,
    ImprovedAnswer as _ImprovedAnswer,  # noqa: F401  (가독성용 별칭)
    Overall,
    QuestionScore,
    ReportAnswer,
    ReportCreateRequest,
    ReportResult,
    ReportRetryRequest,
    Resilience,
    Trend,
    TrendAxes,
    VsPrevious,
    display_of,
)

AXES = ("content", "speech", "gaze")

# 축을 실패시키는 트리거. 실제 서버에서는 분석이 실제로 실패했을 때 일어난다.
FAIL_MARKER = "fail"

# content 축 실패는 전체 실패라 부분 리포트로 만들 수 없다. 별도 트리거를 둔다.
# audio_url에 이 조각이 있으면 리포트를 만들지 않고 태스크를 error로 남긴다.
CONTENT_FAIL_MARKER = "content_fail"

# 주제에서 벗어난 답변. 내용 관련성을 임계 아래로 떨어뜨려 적절성 게이트를 발동시킨다.
# 완전 이탈과 부분 이탈을 따로 둔다. 게이트가 두 단계라서 한쪽만으로는
# 백엔드가 나머지 한 단계를 한 번도 못 보기 때문이다.
OFFTOPIC_MARKER = "offtopic"          # 완전 이탈 → 내용 10~29 → 상한 40
PARTIAL_OFFTOPIC_MARKER = "partial"   # 부분 이탈 → 내용 30~49 → 상한 70

# 적절성 게이트 — 내용 관련성이 임계값 미만이면 총점에 상한을 씌운다. (계약서 5장)
#
# 사람이 매기는 5단계 라벨과 이 점수를 같은 자로 맞춘 것이다.
# 채점 프롬프트(Rubric)에도 같은 구간을 넣어야 라벨과 점수가 어긋나지 않는다.
#
#   5점 우수      85~100
#   4점 양호      70~84
#   3점 중간      50~69   게이트 없음
#   2점 미흡      30~49   총점 상한 70
#   1점 주제이탈   0~29    총점 상한 40
#
# 값은 잠정이며 앵커 답변 세트로 튜닝한다. 바뀌어도 응답 구조는 그대로다.
GATE_SEVERE_THRESHOLD = 30
GATE_PARTIAL_THRESHOLD = 50
GATE_SEVERE_CAP = 40
GATE_PARTIAL_CAP = 70
logger = logging.getLogger("cue.ai.report")

GATE_REASON = "content_relevance_low"

# 실제 내용 채점을 붙이는 자리. None이면 아래 해시 더미가 쓰인다.
#
#   def scorer(question_text: str, answer_text: str) -> int:   # 0~100
#
# 전사 텍스트가 있을 때만 불린다. D 담당이며 프롬프트 초안은
# docs/내용채점_프롬프트_초안.md에 있다. 여기에 함수를 꽂으면 그때부터
# 게이트가 진짜 답변을 보고 걸린다.
from ai.llm import llm_enabled

# 직접 꽂고 싶을 때 쓰는 자리. 테스트가 여기에 가짜 채점기를 넣는다.
# None이면 아래 _scorer()가 환경변수를 보고 정한다.
CONTENT_SCORER = None

# 내용 채점만 따로 끄는 스위치. 채점은 문항마다 LLM을 부르므로
# 요금이 나간다. STT · TTS와 같은 방식으로 따로 켜고 끈다.
#
#   설정 안 함        AI_MODE를 따라간다 (llm이면 켜짐)
#   USE_CONTENT_SCORING=0   AI_MODE가 llm이어도 끈다. 해시 점수로 돈다
SCORING_ENV = "USE_CONTENT_SCORING"


def content_scoring_enabled() -> bool:
    raw = os.environ.get(SCORING_ENV, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return llm_enabled()


def _scorer():
    """지금 쓸 채점기. 호출 시점에 정한다.

    import 시점에 정하면 환경변수를 바꿔도 반영되지 않고, 테스트가
    실제 API를 부를지 여부가 import 순서에 좌우된다.
    """
    if CONTENT_SCORER is not None:
        return CONTENT_SCORER
    if not content_scoring_enabled():
        return None
    from ai.content_eval import score_content

    return score_content


# 리포트 코멘트(내용 근거 · 개선 답변 · 기업 코멘트)를 만드는 Claude 호출을
# 바꿔 끼우는 자리. 테스트가 여기에 가짜를 넣는다. None이면 실제로 부른다.
#
#   def call(system: str, user: str) -> report_writer._Written
#
# 내용 채점과 같은 스위치(USE_CONTENT_SCORING)를 따른다. 채점이 꺼져 있으면
# 코멘트도 만들지 않고 더미 문장을 쓴다. 둘 다 리포트에서 요금이 나가는 부분이다.
WRITER_CALL = None


# 말하기 점수 변환식을 바꿔 끼우는 자리. 테스트가 여기에 가짜를 넣는다.
# None이면 아래 _speech_scorer()가 B의 stt.speech_score를 쓴다.
#
#   def scorer(metrics: dict) -> int:   # 0~100, 클수록 좋다
SPEECH_SCORER = None


def _speech_scorer():
    """지금 쓸 말하기 점수 변환식.

    실제 발화 지표가 있을 때만 불린다. 그때는 전사가 돌았다는 뜻이라
    ai/stt.py를 불러도 안전하다. (faster-whisper import는 그 안에서 또 미뤄진다)
    """
    if SPEECH_SCORER is not None:
        return SPEECH_SCORER
    from ai.stt import speech_score

    return speech_score


def _gaze_scorer():
    """C의 시선 점수 변환식. 실제 분석 결과가 있을 때만 불린다."""
    from infra.gaze_analysis.gaze_score import gaze_score

    return gaze_score


@dataclass
class Measured:
    """실제로 측정한 값. 더미 모드에서는 만들지 않는다.

    heard        question_id → 전사 결과(AnswerText). 되묻기 답변도 들어 있다
    gaze         question_id → C의 시선 분석 결과. 영상이 없는 문항은 빠진다
    gaze_failed  한 문항이라도 분석이 실패했는가 → GAZE_FAILED
    """

    heard: dict = field(default_factory=dict)
    gaze: dict = field(default_factory=dict)
    gaze_failed: bool = False

# 최근 3회차 변화가 이 값 미만이면 정체로 본다.
STALLED_THRESHOLD = 3


# ---------------------------------------------------------------------------
# 결정론적 점수
# ---------------------------------------------------------------------------


def score_for(*parts: str) -> int:
    """같은 입력이면 항상 같은 점수가 나온다. 50~90 범위.

    백엔드가 같은 리포트를 여러 번 요청해도 값이 흔들리지 않아야
    회귀 테스트를 짤 수 있다. 실제 서버에서는 LLM과 CV 분석 결과로 대체된다.
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return 50 + digest[0] % 41


def offtopic_score_for(*parts: str) -> int:
    """완전 이탈 답변의 내용 점수. 10~29로 심각 임계(30) 아래에 둔다."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return 10 + digest[0] % 20


def partial_offtopic_score_for(*parts: str) -> int:
    """부분 이탈 답변의 내용 점수. 30~49로 두 임계 사이에 둔다.

    질문의 핵심을 살짝 비껴간 답변이다. 완전 이탈은 아니므로 상한 40이 아니라
    상한 70이 걸린다. 이 트리거가 없으면 백엔드가 70 상한을 한 번도 못 본다.
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return 30 + digest[0] % 20


def apply_gate(overall_score: int, content_score: Optional[int]) -> tuple[int, bool, Optional[str]]:
    """적절성 게이트.

    주제에서 벗어난 답변일 때 유창함 등으로 점수를 받는 것을 막는다.
    세트 1 A(완전 이탈) -> Severe Cap (40점)
    세트 2 D(부분 이탈) -> Partial Cap (70점)

    반환값은 (총점, gated, gate_reason)이다.
    """
    if content_score is None or content_score >= GATE_PARTIAL_THRESHOLD:
        return overall_score, False, None
    
    if content_score < GATE_SEVERE_THRESHOLD:
        return min(overall_score, GATE_SEVERE_CAP), True, GATE_REASON
    
    # Partial off-topic
    return min(overall_score, GATE_PARTIAL_CAP), True, GATE_REASON


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 답변 정리 — 되묻기는 원 질문에 합산한다
# ---------------------------------------------------------------------------


def scored_answers(answers: list[ReportAnswer]) -> list[ReportAnswer]:
    """채점 대상 문항. 되묻기는 독립 문항으로 세지 않는다."""
    return [a for a in answers if a.type != "reask"]


def reasks_by_target(answers: list[ReportAnswer]) -> dict[str, list[ReportAnswer]]:
    """되묻기를 원 질문 아래로 모은다. reask_of가 없으면 버린다."""
    grouped: dict[str, list[ReportAnswer]] = {}
    for a in answers:
        if a.type == "reask" and a.reask_of:
            grouped.setdefault(a.reask_of, []).append(a)
    return grouped


# ---------------------------------------------------------------------------
# 축 상태 — 계약서 6장
# ---------------------------------------------------------------------------


def _has(values: list[Optional[str]], marker: str) -> bool:
    return any(v and marker in v.lower() for v in values)


def _has_marker(values: list[Optional[str]]) -> bool:
    return _has(values, FAIL_MARKER)


def content_failed(answers: list[ReportAnswer]) -> bool:
    """내용 분석이 실패했는가. 계약서 6장 — content 실패는 전체 실패로 처리한다.

    부분 리포트를 만들면 안 된다. 내용 관련성이 없으면 적절성 게이트가 작동하지
    않아 총점을 신뢰할 수 없기 때문이다.
    """
    return _has([a.audio_url for a in answers], CONTENT_FAIL_MARKER)


def is_offtopic(answers: list[ReportAnswer]) -> bool:
    return _has([a.audio_url for a in answers], OFFTOPIC_MARKER)


def is_partial_offtopic(answers: list[ReportAnswer]) -> bool:
    return _has([a.audio_url for a in answers], PARTIAL_OFFTOPIC_MARKER)


def axis_statuses(answers: list[ReportAnswer]) -> dict[str, tuple[str, Optional[str]]]:
    """축별 (status, error_code 또는 reason)을 정한다.

    content  더미에서는 항상 ok. 실패하면 전체 실패라 부분 리포트로 재현할 수 없다
    speech   audio_url에 fail이 있으면 failed
    gaze     video_url에 fail이 있으면 failed, 전부 null이면 skipped(no_video)
    """
    audio = [a.audio_url for a in answers]
    video = [a.video_url for a in answers]

    # content_fail은 fail을 포함하지만 전체 실패로 먼저 걸러지므로 여기 오지 않는다
    speech = ("failed", "SPEECH_FAILED") if _has_marker(audio) else ("ok", None)

    if _has_marker(video):
        gaze = ("failed", "GAZE_FAILED")
    elif all(v is None for v in video):
        # 카메라를 끄고 면접한 경우는 실패가 아니다
        gaze = ("skipped", "no_video")
    else:
        gaze = ("ok", None)

    return {"content": ("ok", None), "speech": speech, "gaze": gaze}


def renormalized_weights(usable: list[str]) -> dict[str, float]:
    """쓸 수 있는 축에 가중치를 비례 배분한다. (계약서 6장)"""
    total = sum(AXIS_WEIGHTS[a] for a in usable)
    return {a: AXIS_WEIGHTS[a] / total for a in usable}


# ---------------------------------------------------------------------------
# 리포트 조립
# ---------------------------------------------------------------------------


def _evidence(session_id: str, axis: str, rows: list[ReportAnswer]) -> list[Evidence]:
    """감점·강점 근거. t_start와 t_end는 항상 채운다."""
    if not rows:
        return []
    labels = {
        "content": [("weakness", "근거 부족", "선택 이유를 설명했으나 비교 대상이 제시되지 않았습니다."),
                    ("strength", "구체적 사례", "실제 수치를 들어 설명한 점이 좋았습니다.")],
        "speech": [("weakness", "말속도 빠름", "초반 답변에서 말속도가 평균보다 빨랐습니다.")],
        "gaze": [("weakness", "시선 회피", "답변 중반에 화면 밖을 보는 구간이 있었습니다.")],
    }[axis]

    out = []
    for i, (kind, label, comment) in enumerate(labels):
        row = rows[min(i, len(rows) - 1)]
        duration, _ = answer_length(row.audio_url)
        start = round(duration * 0.25, 1)
        out.append(Evidence(
            question_id=row.question_id,
            t_start=start,
            t_end=round(start + min(7.4, duration * 0.15), 1),
            kind=kind, label=label, comment=comment,
        ))
    return out


def _axis_result(
    session_id: str, axis: str, status: str, detail: Optional[str],
    rows: list[ReportAnswer], per_question: dict[str, dict[str, int]],
    measured: Optional[Measured] = None,
) -> AxisResult:
    if status == "failed":
        return AxisResult(status="failed", error_code=detail, evidence=[])
    if status == "skipped":
        return AxisResult(status="skipped", reason=detail, evidence=[])

    # 시선은 영상이 있는 문항만 평균한다. 카메라를 중간에 끈 문항까지
    # 더미 점수로 섞으면 실제 측정값이 흐려진다.
    counted = rows
    if axis == "gaze" and measured and measured.gaze:
        counted = [r for r in rows if r.question_id in measured.gaze]

    scores = [per_question[r.question_id][axis] for r in counted]
    score = round(sum(scores) / len(scores)) if scores else 0
    return AxisResult(
        status="ok",
        score=score,
        display=display_of(score),
        metrics=_metrics(axis, rows, measured),
        evidence=_real_evidence(axis, rows, measured)
        if _is_measured(axis, rows, measured)
        else _evidence(session_id, axis, rows),
    )


def _fluency_of(row, measured: Optional[Measured]) -> Optional[dict]:
    if not measured:
        return None
    heard = measured.heard.get(row.question_id)
    return getattr(heard, "fluency", None)


def _is_measured(axis: str, rows, measured: Optional[Measured]) -> bool:
    """이 축이 실제 측정값으로 채워졌는가. 아니면 더미 근거 문장을 쓴다."""
    if not measured:
        return False
    if axis == "speech":
        return any(_fluency_of(r, measured) for r in rows)
    if axis == "gaze":
        return bool(measured.gaze)
    return False


def _metrics(axis: str, rows, measured: Optional[Measured]) -> dict:
    """축별 세부 지표. 계약서에 키가 확정된 축만 채운다.

    speech          확정. 문항 평균(hesitation · cv)과 합계(반복)
    content · gaze  키가 아직 합의되지 않았다. 빈 객체가 정상이다
    """
    if axis != "speech":
        return {}
    found = [f for f in (_fluency_of(r, measured) for r in rows) if f]
    if not found:
        return {}
    n = len(found)
    return {
        "hesitation_score": round(sum(f["hesitation_score"] for f in found) / n),
        "speech_rate_cv": round(sum(f["speech_rate_cv"] for f in found) / n, 3),
        "repetition_count": sum(f["repetition_count"] for f in found),
    }


def _real_evidence(axis: str, rows, measured: Measured) -> list[Evidence]:
    """실제 측정에서 나온 근거.

    시선은 C가 회피 구간을 Evidence 모양으로 준다. 말하기는 아직 구간 단위
    근거를 만들지 않으므로 빈 배열이다. 더미 문장을 붙이면 실제 지표와
    어긋난 말을 하게 된다.
    """
    if axis != "gaze":
        return []
    out = []
    for r in rows:
        for e in measured.gaze.get(r.question_id, {}).get("evidence", []):
            out.append(Evidence(**{**e, "question_id": r.question_id}))
    return out


def _speech_score(row, measured: Optional[Measured]) -> Optional[int]:
    fluency = _fluency_of(row, measured)
    if not fluency:
        return None
    try:
        return max(0, min(100, int(_speech_scorer()(fluency))))
    except Exception:
        logger.warning("말하기 점수 변환에 실패해 더미 점수를 씁니다: %s", row.question_id)
        return None


def _gaze_score(row, measured: Optional[Measured]) -> Optional[int]:
    if not measured:
        return None
    result = measured.gaze.get(row.question_id)
    if not result:
        return None
    return max(0, min(100, int(_gaze_scorer()(result))))


class ContentScoringError(Exception):
    """실제 내용 채점이 실패했다. 리포트 전체를 CONTENT_FAILED로 끝낸다.

    내용 점수가 없으면 적절성 게이트를 돌릴 수 없어 총점을 믿을 수 없다.
    더미 점수로 채워 넣으면 틀린 총점이 정상 리포트처럼 나간다. (계약서 6장)
    """


# 문항별 내용 점수. 재시도 때 요청하지 않은 축은 다시 채점하지 않으려고 둔다.
# 채점은 문항마다 Claude를 부르므로, 시선만 재시도하는데 내용을 다시 채점하면
# 요금이 그대로 한 번 더 나간다. 서버가 재시작되면 비고, 그때는 다시 채점한다.
_CONTENT_CACHE: "OrderedDict[tuple, int]" = OrderedDict()
CONTENT_CACHE_MAX = 5000


def _cached_content(key: tuple) -> Optional[int]:
    score = _CONTENT_CACHE.get(key)
    if score is not None:
        _CONTENT_CACHE.move_to_end(key)
    return score


def _remember_content(key: tuple, score: int) -> None:
    _CONTENT_CACHE[key] = score
    _CONTENT_CACHE.move_to_end(key)
    while len(_CONTENT_CACHE) > CONTENT_CACHE_MAX:
        _CONTENT_CACHE.popitem(last=False)


# 문항별 시선 분석 결과. 내용 채점 캐시와 같은 이유로 둔다.
# 시선 분석은 9문항에 GPU로 약 4분이라, 말하기만 재시도하는데 다시 돌리면 안 된다.
# 영상 주소(presigned)는 요청마다 서명이 달라서 키에 넣지 않는다.
_GAZE_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()


def cached_gaze(session_id: str, question_id: str) -> Optional[dict]:
    result = _GAZE_CACHE.get((session_id, question_id))
    if result is not None:
        _GAZE_CACHE.move_to_end((session_id, question_id))
    return result


def remember_gaze(session_id: str, question_id: str, result: dict) -> None:
    _GAZE_CACHE[(session_id, question_id)] = result
    _GAZE_CACHE.move_to_end((session_id, question_id))
    while len(_GAZE_CACHE) > CONTENT_CACHE_MAX:
        _GAZE_CACHE.popitem(last=False)


def _answer_text(row, transcripts: dict[str, str], reasks=()) -> str:
    """채점할 답변 텍스트. 되묻기 답변은 원 답변 뒤에 이어 붙인다."""
    parts = [transcripts.get(row.question_id, "")]
    parts += [transcripts.get(r.question_id, "") for r in reasks]
    return " ".join(p.strip() for p in parts if p and p.strip())


def _content_score(
    session_id: str, row, transcripts: Optional[dict[str, str]], reasks=(),
    force: bool = False,
) -> Optional[int]:
    """실제 채점 결과. 채점기가 없거나 전사가 없으면 None.

    None이면 부르는 쪽이 해시 더미를 쓴다. 더미 모드와 채점을 끈 경우다.

    채점기가 있으면 결과는 반드시 진짜여야 한다.
      말이 없는 답변(전사가 빔)   0점. 답한 내용이 없다
      채점이 실패                ContentScoringError → CONTENT_FAILED
    """
    scorer = _scorer()
    if scorer is None or not transcripts:
        return None

    text = _answer_text(row, transcripts, reasks)
    if not text:
        return 0

    key = (session_id, row.question_id, row.text, text)
    if not force:
        cached = _cached_content(key)
        if cached is not None:
            return cached

    try:
        score = max(0, min(100, int(scorer(row.text, text))))
    except Exception as e:
        logger.warning("내용 채점 실패: %s (%s)", row.question_id, type(e).__name__)
        raise ContentScoringError(f"{row.question_id} 내용 채점에 실패했습니다") from e

    _remember_content(key, score)
    return score


def _statuses(answers, measured: Optional[Measured]):
    """더미 트리거로 정한 상태에 실제 분석 실패를 덧씌운다."""
    statuses = axis_statuses(answers)
    if measured and measured.gaze_failed and statuses["gaze"][0] == "ok":
        statuses["gaze"] = ("failed", "GAZE_FAILED")
    return statuses


def _measured_scores(session_id: str, row, measured: Optional[Measured]) -> dict[str, int]:
    """문항 하나의 축 점수. 실제 측정값이 있는 축은 그것으로, 없으면 해시로."""
    scores = {a: score_for(session_id, row.question_id, a) for a in AXES}
    speech = _speech_score(row, measured)
    if speech is not None:
        scores["speech"] = speech
    gaze = _gaze_score(row, measured)
    if gaze is not None:
        scores["gaze"] = gaze
    return scores


def _length(row, measured: Optional[Measured]) -> tuple[int, int]:
    heard = measured.heard.get(row.question_id) if measured else None
    if heard is not None:
        return heard.duration_sec, heard.word_count
    return answer_length(row.audio_url)


def _transcript(row, transcripts: Optional[dict[str, str]], reasks=()) -> str:
    if transcripts:
        text = _answer_text(row, transcripts, reasks)
        if text:
            return text
    return f"(더미 전사) {row.text} 에 대한 답변입니다."


def build_report(
    session_id: str,
    req: ReportCreateRequest,
    transcripts: Optional[dict[str, str]] = None,
    measured: Optional[Measured] = None,
    force_content: bool = False,
) -> ReportResult:
    rows = scored_answers(req.answers)
    extra = reasks_by_target(req.answers)
    statuses = _statuses(req.answers, measured)

    usable = [a for a in AXES if statuses[a][0] == "ok"]
    weights = renormalized_weights(usable)

    # 문항별 축 점수 — 결정론적으로 만든다.
    # 주제 이탈 답변이면 내용 점수만 게이트 임계 아래로 떨어뜨린다.
    # 완전 이탈이 부분 이탈보다 우선한다. 둘 다 들어 있으면 더 심한 쪽으로 본다.
    offtopic = is_offtopic(req.answers)
    partial_offtopic = not offtopic and is_partial_offtopic(req.answers)
    per_question: dict[str, dict[str, int]] = {}
    for r in rows:
        scores = _measured_scores(session_id, r, measured)
        real = _content_score(
            session_id, r, transcripts, extra.get(r.question_id, []), force=force_content
        )
        if real is not None:
            # 실제 채점이 붙어 있으면 더미 트리거보다 우선한다
            scores["content"] = real
        elif offtopic:
            scores["content"] = offtopic_score_for(session_id, r.question_id)
        elif partial_offtopic:
            scores["content"] = partial_offtopic_score_for(session_id, r.question_id)
        per_question[r.question_id] = scores

    questions: list[QuestionScore] = []
    for r in rows:
        picked = {a: per_question[r.question_id][a] for a in usable}
        q_score = round(sum(picked[a] * weights[a] for a in usable))

        duration, words = _length(r, measured)
        for reask in extra.get(r.question_id, []):
            # 되묻기 답변은 원 질문의 답변에 이어 붙여 하나로 채점한다
            d, w = _length(reask, measured)
            duration += d
            words += w

        questions.append(QuestionScore(
            question_id=r.question_id,
            question_number=r.question_number,
            category=r.category,
            difficulty=r.difficulty,
            is_replay=r.is_replay,
            is_spare_topic=r.is_spare_topic,
            score=q_score,
            display=display_of(q_score),
            axes=AxisScores(**{a: picked.get(a) for a in AXES}),
            transcript=_transcript(r, transcripts, extra.get(r.question_id, [])),
            duration_sec=float(duration),
            word_count=words,
            was_timeout=r.is_timeout,
            had_reask=r.question_id in extra,
        ))

    axes = Axes(**{
        a: _axis_result(
            session_id, a, statuses[a][0], statuses[a][1], rows, per_question, measured
        )
        for a in AXES
    })

    axes_failed = [a for a in AXES if statuses[a][0] == "failed"]
    overall_score = round(sum(getattr(axes, a).score * weights[a] for a in usable))

    overall_score, gated, gate_reason = apply_gate(overall_score, axes.content.score)

    # 실제 채점이 돈 경우에만 코멘트를 만든다. 더미는 백엔드가 확인 중인 고정 문장 그대로다.
    written = _written(session_id, req, rows, extra, transcripts, measured, per_question)
    if written is not None:
        if axes.content.status == "ok":
            axes.content.evidence = [
                Evidence(question_id=p.question_id, t_start=p.t_start, t_end=p.t_end,
                         kind=p.kind, label=p.label, comment=p.comment)
                for p in written.evidence
            ]
        improved = [
            ImprovedAnswer(question_id=p.question_id, original_excerpt=p.excerpt,
                           suggestion=p.suggestion, t_start=p.t_start, t_end=p.t_end)
            for p in written.improved_answers
        ]
        company_comment = written.company_comment
        resilience = _real_resilience(req.persona, rows, per_question)
        summary = written.summary
        comments = written.question_comments
    else:
        improved = _improved(rows)
        company_comment = _company_comment(req)
        # 친절형은 압박 구간이 없어 산출할 수 없다
        resilience = _resilience(session_id, req.persona)
        summary = DUMMY_SUMMARY
        comments = {q.question_id: _dummy_question_comment(q.score) for q in questions}

    questions = [
        q.model_copy(update={"comment": comments.get(q.question_id)}) for q in questions
    ]

    return ReportResult(
        session_id=session_id,
        generated_at=_now(),
        report_status="partial" if axes_failed else "complete",
        summary=summary,
        overall=Overall(
            score=overall_score,
            display=display_of(overall_score),
            gated=gated,
            gate_reason=gate_reason,
            partial=bool(axes_failed),
            axes_used=usable,
            axes_failed=axes_failed,
        ),
        axes=axes,
        questions=questions,
        resilience=resilience,
        company_comment=company_comment,
        improved_answers=improved,
    )


def _written(session_id, req, rows, extra, transcripts, measured, per_question):
    """코멘트를 만든다. 만들지 않는 경우(더미 · 요금 스위치 꺼짐)는 None.

    진짜 Claude는 요금 스위치(USE_CONTENT_SCORING)가 켜져 있을 때만 부른다.
    테스트가 채점기만 가짜로 꽂은 경우에 코멘트까지 진짜로 부르면, API 키가 있는
    곳에서 테스트를 돌릴 때마다 요금이 나간다. 테스트는 WRITER_CALL에 가짜를 꽂는다.
    """
    if not transcripts:
        return None
    if WRITER_CALL is None and not content_scoring_enabled():
        return None
    from ai import report_writer

    def answer_of(row) -> "report_writer.Answer":
        heard = measured.heard.get(row.question_id) if measured else None
        duration, _ = _length(row, measured)
        return report_writer.Answer(
            question_id=row.question_id,
            question=row.text,
            category=row.category,
            kind=row.type,
            text=(transcripts.get(row.question_id) or "").strip(),
            content_score=per_question.get(row.question_id, {}).get("content", 0),
            duration_sec=float(duration),
            words=getattr(heard, "words", None),
        )

    answers = []
    for r in rows:
        a = answer_of(r)
        a.reasks = [answer_of(x) for x in extra.get(r.question_id, [])]
        answers.append(a)

    return report_writer.write(
        session_id, req.persona, req.job_role,
        req.company_profile_override, answers, call=WRITER_CALL,
    )


# 압박 대응력 — 계약서 11장에서 미확정이던 산출식. (2026-09-25 확정)
#
# 압박형 면접에서 꼬리질문(파고드는 질문) 답변의 내용 점수가 주질문보다
# 얼마나 떨어졌나로 본다. 덜 떨어질수록 높다. 이미 있는 내용 점수로 계산해
# 요금이 들지 않고, 같은 입력이면 늘 같은 값이 나온다.
#
#   하락 = 주질문 평균 - 꼬리질문 평균 (음수면 0)
#   점수 = 100 - 하락 × 2
#
# 꼬리질문이 하나도 없으면(답변이 모두 부실해 꼬리질문이 생략된 경우) 잴 수 없어 null이다.
RESILIENCE_DROP_WEIGHT = 2


def _real_resilience(persona, rows, per_question) -> Optional[Resilience]:
    if persona != "pressure":
        return None
    mains = [per_question[r.question_id]["content"] for r in rows if r.type == "question"]
    follows = [per_question[r.question_id]["content"] for r in rows if r.type == "followup"]
    if not mains or not follows:
        return None

    main_avg = round(sum(mains) / len(mains))
    follow_avg = round(sum(follows) / len(follows))
    drop = main_avg - follow_avg
    score = max(0, min(100, round(100 - max(0, drop) * RESILIENCE_DROP_WEIGHT)))

    if drop <= 0:
        comment = (f"압박 꼬리질문에서도 주질문과 같거나 더 높은 내용 점수를 유지했습니다 "
                   f"(주질문 {main_avg}점, 꼬리질문 {follow_avg}점).")
    elif drop < 10:
        comment = (f"꼬리질문 내용 점수가 주질문보다 {drop}점 낮았지만 크게 흔들리지 않았습니다 "
                   f"(주질문 {main_avg}점, 꼬리질문 {follow_avg}점).")
    else:
        comment = (f"꼬리질문에서 내용 점수가 {drop}점 떨어졌습니다 "
                   f"(주질문 {main_avg}점, 꼬리질문 {follow_avg}점). "
                   f"파고드는 질문에서 근거를 이어가는 연습이 필요합니다.")
    return Resilience(score=score, display=display_of(score), comment=comment)


def _resilience(session_id: str, persona: str) -> Optional[Resilience]:
    if persona != "pressure":
        return None
    score = score_for(session_id, "resilience")
    return Resilience(
        score=score,
        display=display_of(score),
        comment="압박 질문 이후 답변 길이가 줄어드는 구간이 관찰되었습니다.",
    )


# 더미의 총평 · 문항 코멘트. 프론트가 칸 모양을 확인할 수 있게 고정 문장을 준다
DUMMY_SUMMARY = "질문 의도는 잘 파악했지만, 경험을 수치와 근거로 뒷받침하는 연습이 필요합니다."


def _dummy_question_comment(score: int) -> str:
    if score >= 80:
        return "핵심을 먼저 말하고 근거를 이어 가 흐름이 안정적이었습니다."
    if score >= 60:
        return "요지는 전달됐지만 구체적인 사례가 더 필요합니다."
    return "질문에 대한 결론이 분명하게 드러나지 않았습니다."


def _company_comment(req: ReportCreateRequest) -> Optional[str]:
    # 계약서: company_profile_override가 null이면 null. company_id는 추적용이라 보지 않는다
    if not req.company_profile_override or not req.company_profile_override.strip():
        return None
    return "인재상에 비추어 협업 경험을 더 구체적으로 제시하면 좋겠습니다."


def _improved(rows: list[ReportAnswer]) -> list[ImprovedAnswer]:
    out = []
    for r in rows[:2]:
        duration, _ = answer_length(r.audio_url)
        start = round(duration * 0.25, 1)
        out.append(ImprovedAnswer(
            question_id=r.question_id,
            original_excerpt="(더미) 답변 일부 발췌",
            suggestion="선택 이유와 대안 비교를 함께 언급하면 설득력이 올라갑니다.",
            t_start=start,
            t_end=round(start + 7.4, 1),
        ))
    return out


def build_retry(
    session_id: str,
    req: ReportRetryRequest,
    transcripts: Optional[dict[str, str]] = None,
    measured: Optional[Measured] = None,
) -> ReportResult:
    """재시도. 요청한 축을 다시 계산하고 리포트 전체를 다시 조립한다. (계약서 7장)

    총점 · 게이트 · 부분 실패 여부까지 새로 계산해야 하므로 축만 돌려주지 않는다.
    요청하지 않은 축은 첫 리포트 때의 값을 쓴다. 내용은 채점 캐시에서, 시선은
    분석 캐시에서(report_pipeline), 말하기는 전사 캐시에서 나온다.
    """
    return build_report(
        session_id,
        req,
        transcripts=transcripts,
        measured=measured,
        force_content="content" in req.axes,
    )


# ---------------------------------------------------------------------------
# 회차 비교 — 계약서 8장
# ---------------------------------------------------------------------------


def build_compare(req: CompareRequest) -> CompareResponse:
    items = sorted(req.reports, key=lambda r: r.round)
    rounds = [i.round for i in items]
    partial_rounds = [i.round for i in items if i.report.report_status == "partial"]

    # 부분 리포트는 재정규화된 총점이라 스케일이 달라 비교에서 제외한다
    overall = [
        None if i.report.report_status == "partial" else i.report.overall.score
        for i in items
    ]
    axes_trend = {
        a: [getattr(i.report.axes, a).score for i in items] for a in AXES
    }

    known = [(r, s) for r, s in zip(rounds, overall) if s is not None]
    best_round = max(known, key=lambda x: x[1])[0] if known else rounds[-1]

    return CompareResponse(
        latest_round=rounds[-1],
        compared_rounds=rounds,
        vs_previous=_vs_previous(items) if len(items) >= 2 else None,
        trend=Trend(
            overall=overall,
            axes=TrendAxes(**axes_trend),
            comment=_trend_comment(axes_trend, _stalled(axes_trend)),
            stalled_axes=_stalled(axes_trend),
            best_round=best_round,
        ),
        by_question=_by_question(items),
        partial_rounds=partial_rounds,
    )


def _comparable_scores(items) -> list[dict[str, QuestionScore]]:
    """회차마다 비교할 수 있는 문항. {question_id: 문항}

    재면접 회차는 is_replay가 true인 문항(1회차와 같은 주질문)만 비교한다.
    is_spare_topic은 필터에 쓰지 않는다.

    1회차(round 1)는 재면접이 아니라서 문항이 전부 is_replay false다. 그대로 거르면
    1회차가 들어간 비교는 문항별 결과가 늘 비게 된다. 재면접은 1회차 replay_log 순서를
    그대로 재생해 같은 주질문이 같은 question_id를 받으므로, 1회차에서는 다른 회차가
    is_replay로 다시 낸 question_id만 고른다.
    """
    replay = [{q.question_id: q for q in i.report.questions if q.is_replay} for i in items]
    replayed_ids = set()
    for item, scores in zip(items, replay):
        if item.round != 1:
            replayed_ids |= set(scores)

    out = []
    for item, scores in zip(items, replay):
        if item.round == 1:
            scores = {q.question_id: q for q in item.report.questions
                      if q.question_id in replayed_ids}
        out.append(scores)
    return out


def _vs_previous(items) -> VsPrevious:
    prev, cur = items[-2], items[-1]
    prev_q, cur_q = _comparable_scores(items)[-2:]
    shared = [qid for qid in cur_q if qid in prev_q]

    improved = [q for q in shared if cur_q[q].score > prev_q[q].score]
    declined = [q for q in shared if cur_q[q].score < prev_q[q].score]
    unchanged = [q for q in shared if cur_q[q].score == prev_q[q].score]

    both_complete = (
        prev.report.report_status == "complete" and cur.report.report_status == "complete"
    )
    # 부분 리포트가 끼면 비교 불가라 null이다. 0(변화 없음)과 구분한다
    overall_delta = (
        cur.report.overall.score - prev.report.overall.score if both_complete else None
    )

    delta = {}
    for a in AXES:
        p, c = getattr(prev.report.axes, a).score, getattr(cur.report.axes, a).score
        delta[a] = c - p if (p is not None and c is not None) else None

    comment = (
        "부분 리포트가 포함되어 총점은 비교하지 않았습니다."
        if not both_complete
        else _vs_comment(overall_delta, delta)
    )
    return VsPrevious(
        from_round=prev.round, to_round=cur.round,
        overall_delta=overall_delta, axis_delta=AxisDelta(**delta),
        improved=improved, declined=declined, unchanged=unchanged, comment=comment,
    )


def _stalled(axes_trend: dict[str, list[Optional[int]]]) -> list[str]:
    """최근 3회차 변화가 임계 미만인 축. 없으면 빈 배열."""
    out = []
    for a in AXES:
        recent = [s for s in axes_trend[a][-3:] if s is not None]
        if len(recent) == 3 and max(recent) - min(recent) < STALLED_THRESHOLD:
            out.append(a)
    return out


def _by_question(items) -> list[ByQuestion]:
    """모든 회차에 공통으로 있는 is_replay 문항만 담는다."""
    per_round = _comparable_scores(items)
    if not per_round:
        return []
    first_round = items[0].round

    shared = set(per_round[0])
    for m in per_round[1:]:
        shared &= set(m)

    out = []
    for qid in sorted(shared, key=lambda q: per_round[0][q].question_number):
        scores = [m[qid].score for m in per_round]
        first_q = per_round[0][qid]
        from_prev = scores[-1] - scores[-2] if len(scores) >= 2 else 0
        from_first = scores[-1] - scores[0]
        out.append(ByQuestion(
            question_id=qid,
            # 리포트에는 질문 문장이 없어(계약서 4장) 번호와 카테고리로 부른다.
            # 화면에 문장이 필요하면 백엔드가 저장해 둔 질문을 쓰면 된다.
            text=f"{first_q.question_number}번 {first_q.category or '질문'}",
            category=first_q.category,
            scores=scores,
            delta_from_previous=from_prev,
            delta_from_first=from_first,
            comment=_question_comment(from_prev, from_first, len(scores), first_round),
        ))
    return out


# 회차 비교 코멘트는 점수 변화를 그대로 문장으로 옮긴다. Claude를 부르지 않는다.
# 숫자에 있는 것만 말하므로 틀릴 일이 없고 요금도 들지 않는다.
AXIS_NAMES = {"content": "내용", "speech": "말하기", "gaze": "시선"}


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def _vs_comment(overall_delta: int, axis_delta: dict) -> str:
    if overall_delta > 0:
        head = f"직전 회차보다 총점이 {overall_delta}점 올랐습니다."
    elif overall_delta < 0:
        head = f"직전 회차보다 총점이 {-overall_delta}점 내려갔습니다."
    else:
        head = "직전 회차와 총점이 같습니다."
    parts = [f"{AXIS_NAMES[a]} {_signed(d)}" for a, d in axis_delta.items() if d]
    return f"{head} ({', '.join(parts)})" if parts else head


def _trend_comment(axes_trend: dict, stalled: list) -> str:
    moves = []
    for a in AXES:
        known = [s for s in axes_trend[a] if s is not None]
        if len(known) >= 2 and known[-1] != known[0]:
            moves.append(f"{AXIS_NAMES[a]} {known[0]}→{known[-1]}")
    if not moves:
        text = "회차 간 축 점수 변화가 없습니다."
    else:
        text = "첫 회차부터 지금까지 " + ", ".join(moves) + "."
    if stalled:
        text += " 최근 3회차 동안 " + ", ".join(AXIS_NAMES[a] for a in stalled) + " 점수가 정체돼 있습니다."
    return text


def _question_comment(from_prev: int, from_first: int, rounds: int, first_round: int = 1) -> str:
    if rounds < 2:
        return "비교할 이전 회차가 없습니다."
    if from_prev > 0:
        text = f"직전 회차보다 {from_prev}점 올랐습니다."
    elif from_prev < 0:
        text = f"직전 회차보다 {-from_prev}점 내려갔습니다."
    else:
        text = "직전 회차와 같은 점수입니다."
    if rounds > 2:
        # 비교한 첫 회차가 1회차가 아닐 수 있다 (1회차 리포트가 없을 때 [2, 3])
        text += f" {first_round}회차 대비 {_signed(from_first)}점입니다."
    return text


# ---------------------------------------------------------------------------
# 멱등성 — 같은 Idempotency-Key로 다시 요청하면 기존 task_id를 돌려준다
# ---------------------------------------------------------------------------

# 워커마다 따로 노는 itertools.count 대신 Redis INCR로 워커 간 원자적으로 증가.
# dummy.py의 _TASK_SEQ와는 별도 카운터 (task_r 접두사가 겹치면 안 되므로 네임스페이스 분리).
_TASK_SEQ = RedisCounter("report_task_seq")

# 태스크 상한과 맞춘다. 키가 지워지면 같은 키로 다시 요청했을 때 새 작업이 생기는데,
# 그 시점에는 원래 태스크도 이미 정리된 뒤라 어차피 다시 만들어야 한다.
MAX_IDEMPOTENCY_KEYS = 5000

# Redis로 이전 (2026-09-08). SESSIONS/TASKS와 같은 이유 - 워커 간 공유 필요.
IDEMPOTENCY = RedisDict("idempotency")


def new_report_task_id() -> str:
    return f"task_r{_TASK_SEQ.next():03d}"


def remember(idempotency_key: str, task_id: str) -> None:
    IDEMPOTENCY[idempotency_key] = task_id
    while len(IDEMPOTENCY) > MAX_IDEMPOTENCY_KEYS:
        IDEMPOTENCY.pop(next(iter(IDEMPOTENCY)))


def reset() -> None:
    IDEMPOTENCY.clear()
    _CONTENT_CACHE.clear()
    _GAZE_CACHE.clear()
    from ai import report_writer

    report_writer.reset()
