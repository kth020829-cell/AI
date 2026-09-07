# 질문 생성 — AI ↔ 백엔드 계약

용어 — 문서에서는 **친절형 / 압박형**으로 쓰고,
API 값은 `friendly` / `pressure`를 그대로 사용한다.

---

## 0. 기본 원칙

**세션 상태는 AI 서버가 들고 있다.**

난이도 배분, 토픽 진행, 꼬리질문 횟수, 중복 방지는 전부 AI 내부 로직이다.
백엔드는 `session_id` 하나만 들고 다니면 되고, 난이도나 카테고리 규칙을
알 필요가 없다.

단 **세션 상태는 임시 보관이다.** 세션이 끝나면 정리되고,
AI 서버를 재배포하면 진행 중이던 세션은 사라진다.
사용자에게 보이는 상태는 백엔드 DB를 기준으로 한다.

**폴링은 백엔드(Spring)가 한다.**

프론트는 AI 서버 주소를 모른다.
백엔드가 폴링해서 받은 결과를 WebSocket 등으로 프론트에 밀어준다.

```
프론트  ──WebSocket──▶  Spring  ──폴링(1초)──▶  AI 서버
        ◀──push──────           ◀────────────
```

`stage` 값은 AI 내부 파이프라인 이름이므로 프론트에 그대로 노출하지 않고
백엔드에서 자체 enum으로 매핑한다.

**모든 생성 작업은 비동기다.**

질문 생성에는 STT(Whisper) + LLM + TTS가 순차로 들어가 5~15초가 걸린다.
동기 응답으로 만들면 타임아웃이 난다.
요청하면 `task_id`를 즉시 반환하고, 백엔드가 폴링한다.

---

## 0-1. 인증

모든 요청에 공유 시크릿 헤더를 넣는다.

```
X-Cueanda-Secret: <SHARED_SECRET>
```

AI 서버는 내부망에 두고 외부에 노출하지 않는다.
헤더가 없거나 값이 다르면 401(`UNAUTHORIZED`)을 반환한다.

---

## 1. 엔드포인트 목록

```
POST  /ai/sessions                         세션 시작
POST  /ai/sessions/{session_id}/answers    답변 제출
GET   /ai/tasks/{task_id}                  작업 상태 조회 (폴링)
GET   /ai/companies                        회사 목록
POST  /ai/sessions/{session_id}/abort      세션 중단

GET   /health                              헬스체크
GET   /ready                               준비 확인
```

`/health`와 `/ready`는 배포·오케스트레이션용이며 시크릿 헤더가 필요 없다.
컨테이너 헬스체크에 `/health`를 쓰고 있으므로
`depends_on`의 `condition: service_healthy`로 기동 순서를 잡을 수 있다.

---

## 2. 세션 시작

### 요청

```
POST /ai/sessions

{
  "resume_file_url": "https://s3.../resume_abc.pdf",
  "job_role": "백엔드 개발",
  "persona": "pressure",
  "company_id": "hyundai_enc",
  "company_profile_override": null,
  "question_count": 9,
  "retry_of_session_id": null,
  "doc_id": null
}
```

```
resume_file_url           필수. 백엔드가 발급한 presigned URL
job_role                  필수. 자유 문자열
persona                   필수. "friendly" | "pressure"
company_id                선택. null 가능 (회사 미선택 연습 모드)
company_profile_override  선택. 미등록 기업 인재상 직접 입력값
question_count            선택. 3 | 6 | 9. 기본값 6
retry_of_session_id       선택. 재연습이면 최초 세션 ID
replay_log                재연습일 때만 필수. 최초 세션의 진행 로그
doc_id                    선택. 이력서 파싱 결과 재사용 키
```

토픽 수는 보내지 않는다. `question_count`에서 자동으로 결정된다.

```
3문항 → 2토픽    6문항 → 3토픽    9문항 → 4토픽
```

**`resume_file_url`의 presigned URL 만료는 10분 이상으로 설정한다.**
이력서 파싱이 세션 시작 태스크 안에서 이루어지는데,
큐가 밀리면 발급 시점과 사용 시점 사이에 간격이 생긴다.

### 예약 필드 — 지금은 항상 null

