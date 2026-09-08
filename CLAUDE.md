# Cue AI 서버

## 이 프로젝트가 무엇인가

AI 모의면접 서비스 "Cue"의 AI 파트 서버입니다.
사용자가 자소서를 올리고 면접을 보면, AI가 질문을 만들어 던지고
끝난 뒤 답변을 채점해 리포트를 만듭니다.

팀은 AI · 백엔드(Spring) · 프론트 세 파트로 나뉩니다.
이 저장소는 AI 파트만 담당합니다.

## 지금 만드는 것 — 더미 서버

**실제 모델을 붙이기 전에 API 껍데기를 먼저 배포합니다.**

백엔드와 프론트가 우리 서버 없이는 자기 코드를 검증할 수 없기 때문입니다.
폴링, WebSocket push, 로그 저장, 재연습 흐름을 확인하려면
응답이 오는 서버가 있어야 합니다.

그래서 LLM·STT·TTS 없이 고정 응답을 반환하되,
**세션 구성 로직은 실제로 돌립니다.**

```
가짜   질문 텍스트, 음성 파일 URL
진짜   문항 수, 난이도 배분, 주제 구조, 되묻기, 예비 주제, 재연습
```

이렇게 하면 백엔드가 받는 데이터가 실제 서비스와 구조적으로 동일합니다.

## 저장소 구조

```
docs/                 계약서와 설계 문서. 이것이 스펙입니다
  질문생성_API계약_백엔드전달용.md      구현 완료
  리포트생성_API계약_백엔드전달용.md    구현 완료 (v0.2)
  질문 유형.md
  계약서_변경사항.md                    백엔드 전달용. 1주차에 바뀐 것
  백엔드_요청사항.md                    백엔드 전달용. P0 / P1 / 배포
  기업목록_프론트전달용.md              프론트 전달용. 기업 선택 UI
  시선모델_선정.md                      gaze 축 모델 비교와 결정 (L2CS-Net)
ai/
  session_plan.py     세션 구성 로직. 검증된 파일 — 아래 예외 외에는 수정 금지
  schemas.py          계약서의 요청 · 응답 Pydantic 모델
  dummy.py            고정 질문 텍스트, 세션 진행, 메모리 보관소
  llm.py              Claude로 주질문 · 꼬리질문 생성. 모델 · effort · 프롬프트
  resume.py           이력서 다운로드 (PDF · Word · 텍스트)
  tasks.py            진짜 백그라운드 실행 (스레드풀)
  pipeline.py         세션 시작 흐름 — 더미/llm 분기, 인재상 반영
  router.py           /ai/* 엔드포인트, 시크릿 헤더 검증
  errors.py           에러 응답 형식
  companies.py        회사 목록 (verified 필터)
  data/companies.json
  report_router.py    리포트 엔드포인트
  report_schemas.py   리포트 생성 계약의 요청 · 응답 모델
  report_dummy.py     점수 생성, 축 재정규화, 회차 비교
main.py               FastAPI 진입점, 예외 핸들러, /health · /ready
tests/
  test_schemas.py     계약서 JSON 예시 파싱
  test_session_plan.py  is_timeout 인자 회귀
  test_scenarios.py   문서 13장 시나리오 A·B·C·D + 재연습 + 인증
  test_report.py      리포트 생성 · 부분 실패 · 멱등성 · 회차 비교
  test_ops.py         운영 제약 — 워커 수 · 시크릿 가드 · 보관소 상한
  test_polling.py     processing 흉내 — 단계 진행 · progress
  test_llm.py         이력서 로딩 · 주질문 · 꼬리질문 생성 요청 · 실패 처리
  test_tasks.py       백그라운드 실행
  test_pipeline.py    세션 시작 흐름
  test_companies.py   인재상 데이터 — 파싱 잔재 · 스키마
scripts/
  compare_models.py   같은 이력서로 모델을 바꿔 돌려 품질 비교
Dockerfile            base / dummy / full 멀티스테이지
README.md             백엔드 담당자용 curl 가이드
```

**질문 생성 · 리포트 생성 두 계약 모두 완성되어 동작합니다.**
`AI_MODE=llm`이면 주질문이 이력서를 읽고 실제로 생성됩니다.
꼬리질문 · 되묻기 · verdict 판정은 답변 텍스트가 필요하므로 STT가 붙어야 가능합니다.

### 꼬리질문 — 만들어 뒀지만 아직 연결되지 않았습니다

