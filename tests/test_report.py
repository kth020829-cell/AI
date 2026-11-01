"""리포트 생성 API — docs/리포트생성_API계약_백엔드전달용.md

계약서의 JSON 예시 파싱과 엔드포인트 동작을 함께 확인한다.
FastAPI TestClient로 실제 HTTP 요청을 보낸다.
"""
import json

import pytest

from ai.report_dummy import (
    GATE_PARTIAL_CAP,
    GATE_PARTIAL_THRESHOLD,
    GATE_SEVERE_CAP,
    GATE_SEVERE_THRESHOLD,
    apply_gate,
)
from ai.report_schemas import (
    AXIS_WEIGHTS,
    CompareResponse,
    ReportCreateRequest,
    ReportResult,
    display_of,
)

AXES = ("content", "speech", "gaze")


# ---------------------------------------------------------------------------
# 요청 조립 헬퍼
# ---------------------------------------------------------------------------


def answer(n, *, type="question", category="지원동기", difficulty="L1",
           audio="ok", video="ok", is_timeout=False, reask_of=None,
           is_replay=False, is_spare_topic=False):
    """답변 하나. audio/video는 ok · short · fail · none 중 하나."""
    urls = {
        "ok": f"https://s3.../ans_{n}.webm",
        "short": f"https://s3.../ans_{n}_short.webm",
        "fail": f"https://s3.../ans_{n}_fail.webm",
        # 내용 분석 실패 — 전체 실패다. speech의 fail과 구분된다
        "content_fail": f"https://s3.../ans_{n}_content_fail.webm",
        # 완전 이탈 — 내용 점수가 10~29로 떨어져 상한 40이 걸린다
        "offtopic": f"https://s3.../ans_{n}_offtopic.webm",
        # 부분 이탈 — 내용 점수가 30~49로 떨어져 상한 70이 걸린다
        "partial": f"https://s3.../ans_{n}_partial.webm",
        # 주제 이탈 + 말하기 분석 실패
        "offtopic_fail": f"https://s3.../ans_{n}_offtopic_fail.webm",
        "none": None,
    }
    vurls = {
        "ok": f"https://s3.../ans_{n}.mp4",
        "fail": f"https://s3.../ans_{n}_fail.mp4",
        "none": None,
    }
    qid = f"q_{n}r" if type == "reask" else f"q_{n}"
    return {
        "question_id": qid,
        "type": type,
        "text": f"{n}번 질문",
        "category": None if type == "reask" else category,
        "difficulty": None if type == "reask" else difficulty,
        "question_number": n,
        "audio_url": urls[audio],
        "video_url": vurls[video],
        "is_timeout": is_timeout,
        "reask_of": reask_of,
        "is_replay": is_replay,
        "is_spare_topic": is_spare_topic,
    }


def report_body(answers, *, persona="pressure", company_id="hyundai_enc"):
    return {
        "persona": persona,
        "job_role": "백엔드 개발",
        "company_id": company_id,
        "company_profile_override": None,
        "answers": answers,
    }


def key(n=1):
    return {"Idempotency-Key": f"rpt_sess9f2a1c_{n:02d}"}


def make_report(client, auth, answers, *, persona="pressure",
                company_id="hyundai_enc", session_id="sess_9f2a1c", idem=1):
    res = client.post(
        f"/ai/sessions/{session_id}/report",
        headers={**auth, **key(idem)},
        json=report_body(answers, persona=persona, company_id=company_id),
    )
    assert res.status_code == 202, res.json()
    task_id = res.json()["task_id"]
    got = client.get(f"/ai/tasks/{task_id}", headers=auth)
    assert got.status_code == 200, got.json()
    body = got.json()
    assert body["status"] == "done"
    return body["result"], task_id


def six_answers(**kw):
    return [answer(n, is_replay=(n % 2 == 1), **kw) for n in range(1, 7)]


# ---------------------------------------------------------------------------
# 계약서 JSON 예시 파싱
# ---------------------------------------------------------------------------


def test_계약서_2장_리포트_생성_요청():
    raw = """
    {
      "persona": "pressure",
      "job_role": "백엔드 개발",
      "company_id": "hyundai_enc",
      "company_profile_override": null,
      "answers": [
        {
          "question_id": "q_1",
          "type": "question",
          "text": "백엔드 개발 직무에 지원하신 이유를 말씀해 주세요.",
          "category": "지원동기",
          "difficulty": "L1",
          "question_number": 1,
          "audio_url": "https://s3.../ans_1.webm",
          "video_url": "https://s3.../ans_1.mp4",
          "is_timeout": false,
          "reask_of": null,
          "is_replay": false,
          "is_spare_topic": false
        },
        {
          "question_id": "q_1r",
          "type": "reask",
          "text": "어떤 계기가 있었는지 조금 더 말씀해 주시겠어요?",
          "category": null,
          "difficulty": null,
          "question_number": 1,
          "audio_url": "https://s3.../ans_1r.webm",
          "video_url": "https://s3.../ans_1r.mp4",
          "is_timeout": false,
          "reask_of": "q_1"
        }
      ]
    }
    """
    req = ReportCreateRequest.model_validate(json.loads(raw))
    assert req.persona == "pressure"
    assert len(req.answers) == 2
    assert req.answers[1].type == "reask"
    assert req.answers[1].reask_of == "q_1"
    assert req.answers[1].category is None
    assert req.answers[0].is_replay is False
    assert req.answers[0].is_spare_topic is False