```
company_profile_override   미등록 기업 인재상을 사용자가 직접 입력한 경우
                           값이 있으면 company_id보다 우선한다

doc_id                     이력서를 한 번 파싱한 결과의 키
                           같은 이력서로 다시 면접할 때 파싱을 건너뛴다
                           null이면 resume_file_url로 새로 파싱한다
```

**필드만 미리 잡아둔다.** 나중에 값을 채워도 스키마가 깨지지 않게 하기 위해서다.

### 재연습 요청 — replay_log

AI 서버는 1회차 로그를 보관하지 않는다.
**재연습의 진실 소스는 백엔드 DB다.** 백엔드가 로그를 함께 보낸다.

```
{
  "question_count": 6,
  "retry_of_session_id": "sess_abc",
  "replay_log": [
    { "type": "question", "text": "백엔드 개발 직무에 지원하신 이유를 말씀해 주세요.",
      "category": "지원동기", "difficulty": "L1", "is_spare_topic": false },
    { "type": "question", "text": "가장 자신 있는 기술 스택은 무엇인가요?",
      "category": "직무역량", "difficulty": "L1", "is_spare_topic": false },
    { "type": "followup", "difficulty": "L2" },
    { "type": "question", "text": "팀원과 의견이 갈렸던 경험을 말씀해 주세요.",
      "category": "협업·갈등", "difficulty": "L2", "is_spare_topic": false },
    { "type": "followup", "difficulty": "L3" },
    { "type": "followup", "difficulty": "L3" }
  ]
}
```

```
배열 순서    1회차에 나간 순서 그대로. 순서 자체가 토픽 구조다
question    text · category · difficulty · is_spare_topic 모두 필수
followup    difficulty만 필요. text는 새로 생성하므로 불필요
reask       배열에서 제외한다. 재현하지 않는다
```

`category`는 카테고리 8종 문자열과 정확히 일치해야 한다.
가운뎃점(·)까지 포함하며, 다르면 `INVALID_CATEGORY`를 반환한다.

```
지원동기  직무역량  프로젝트경험  문제해결
협업·갈등  실패·성장  가치관·인성  미래계획
```

**`retry_of_session_id`는 항상 최초 세션을 가리킨다.**
3회차, 4회차도 직전 회차가 아니라 1회차 로그를 보낸다.
직전 회차를 기준으로 하면 회차마다 질문 세트가 불어나
1회차와 비교할 수 없게 된다.

점수 비교는 직전 회차 기준으로 하며, 자세한 내용은 리포트 계약 8장에 있다.

### 재연습에서 옵션을 바꾼 경우

사용자가 재연습 화면에서 문항 수나 페르소나를 변경할 수 있다.
이 경우 1회차 구조를 재생할 수 없다.

```
question_count 또는 persona가 원본과 다르다
  → replay_log를 무시하고 일반 세션으로 처리한다
  → 모든 질문의 is_replay가 false가 된다
  → 회차 비교 대상에서 제외된다
```

백엔드는 옵션이 바뀐 경우 `replay_log`를 보내지 않아도 된다.
보내더라도 AI가 무시하며, 응답의 `is_replay`로 판별할 수 있다.

**`retry_of_session_id`를 보냈는데 `replay_log`가 없으면 400 `INVALID_REQUEST`다.**
재연습 의도는 밝혔는데 재생할 로그가 없어 진행할 수 없기 때문이다.
옵션을 바꿔 일반 세션으로 진행하려면 `retry_of_session_id`도 함께 빼야 한다.

**AI는 `question_count`가 원본과 다른 것만 감지할 수 있다.**
`replay_log`가 나타내는 문항 수와 요청의 `question_count`를 비교한다.
페르소나 변경은 로그에 단서가 없어 감지할 수 없으므로,
페르소나가 바뀌었으면 백엔드가 `replay_log`를 보내지 않아야 한다.

### 응답 (즉시)

```
202 Accepted

{
  "session_id": "sess_9f2a1c",
  "task_id": "task_001",
  "question_total": 9
}
```

첫 질문은 `task_id`로 폴링해서 받는다.

---

## 3. 답변 제출

### 요청

```
POST /ai/sessions/{session_id}/answers

{
  "question_id": "q_1",
  "audio_url": "https://s3.../ans_1.webm",
  "video_url": "https://s3.../ans_1.mp4",
  "is_timeout": false
}
```