`llm.generate_followup()`은 완성되어 있고 프롬프트도 실제 이력서 기반 대화로
두 번 튜닝했습니다. 다만 흐름에 붙이지 않았습니다.

계약서의 답변 제출 요청에는 `audio_url`만 있고 답변 텍스트가 없습니다.
꼬리질문은 직전 답변을 읽고 만드는 것이라 텍스트 없이는 부를 수 없습니다.
없는 입력을 흉내내서 붙이면 그 순간부터 가짜 질문이 나갑니다.

**STT를 붙이는 사람이 할 일은 두 가지입니다.**

```
1. ai/dummy.py의 answer_length()를 STT 결과 기반으로 교체
2. 주제별로 (질문 텍스트, 답변 텍스트) 쌍을 쌓아
   llm.generate_followup(history=..., difficulty=..., persona=..., job_role=...)에 넘김
   history의 마지막 항목이 파고들 대상입니다
```

`generate_followup`에는 이력서를 넣지 않습니다. 넣으면 답변에 없는 내용을
끌어와 물어서 "내 답변을 안 들었다"는 인상을 줍니다. 이유는 `ai/llm.py`
꼬리질문 섹션 주석에 적어 뒀습니다.

## 절대 하지 말 것

**1. `ai/session_plan.py`를 수정하지 마세요.**

3840개 조합으로 전수 검증이 끝난 파일입니다.
함수 이름, 인자, 반환값을 바꾸면 검증이 무효가 됩니다.
개선하고 싶은 부분이 보여도 그대로 두고 사용만 하세요.

> **합의된 예외 1건.** `SessionRunner.next`와 `RetryRunner.next`에
> `is_timeout=False` 인자를 추가했습니다. True면 되묻기 경로를 타지 않아
> `reask_used`를 소모하지 않습니다. 기본값이 False라 기존 동작은 그대로이며,
> 수정 전후를 2880개 조합으로 나란히 돌려 로그가 완전히 동일함을 확인했습니다.
> 회귀 테스트는 `tests/test_session_plan.py`에 있습니다.
> 추가 수정이 필요하면 똑같이 먼저 물어보세요.

**2. 계약서에 없는 필드를 추가하지 마세요.**

백엔드는 계약서만 보고 파싱 코드를 만듭니다.
"있으면 편할 것 같은" 필드를 추가하면 통합할 때 어긋납니다.
필드가 필요하다고 판단되면 추가하지 말고 먼저 알려주세요.

**3. 계약서와 다르게 만들지 마세요.**

필드명, 타입, Optional 여부, 에러 코드까지 계약서가 기준입니다.
더 나은 설계가 떠올라도 계약서를 따르세요.
이미 백엔드·프론트와 합의된 내용입니다.

**4. Celery, Redis를 지금 붙이지 마세요.**

인프라는 다른 담당자가 맡습니다.
세션 상태는 일단 메모리 딕셔너리에 보관하세요.

## session_plan.py 사용법

```python
from ai.session_plan import build_plan, SessionRunner, RetryRunner, topics_from_log

# 일반 세션
plan = build_plan(question_count=9, persona="pressure")
runner = SessionRunner(plan)
item = runner.start()                              # 첫 주질문

while True:
    item = runner.next(duration_sec, word_count, verdict=None)
    if item["type"] == "session_end":
        break
    # item["type"]        question | followup | reask
    # item["difficulty"]  L1 | L2 | L3 (되묻기는 None)
    # item["category"]    주질문만 값이 있음

# 재연습
topics = topics_from_log(이전회차_로그, 주질문_텍스트_목록)
runner = RetryRunner(topics)
```

`verdict`는 LLM 판정 결과입니다. 더미 단계에서는 항상 `None`을 넘기세요.
`None`이면 길이 게이트만 작동하며, 이것으로도 세션은 정상 진행됩니다.

## 핵심 개념

**주질문과 꼬리질문**

면접은 주제 단위로 진행됩니다.
한 주제는 주질문 1개 + 꼬리질문 0~2개입니다.

```
주질문    자소서만 보고 만듭니다. 미리 생성 가능
꼬리질문  직전 답변을 읽고 만듭니다. 실시간 생성만 가능
```

**되묻기**

답변이 부실하면 같은 질문을 다시 묻습니다.
`type`이 `reask`이며 **문항 수에 포함되지 않습니다.**
`question_number`가 올라가지 않는 것이 중요합니다.

**문항 수 보장**