def test_v0_1_요청도_그대로_파싱된다():
    """is_replay · is_spare_topic은 v0.2에서 추가됐다. 없으면 false로 본다.

    백엔드가 아직 안 보내도 요청은 통과해야 한다.
    다만 두 필드가 없으면 회차 비교의 by_question이 빈 배열이 된다.
    """
    req = ReportCreateRequest.model_validate({
        "persona": "friendly",
        "job_role": "백엔드 개발",
        "answers": [{
            "question_id": "q_1", "type": "question", "text": "t",
            "category": "지원동기", "difficulty": "L1", "question_number": 1,
            "audio_url": "https://s3.../ans_1.webm", "video_url": None,
            "is_timeout": False, "reask_of": None,
        }],
    })
    assert req.answers[0].is_replay is False
    assert req.answers[0].is_spare_topic is False


def test_계약서_4장_리포트_응답_예시가_파싱된다():
    raw = """
    {
      "session_id": "sess_9f2a1c",
      "generated_at": "2026-09-05T14:22:31Z",
      "report_status": "partial",
      "overall": {
        "score": 68, "display": 4, "gated": false, "gate_reason": null,
        "partial": true, "axes_used": ["content", "speech"], "axes_failed": ["gaze"]
      },
      "axes": {
        "content": {
          "status": "ok", "score": 72, "display": 4, "metrics": {},
          "evidence": [
            { "question_id": "q_3", "t_start": 12.4, "t_end": 19.8,
              "kind": "weakness", "label": "근거 부족",
              "comment": "선택 이유를 설명했으나 비교 대상이 제시되지 않았습니다." }
          ]
        },
        "speech": { "status": "ok", "score": 61, "display": 4, "metrics": {}, "evidence": [] },
        "gaze": { "status": "failed", "error_code": "GAZE_FAILED", "score": null,
                  "display": null, "metrics": null, "evidence": [] }
      },
      "questions": [
        { "question_id": "q_1", "question_number": 1, "category": "지원동기",
          "difficulty": "L1", "is_replay": false, "is_spare_topic": false,
          "score": 70, "display": 4,
          "axes": { "content": 74, "speech": 63, "gaze": null },
          "transcript": "저는 데이터가 쌓이고 흐르는 구조에...",
          "duration_sec": 46.2, "word_count": 138,
          "was_timeout": false, "had_reask": true }
      ],
      "resilience": { "score": 58, "display": 3,
                      "comment": "압박 질문 이후 답변 길이가 절반으로 줄었습니다." },
      "company_comment": "도전과 협업을 강조하는 인재상에 비추어...",
      "improved_answers": [
        { "question_id": "q_3", "original_excerpt": "낙관적 락을 썼습니다.",
          "suggestion": "선택 이유와 대안 비교를 함께 언급하면...",
          "t_start": 12.4, "t_end": 19.8 }
      ]
    }
    """
    r = ReportResult.model_validate(json.loads(raw))
    assert r.report_status == "partial"
    assert r.overall.axes_failed == ["gaze"]
    assert r.axes.gaze.status == "failed"
    assert r.axes.gaze.error_code == "GAZE_FAILED"
    assert r.axes.gaze.score is None
    assert r.axes.content.metrics == {}
    assert r.questions[0].axes.gaze is None
    assert r.questions[0].had_reask is True


def test_계약서_6장_시선_건너뜀_예시():
    from ai.report_schemas import AxisResult

    a = AxisResult.model_validate(
        json.loads('{"status":"skipped","reason":"no_video","score":null}')
    )
    assert a.status == "skipped"
    assert a.reason == "no_video"
    assert a.evidence == []


@pytest.mark.parametrize(
    "score,expected",
    [(0, 1), (19, 1), (20, 2), (39, 2), (40, 3), (59, 3), (60, 4), (79, 4), (80, 5), (100, 5)],
)
def test_계약서_0장_표시점수_변환(score, expected):
    assert display_of(score) == expected


# ---------------------------------------------------------------------------
# 리포트 생성
# ---------------------------------------------------------------------------


def test_리포트_생성과_폴링(client, auth):
    result, _ = make_report(client, auth, six_answers())

    assert result["session_id"] == "sess_9f2a1c"
    assert result["generated_at"].endswith("Z")
    assert result["report_status"] == "complete"
    assert result["overall"]["partial"] is False
    assert sorted(result["overall"]["axes_used"]) == ["content", "gaze", "speech"]
    assert result["overall"]["axes_failed"] == []
    assert len(result["questions"]) == 6

    for axis in AXES:
        a = result["axes"][axis]
        assert a["status"] == "ok"
        assert 0 <= a["score"] <= 100
        assert a["display"] == display_of(a["score"])
        assert a["metrics"] == {}          # 이번 주차에는 빈 객체
        assert isinstance(a["evidence"], list)   # 항상 존재

    for q in result["questions"]:
        assert q["display"] == display_of(q["score"])
        assert set(q["axes"]) == {"content", "speech", "gaze"}