```
question_id   필수. 어느 질문에 대한 답인지
audio_url     필수. 백엔드가 저장한 답변 오디오
video_url     선택. 답변 영상. 카메라 미사용이면 null
is_timeout    필수. 제한 시간 만료로 자동 제출된 답변인가
```

발화 시간과 어절 수는 AI가 STT 결과에서 계산한다. 백엔드가 보낼 필요 없다.

**`video_url`은 질문 진행에 쓰이지 않는다.**
꼬리질문 생성은 음성만 사용한다. 영상은 리포트의 시선 축에서만 쓰이므로,
백엔드가 저장해 두었다가 리포트 생성 요청 시 함께 보내면 된다.
여기서 받는 이유는 답변 단위로 짝을 맞춰 두기 위해서다.

**`is_timeout`이 true이면 되묻기를 하지 않는다.**
시간이 끊은 답변에 "조금 더 말씀해 주시겠어요"라고 되묻는 것은 오작동이다.
답변이 짧아도 그대로 다음 질문으로 넘어간다.

### 응답 (즉시)

```
202 Accepted

{ "task_id": "task_002" }
```

---

## 4. 작업 상태 조회 (폴링)

```
GET /ai/tasks/{task_id}
```

폴링 간격은 1초를 권장한다.

**타임아웃은 작업 종류에 따라 나눈다.**

```
세션 시작 (주질문 2~4개 + TTS)   예상 10~30초   권장 타임아웃 90초
답변 처리 (STT + LLM + TTS)      예상 5~15초    권장 타임아웃 60초
```

세션 시작은 주질문을 여러 개 한 번에 만들기 때문에 더 걸린다.
60초 단일 기준이면 세션 시작에서 오탐이 난다.

### 진행 중

```
{ "status": "processing", "stage": "stt" }

stage 값: "stt" | "generating" | "tts"
```

**이 값을 프론트에 그대로 노출하지 않는다.** 백엔드에서 자체 enum으로 매핑한다.

### 완료 — 질문

```
{
  "status": "done",
  "result": {
    "type": "question",
    "question_id": "q_4",
    "text": "왜 낙관적 락을 선택하셨나요?",
    "audio_url": "https://s3.../q_4.mp3",
    "category": "프로젝트경험",
    "difficulty": "L2",
    "question_number": 4,
    "question_total": 9,
    "topic_index": 2,
    "topic_total": 4,
    "is_spare_topic": false,
    "is_replay": false
  }
}
```

```
type            "question" 주질문 · "followup" 꼬리질문
question_id     세션 안에서만 유일하다. 다른 세션과 겹칠 수 있다
audio_url       S3 URL. TTS 실패 시 null
question_number 몇 번째 문항인가. 진행률 표시에 사용
question_total  사용자가 선택한 총 문항 수 (3 | 6 | 9)
topic_index     몇 번째 토픽인가. 참고용
                주질문이 나갈 때마다 1씩 오르고, 꼬리질문·되묻기에서는 그대로다
topic_total     총 토픽 수. 세션마다 달라진다
                계획된 토픽 수로 시작해, 예비 토픽이 투입되면 세션 도중에 늘어난다
                즉 `max(계획된 토픽 수, 지금까지 열린 토픽 수)`이다
is_spare_topic  문항 수를 채우려고 추가된 토픽에서 나온 질문인가
                통계·디버깅용이며 화면 로직에 쓰지 않는다
is_replay       재연습에서 1회차와 동일한 질문인가
                일반 세션에서는 항상 false
```

### 완료 — 되묻기

```
{
  "status": "done",
  "result": {
    "type": "reask",
    "question_id": "q_4r",
    "reask_of": "q_4",
    "text": "어떤 기술을 사용하셨는지 조금 더 말씀해 주시겠어요?",
    "audio_url": "https://s3.../q_4r.mp3",
    "category": null,
    "difficulty": null,
    "question_number": 4,
    "question_total": 9,
    "topic_index": 2,
    "topic_total": 4,
    "is_spare_topic": false,
    "is_replay": false
  }
}
```

되묻기는 `category`와 `difficulty`만 null이며 나머지 필드는 값이 온다.
`is_spare_topic`과 `is_replay`는 항상 false다.
**모든 응답에 두 필드가 포함되므로 nullable 처리는 필요 없다.**

새 질문이 아니라 같은 질문의 재요청이므로 문항 수에 세지 않으며,
`question_number`도 올라가지 않는다.