사용자가 9문항을 고르면 어떤 경우에도 9문항이 나갑니다.
답변이 부실해 꼬리질문이 생략되면 예비 주제를 추가해 채웁니다.
따라서 문항 수는 고정이지만 주제 수는 세션마다 다릅니다.

**재연습**

최초 회차와 같은 주질문이 나옵니다.
AI 서버는 이전 회차를 저장하지 않으므로
백엔드가 `replay_log`를 요청에 담아 보냅니다.

## 기술 스택

```
Python 3.11
FastAPI + uvicorn
포트 8000
Pydantic v2
```

## 인증

모든 요청에 헤더가 필요합니다.

```
X-Cueanda-Secret: <환경변수 CUEANDA_SHARED_SECRET 값>
```

없거나 다르면 401을 반환합니다.
`/health`와 `/ready`는 예외입니다.

## 더미를 만들며 정한 것 — 계약서에 없던 부분

계약서가 정하지 않은 것들입니다. 백엔드와 합의했고 계약서 8장에 반영 예정입니다.
**임의로 바꾸지 마세요.** 백엔드 파싱 코드가 이 형태에 맞춰져 있습니다.

```
HTTP 에러 본문     {error_code, message}. 폴링 실패 응답과 같되 status는 뺌
INVALID_REQUEST   계약서에 없던 코드. 일반 검증 오류용
검증 오류 코드     FastAPI 기본 422가 아니라 전부 400
replay_log 누락    retry_of_session_id만 있으면 400 INVALID_REQUEST
없는 task_id      404 SESSION_NOT_FOUND
없는 경로          404 INVALID_REQUEST
topic_total       max(계획된 주제 수, 지금까지 열린 주제 수)

리포트 answers[]  is_replay · is_spare_topic 추가. AI가 세션을 보관하지 않아
                  요청으로 받지 않으면 알 수 없다. 기본값 false
부분 실패 트리거   video_url · audio_url에 fail이 들어 있으면 해당 축이 failed
게이트 트리거      audio_url에 offtopic이 있으면 내용 점수 10~29 → gated: true
전체 실패 트리거   audio_url에 content_fail이 있으면 태스크가 status: error
더미 점수         session_id + question_id 해시로 50~90. 같은 요청은 같은 점수
DUMMY_POLL_TICKS  0이면 즉시 done. 올리면 그 횟수만큼 processing을 거친다
                  백엔드 폴링 루프 검증용. dummy 모드 전용

AI_MODE=llm 일 때
  모델            claude-sonnet-5 (LLM_MODEL로 교체). 질문 생성에는 이걸로 충분하다
  effort          medium (LLM_EFFORT로 교체). thinking 토큰이 비용을 좌우한다
  호출 횟수        세션당 1회. 계획 토픽과 예비 토픽을 한 번에 만들어 둔다
  재연습          호출하지 않는다. 1회차 주질문을 그대로 재생한다
  프롬프트         ai/llm.py의 SYSTEM_PROMPT. 실제 이력서로 3회 튜닝했다
                  규칙을 지우면 품질이 조용히 나빠진다. test_llm.py가 잠가둔다

축마다 실패 처리가 다르다. 이것을 어기면 심각한 버그가 된다.
  content 실패   전체 실패. 리포트를 만들지 않고 CONTENT_FAILED를 남긴다
  speech 실패    부분 리포트. 남은 축으로 재정규화
  gaze 실패      부분 리포트. 남은 축으로 재정규화
content 실패에서 부분 리포트가 나가면 게이트가 돌지 않아 총점을 신뢰할 수 없다.
```

**부실한 답변 판정은 `audio_url` 문자열로 합니다.**

더미에는 STT가 없고 계약서의 답변 제출 요청에도 발화 길이가 없습니다.
계약서에 필드를 추가하지 않으려고 `audio_url`에 `short` 또는 `insufficient`가
들어 있으면 부실(5초·10어절), 아니면 충분(45초·60어절)으로 봅니다.
`ai/dummy.py`의 `answer_length()` 하나에 격리되어 있으니 실제 STT를 붙일 때
이 함수만 교체하세요.

## 작업 방식

- 한 번에 한 파일씩 만들고, 만들 때마다 실행해서 확인하세요.
- 파일을 만들 때마다 확인 코드도 함께 만드세요.
- 계약서를 추측하지 말고 실제로 읽으세요.
- 막히거나 계약서가 모호하면 임의로 정하지 말고 물어보세요.
