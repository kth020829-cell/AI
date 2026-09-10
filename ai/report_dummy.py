"""더미 리포트 생성 — 점수와 코멘트는 가짜, 구조와 계산 규칙은 진짜.

가짜   점수 값, 전사 텍스트, 코멘트, 근거 문장
진짜   표시 점수 변환, 축 재정규화, 되묻기 합산, 부분 실패 처리, 회차 비교 계산

LLM · STT · 시선 분석을 호출하지 않는다.
"""
import hashlib
import itertools
import logging
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
    PartialAxes,
    QuestionScore,
    ReportAnswer,
    ReportCreateRequest,
    ReportResult,
    ReportRetryRequest,
    ReportRetryResult,
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
CONTENT_SCORER = None

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
) -> AxisResult:
    if status == "failed":
        return AxisResult(status="failed", error_code=detail, evidence=[])
    if status == "skipped":
        return AxisResult(status="skipped", reason=detail, evidence=[])

    scores = [per_question[r.question_id][axis] for r in rows]
    score = round(sum(scores) / len(scores)) if scores else 0
    return AxisResult(
        status="ok",
        score=score,
        display=display_of(score),
        metrics={},  # 축별 세부 지표는 아직 확정되지 않았다. 빈 객체가 정상이다
        evidence=_evidence(session_id, axis, rows),
    )


def _content_score(
    session_id: str, row, transcripts: Optional[dict[str, str]]
) -> Optional[int]:
    """실제 채점 결과. 채점기가 없거나 전사가 없으면 None.

    None이면 부르는 쪽이 해시 더미를 쓴다.
    """
    if CONTENT_SCORER is None or not transcripts:
        return None

    text = transcripts.get(row.question_id, "").strip()
    if not text:
        return None

    try:
        score = int(CONTENT_SCORER(row.text, text))
    except Exception:
        # 채점이 터져도 리포트 전체를 날리지는 않는다. 더미 점수로 이어간다.
        logger.warning("내용 채점에 실패해 더미 점수를 씁니다: %s", row.question_id)
        return None

    return max(0, min(100, score))


def build_report(
    session_id: str,
    req: ReportCreateRequest,
    transcripts: Optional[dict[str, str]] = None,
) -> ReportResult:
    rows = scored_answers(req.answers)
    extra = reasks_by_target(req.answers)
    statuses = axis_statuses(req.answers)

    usable = [a for a in AXES if statuses[a][0] == "ok"]
    weights = renormalized_weights(usable)

    # 문항별 축 점수 — 결정론적으로 만든다.
    # 주제 이탈 답변이면 내용 점수만 게이트 임계 아래로 떨어뜨린다.
    # 완전 이탈이 부분 이탈보다 우선한다. 둘 다 들어 있으면 더 심한 쪽으로 본다.
    offtopic = is_offtopic(req.answers)
    partial_offtopic = not offtopic and is_partial_offtopic(req.answers)
    per_question: dict[str, dict[str, int]] = {}
    for r in rows:
        scores = {a: score_for(session_id, r.question_id, a) for a in AXES}
        real = _content_score(session_id, r, transcripts)
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

        duration, words = answer_length(r.audio_url)
        for reask in extra.get(r.question_id, []):
            # 되묻기 답변은 원 질문의 답변에 이어 붙여 하나로 채점한다
            d, w = answer_length(reask.audio_url)
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
            transcript=f"(더미 전사) {r.text} 에 대한 답변입니다.",
            duration_sec=float(duration),
            word_count=words,
            was_timeout=r.is_timeout,
            had_reask=r.question_id in extra,
        ))

    axes = Axes(**{
        a: _axis_result(session_id, a, statuses[a][0], statuses[a][1], rows, per_question)
        for a in AXES
    })

    axes_failed = [a for a in AXES if statuses[a][0] == "failed"]
    overall_score = round(sum(getattr(axes, a).score * weights[a] for a in usable))

    overall_score, gated, gate_reason = apply_gate(overall_score, axes.content.score)

    return ReportResult(
        session_id=session_id,
        generated_at=_now(),
        report_status="partial" if axes_failed else "complete",
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
        # 친절형은 압박 구간이 없어 산출할 수 없다
        resilience=_resilience(session_id, req.persona),
        company_comment=_company_comment(req),
        improved_answers=_improved(rows),
    )


