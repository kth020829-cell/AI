"""이력서 로딩과 주질문 생성.

실제 API를 부르지 않는다. 호출 자체는 모킹하고, 우리가 만든 부분만 검증한다.
  - 이력서 파일 판별과 실패 처리
  - Claude에 넘기는 요청 모양 (PDF 블록, 캐시 지점, 슬롯 목록)
  - 응답 정리와 누락 감지
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx2
import pytest

from ai import llm, resume
from ai.llm import GeneratedQuestion, GeneratedQuestions, LlmError
from ai.resume import Resume, ResumeError

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
SLOTS = [("지원동기", "L1"), ("직무역량", "L2"), ("협업·갈등", "L2")]


# ---------------------------------------------------------------------------
# 이력서
# ---------------------------------------------------------------------------


def test_PDF는_그대로_넘긴다():
    """텍스트로 바꾸지 않는다. Claude가 PDF를 직접 읽는다."""
    r = resume.from_bytes(PDF)
    assert r.is_pdf
    assert r.pdf_bytes == PDF

    block = r.as_content_block()
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"
    # base64 문자열에 개행이 있으면 안 된다
    assert "\n" not in block["source"]["data"]


def test_이력서에_캐시_지점을_둔다():
    """세션 내내 바뀌지 않고, 재연습에서도 같은 이력서가 온다."""
    assert resume.from_bytes(PDF).as_content_block()["cache_control"] == {"type": "ephemeral"}
    assert resume.from_bytes("이력서 내용".encode()).as_content_block()["cache_control"] == {
        "type": "ephemeral"
    }


def test_텍스트_파일도_받는다():
    r = resume.from_bytes("백엔드 개발 지원자입니다.".encode("utf-8"))
    assert not r.is_pdf
    assert "백엔드" in r.text
    assert r.as_content_block()["type"] == "text"


def test_cp949_이력서도_읽는다():
    r = resume.from_bytes("한글 이력서".encode("cp949"))
    assert "한글" in r.text


@pytest.mark.parametrize(
    "raw,이유",
    [
        (b"", "빈 파일"),
        (b"PK\x03\x04rest", "DOCX가 아닌 ZIP"),
        (b"\xd0\xcf\x11\xe0rest", "구형 오피스 문서 · 한글"),
        (b"\xff\xfe\x00\x00\xff", "알 수 없는 인코딩"),
    ],
)
def test_읽을_수_없는_파일은_ResumeError(raw, 이유):
    with pytest.raises(ResumeError):
        resume.from_bytes(raw)


def make_docx(paragraphs) -> bytes:
    """최소한의 DOCX를 만든다. 표준 라이브러리만 쓴다."""
    import io as _io
    import zipfile

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        # 서식 때문에 한 문단이 여러 조각으로 쪼개지는 실제 구조를 흉내낸다
        "<w:p>" + "".join(f"<w:r><w:t>{part}</w:t></w:r>" for part in p) + "</w:p>"
        for p in paragraphs
    )
    xml = (
        '<?xml version="1.0"?>'
        f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    )

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_Word_이력서를_텍스트로_읽는다():
    """국내 자소서·이력서는 Word로 쓰는 경우가 많아 거부할 수 없다."""
    raw = make_docx([["안녕하십니까, ", "백엔드 개발자"], ["지원 동기는"]])
    r = resume.from_bytes(raw)

    assert not r.is_pdf
    # 한 문단이 여러 조각으로 쪼개져 있어도 이어 붙인다
    assert r.text.splitlines() == ["안녕하십니까, 백엔드 개발자", "지원 동기는"]
    assert r.as_content_block()["type"] == "text"


def test_빈_Word_파일은_ResumeError():
    with pytest.raises(ResumeError, match="읽어낼 내용이 없습니다"):
        resume.from_bytes(make_docx([]))


def test_DOCX가_아닌_ZIP은_거부한다():
    """word/document.xml이 없으면 이력서가 아니다."""
    import io as _io
    import zipfile

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "not a resume")

    with pytest.raises(ResumeError, match="한글"):
        resume.from_bytes(buf.getvalue())


def test_너무_큰_파일은_거부한다():
    with pytest.raises(ResumeError, match="너무 큽니다"):
        resume.from_bytes(b"%PDF-" + b"x" * resume.MAX_BYTES)


def test_다운로드_실패는_ResumeError(monkeypatch):
    """presigned URL 만료가 가장 흔하다."""
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            request = httpx2.Request("GET", url)
            response = httpx2.Response(403, request=request)
            raise httpx2.HTTPStatusError("expired", request=request, response=response)

    monkeypatch.setattr(httpx2, "Client", lambda **kw: FakeClient())
    with pytest.raises(ResumeError, match="만료"):
        resume.fetch("https://s3.../resume.pdf")


def test_빈_URL은_ResumeError():
    with pytest.raises(ResumeError):
        resume.fetch("")


# ---------------------------------------------------------------------------
# 주질문 생성 — 요청 모양
# ---------------------------------------------------------------------------


def fake_response(questions, *, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=None,
        parsed_output=GeneratedQuestions(questions=questions),
        usage=SimpleNamespace(
            input_tokens=5000, cache_read_input_tokens=0, output_tokens=400
        ),
    )


def answers_for(slots):
    return [
        GeneratedQuestion(category=c, difficulty=d, text=f"{c}에 대한 질문입니다.")
        for c, d in slots
    ]


@pytest.fixture
def call():
    """messages.parse를 가로채고 전달된 인자를 돌려준다."""
    client = MagicMock()
    with patch.object(llm, "_client", return_value=client):
        yield client.messages.parse


def test_슬롯대로_주질문을_만든다(call):
    call.return_value = fake_response(answers_for(SLOTS))

    out = llm.generate_main_questions(
        resume=Resume(pdf_bytes=PDF), job_role="백엔드 개발",
        persona="pressure", slots=SLOTS,
    )

    assert set(out) == {"지원동기", "직무역량", "협업·갈등"}
    assert all(v for v in out.values())


def test_요청_모양이_맞다(call):
    call.return_value = fake_response(answers_for(SLOTS))
    llm.generate_main_questions(
        resume=Resume(pdf_bytes=PDF), job_role="백엔드 개발",
        persona="pressure", slots=SLOTS,
    )

    kwargs = call.call_args.kwargs
    # 질문 생성은 잘 정의된 작업이라 Sonnet을 기본으로 둔다
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["output_format"] is GeneratedQuestions
    # thinking 토큰이 비용의 대부분이라 기본을 medium으로 둔다
    assert kwargs["output_config"] == {"effort": "medium"}

    # 시스템 프롬프트에 캐시 지점을 둔다
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}

    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "document"          # 이력서가 먼저
    instruction = content[1]["text"]
    assert "백엔드 개발" in instruction
    # 슬롯이 순서대로 들어간다
    for i, (category, difficulty) in enumerate(SLOTS, start=1):
        assert f"{i}. 카테고리 {category} / 난이도 {difficulty}" in instruction


def test_페르소나가_지시문에_들어간다(call):
    call.return_value = fake_response(answers_for(SLOTS))
    for persona, 표현 in (("friendly", "친절형"), ("pressure", "압박형")):
        llm.generate_main_questions(
            resume=Resume(text="이력서"), job_role="백엔드 개발",
            persona=persona, slots=SLOTS,
        )
        assert 표현 in call.call_args.kwargs["messages"][0]["content"][1]["text"]


def test_인재상은_있을_때만_넣는다(call):
    call.return_value = fake_response(answers_for(SLOTS))

    llm.generate_main_questions(
        resume=Resume(text="이력서"), job_role="백엔드 개발", persona="friendly",
        slots=SLOTS, company_profile="도전과 협업을 중시합니다",
    )
    assert "도전과 협업" in call.call_args.kwargs["messages"][0]["content"][1]["text"]

    llm.generate_main_questions(
        resume=Resume(text="이력서"), job_role="백엔드 개발",
        persona="friendly", slots=SLOTS,
    )
    assert "인재상" not in call.call_args.kwargs["messages"][0]["content"][1]["text"]


def test_카테고리_설명이_프롬프트에_들어간다(call):
    """이름만 나열하면 협업·갈등과 가치관·인성이 섞인다."""
    from ai.schemas import CATEGORIES

    call.return_value = fake_response(answers_for(SLOTS))
    llm.generate_main_questions(
        resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=SLOTS
    )
    system = call.call_args.kwargs["system"][0]["text"]

    for category in CATEGORIES:
        assert category in system, category
    # 둘을 갈라놓는 문장이 살아 있어야 한다
    assert "사람 사이의 조율이 아니라 판단 기준" in system


def test_되묻기_프롬프트에_규칙이_남아_있다():
    """지우면 「조금 더 자세히」 한 문장으로 돌아간다.

    그러면 지원자가 두 번째에도 같은 대답을 한다.
    """
    rules = [
        # 무엇이 빠졌는지 짚어 주지 않으면 되묻는 의미가 없다
        "원래 질문에서 아직 답이 나오지 않은 부분을 골라",
        # 지적하면 두 번째 답변은 더 짧아진다
        "답변이 짧았다고 지적하지 않습니다",
        # 새 주제를 열면 되묻기가 아니라 주질문이다
        "새 주제를 열지 않습니다",
        "묻는 대상이 여럿이면",
        "60자 안팎",
    ]
    for rule in rules:
        assert rule in llm.REASK_SYSTEM_PROMPT, rule


def test_꼬리질문_프롬프트에_규칙이_남아_있다():
    """실수로 지워지면 환각과 장문 질문이 조용히 돌아온다."""
    rules = [
        "답변에서 이미 말한 것을 다시 묻지 않습니다",
        # 대화에 없는 이력서 내용을 지어내는 것을 막는다
        "위 대화에 실제로 나온 말만 근거로 삼습니다",
        "확인할 수 없는 것을 전제로 깔지 않습니다",
        # 양자택일 · 예/아니오 질문을 막는다
        "고르게 하지 않습니다",
        "예 · 아니오로 답하고 끝날 수 있는 질문도 피합니다",
        # 음성으로 읽어주므로 길면 안 된다
        "70자 안팎",
    ]
    for rule in rules:
        assert rule in llm.FOLLOWUP_SYSTEM_PROMPT, rule


def test_L1은_바로_답이_나오는_질문_금지에서_예외다():
    """이 예외가 없으면 규칙끼리 충돌한다.

    난이도 정의는 L1을 「적힌 것을 그대로 확인」이라 하는데, 규칙은
    「읽으면 바로 답이 나오는 것은 묻지 말라」고 한다. 모델은 이 충돌을
    L1을 L2 쪽으로 밀어서 해결한다.
    """
    assert "L1은 여기서 예외입니다" in llm.SYSTEM_PROMPT
    assert "그건 L2가 됩니다" in llm.SYSTEM_PROMPT


def test_L1이_L2로_번지지_않게_막는다():
    """블라인드 채점에서 틀린 것이 전부 L1과 L2 사이였다.

    L1 다섯 개 중 넷에 「구체적으로」나 「정확히」가 붙어 있었고,
    그 부사가 붙으면 사실 확인이 아니라 근거 요구처럼 읽힌다.
    """
    rules = [
        "이력서에 적힌 것을 그대로 확인한다",
        "붙이는 순간 L2가 된다",
        # 너무 문자 그대로 만들면 「몇 명이었나요」 같은 한 마디 질문이 나와
        # 9문항 중 하나를 버리게 된다
        "한 단어로 답이 끝나는 질문은 만들지 않는다",
        # 이력서에 한 줄뿐인 항목(향후 계획 등)을 L1으로 물으면 낭독이 된다
        "이력서에 한 줄로만 적힌 항목은 L1으로 묻지 않는다",
    ]
    for rule in rules:
        assert rule in llm.DIFFICULTY_GUIDE, rule


def test_주질문_프롬프트에_지원동기_정의가_있다():
    """없으면 이력서 속 과거 활동의 지원 동기를 묻는다.

    실제로 「코드프레소 프로젝트1에 지원하게 된 계기가 무엇인가요」가 나왔다.
    우리가 물어야 하는 것은 지금 지원하는 직무를 택한 이유다.
    """
    for rule in ("지금 지원하는 이 직무를 왜 택했는가", "과거 프로젝트나 동아리"):
        assert rule in llm.SYSTEM_PROMPT, rule


def test_주질문_프롬프트가_예_아니오_질문을_막는다():
    """「~해본 적이 있으신가요」로 시작하면 한 마디로 끝난다."""
    for rule in ("예 · 아니오로 답하고 끝날 수 있는 질문은 피합니다", "해본 적이 있으신가요"):
        assert rule in llm.SYSTEM_PROMPT, rule


def test_주질문_프롬프트에_길이_규칙이_있다():
    """없으면 100자가 넘는 질문이 나온다. 음성으로 읽어주면 못 알아듣는다."""
    for rule in ("90자 안팎", "음성으로 읽어주는 질문이라", "뒤엣것만 남깁니다"):
        assert rule in llm.SYSTEM_PROMPT, rule


def test_프롬프트에_규칙이_남아_있다():
    """실수로 지워지면 질문 품질이 조용히 나빠진다. 여기서 잡는다.

    실제로 규칙이 지켜지는지는 API를 불러야 알 수 있다.
    scripts/compare_models.py로 눈으로 확인한다.
    """
    rules = [
        "이력서에 없는 사실을 지어내지 않습니다",
        "한 질문에 한 가지만 묻습니다",
        "질문끼리 겹치지 않게 합니다",
        "이력서의 서로 다른 부분에서 하나씩 뽑습니다",
        "이력서를 읽으면 바로 답이 나오는 것을 묻지 않습니다",
        # 이력서 파일에 자격 체크리스트·동의서가 붙어 오는 경우가 실제로 있다
        "제출용 체크리스트",
        "개인정보",
        "서명란",
    ]
    for rule in rules:
        assert rule in llm.SYSTEM_PROMPT, rule


def test_슬롯이_없으면_부르지_않는다(call):
    assert llm.generate_main_questions(
        resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=[]
    ) == {}
    call.assert_not_called()


# ---------------------------------------------------------------------------
# 주질문 생성 — 실패 처리
# ---------------------------------------------------------------------------


def test_카테고리가_빠지면_LlmError(call):
    """세션 도중에 KeyError가 나는 것보다 여기서 실패하는 편이 낫다."""
    call.return_value = fake_response(answers_for(SLOTS[:2]))
    with pytest.raises(LlmError, match="협업·갈등"):
        llm.generate_main_questions(
            resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=SLOTS
        )


def test_빈_문장은_누락으로_본다(call):
    answers = answers_for(SLOTS)
    answers[0].text = "   "
    call.return_value = fake_response(answers)
    with pytest.raises(LlmError, match="지원동기"):
        llm.generate_main_questions(
            resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=SLOTS
        )


def test_거부되면_LlmError(call):
    call.return_value = fake_response(answers_for(SLOTS), stop_reason="refusal")
    with pytest.raises(LlmError, match="거부"):
        llm.generate_main_questions(
            resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=SLOTS
        )


def test_API_오류는_LlmError(call):
    call.side_effect = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    with pytest.raises(LlmError, match="연결"):
        llm.generate_main_questions(
            resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=SLOTS
        )


def test_알_수_없는_카테고리는_부르기_전에_막는다(call):
    with pytest.raises(LlmError, match="협업"):
        llm.generate_main_questions(
            resume=Resume(text="이력서"), job_role="x", persona="friendly",
            slots=[("협업", "L1")],   # 가운뎃점 누락
        )
    call.assert_not_called()


# ---------------------------------------------------------------------------
# 모드
# ---------------------------------------------------------------------------


def test_모델은_환경변수로_바꿔_낄_수_있다(monkeypatch, call):
    """같은 이력서로 모델을 바꿔 돌려 품질을 비교할 수 있어야 한다."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert llm.model() == "claude-sonnet-5"

    monkeypatch.setenv("LLM_MODEL", "claude-opus-5")
    call.return_value = fake_response(answers_for(SLOTS))
    llm.generate_main_questions(
        resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=SLOTS
    )
    assert call.call_args.kwargs["model"] == "claude-opus-5"