되묻기 문구는 고정이 아니다. 답변에서 무엇이 빠졌는지에 따라
LLM이 매번 다르게 만든다.

**되묻기 한도**

```
토픽당   1회. 되묻고도 부실하면 그 토픽의 꼬리질문을 포기하고 다음으로
세션당   3회. 넘으면 되묻지 않고 바로 다음으로
```

판단 기준과 상세 규칙은 「질문 구조와 세션 진행」 5장 참고.

### 완료 — 세션 종료

```
{
  "status": "done",
  "result": {
    "type": "session_end",
    "total_questions": 9
  }
}
```

`total_questions`는 되묻기를 제외한 실제 질문 수이며,
`question_total`과 항상 같다. 문항 수는 반드시 지켜지기 때문이다.

### 실패

```
{
  "status": "error",
  "error_code": "STT_FAILED",
  "message": "음성을 인식하지 못했습니다"
}
```

**없는 `task_id`를 조회하면 404 `SESSION_NOT_FOUND`다.**
작업 전용 코드를 따로 두지 않았다. 재시도 없이 세션을 정리해야 하는 상황이라
처리 방식이 `SESSION_NOT_FOUND`와 같기 때문이다.
작업 결과도 임시 보관이라 오래되면 사라진다.

---

## 5. 진행률 표시 — 주의

**문항 수는 사용자가 선택한 값이 그대로 지켜진다.**
답변이 부실해 꼬리질문이 생략되면 예비 토픽을 투입해 채우기 때문이다.

```
권장    "질문 4 / 9"    question_number / question_total
비권장  "주제 2 / 4"    topic_total이 세션마다 달라진다
```

**토픽 수는 세션마다 다르다.** 부실하게 답할수록 토픽이 늘어난다.
`topic_index`와 `topic_total`은 참고용으로만 쓴다.

되묻기가 나가도 `question_number`는 올라가지 않으므로
진행률이 뒤로 가거나 멈춘 것처럼 보이지 않는다.

---

## 6. 회사 목록

```
GET /ai/companies

[
  { "company_id": "hyundai_enc", "name": "현대건설(주)", "industry": "종합건설 · 플랜트" }
]
```

백엔드는 이 응답을 그대로 프론트에 프록시한다.
회사 목록을 별도로 저장하지 않는다. 원본이 두 곳에 있으면 반드시 어긋난다.

응답이 자주 바뀌지 않으므로 Redis에 5분~1시간 캐시를 두어도 된다.

`verified`가 false인 회사는 AI 서버에서 걸러서 내보낸다.
미확인 데이터가 서비스에 노출되지 않는 것이 자동으로 보장된다.

---

## 7. 세션 중단

```
POST /ai/sessions/{session_id}/abort

{ "status": "aborted" }
```

사용자가 중간에 나간 경우. AI 서버의 세션 상태를 정리한다.
중단된 세션은 리포트를 생성하지 않는다.

---

## 8. 에러 코드

```
INVALID_REQUEST       일반 검증 오류                  400
UNAUTHORIZED          시크릿 헤더 누락 또는 불일치     401
SESSION_NOT_FOUND     세션 없음 또는 만료             404
SESSION_ENDED         이미 종료된 세션에 답변 제출     409
INVALID_QUESTION_ID   현재 질문과 불일치              400
INVALID_CATEGORY      replay_log의 카테고리 값 불일치  400
RESUME_PARSE_FAILED   이력서 파싱 실패                422
STT_FAILED            음성 인식 실패                  500
LLM_FAILED            질문 생성 실패                  500
TTS_FAILED            음성 합성 실패                  500
```

### 에러 본문 형식

**모든 HTTP 에러는 같은 모양이다.**

```json
{
  "error_code": "UNAUTHORIZED",
  "message": "시크릿 헤더가 없거나 올바르지 않습니다"
}
```

폴링 실패 응답과 같되 `status`는 없다. HTTP 에러는 상태 코드로 이미 구분되므로
중복이기 때문이다. `error_code`는 백엔드가 분기하는 기준이므로 null이 되지 않는다.

FastAPI 기본 형식인 `{"detail": "..."}`는 쓰지 않는다.
문자열 하나라 백엔드가 분기하려면 문자열을 파싱해야 한다.