def _resilience(session_id: str, persona: str) -> Optional[Resilience]:
    if persona != "pressure":
        return None
    score = score_for(session_id, "resilience")
    return Resilience(
        score=score,
        display=display_of(score),
        comment="압박 질문 이후 답변 길이가 줄어드는 구간이 관찰되었습니다.",
    )


def _company_comment(req: ReportCreateRequest) -> Optional[str]:
    if not req.company_id and not req.company_profile_override:
        return None  # 회사 미선택이면 null
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
) -> ReportRetryResult:
    """요청한 축만 다시 계산해 돌려준다. 백엔드가 기존 리포트에 병합한다."""
    rows = scored_answers(req.answers)
    statuses = axis_statuses(req.answers)
    per_question = {
        r.question_id: {a: score_for(session_id, r.question_id, a) for a in AXES}
        for r in rows
    }
    picked = {
        a: _axis_result(session_id, a, statuses[a][0], statuses[a][1], rows, per_question)
        for a in req.axes
    }
    return ReportRetryResult(
        session_id=session_id, generated_at=_now(), axes=PartialAxes(**picked)
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
            comment="내용 축은 상승했으나 시선 축은 변화가 크지 않습니다.",
            stalled_axes=_stalled(axes_trend),
            best_round=best_round,
        ),
        by_question=_by_question(items),
        partial_rounds=partial_rounds,
    )


def _replay_scores(item) -> dict[str, QuestionScore]:
    """비교 대상은 is_replay가 true인 문항뿐이다. is_spare_topic은 필터에 쓰지 않는다."""
    return {q.question_id: q for q in item.report.questions if q.is_replay}


def _vs_previous(items) -> VsPrevious:
    prev, cur = items[-2], items[-1]
    prev_q, cur_q = _replay_scores(prev), _replay_scores(cur)
    shared = [qid for qid in cur_q if qid in prev_q]

    improved = [q for q in shared if cur_q[q].score > prev_q[q].score]
    declined = [q for q in shared if cur_q[q].score < prev_q[q].score]
    unchanged = [q for q in shared if cur_q[q].score == prev_q[q].score]

    both_complete = (
        prev.report.report_status == "complete" and cur.report.report_status == "complete"
    )
    overall_delta = (
        cur.report.overall.score - prev.report.overall.score if both_complete else 0
    )

    delta = {}
    for a in AXES:
        p, c = getattr(prev.report.axes, a).score, getattr(cur.report.axes, a).score
        delta[a] = c - p if (p is not None and c is not None) else None

    comment = (
        "부분 리포트가 포함되어 총점은 비교하지 않았습니다."
        if not both_complete
        else "문항별 점수 변화는 by_question에서 확인할 수 있습니다."
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
    per_round = [_replay_scores(i) for i in items]
    if not per_round:
        return []

    shared = set(per_round[0])
    for m in per_round[1:]:
        shared &= set(m)

    out = []
    for qid in sorted(shared, key=lambda q: per_round[0][q].question_number):
        scores = [m[qid].score for m in per_round]
        out.append(ByQuestion(
            question_id=qid,
            text=f"(더미) {qid} 주질문",
            category=per_round[0][qid].category,
            scores=scores,
            delta_from_previous=scores[-1] - scores[-2] if len(scores) >= 2 else 0,
            delta_from_first=scores[-1] - scores[0],
            comment="회차를 거듭하며 답변 구성이 달라졌습니다.",
        ))
    return out


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