def test_같은_요청은_항상_같은_점수를_준다(client, auth):
    """백엔드가 회귀 테스트를 짤 수 있어야 한다."""
    a, _ = make_report(client, auth, six_answers(), idem=1)
    b, _ = make_report(client, auth, six_answers(), idem=2)
    assert [q["score"] for q in a["questions"]] == [q["score"] for q in b["questions"]]
    assert a["overall"]["score"] == b["overall"]["score"]


def test_되묻기는_원_질문에_합산된다(client, auth):
    """되묻기 자체는 독립 문항으로 점수를 내지 않는다. (계약서 2장)"""
    answers = [
        answer(1),
        answer(1, type="reask", reask_of="q_1"),
        answer(2),
    ]
    result, _ = make_report(client, auth, answers)

    ids = [q["question_id"] for q in result["questions"]]
    assert ids == ["q_1", "q_2"]        # q_1r은 문항으로 세지 않는다

    q1, q2 = result["questions"]
    assert q1["had_reask"] is True
    assert q2["had_reask"] is False
    # 되묻기 답변의 발화 길이가 원 질문에 이어 붙는다
    assert q1["duration_sec"] == q2["duration_sec"] * 2
    assert q1["word_count"] == q2["word_count"] * 2


def test_is_timeout은_was_timeout으로_전달된다(client, auth):
    answers = [answer(1, is_timeout=True), answer(2)]
    result, _ = make_report(client, auth, answers)
    assert result["questions"][0]["was_timeout"] is True
    assert result["questions"][1]["was_timeout"] is False


def test_친절형은_resilience가_null(client, auth):
    """압박 구간이 없어 산출할 수 없다. (계약서 4장)"""
    friendly, _ = make_report(client, auth, six_answers(), persona="friendly", idem=1)
    pressure, _ = make_report(client, auth, six_answers(), persona="pressure", idem=2)
    assert friendly["resilience"] is None
    assert pressure["resilience"] is not None
    assert pressure["resilience"]["display"] == display_of(pressure["resilience"]["score"])


def test_인재상이_없으면_company_comment가_null(client, auth):
    """계약서: company_profile_override가 null이면 null.
    company_id는 추적용이라 있어도 코멘트를 만들지 않는다."""
    body = report_body(six_answers(), company_id="17")
    res = client.post("/ai/sessions/sess_cc/report", headers={**auth, **key(1)}, json=body)
    없음 = client.get(f"/ai/tasks/{res.json()['task_id']}", headers=auth).json()["result"]

    body["company_profile_override"] = "현대건설(주) (종합건설)\n\n핵심 가치\n  도전"
    res = client.post("/ai/sessions/sess_cc/report", headers={**auth, **key(2)}, json=body)
    있음 = client.get(f"/ai/tasks/{res.json()['task_id']}", headers=auth).json()["result"]

    assert 없음["company_comment"] is None
    assert 있음["company_comment"] is not None


def test_evidence에는_타임스탬프가_반드시_있다(client, auth):
    """근거 표시가 P1이더라도 값은 항상 채워 보낸다. (계약서 4장)"""
    result, _ = make_report(client, auth, six_answers())
    found = 0
    for axis in AXES:
        for e in result["axes"][axis]["evidence"]:
            assert isinstance(e["t_start"], (int, float))
            assert isinstance(e["t_end"], (int, float))
            assert e["t_end"] > e["t_start"]
            assert e["kind"] in ("strength", "weakness")
            assert e["question_id"] and e["label"] and e["comment"]
            found += 1
    assert found > 0
    for imp in result["improved_answers"]:
        assert imp["t_end"] > imp["t_start"]


# ---------------------------------------------------------------------------
# 부분 실패 — 계약서 6장
# ---------------------------------------------------------------------------


def test_카메라_미사용은_실패가_아니라_skipped(client, auth):
    result, _ = make_report(client, auth, six_answers(video="none"))

    assert result["axes"]["gaze"]["status"] == "skipped"
    assert result["axes"]["gaze"]["reason"] == "no_video"
    assert result["axes"]["gaze"]["score"] is None
    # 실패가 아니므로 부분 리포트가 아니다
    assert result["report_status"] == "complete"
    assert result["overall"]["partial"] is False
    assert result["overall"]["axes_failed"] == []
    assert sorted(result["overall"]["axes_used"]) == ["content", "speech"]
    # 문항별 시선 점수도 null
    assert all(q["axes"]["gaze"] is None for q in result["questions"])


def test_시선_분석_실패는_partial_리포트(client, auth):
    result, _ = make_report(client, auth, six_answers(video="fail"))

    assert result["axes"]["gaze"]["status"] == "failed"
    assert result["axes"]["gaze"]["error_code"] == "GAZE_FAILED"
    assert result["report_status"] == "partial"
    assert result["overall"]["partial"] is True
    assert result["overall"]["axes_failed"] == ["gaze"]