### 검증 오류는 전부 400이다

**스키마 검증 실패도 422가 아니라 400으로 내려간다.**
백엔드가 두 가지 형식을 처리하지 않아도 되게 하기 위해서다.

```
replay_log[].category가 카테고리 8종과 다름   → 400 INVALID_CATEGORY
그 외 모든 검증 오류                          → 400 INVALID_REQUEST
```

카테고리 오류만 따로 빼는 이유는, `replay_log`를 조립하다 문자열을 잘못 넣은 것이
백엔드가 고쳐야 할 버그인데 다른 검증 오류에 섞이면 원인을 찾기 어렵기 때문이다.

`INVALID_REQUEST`가 나는 대표적인 경우다.

```
persona · question_count 등 값이 허용 범위 밖
필수 필드 누락
retry_of_session_id만 있고 replay_log가 없음
```

**존재하지 않는 경로는 404 `INVALID_REQUEST`다.**
`SESSION_NOT_FOUND`를 주면 통합할 때 원인을 잘못 짚게 된다.

`TTS_FAILED`는 질문 텍스트가 이미 생성된 상태일 수 있다.
이 경우 `audio_url`을 null로 하고 텍스트만 반환한다.

**AI 서버 재배포 중에는 `SESSION_NOT_FOUND`가 날 수 있다.**
세션 상태를 임시 보관하기 때문이며 버그가 아니다.
개발 기간에는 재배포 전에 공유한다.

### 재시도 분류

```
INVALID_REQUEST       재시도 없음. 클라이언트 버그
LLM_FAILED            1회 재시도. 같은 요청 재전송
STT_FAILED            1회 재시도하되 성공률이 낮다
                      무음·잡음·파일 손상이 원인인 경우가 많으므로
                      실패 시 바로 재녹음 안내로 넘어간다
TTS_FAILED            재시도 없음. audio_url null로 텍스트만 진행
SESSION_NOT_FOUND     재시도 없음. 세션 aborted 처리
SESSION_ENDED         재시도 없음. 무시 (중복 제출)
INVALID_QUESTION_ID   재시도 없음. 클라이언트 버그
INVALID_CATEGORY      재시도 없음. replay_log 조립 오류
RESUME_PARSE_FAILED   재시도 없음. 다른 파일 안내
```

---

## 9. 백엔드가 저장할 것

### 세션 테이블

```
session_id           VARCHAR(50)   PK
user_id              VARCHAR(50)
resume_id            VARCHAR(50)   FK
company_id           VARCHAR(50)   NULL 허용
persona              VARCHAR(20)   friendly | pressure
job_role             VARCHAR(100)
question_count       INT           3 | 6 | 9
status               VARCHAR(20)   in_progress | completed | aborted
retry_of_session_id  VARCHAR(50)   NULL 허용
created_at           DATETIME
```

`company_id`가 NULL 허용인 것이 중요하다.
회사를 고르지 않고 연습만 하는 경우가 있다.

**`resume_file_url`을 직접 저장하지 않는다.**
presigned URL은 만료되고 S3 경로가 바뀌면 세션을 전부 손봐야 한다.
`resume_id`로 조회해 요청 시점에 URL을 발급한다.

**`retry_of_session_id`는 항상 최초 세션을 가리킨다.**
3회차 이후에도 직전 회차가 아니라 1회차 ID를 넣는다.

### 질문·답변 로그 테이블

```
question_id       VARCHAR(50)
session_id        VARCHAR(50)  FK
type              VARCHAR(20)  question | followup | reask
text              TEXT
audio_url         TEXT
category          VARCHAR(20)  NULL 허용 (되묻기는 없음)
difficulty        VARCHAR(5)   NULL 허용 (되묻기는 없음)
is_spare_topic    BOOLEAN
is_replay         BOOLEAN      DEFAULT false
reask_of          VARCHAR(50)  NULL 허용 (되묻기인 경우 원 질문 ID)
question_number   INT
topic_index       INT
answer_audio_url  TEXT
answer_video_url  TEXT         NULL 허용 (카메라 미사용 시)
answer_is_timeout BOOLEAN
created_at        DATETIME
```

**PK는 `(session_id, question_id)` 복합키로 잡는다.**
`question_id`가 `"q_1"`, `"q_4r"` 형태라 세션 안에서만 유일하다.
단일 PK로 두면 다른 세션과 충돌한다.