def test_모든_모델에_요금표가_있다():
    """로그에 비용을 남기려면 요금을 알아야 한다."""
    assert llm.DEFAULT_MODEL in llm.PRICING
    for name, (i, o) in llm.PRICING.items():
        assert 0 < i < o


def test_effort는_환경변수로_덮어쓸_수_있다(monkeypatch, call):
    """품질이 아쉬우면 올리고, 배선만 볼 때는 내린다."""
    monkeypatch.delenv("LLM_EFFORT", raising=False)
    assert llm.effort() == "medium"

    monkeypatch.setenv("LLM_EFFORT", "high")
    call.return_value = fake_response(answers_for(SLOTS))
    llm.generate_main_questions(
        resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=SLOTS
    )
    assert call.call_args.kwargs["output_config"] == {"effort": "high"}


def test_사용량을_로그로_남긴다(call, caplog):
    """비용이 예상과 맞는지 확인할 수 있어야 한다."""
    import logging

    call.return_value = fake_response(answers_for(SLOTS))
    with caplog.at_level(logging.INFO, logger="cue.ai.llm"):
        llm.generate_main_questions(
            resume=Resume(text="이력서"), job_role="x", persona="friendly", slots=SLOTS
        )
    line = caplog.text
    assert "effort=medium" in line
    assert "claude-sonnet-5" in line
    assert "$" in line          # 대략적인 비용도 함께 남긴다


@pytest.mark.parametrize(
    "mode,expected", [("dummy", False), ("llm", True), ("full", True), (None, False)]
)
def test_AI_MODE로_실제_생성_여부를_정한다(monkeypatch, mode, expected):
    if mode is None:
        monkeypatch.delenv("AI_MODE", raising=False)
    else:
        monkeypatch.setenv("AI_MODE", mode)
    assert llm.llm_enabled() is expected