def test_말하기_분석_실패도_partial_리포트(client, auth):
    result, _ = make_report(client, auth, six_answers(audio="fail"))
    assert result["axes"]["speech"]["status"] == "failed"
    assert result["axes"]["speech"]["error_code"] == "SPEECH_FAILED"
    assert result["overall"]["axes_failed"] == ["speech"]


def test_축이_빠지면_남은_축으로_재정규화한다(client, auth):
    """정상 content 0.5 speech 0.3 gaze 0.2 → gaze 실패 시 0.625 / 0.375"""
    result, _ = make_report(client, auth, six_answers(video="fail"))

    content = result["axes"]["content"]["score"]
    speech = result["axes"]["speech"]["score"]
    expected = round(content * 0.625 + speech * 0.375)
    assert result["overall"]["score"] == expected

    # 문항별 점수도 같은 가중치로 계산된다
    q = result["questions"][0]
    assert q["score"] == round(q["axes"]["content"] * 0.625 + q["axes"]["speech"] * 0.375)


def test_정상일_때는_기본_가중치를_쓴다(client, auth):
    result, _ = make_report(client, auth, six_answers())
    s = result["axes"]
    expected = round(
        s["content"]["score"] * AXIS_WEIGHTS["content"]
        + s["speech"]["score"] * AXIS_WEIGHTS["speech"]
        + s["gaze"]["score"] * AXIS_WEIGHTS["gaze"]
    )
    assert result["overall"]["score"] == expected


# ---------------------------------------------------------------------------
# 멱등성 · 에러 — 계약서 2장 · 9장
# ---------------------------------------------------------------------------


def test_같은_Idempotency_Key는_같은_task_id를_준다(client, auth):
    body = report_body(six_answers())
    first = client.post("/ai/sessions/sess_a/report", headers={**auth, **key(1)}, json=body)
    second = client.post("/ai/sessions/sess_a/report", headers={**auth, **key(1)}, json=body)
    assert first.json()["task_id"] == second.json()["task_id"]

    third = client.post("/ai/sessions/sess_a/report", headers={**auth, **key(2)}, json=body)
    assert third.json()["task_id"] != first.json()["task_id"]


def test_Idempotency_Key가_없으면_400(client, auth):
    res = client.post("/ai/sessions/sess_a/report", headers=auth,
                      json=report_body(six_answers()))
    assert res.status_code == 400
    assert res.json()["error_code"] == "INVALID_REQUEST"


def test_답변이_2문항_미만이면_REPORT_TOO_SHORT(client, auth):
    res = client.post("/ai/sessions/sess_a/report", headers={**auth, **key(1)},
                      json=report_body([answer(1)]))
    assert res.status_code == 422
    assert res.json()["error_code"] == "REPORT_TOO_SHORT"


def test_되묻기만_있으면_문항으로_세지_않는다(client, auth):
    """되묻기는 독립 문항이 아니므로 2문항 기준을 채우지 못한다."""
    answers = [answer(1), answer(1, type="reask", reask_of="q_1")]
    res = client.post("/ai/sessions/sess_a/report", headers={**auth, **key(1)},
                      json=report_body(answers))
    assert res.status_code == 422
    assert res.json()["error_code"] == "REPORT_TOO_SHORT"


def test_answers가_비어_있으면_INVALID_ANSWERS(client, auth):
    res = client.post("/ai/sessions/sess_a/report", headers={**auth, **key(1)},
                      json=report_body([]))
    assert res.status_code == 400
    assert res.json()["error_code"] == "INVALID_ANSWERS"


def test_answers_형식_오류는_INVALID_ANSWERS(client, auth):
    """카테고리 문자열이 8종과 다르면 answers 오류로 잡힌다."""
    bad = answer(1)
    bad["category"] = "협업"  # 가운뎃점 누락
    res = client.post("/ai/sessions/sess_a/report", headers={**auth, **key(1)},
                      json=report_body([bad, answer(2)]))
    assert res.status_code == 400
    assert res.json()["error_code"] == "INVALID_ANSWERS"