**`is_replay`를 저장하지 않으면 회차 비교를 할 수 없다.**
리포트를 나중에 다시 열었을 때 무엇이 비교 대상이었는지 알 수 없어진다.

**`answer_video_url`이 없으면 리포트의 시선 축을 만들 수 없다.**

**`reask_of`가 없으면 리포트에서 되묻기를 원 질문에 합칠 수 없다.**
되묻기 답변은 원 질문의 답변에 이어 붙여 하나로 채점하는데,
어느 질문의 되묻기였는지 모르면 별도 문항으로 잘못 세게 된다.
되묻기 응답의 `question_id`가 `q_4r` 형태이므로 접미사로 추측하지 말고
값으로 저장한다.

### 재연습에 필요한 것

이 로그가 전부다. `type`으로 주질문과 꼬리질문을 구분할 수 있으므로
1회차의 토픽 구조를 복원할 수 있다.

백엔드는 재연습 요청에 `retry_of_session_id`와 `replay_log`를 넣어 보낸다.

**인재상 내용 자체는 백엔드가 저장하지 않는다.**
`company_id` 문자열만 들고 있으면 된다.

---

## 10. 오디오 파일 — S3 업로드와 CORS

**AI가 TTS로 mp3를 만들어 S3에 직접 업로드하고 URL만 반환한다.**

```
AI      TTS 생성 → S3 업로드 → 응답에 S3 URL
백엔드   S3 버킷 · 업로드 권한 제공, 버킷 CORS 설정
프론트   받은 S3 URL을 <audio>에 꽂아 재생
```

AI 서버 주소가 프론트에 노출되지 않고,
mp3 바이너리를 API 응답에 실어 나르지 않아도 된다.

### 버킷 CORS 설정 필수

```
Access-Control-Allow-Origin: *
```

없으면 오디오는 재생되지만 **아바타 입이 움직이지 않는다.**
프론트가 Web Audio API로 음량을 읽어야 하는데 CORS 헤더가 없으면
무음으로 읽힌다. 로컬에서는 정상이다가 배포 후에만 실패하는 유형이다.

### TTS 실패 시 텍스트 숨김을 강제 해제한다

사용자가 질문 텍스트 숨김 옵션을 켠 상태에서 `audio_url`이 null로 오면
화면에도 안 보이고 소리도 안 나서 면접을 진행할 수 없다.
프론트는 `audio_url`이 null이면 숨김 설정을 무시하고 텍스트를 표시한다.

### 백엔드에 요청하는 것

```
S3 버킷 이름과 경로 규칙
AI 서버용 업로드 권한 (IAM 또는 액세스 키)
버킷 CORS 설정
```

S3 자격증명 제공이 어려우면 AI가 base64로 반환하고
백엔드가 업로드하는 방식으로 전환할 수 있다.
다만 폴링 응답이 질문당 100KB 이상으로 커진다.

---

## 11. 개발 순서

먼저 AI 서버가 더미 응답을 반환한다.
LLM도 TTS도 Whisper도 없이 고정된 질문 텍스트와 샘플 mp3를 돌려준다.

**단 세션 구성 로직은 실제로 돌린다.**
문항 수, 난이도, 토픽 구조, 되묻기, 예비 토픽, 재연습이 전부 진짜 값으로 나오므로
백엔드는 폴링·WebSocket push·로그 저장·재연습을 전부 검증할 수 있다.

```
1주차   더미 응답 (스키마 확정, 흐름 검증)
2주차   LLM 연결 (실제 질문 생성)
3주차   Whisper + TTS 연결 (실제 음성)
4주차   더미를 실제 모듈로 교체
```

스키마는 가장 먼저 확정하고 이후 **추가만 허용한다.**
기존 필드명과 타입은 바꾸지 않는다.

---

## 12. 백엔드에 요청하는 것 (정리)

```
P0  로그 테이블에 answer_video_url 추가        리포트 시선 축에 필요
P0  S3 버킷 + AI 업로드 권한 + 버킷 CORS
P0  replay_log 꼬리질문에 difficulty 포함
P0  presigned URL 만료 — 이력서 10분 이상
P1  폴링 타임아웃 — 세션 시작 90초 / 답변 60초
P1  (session_id, question_id) 복합키
```