def test_리포트도_시크릿_헤더가_필요하다(client):
    res = client.post("/ai/sessions/sess_a/report",
                      headers={"X-Cueanda-Secret": "wrong", **key(1)},
                      json=report_body(six_answers()))
    assert res.status_code == 401
    assert res.json()["error_code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 실패한 축만 재시도 — 계약서 7장
# ---------------------------------------------------------------------------


def retry_body(axes, answers, **kw):
    """재시도 요청은 리포트 생성 본문에 axes를 더한 모양이다. (계약서 7장)"""
    return {**report_body(answers, **kw), "axes": axes}


def test_재시도는_전체_리포트를_돌려준다(client, auth):
    """축만 돌려주면 총점 · 게이트 · 부분 실패 여부가 옛 값으로 남는다.
    그걸 백엔드가 다시 계산하게 두지 않는다."""
    res = client.post(
        "/ai/sessions/sess_9f2a1c/report/retry",
        headers={**auth, **key(2)},
        json=retry_body(["gaze"], six_answers()),
    )
    assert res.status_code == 202
    body = client.get(f"/ai/tasks/{res.json()['task_id']}", headers=auth).json()
    assert body["status"] == "done"

    result = ReportResult.model_validate(body["result"])   # 생성 응답과 같은 모양
    assert result.axes.gaze.status == "ok"
    assert result.axes.content.score is not None
    assert result.axes.speech.score is not None
    assert result.overall.axes_used == ["content", "speech", "gaze"]
    assert result.overall.partial is False


def test_재시도로_성공하면_부분_리포트가_풀린다(client, auth):
    answers = six_answers(video="fail")
    first, _ = make_report(client, auth, answers, session_id="sess_p", idem=1)
    assert first["report_status"] == "partial"

    res = client.post("/ai/sessions/sess_p/report/retry", headers={**auth, **key(2)},
                      json=retry_body(["gaze"], six_answers()))
    retried = client.get(f"/ai/tasks/{res.json()['task_id']}", headers=auth).json()["result"]

    assert retried["report_status"] == "complete"
    assert retried["overall"]["partial"] is False
    assert retried["overall"]["axes_failed"] == []


def test_재시도_점수는_최초_리포트와_같다(client, auth):
    """요청하지 않은 축은 다시 계산하지 않고 같은 값을 쓴다. (계약서 7장)"""
    answers = six_answers()
    first, _ = make_report(client, auth, answers, session_id="sess_x", idem=1)

    res = client.post("/ai/sessions/sess_x/report/retry", headers={**auth, **key(9)},
                      json=retry_body(["gaze"], answers))
    retried = client.get(f"/ai/tasks/{res.json()['task_id']}", headers=auth).json()["result"]
    for axis in ("content", "speech", "gaze"):
        assert retried["axes"][axis]["score"] == first["axes"][axis]["score"]
    assert retried["overall"]["score"] == first["overall"]["score"]


def test_재시도에_생성_본문이_없으면_400(client, auth):
    """persona · job_role이 있어야 회복력 · 기업 코멘트까지 다시 만든다."""
    res = client.post("/ai/sessions/sess_a/report/retry", headers={**auth, **key(1)},
                      json={"axes": ["gaze"], "answers": six_answers()})
    assert res.status_code == 400
    assert res.json()["error_code"] == "INVALID_REQUEST"


def test_재시도에_축을_안_주면_400(client, auth):
    res = client.post("/ai/sessions/sess_a/report/retry", headers={**auth, **key(1)},
                      json=retry_body([], six_answers()))
    assert res.status_code == 400
    assert res.json()["error_code"] == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# 회차 비교 — 계약서 8장
# ---------------------------------------------------------------------------


def rounds(client, auth, n, **kw):
    """n회차치 리포트를 만들어 compare 요청 형태로 담는다."""
    out = []
    for r in range(1, n + 1):
        sid = f"sess_r{r}"
        result, _ = make_report(client, auth, six_answers(**kw),
                                session_id=sid, idem=100 + r)
        out.append({"session_id": sid, "round": r, "report": result})
    return out


def test_회차_비교(client, auth):
    res = client.post("/ai/reports/compare", headers=auth,
                      json={"reports": rounds(client, auth, 4)})
    assert res.status_code == 200
    body = CompareResponse.model_validate(res.json())

    assert body.latest_round == 4
    assert body.compared_rounds == [1, 2, 3, 4]
    assert len(body.trend.overall) == 4
    assert len(body.trend.axes.content) == 4
    assert body.vs_previous is not None
    assert body.vs_previous.from_round == 3
    assert body.vs_previous.to_round == 4
    assert body.partial_rounds == []
    assert body.trend.best_round in (1, 2, 3, 4)
    assert body.trend.stalled_axes == [] or set(body.trend.stalled_axes) <= set(AXES)


def test_회차가_하나면_vs_previous가_null(client, auth):
    """회차가 하나뿐일 때도 오류가 아니다. (계약서 8장)"""
    res = client.post("/ai/reports/compare", headers=auth,
                      json={"reports": rounds(client, auth, 1)})
    body = CompareResponse.model_validate(res.json())
    assert body.vs_previous is None
    assert len(body.trend.overall) == 1
    assert body.compared_rounds == [1]


def test_비교는_is_replay가_true인_문항만(client, auth):
    """is_spare_topic은 필터에 쓰지 않는다. (계약서 8장)"""
    res = client.post("/ai/reports/compare", headers=auth,
                      json={"reports": rounds(client, auth, 2)})
    body = CompareResponse.model_validate(res.json())

    # six_answers는 홀수 번호만 is_replay=True로 만든다
    assert [q.question_id for q in body.by_question] == ["q_1", "q_3", "q_5"]
    for q in body.by_question:
        assert len(q.scores) == 2
        assert q.delta_from_previous == q.scores[-1] - q.scores[-2]
        assert q.delta_from_first == q.scores[-1] - q.scores[0]


def test_예비_토픽_주질문도_비교_대상이다(client, auth):
    """is_spare_topic: true이면서 is_replay: true면 비교 대상이다."""
    answers = [
        answer(1, is_replay=True, is_spare_topic=False),
        answer(2, is_replay=True, is_spare_topic=True),
        answer(3, is_replay=False, is_spare_topic=True),
    ]
    reports = []
    for r in (1, 2):
        sid = f"sess_s{r}"
        result, _ = make_report(client, auth, answers, session_id=sid, idem=200 + r)
        reports.append({"session_id": sid, "round": r, "report": result})

    body = CompareResponse.model_validate(
        client.post("/ai/reports/compare", headers=auth, json={"reports": reports}).json()
    )
    ids = [q.question_id for q in body.by_question]
    assert ids == ["q_1", "q_2"]   # 예비 토픽 q_2 포함, is_replay false인 q_3 제외


def test_부분_리포트_회차는_총점에서_제외된다(client, auth):
    """재정규화된 총점은 다른 회차와 스케일이 다르다. (계약서 8장)"""
    reports = []
    for r, kw in enumerate([{}, {"video": "fail"}, {}], start=1):
        sid = f"sess_p{r}"
        result, _ = make_report(client, auth, six_answers(**kw),
                                session_id=sid, idem=300 + r)
        reports.append({"session_id": sid, "round": r, "report": result})

    body = CompareResponse.model_validate(
        client.post("/ai/reports/compare", headers=auth, json={"reports": reports}).json()
    )
    assert body.partial_rounds == [2]
    assert body.trend.overall[1] is None          # 2회차는 끊어서 표시한다
    assert body.trend.overall[0] is not None
    assert body.trend.overall[2] is not None
    assert body.trend.axes.gaze[1] is None        # 실패한 축도 null


def _real_rounds(client, auth, round_numbers, partial=()):
    """실제와 같은 회차 묶음. 1회차는 재면접이 아니라 문항이 전부 is_replay false다.

    재면접(2회차~)은 홀수 번호 주질문만 1회차와 같은 질문(is_replay true)이다.
    """
    out = []
    for r in round_numbers:
        kw = {"video": "fail"} if r in partial else {}
        answers = [answer(n, is_replay=(r != 1 and n % 2 == 1), **kw) for n in range(1, 7)]
        sid = f"sess_real{r}"
        result, _ = make_report(client, auth, answers, session_id=sid, idem=700 + r)
        out.append({"session_id": sid, "round": r, "report": result})
    return out


def _compare(client, auth, reports):
    res = client.post("/ai/reports/compare", headers=auth, json={"reports": reports})
    assert res.status_code == 200, res.json()
    return CompareResponse.model_validate(res.json())


def test_1회차는_is_replay가_false여도_재면접_문항과_짝을_맞춘다(client, auth):
    """1회차 문항은 재면접이 아니라 is_replay가 전부 false다. 그래도 문항별 비교가 비면 안 된다."""
    body = _compare(client, auth, _real_rounds(client, auth, [1, 2]))

    assert [q.question_id for q in body.by_question] == ["q_1", "q_3", "q_5"]
    for q in body.by_question:
        assert len(q.scores) == 2
    vs = body.vs_previous
    assert (vs.from_round, vs.to_round) == (1, 2)
    assert sorted(vs.improved + vs.declined + vs.unchanged) == ["q_1", "q_3", "q_5"]


def test_1회차만_보내면_문항별_비교가_비어_있다(client, auth):
    """비교할 재면접이 없으니 1회차 문항(꼬리질문 포함)을 늘어놓지 않는다."""
    body = _compare(client, auth, _real_rounds(client, auth, [1]))
    assert body.vs_previous is None
    assert body.by_question == []


def test_회차_번호는_보낸_그대로_쓴다(client, auth):
    """2회차 리포트가 없으면 [1, 3]이 온다. 다시 매기지 않고 직전 비교는 3 vs 1이다."""
    body = _compare(client, auth, _real_rounds(client, auth, [1, 3]))
    assert body.compared_rounds == [1, 3]
    assert body.latest_round == 3
    assert (body.vs_previous.from_round, body.vs_previous.to_round) == (1, 3)
    assert [q.question_id for q in body.by_question] == ["q_1", "q_3", "q_5"]


def test_첫_비교_회차가_2회차면_코멘트도_2회차_기준이다(client, auth):
    body = _compare(client, auth, _real_rounds(client, auth, [2, 3, 4]))
    assert body.compared_rounds == [2, 3, 4]
    for q in body.by_question:
        assert q.delta_from_first == q.scores[-1] - q.scores[0]
        assert "2회차 대비" in q.comment and "1회차" not in q.comment


def test_부분_리포트가_끼면_overall_delta는_null이다(client, auth):
    """0(변화 없음)과 구분해야 한다. 축 변화는 양쪽 값이 있는 축만 숫자다."""
    body = _compare(client, auth, _real_rounds(client, auth, [1, 2], partial={2}))
    vs = body.vs_previous
    assert vs.overall_delta is None
    assert vs.axis_delta.gaze is None
    assert vs.axis_delta.content is not None


def test_reports가_비면_400(client, auth):
    res = client.post("/ai/reports/compare", headers=auth, json={"reports": []})
    assert res.status_code == 400
    assert res.json()["error_code"] == "INVALID_REQUEST"


def test_compare도_시크릿이_필요하다(client, auth):
    res = client.post("/ai/reports/compare",
                      headers={"X-Cueanda-Secret": "wrong"}, json={"reports": []})
    assert res.status_code == 401

# ---------------------------------------------------------------------------
# 적절성 게이트 — 계약서 5장
#
# 주제에서 벗어난 유창한 답변이 말하기·시선 점수만으로 높은 총점을 받는 것을 막는다.
# 계산은 apply_gate 한 함수에 모여 있어 단위로 확인하고, HTTP로도 발동시킨다.
# ---------------------------------------------------------------------------


def test_게이트_완전_이탈은_총점_상한이_40이다():
    """관련성 20 → 총점 상한 40, gated true, gate_reason이 붙는다.

    유창한 딴소리를 걸러내기 위한 것이다. 말하기·시선이 아무리 좋아도
    내용이 질문을 안 다뤘으면 여기서 막힌다.
    """
    score, gated, reason = apply_gate(68, 20)
    assert score == GATE_SEVERE_CAP == 40
    assert gated is True
    assert reason == "content_relevance_low"


def test_게이트_부분_이탈은_총점_상한이_70이다():
    """관련성 40 → 총점 상한 70. 질문의 핵심을 살짝 비껴간 답변이다."""
    score, gated, reason = apply_gate(85, 40)
    assert score == GATE_PARTIAL_CAP == 70
    assert gated is True
    assert reason == "content_relevance_low"


def test_게이트_관련성이_충분하면_총점이_그대로다():
    """관련성 50 → 원래 총점 그대로, gated false, gate_reason은 null."""
    score, gated, reason = apply_gate(68, 50)
    assert score == 68
    assert gated is False
    assert reason is None


@pytest.mark.parametrize(
    "content,expected_cap",
    [
        (0, GATE_SEVERE_CAP),
        (29, GATE_SEVERE_CAP),
        (30, GATE_PARTIAL_CAP),
        (49, GATE_PARTIAL_CAP),
        (50, None),
        (72, None),
        (100, None),
    ],
)
def test_게이트_경계는_30과_50이다(content, expected_cap):
    """사람이 매기는 5단계 라벨과 맞춘 경계다.

        1점 주제이탈   0~29    상한 40
        2점 미흡      30~49   상한 70
        3점 중간      50~69   게이트 없음

    잠정값이며 앵커 답변 세트로 튜닝한다.
    """
    score, gated, _ = apply_gate(95, content)
    if expected_cap is None:
        assert gated is False
        assert score == 95
    else:
        assert gated is True
        assert score == expected_cap


def test_게이트_경계값이_서로_어긋나지_않는다():
    """임계와 상한을 따로 고치다 순서가 뒤집히면 조용히 이상해진다."""
    assert GATE_SEVERE_THRESHOLD < GATE_PARTIAL_THRESHOLD
    assert GATE_SEVERE_CAP < GATE_PARTIAL_CAP


def test_게이트는_총점을_올리지_않는다():
    """상한이므로 이미 낮은 총점은 건드리지 않는다."""
    assert apply_gate(25, 10)[0] == 25


def test_content_축이_없으면_게이트를_적용하지_않는다():
    """점수가 없으면 판단할 수 없다. 다만 content 실패는 전체 실패라 여기까지 오지 않는다."""
    score, gated, reason = apply_gate(68, None)
    assert (score, gated, reason) == (68, False, None)


def test_주제_이탈_답변은_HTTP로도_게이트가_발동한다(client, auth):
    result, _ = make_report(client, auth, six_answers(audio="offtopic"))

    assert result["overall"]["gated"] is True
    assert result["overall"]["gate_reason"] == "content_relevance_low"
    assert result["overall"]["score"] <= GATE_SEVERE_CAP
    # 내용 점수만 임계 아래로 떨어지고 나머지 축은 정상이다
    assert result["axes"]["content"]["score"] < GATE_SEVERE_THRESHOLD
    assert result["axes"]["speech"]["score"] >= 50
    assert result["axes"]["gaze"]["score"] >= 50
    # 게이트가 걸려도 리포트 자체는 정상이다
    assert result["report_status"] == "complete"
    assert result["overall"]["partial"] is False


def test_부분_이탈_답변은_HTTP로도_70_상한이_걸린다(client, auth):
    """백엔드가 두 단계를 모두 볼 수 있어야 한다.

    완전 이탈 트리거만 있으면 70 상한 경로가 한 번도 실행되지 않는다.
    """
    result, _ = make_report(client, auth, six_answers(audio="partial"))

    assert result["overall"]["gated"] is True
    assert result["overall"]["gate_reason"] == "content_relevance_low"
    assert result["overall"]["score"] <= GATE_PARTIAL_CAP
    content = result["axes"]["content"]["score"]
    assert GATE_SEVERE_THRESHOLD <= content < GATE_PARTIAL_THRESHOLD
    assert result["report_status"] == "complete"


def test_평범한_답변은_게이트가_걸리지_않는다(client, auth):
    result, _ = make_report(client, auth, six_answers())
    assert result["overall"]["gated"] is False
    assert result["overall"]["gate_reason"] is None


# ---------------------------------------------------------------------------
# 축별 실패 처리가 다르다 — 계약서 6장
#
#   content 실패   전체 실패로 처리한다
#   speech 실패    남은 축으로 재정규화
#   gaze 실패      남은 축으로 재정규화
#
# content만 필수인 이유는 게이트 때문이다. 내용 관련성이 없으면 게이트가 작동하지
# 않아 총점을 신뢰할 수 없다. content 실패에서 부분 리포트가 나가면
# 게이트 없이 총점이 계산되는 심각한 버그가 된다.
# ---------------------------------------------------------------------------


def test_content_실패는_전체_실패다(client, auth):
    """리포트 본문이 나가지 않고 태스크가 error로 남는다."""
    res = client.post(
        "/ai/sessions/sess_cf/report",
        headers={**auth, **key(1)},
        json=report_body(six_answers(audio="content_fail")),
    )
    assert res.status_code == 202, res.json()

    body = client.get(f"/ai/tasks/{res.json()['task_id']}", headers=auth).json()
    assert body["status"] == "error"
    assert body["error_code"] == "CONTENT_FAILED"
    assert body["message"]


def test_content_실패에는_리포트_본문이_절대_없다(client, auth):
    """부분 리포트가 새어 나가면 게이트 없이 총점이 계산된다. 회귀 방지."""
    res = client.post(
        "/ai/sessions/sess_cf/report",
        headers={**auth, **key(1)},
        json=report_body(six_answers(audio="content_fail")),
    )
    body = client.get(f"/ai/tasks/{res.json()['task_id']}", headers=auth).json()

    assert "result" not in body
    assert "report_status" not in body
    assert "overall" not in body
    assert "axes" not in body


def test_세_축의_실패_처리가_다르다(client, auth):
    """content는 전체 실패, speech와 gaze는 부분 리포트."""
    # content — 전체 실패
    res = client.post("/ai/sessions/s/report", headers={**auth, **key(1)},
                      json=report_body(six_answers(audio="content_fail")))
    content_task = client.get(f"/ai/tasks/{res.json()['task_id']}", headers=auth).json()

    # speech — 부분 리포트
    speech, _ = make_report(client, auth, six_answers(audio="fail"), idem=2)

    # gaze — 부분 리포트
    gaze, _ = make_report(client, auth, six_answers(video="fail"), idem=3)

    assert content_task["status"] == "error"
    assert content_task["error_code"] == "CONTENT_FAILED"

    for label, report, failed_axis in (("speech", speech, "speech"), ("gaze", gaze, "gaze")):
        assert report["report_status"] == "partial", label
        assert report["overall"]["partial"] is True, label
        assert report["overall"]["axes_failed"] == [failed_axis], label
        # 남은 축으로 재정규화되어 총점이 계산된다
        assert report["overall"]["score"] > 0, label
        # content는 살아 있으므로 게이트 판단이 가능하다
        assert report["axes"]["content"]["status"] == "ok", label
        assert report["overall"]["gated"] is False, label


def test_speech_실패는_content로_게이트를_계속_판단한다(client, auth):
    """축이 하나 빠져도 게이트는 살아 있어야 한다."""
    result, _ = make_report(
        client, auth,
        [answer(n, audio="offtopic_fail") for n in range(1, 4)],
    )
    assert result["overall"]["axes_failed"] == ["speech"]
    assert result["overall"]["gated"] is True
    assert result["overall"]["score"] <= GATE_SEVERE_CAP


def test_시선만_남아도_재정규화된다(client, auth):
    """speech(0.3) 실패 → 남은 content 0.5와 gaze 0.2를 0.7로 나눠 배분한다."""
    result, _ = make_report(client, auth, six_answers(audio="fail"))
    c = result["axes"]["content"]["score"]
    g = result["axes"]["gaze"]["score"]
    total = AXIS_WEIGHTS["content"] + AXIS_WEIGHTS["gaze"]
    expected = round(
        c * AXIS_WEIGHTS["content"] / total + g * AXIS_WEIGHTS["gaze"] / total
    )
    assert result["overall"]["score"] == expected


def test_두_축이_모두_실패하면_content만_남는다(client, auth):
    result, _ = make_report(client, auth, six_answers(audio="fail", video="fail"))
    assert sorted(result["overall"]["axes_failed"]) == ["gaze", "speech"]
    assert result["overall"]["axes_used"] == ["content"]
    # 남은 축이 하나면 그 점수가 그대로 총점이 된다
    assert result["overall"]["score"] == result["axes"]["content"]["score"]
