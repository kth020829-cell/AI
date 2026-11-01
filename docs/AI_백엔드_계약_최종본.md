# AI ↔ 백엔드 계약 최종본

| 항목 | 값 |
| --- | --- |
| 기준일 | 2026-09-27 |
| 기준 코드 | AI 레포 main `fe0107d` · 백엔드 레포 dev `74a8164`와 열린 PR #43 · #45 · #49 · #50 |
| 대체하는 문서 | 질문생성 API계약(구 · 수정본 · 수정본2), 리포트생성 API계약, 계약서 변경사항 모아둔것, 백엔드 요청사항, 백엔드가 알아야 할 것 · AI 파트 회신, 이력서 전달 방식, 백엔드 전달 사항, 위 문서들의 댓글 합의 |
| 우선순위 | **이 문서와 다른 문서가 다르면 이 문서가 맞습니다** |
| 변경 규칙 | 필드는 추가만 합니다. 기존 필드 이름과 타입은 바꾸지 않습니다 |

구버전과 달라진 점만 빠르게 보려면 19장 「구버전과 달라진 것」을 보세요.

---

## 0. 한눈에 보기

```
프론트 ──WebSocket──▶ Spring ──폴링(1초)──▶ AI 서버
       ◀────push─────        ◀─────────────
                               │
                 presigned GET │ 이력서 · 답변 녹음 · 답변 영상을 AI가 직접 받음
                     S3 ◀──────┘ 질문 음성(mp3)은 AI가 S3에 직접 올림
```

| 작업 | AI가 하는 일 | 백엔드가 받는 것 |
| --- | --- | --- |
| 세션 시작 | 이력서를 읽고 주질문을 한 번에 생성, 첫 질문 음성(TTS) 생성 | 첫 질문 |
| 답변 제출 | 답변 전사(STT), 다음 항목 결정(주질문 · 꼬리질문 · 되묻기 · 종료), 질문 음성 생성 | 다음 질문 또는 종료 |
| 리포트 생성 | 답변 전사, 말하기 · 시선 · 내용 분석, 총점 · 근거 · 코멘트 조립 | 리포트 JSON 전체 |
| 회차 비교 | 회차별 리포트를 비교해 변화 · 추이 · 코멘트 생성 | 비교 결과 (즉시 응답) |

**AI는 데이터를 보관하지 않습니다.** 문서, 기업 인재상, 질문 로그, 리포트는 모두 백엔드가 보관하고 필요할 때 요청에 실어 보냅니다. AI에는 속도용 임시 캐시만 있고, 서버가 재시작되면 비워집니다.

**진행 중인 세션 상태는 AI가 임시로 들고 있습니다.** 난이도 배분 · 토픽 진행 · 되묻기 한도는 AI 내부 로직이라 백엔드는 `session_id`만 들고 다니면 됩니다. 사용자에게 보이는 상태는 백엔드 DB가 기준입니다. AI 서버가 재시작되면 진행 중이던 세션이 사라지고 `SESSION_NOT_FOUND`가 납니다(버그 아님). 개발 기간에는 재시작 전에 공유합니다.

**폴링은 Spring만 합니다.** 프론트는 AI 서버 주소를 모릅니다. AI의 `stage` 값은 프론트에 그대로 보내지 않고 백엔드 enum으로 바꿔 보냅니다.

---

## 1. 연결

| 항목 | 값 |
| --- | --- |
| 주소 (`APP_AI_BASE_URL`) | 로컬 더미 `http://localhost:8000` · 실제 모드는 학과 GPU 서버의 고정 `https://...` 주소 (서버를 올리면 전달) |
| 인증 헤더 | `X-Cueanda-Secret: <공유 비밀번호>`. 백엔드는 `APP_AI_SECRET`, AI는 `CUEANDA_SHARED_SECRET`에 **같은 값** |
| 인증 실패 | 401 `UNAUTHORIZED` |
| 인증 예외 | `GET /health`, `GET /ready` |
| 실제 비밀번호 | AI 서버를 올릴 때 주소와 함께 DM으로 전달 |

**실제 모드에서 AI 서버는 학과 GPU 서버에서만 돕니다.** Whisper(STT)와 시선 분석에 GPU가 필요하기 때문입니다. 주소가 인터넷에 공개되므로 비밀번호 헤더가 유일한 보호 수단입니다.

### 헬스체크

```
GET /health  →  { "status": "ok",    "mode": "dummy" }
GET /ready   →  { "status": "ready", "mode": "dummy" }
```

`mode`는 `dummy`(고정 문장, 요금 없음) 또는 `llm`(실제 생성)입니다.

### 로컬 더미로 시험하는 법

| 하고 싶은 것 | 방법 |
| --- | --- |
| processing 분기 확인 | AI를 `DUMMY_POLL_TICKS=3`으로 실행 (기본 0이면 즉시 done) |
| 부실한 답변 → 되묻기 · 예비 토픽 | 답변 `audio_url`에 `short` 포함 (`.../ans_2_short.webm`) |
| 리포트 내용 실패(`CONTENT_FAILED`) | 답변 `audio_url` 하나에 `content_fail` 포함 |
| 리포트 말하기 축 실패 | 답변 `audio_url`에 `fail` 포함 |
| 리포트 시선 축 실패 | 답변 `video_url`에 `fail` 포함 |
| 시선 skipped | 모든 답변 `video_url`을 null |
| 게이트 발동 | `audio_url`에 `offtopic`(상한 40) 또는 `partial`(상한 70) 포함 |

**더미는 응답 구조와 필드가 실제와 같지만 동작 일부가 다릅니다.** 규칙은 이 문서의 실제 모드 기준을 따르세요.

- `USE_TTS`를 켜지 않으면 질문 음성이 없어 `audio_url`이 null입니다. 켜면 더미 문장도 음성이 붙습니다(8장).
- 리포트의 점수 · 근거 · 코멘트는 예시값입니다. 더미는 말하기 근거가 들어가고, 꼬리질문이 없어도 회복력이 나옵니다(실제 규칙은 13장).
- 세션 시작 폴링에도 `stt` 단계가 보입니다(실제는 `generating` → `tts`).
- 첫 질문을 바로 만들어 두기 때문에 6장의 「첫 질문 생성 전 제출 → 400」이 재현되지 않습니다.

---

## 2. 엔드포인트

| 메서드 | 경로 | 용도 | 응답 |
| --- | --- | --- | --- |
| POST | `/ai/sessions` | 세션 시작 (재면접 포함) | 202 |
| POST | `/ai/sessions/{session_id}/answers` | 답변 제출 | 202 |
| GET | `/ai/tasks/{task_id}` | 작업 상태 폴링 (질문 · 리포트 공통) | 200 |
| POST | `/ai/sessions/{session_id}/abort` | 세션 중단 | 200 |
| POST | `/ai/sessions/{session_id}/report` | 리포트 생성 | 202 |
| POST | `/ai/sessions/{session_id}/report/retry` | 리포트 부분 재시도 | 202 |
| POST | `/ai/reports/compare` | 회차 비교 | 200 (즉시) |
| GET | `/health`, `/ready` | 헬스체크 (인증 없음) | 200 |

**없는 것:** 문서 등록 · 인덱싱 API, `GET /ai/companies`, 리포트 재조회 API.

---

## 3. 공통 규칙

### 비동기 작업과 폴링

생성 작업은 전부 `202 { task_id }`를 즉시 돌려주고, 백엔드가 `GET /ai/tasks/{task_id}`를 1초 간격으로 폴링합니다.

```json
{ "status": "processing", "stage": "stt" }
{ "status": "done", "result": { ... } }
{ "status": "error", "error_code": "STT_FAILED", "message": "..." }
```

`result`는 `done`일 때만 있습니다. **`status`를 먼저 보고 분기하세요.** 리포트 작업의 processing에는 `progress`(0~1)가 더 붙습니다.

`stage`는 정해진 순서대로 바뀌지만, 1초 폴링 사이에 지나간 단계는 보이지 않을 수 있습니다. 모든 단계가 한 번씩 온다고 가정하지 마세요.

| 작업 | 예상 시간 | 폴링 타임아웃 |
| --- | --- | --- |
| 세션 시작 | 10~30초 | **90초** |
| 답변 처리 | 5~15초 | **60초** |
| 리포트 생성 · 재시도 | 9문항 약 5분 | **10분** |

없는 `task_id`를 폴링하면 404 `SESSION_NOT_FOUND`입니다. 작업 결과도 임시 보관이라 오래되면 사라집니다.

### HTTP 에러 본문

```json
{ "error_code": "UNAUTHORIZED", "message": "시크릿 헤더가 없거나 올바르지 않습니다" }
```

- 검증 오류는 422가 아니라 전부 **400**입니다. `replay_log` 카테고리 오류만 `INVALID_CATEGORY`, 리포트 `answers` 형식 오류는 `INVALID_ANSWERS`, 나머지는 `INVALID_REQUEST`입니다.
- 없는 경로는 404 `INVALID_REQUEST`입니다.
- `STT_FAILED` · `LLM_FAILED` · `RESUME_PARSE_FAILED` · `CONTENT_FAILED` · `MEDIA_FETCH_FAILED`는 HTTP 오류가 아니라 **폴링 응답의 `status: error`**로 옵니다.

### 값 규칙

| 값 | 규칙 |
| --- | --- |
| `persona` | `friendly`(친절형) · `pressure`(압박형) |
| `question_count` | `3` · `6` · `9` (기본 6) |
| `difficulty` | `L1` · `L2` · `L3` |
| `category` | 아래 8종 문자열과 **정확히 일치** (가운뎃점 포함). 백엔드는 enum으로 바꾸지 말고 문자열 그대로 저장 |
| `question_id` | 세션 안에서만 유일 (`q_1`, `q_4r`). 다른 세션과 겹칠 수 있음 |

```
지원동기  직무역량  프로젝트경험  문제해결
협업·갈등  실패·성장  가치관·인성  미래계획
```

---

## 4. 이력서 전달

- **RAG · 벡터 DB · 인덱싱은 쓰지 않습니다.** 이력서를 통째로 Claude에 넣어야 여러 부분을 엮는 질문이 나오기 때문입니다.
- **문서를 등록할 때 AI를 부르지 않습니다.** 저장하고 바로 `READY`(프론트에는 `COMPLETED`)로 두면 됩니다. 백엔드 코드가 이미 그렇게 동작합니다.
- **AI는 면접을 시작할 때 `resume_file_url`로 파일을 직접 받습니다.** 매번 새로 읽고 저장하지 않으므로 문서 수정 · 삭제 때 AI 쪽 처리는 없습니다.
- `doc_id`는 예약 필드입니다. 지금은 항상 null로 보내고 저장할 필요가 없습니다. 나중에 파싱 재사용을 켜면 AI가 세션 시작 응답에 담아 주는 방식이 되며, 그때 이 문서를 먼저 고칩니다.

| 조건 | 값 | 백엔드 현재 |
| --- | --- | --- |
| presigned GET 만료 | 10분 이상 | 15분 ✅ |
| 파일 형식 | PDF(그대로 읽음) · DOCX(글자 추출) · TXT 등 텍스트 | ✅ |
| 크기 | 10MB 이하 | ✅ |
| 한글(.hwp) | 읽지 못함 → `RESUME_PARSE_FAILED` | 업로드 단계에서 차단 ✅ |
| 마크다운으로 쓴 문서 | 텍스트 파일로 저장해 URL을 주면 읽음 | — |
| 실제 모드 | URL이 **S3**여야 함 (학과 서버에서 노트북의 MinIO에 닿지 않음) | 배포 시 S3 |

---

## 5. 세션 시작

```
POST /ai/sessions
```

```json
{
  "resume_file_url": "https://s3.../resume.pdf",
  "job_role": "백엔드 개발",
  "persona": "pressure",
  "company_id": "17",
  "company_profile_override": "현대건설(주) (종합건설 · 플랜트)\n\n핵심 가치\n  도전 — ...",
  "question_count": 6,
  "retry_of_session_id": null,
  "replay_log": null,
  "doc_id": null
}
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `resume_file_url` | O | 이력서 presigned GET |
| `job_role` | O | 직무. 자유 문자열 |
| `persona` | O | `friendly` · `pressure` |
| `company_id` | X | 백엔드 기업 PK를 문자열로 (`17` → `"17"`). AI는 로그 추적에만 씀 |
| `company_profile_override` | X | 기업 인재상 텍스트(11장). 기업을 골랐으면 반드시 채움 |
| `question_count` | X | 3 · 6 · 9. 기본 6 |
| `retry_of_session_id` | X | 재면접일 때 1회차 세션 ID (10장) |
| `replay_log` | 재면접이면 O | 1회차 질문 기록 (10장) |
| `doc_id` | X | 항상 null |

```json
202 { "session_id": "sess_9f2a1c", "task_id": "task_001", "question_total": 6 }
```

첫 질문은 `task_id`를 폴링해서 받습니다(7장). 진행 단계는 `generating` → `tts`입니다.

| 폴링 에러 | 처리 |
| --- | --- |
| `RESUME_PARSE_FAILED` | 재시도 없음. 다른 파일을 올리도록 안내 |
| `LLM_FAILED` | **재시도 없음.** AI가 같은 작업 안에서 이미 1회 더 시도한 뒤입니다. 세션을 실패로 정리 |

세션 시작을 다시 부르면 **새 세션**이 만들어지므로 백엔드는 자동 재시도하지 않습니다.

---

## 6. 답변 제출

```
POST /ai/sessions/{session_id}/answers
```

```json
{
  "question_id": "q_1",
  "audio_url": "https://s3.../sessions/sess_9f2a1c/answers/q_1.webm",
  "video_url": "https://s3.../sessions/sess_9f2a1c/answers/q_1_video.webm",
  "is_timeout": false
}
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `question_id` | O | 지금 질문 ID |
| `audio_url` | O | 답변 녹음 presigned GET |
| `video_url` | X | 답변 영상 presigned GET. 카메라 미사용이면 null. 질문 진행에는 안 쓰고 리포트 시선 분석에만 씀 |
| `is_timeout` | O | 제한 시간 만료로 자동 제출됐는가. **true면 되묻기를 하지 않음** |

발화 시간과 어절 수는 AI가 전사 결과로 계산하므로 보내지 않습니다.

```json
202 { "task_id": "task_002" }
```

진행 단계는 `stt` → `generating` → `tts`입니다.

| 에러 | 오는 곳 | 처리 |
| --- | --- | --- |
| `INVALID_QUESTION_ID` | HTTP 400 | 지금 질문과 다름, 또는 첫 질문 생성이 끝나기 전에 제출(첫 질문은 폴링 done을 받은 뒤 제출). 처음이면 세션 유지하고 오류만 알림, 재전송 뒤에 오면 ABORTED |
| `SESSION_ENDED` | HTTP 409 | 이미 끝난 세션. 중복 제출로 보고 무시 |
| `SESSION_NOT_FOUND` | HTTP 404 | 세션 없음(AI 재시작 등). 세션 ABORTED |
| `STT_FAILED` | 폴링 | 같은 요청 1회 재전송, 또 실패하면 재녹음 안내 |
| `LLM_FAILED` | 폴링 | AI 서버의 예상 밖 오류(Claude 실패 아님). 1회 재전송, 또 실패하면 ABORTED |

- 꼬리질문 · 되묻기 생성이 실패하면 AI가 고정 문장으로 이어가므로 에러가 나지 않습니다.
- **재전송은 폴링에서 `status: error`를 받은 뒤에만** 합니다. 처리 중에 다시 보내면 두 작업이 세션을 한 칸씩 진행시켜 문항이 하나 건너뜁니다.
- 같은 답변을 다시 보내도 안전합니다. 실패하면 세션이 다음으로 넘어가기 전에 멈추기 때문입니다. 이미 넘어간 뒤라면 400 `INVALID_QUESTION_ID`가 오는데, 이때도 ABORTED로 정리합니다.
- 재전송하면 `task_id`가 새로 나옵니다. 질문 쪽에는 멱등 키가 없습니다.
- 꼬리질문은 답변을 읽고 만들어야 해서 미리 만들 수 없습니다. 답변 제출 뒤 5~15초 기다리므로 프론트에 로딩 표시가 필요합니다.

---

## 7. 폴링 결과 (질문 쪽)

### 진행 중

| AI `stage` | 의미 | 백엔드 enum |
| --- | --- | --- |
| `stt` | 답변 전사 | `TRANSCRIBING` |
| `generating` | 질문 생성 | `GENERATING` |
| `tts` | 질문 음성 생성 | `SYNTHESIZING` |

### 완료 — 질문 · 꼬리질문 · 되묻기

```json
{
  "status": "done",
  "result": {
    "type": "question",
    "question_id": "q_4",
    "reask_of": null,
    "text": "왜 낙관적 락을 선택하셨나요?",
    "audio_url": "https://{버킷}.s3.{리전}.amazonaws.com/sessions/sess_9f2a1c/questions/q_4.mp3",
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

| 필드 | 설명 |
| --- | --- |
| `type` | `question` 주질문 · `followup` 꼬리질문 · `reask` 되묻기 |
| `reask_of` | 되묻기일 때 원 질문 ID. 그 외 null |
| `audio_url` | 질문 음성 S3 주소(8장). 없으면 null |
| `category`, `difficulty` | 되묻기는 null |
| `question_number` / `question_total` | 진행률 표시용 (`질문 4 / 9`) |
| `topic_index`, `topic_total` | 참고용. `topic_total`은 세션 중에 늘어날 수 있어 진행률에 쓰지 않음 |
| `is_spare_topic` | 문항 수를 채우려고 추가된 토픽의 질문인가. 통계용, 화면 로직에 쓰지 않음. 되묻기는 false |
| `is_replay` | 재면접에서 1회차와 같은 주질문인가. 되묻기 · 꼬리질문은 항상 false |

- **되묻기**는 답변이 부실할 때 같은 질문을 보충해 달라고 다시 묻는 것입니다. 발화 10초 미만이거나 25어절 미만이면 부실로 보고, 그 이상은 AI가 내용을 읽고 판단합니다. 문항 수에 세지 않고 `question_number`도 그대로입니다. 한도는 **토픽당 1회, 세션당 3회**입니다. 되묻고도 부실하면 그 토픽의 꼬리질문을 건너뜁니다.
- **문항 수는 항상 지켜집니다.** 부실한 답변으로 꼬리질문을 건너뛰면 예비 토픽의 주질문으로 채웁니다. 토픽은 3문항 → 2개, 6문항 → 3개, 9문항 → 4개로 시작하고, 예비 토픽이 들어오면 `topic_total`이 늘어납니다.

### 완료 — 세션 종료

```json
{ "status": "done", "result": { "type": "session_end", "total_questions": 9 } }
```

`total_questions`는 되묻기를 뺀 실제 질문 수이며 `question_total`과 항상 같습니다.

---

## 8. 질문 음성 (TTS)

| 항목 | 값 |
| --- | --- |
| 올리는 주체 | AI가 mp3를 만들어 S3에 직접 업로드 |
| 경로 | `sessions/{session_id}/questions/{question_id}.mp3` |
| `audio_url` 형식 | `https://{버킷}.s3.{리전}.amazonaws.com/{경로}` (서명 없는 주소) |
| 재생 방식 | **버킷은 비공개.** 백엔드가 경로로 presigned GET을 발급해 프론트에 전달 (백엔드 이슈 #41) |
| AI 권한 | `sessions/*/questions/*` 쓰기만 |
| 버킷 CORS | 프론트 도메인 GET 허용 필수. 없으면 소리는 나도 아바타 입이 안 움직임(Web Audio가 무음으로 읽음) |

경로는 `session_id`와 `question_id`로 정해지므로 `audio_url`을 파싱하지 않고 경로를 직접 만들어도 됩니다.

**`audio_url`이 null인 경우:** TTS 꺼짐(`USE_TTS` 미설정, 더미 모드 기본값) · 실패, S3 설정 전. 더미 모드도 TTS를 켜면 첫 질문부터 음성이 붙고, 합성하는 동안 `processing`(stage `tts`)이 잠깐 나갑니다. 이때도 에러가 아니라 `status: done`이고 질문 텍스트는 그대로 옵니다. `TTS_FAILED`는 실제로 나오지 않는 코드입니다. 프론트는 `audio_url`이 null이면 텍스트 숨김 설정이 켜져 있어도 텍스트를 보여 줘야 합니다.

---

## 9. 세션 중단

```
POST /ai/sessions/{session_id}/abort  →  200 { "status": "aborted" }
```

AI는 세션 상태를 지우고, 중단된 세션은 리포트를 만들지 않습니다. 세션이 이미 없으면 404 `SESSION_NOT_FOUND`인데, 백엔드는 로컬 세션만 정리하면 됩니다.

---

## 10. 재면접 (replay)

AI는 1회차 기록을 보관하지 않으므로 **백엔드 DB가 기준**이고, 요청에 함께 보냅니다. AI 호출은 세션 시작과 같은 `POST /ai/sessions`입니다.

```json
{
  "resume_file_url": "...", "job_role": "백엔드 개발", "persona": "pressure", "question_count": 6,
  "retry_of_session_id": "sess_abc",
  "replay_log": [
    { "type": "question", "text": "백엔드 개발 직무에 지원하신 이유를 말씀해 주세요.",
      "category": "지원동기", "difficulty": "L1", "is_spare_topic": false },
    { "type": "followup", "difficulty": "L2" },
    { "type": "question", "text": "가장 자신 있는 기술 스택은 무엇인가요?",
      "category": "직무역량", "difficulty": "L1", "is_spare_topic": false },
    { "type": "followup", "difficulty": "L2" },
    { "type": "question", "text": "팀원과 의견이 갈렸던 경험을 말씀해 주세요.",
      "category": "협업·갈등", "difficulty": "L2", "is_spare_topic": false },
    { "type": "followup", "difficulty": "L3" }
  ]
}
```

| 규칙 | 내용 |
| --- | --- |
| `retry_of_session_id` | **항상 1회차 세션 ID.** 3회차 · 4회차도 1회차 ID와 1회차 기록을 보냄 |
| `replay_log` 순서 | 1회차에 나간 순서 그대로. 순서가 곧 토픽 구조. 되묻기를 뺀 항목 수가 `question_count`와 같아야 함 (다르면 일반 세션으로 처리) |
| `question` 항목 | `text` · `category` · `difficulty` · `is_spare_topic` 필수 |
| `followup` 항목 | `difficulty`만 (문장은 새 답변을 읽고 새로 만듦) |
| 되묻기 | 넣지 않음 |
| 카테고리 오류 | 400 `INVALID_CATEGORY` |
| 문항 수나 면접관 스타일을 바꾼 경우 | `retry_of_session_id`와 `replay_log`를 **둘 다 빼고** 일반 세션으로 요청. `retry_of_session_id`만 있으면 400 `INVALID_REQUEST`. 면접관 스타일 변경은 AI가 알아챌 수 없으므로 반드시 빼야 함 |

재면접에서 1회차 주질문은 같은 문장 그대로 나가고 `is_replay: true`입니다. 모든 회차가 1회차 질문 세트를 쓰므로 회차 비교(15장)가 가능합니다.

- 재면접에서도 부실하게 답해 꼬리질문을 건너뛰면 대체 주질문으로 채웁니다. 대체 질문은 1회차에 없던 것이라 `is_replay: false`이고 비교에서 빠집니다.
- 문항 수가 1회차와 다른 `replay_log`를 보내면 AI가 로그를 무시하고 일반 세션으로 진행합니다(모든 질문 `is_replay: false`). 그래도 옵션을 바꿨으면 위 표대로 두 필드를 빼고 보내는 것이 원칙입니다.

---

## 11. 기업 인재상

- **기업 데이터는 백엔드가 관리합니다.** `company` 테이블에 인재상까지 저장하고, `verified=false`인 기업은 서비스에 노출하지 않습니다. 원본 데이터는 AI 레포 `ai/data/companies.json`에 있습니다.
- 면접 시작과 리포트 생성 때 인재상 텍스트를 `company_profile_override`로 보냅니다. AI는 질문과 기업 코멘트에 반영만 하고 저장하지 않습니다.
- `company_id`는 백엔드 PK 문자열이며 AI는 로그에만 씁니다.
- 기업을 고르지 않았으면 두 값 모두 null입니다.

```
기업명 (업종)

핵심 가치
  가치 이름 — 행동지표
  가치 이름 — 행동지표

{직무} 직무 요구역량        ← 있을 때만
  요건
```

직무 요구역량이 없으면 AI가 요구역량을 지어내지 않도록 자동으로 막습니다. 백엔드는 인재상만 보내면 됩니다.

---

## 12. 리포트 생성

```
POST /ai/sessions/{session_id}/report
Idempotency-Key: rpt_{session_id}_{시도번호}
```

```json
{
  "persona": "pressure",
  "job_role": "백엔드 개발",
  "company_id": "17",
  "company_profile_override": "...",
  "answers": [
    { "question_id": "q_1", "type": "question", "text": "...", "category": "지원동기",
      "difficulty": "L1", "question_number": 1,
      "audio_url": "https://s3.../q_1.webm", "video_url": "https://s3.../q_1_video.webm",
      "is_timeout": false, "reask_of": null, "is_replay": false, "is_spare_topic": false },
    { "question_id": "q_1r", "type": "reask", "text": "...", "category": null,
      "difficulty": null, "question_number": 1,
      "audio_url": "https://s3.../q_1r.webm", "video_url": null,
      "is_timeout": false, "reask_of": "q_1", "is_replay": false, "is_spare_topic": false },
    { "question_id": "q_2", "type": "followup", "text": "...", "category": "지원동기",
      "difficulty": "L2", "question_number": 2,
      "audio_url": "https://s3.../q_2.webm", "video_url": "https://s3.../q_2_video.webm",
      "is_timeout": false, "reask_of": null, "is_replay": false, "is_spare_topic": false }
  ]
}
```

| 필드 | 설명 |
| --- | --- |
| `persona`, `job_role` | 세션 값 |
| `company_id`, `company_profile_override` | 면접 시작 때와 같은 값 |
| `answers[]` | 세션에서 나간 질문과 답변 전체. 되묻기 포함, 순서대로. 답변 녹음이 없는 질문 행(답하지 않고 끝난 경우 등)은 뺌 |
| `answers[].audio_url`, `video_url` | 답변 object key로 새로 발급한 presigned GET (30분 이상, 백엔드 설정 1시간). 카메라 미사용이면 `video_url` null |
| `answers[].reask_of` | 되묻기면 원 질문 ID. **접미사로 추측하지 말고 저장값 사용** |
| `answers[].is_replay`, `is_spare_topic` | 로그 테이블 값 그대로. 안 보내면 false로 보고, 회차 비교의 문항별 비교가 비게 됨 |

```json
202 { "task_id": "task_r001" }
```

### 멱등 키

- **`Idempotency-Key` 헤더는 필수입니다.** 없으면 400 `INVALID_REQUEST`입니다.
- **같은 키로 다시 보내면 새 작업을 만들지 않고 기존 `task_id`를 돌려줍니다.** 응답을 못 받아 다시 보낼 때 중복 분석을 막는 용도입니다.
- 이전 작업이 **실패했거나 사용자가 재분석을 누르면 시도번호를 올린 새 키**로 보냅니다. 같은 키면 실패한 작업이 그대로 돌아옵니다.

### 규칙

| 항목 | 내용 |
| --- | --- |
| 되묻기 | 원 질문 답변에 이어 붙여 한 문항으로 채점. 독립 문항으로 세지 않음 |
| `is_timeout: true` | 감점하지 않음. "시간 초과로 중단됨"으로 표시 |
| 문항이 2개 미만 | 즉시 HTTP 422 `REPORT_TOO_SHORT` (되묻기 제외 기준). report 행을 만들지 않음 |
| 말이 전혀 없는 답변 | 실패가 아니라 그 문항 내용 0점 |

### 진행 단계

| AI `stage` | 백엔드 enum 예 |
| --- | --- |
| `transcribing` | `TRANSCRIBING` |
| `analyzing_speech` | `ANALYZING_SPEECH` |
| `analyzing_gaze` | `ANALYZING_GAZE` (가장 오래 걸림) |
| `analyzing_content` | `ANALYZING_CONTENT` |
| `composing` | `COMPOSING` |

`progress`(0~1)가 함께 옵니다.

### 폴링 에러

| 에러 | 처리 |
| --- | --- |
| `CONTENT_FAILED` | 리포트 전체 실패. 새 키로 생성 재요청 1회(자동), 또 실패하면 FAILED |
| `MEDIA_FETCH_FAILED` | presigned URL 만료 가능성. URL을 새로 발급해 새 키로 재요청 1회(자동), 또 실패하면 FAILED |
| `STT_FAILED` | 재시도 없음. 바로 FAILED (오디오 자체 문제일 가능성이 높음) |
| `LLM_FAILED` · 그 외 | AI 서버의 예상 밖 오류(리포트 작업에서도 이 코드로 나옴). 자동 재시도 없이 FAILED. 사용자가 다시 요청하면 새 키 |

FAILED 리포트가 있는 세션도 다시 요청할 수 있어야 합니다.

---

## 13. 리포트 응답

```json
{
  "status": "done",
  "result": {
    "session_id": "sess_9f2a1c",
    "generated_at": "2026-09-05T14:22:31Z",
    "report_status": "partial",
    "summary": "역할과 해결 과정은 분명했지만, 선택 근거와 성과 수치를 보완하면 좋겠습니다.",
    "overall": {
      "score": 68, "display": 4, "gated": false, "gate_reason": null,
      "partial": true, "axes_used": ["content", "speech"], "axes_failed": ["gaze"]
    },
    "axes": {
      "content": { "status": "ok", "score": 72, "display": 4, "metrics": {},
        "evidence": [ { "question_id": "q_3", "t_start": 12.4, "t_end": 19.8,
          "kind": "weakness", "label": "근거 부족", "comment": "비교 대상이 제시되지 않았습니다." } ] },
      "speech": { "status": "ok", "score": 61, "display": 4,
        "metrics": { "hesitation_score": 32, "speech_rate_cv": 0.284, "repetition_count": 3 },
        "evidence": [] },
      "gaze": { "status": "failed", "error_code": "GAZE_FAILED", "score": null,
        "display": null, "metrics": null, "evidence": [] }
    },
    "questions": [
      { "question_id": "q_1", "question_number": 1, "category": "지원동기", "difficulty": "L1",
        "is_replay": false, "is_spare_topic": false, "score": 70, "display": 4,
        "axes": { "content": 74, "speech": 63, "gaze": null },
        "transcript": "저는 데이터가 쌓이고 흐르는 구조에...", "duration_sec": 46.2,
        "word_count": 138, "was_timeout": false, "had_reask": true,
        "comment": "지원 동기를 경험과 연결했지만 결론이 늦게 나왔습니다." }
    ],
    "resilience": { "score": 58, "display": 3, "comment": "..." },
    "company_comment": "도전과 협업을 강조하는 인재상에 비추어...",
    "improved_answers": [
      { "question_id": "q_3", "original_excerpt": "낙관적 락을 썼습니다.",
        "suggestion": "선택 이유와 대안 비교를 함께 언급하면...", "t_start": 12.4, "t_end": 19.8 }
    ]
  }
}
```

### 백엔드가 저장할 값

| 저장 | 값 |
| --- | --- |
| 총점 | `overall.score` |
| 3축 점수 | `axes.{content,speech,gaze}.score` (실패 · 미사용이면 null) |
| 상태 | `report_status`: `complete` → COMPLETED, `partial` → PARTIAL. 폴링 error면 FAILED |
| 원본 | `result` 전체. 회차 비교에 다시 보내야 하므로 오래된 회차도 지우지 않음 |

### 점수

| 항목 | 규칙 |
| --- | --- |
| 스케일 | 내부 0~100 정수, 표시 1~5. 둘 다 AI가 줌 |
| 1~5 구간 | 0~19→1 · 20~39→2 · 40~59→3 · 60~79→4 · 80~100→5 |
| 총점 가중치 | 내용 0.5 · 말하기 0.25 · 시선 0.25 |
| 빠진 축 | 남은 축 비율로 재정규화 (시선 빠짐 → 내용 0.667 · 말하기 0.333) |
| 게이트 | 내용 50 이상 → 없음 · 30~49 → 총점 상한 70 · 30 미만 → 상한 40 |
| 게이트 표시 | `gated: true`, `gate_reason: "content_relevance_low"`(두 단계 공통). 프론트는 "내용 때문에 점수가 제한됨" 안내 |

게이트는 주제에서 벗어난 유창한 답변이 말하기 · 시선만으로 높은 총점을 받는 것을 막습니다. 게이트 임계값은 조정될 수 있지만 응답 구조는 바뀌지 않습니다.

### 축 상태와 부분 실패

| 상황 | 결과 |
| --- | --- |
| 내용 분석 실패 | 리포트를 만들지 않음. 폴링 `status: error`, `CONTENT_FAILED` (`result` 없음) |
| 말하기 분석 실패 | `status: done`, `report_status: partial`, `speech.status: failed`, `error_code: SPEECH_FAILED` |
| 시선 분석 실패 | 위와 같고 `gaze.status: failed`, `error_code: GAZE_FAILED` |
| 카메라 미사용 | 실패 아님. `gaze.status: skipped`, `reason: "no_video"`. `report_status: complete`, `overall.partial: false`라 **COMPLETED**. 총점은 내용 · 말하기 두 축으로 계산. 화면에는 "카메라 미사용" |

`axes.*.status`는 `ok` · `failed` · `skipped`입니다. `error_code`는 failed일 때만, `reason`은 skipped일 때만 있고, 그 외에는 null이 아니라 **키 자체가 없습니다.**

### 글로 된 칸 (실제 모드 기준)

| 필드 | 규칙 |
| --- | --- |
| `summary` | 한 줄 총평. 리포트 화면 맨 위. 강점과 먼저 고칠 점을 한 문장으로 |
| `questions[].comment` | 문항별 한 줄 코멘트. 화면의 「면접 흐름」 줄. 되묻기 답변은 원 문항에 합쳐서 봄. 생성 결과에 없는 문항은 null |
| `axes.*.evidence[]` | `question_id`, `t_start` · `t_end`(그 답변 오디오 기준 초), `kind`(`strength` · `weakness`), `label`(배지용 짧은 이름), `comment`. 내용은 답변 원문 인용 근거, 시선은 시선 회피 구간, 말하기는 지금 빈 배열 |
| `improved_answers[]` | 내용 점수가 낮은 문항 최대 2개. `original_excerpt`는 전사에 실제로 있는 구절. 모두 잘 답했거나 생성 실패면 빈 배열 |
| `company_comment` | 인재상(`company_profile_override`)이 없으면 null. 생성 실패도 null |
| `resilience` | 회복력. `100 − (주질문 내용 평균 − 꼬리질문 내용 평균) × 2` (떨어진 만큼만, 0~100). **친절형이거나 꼬리질문이 없으면 null** |

글로 된 칸은 생성에 실패해도 리포트를 막지 않고 빈 배열이나 null이 됩니다. 가짜 문장으로 채우지 않습니다.

### metrics

| 축 | 키 |
| --- | --- |
| `speech` | `hesitation_score`(0~100, 클수록 많이 머뭇거림 · 화면 대표 지표), `speech_rate_cv`(말 속도 변동), `repetition_count`(바로 이어 같은 말을 반복한 횟수) |
| `content`, `gaze` | 지금은 빈 객체 `{}` (오류 아님. 정해지면 키가 추가됨) |

필러워드(「음」 「어」)는 세지 않습니다. Whisper가 안정적으로 잡지 못해 머뭇거림 지표로 대신합니다.

### `questions[]`

문항별 `score` · `display` · 축별 점수(`axes`, 실패 · 미사용 축은 null), `transcript`, `duration_sec`, `word_count`, `was_timeout`, `had_reask`, `comment`(한 줄 코멘트, 없으면 null). 되묻기는 따로 나오지 않고 원 질문에 합쳐지며 `had_reask: true`가 됩니다.

---

## 14. 리포트 재시도 (PARTIAL만)

```
POST /ai/sessions/{session_id}/report/retry
Idempotency-Key: rpt_{session_id}_{새 시도번호}
```

```json
{ "axes": ["gaze"], "persona": "...", "job_role": "...", "company_id": "...",
  "company_profile_override": "...", "answers": [ ... ] }
```

| 항목 | 내용 |
| --- | --- |
| 대상 | `report_status: partial` 리포트. FAILED(내용 실패)는 12장의 생성 재요청으로 복구 |
| 본문 | 리포트 생성 본문 + `axes`(부분 리포트의 `overall.axes_failed` 그대로). presigned URL은 새로 발급 |
| 응답 | `202 { task_id }` → 폴링 결과는 생성과 같은 **전체 리포트** |
| 저장 | 기존 리포트를 **통째로 교체**하고 총점 · 상태도 새 값으로 (PARTIAL → COMPLETED 가능) |
| 비용 | 요청한 축만 다시 계산. 시선만 재시도하면 Claude 요금이 없고, 말하기만 재시도하면 GPU를 쓰지 않음 |

`axes`가 비어 있으면 400 `INVALID_REQUEST`입니다.

---

## 15. 회차 비교

```
POST /ai/reports/compare     (폴링 없이 즉시 응답)
```

```json
{ "reports": [
    { "session_id": "sess_a", "round": 1, "report": { ...리포트 result 원본... } },
    { "session_id": "sess_b", "round": 2, "report": { ... } }
] }
```

- `round`는 연습 기록의 회차 번호를 그대로 보냅니다. **1회차(원래 면접)는 반드시 `round: 1`입니다.** 비어도 됩니다(`[1, 3]`, `[2, 3]`). 다시 매기지 않으며, 같은 번호를 두 번 보내지만 않으면 됩니다.
- 배열은 회차 오름차순(AI도 `round`로 다시 정렬). 회차 수 제한 없음. AI가 저장하지 않으므로 백엔드가 모아서 보냅니다.
- 1회차만 보내도 오류가 아니고 `vs_previous`가 null, `by_question`이 빈 배열입니다. 빈 배열은 400.
- LLM을 부르지 않습니다. 코멘트는 점수 변화로 만든 문장이라 요금이 없고, 같은 입력이면 항상 같은 결과입니다.

```json
200
{
  "latest_round": 3,
  "compared_rounds": [1, 2, 3],
  "vs_previous": {
    "from_round": 2, "to_round": 3,
    "overall_delta": null,
    "axis_delta": { "content": 6, "speech": 3, "gaze": null },
    "improved": ["q_1"], "declined": ["q_3"], "unchanged": ["q_5"],
    "comment": "부분 리포트가 포함되어 총점은 비교하지 않았습니다."
  },
  "trend": {
    "overall": [68, null, 74],
    "axes": { "content": [70, 72, 78], "speech": [61, 60, 63], "gaze": [55, null, 58] },
    "comment": "첫 회차부터 지금까지 내용 70→78, 말하기 61→63, 시선 55→58.",
    "stalled_axes": [],
    "best_round": 3
  },
  "by_question": [
    { "question_id": "q_1", "text": "1번 지원동기", "category": "지원동기",
      "scores": [62, 70, 75], "delta_from_previous": 5, "delta_from_first": 13,
      "comment": "직전 회차보다 5점 올랐습니다. 1회차 대비 +13점입니다." }
  ],
  "partial_rounds": [2]
}
```

| 응답 필드 | 내용 |
| --- | --- |
| `latest_round`, `compared_rounds` | 마지막 회차, 비교한 회차 목록(보낸 번호 그대로). 아래 배열의 n번째 값은 `compared_rounds`의 n번째 회차 |
| `vs_previous` | 배열에서 **바로 앞 회차** 대비(`[1, 3]`이면 3 vs 1). `from_round` · `to_round`, `overall_delta`(둘 중 하나라도 부분 리포트면 null), `axis_delta`(한쪽 축이 실패 · 미사용이면 null), `improved` · `declined` · `unchanged`(question_id 목록), `comment`. 회차가 1개면 null |
| `trend` | 회차별 `overall`(부분 리포트 회차는 null) · `axes`(실패 · 미사용 축은 null) 배열, `comment`, `stalled_axes`(최근 3회차 정체 축), `best_round`(overall이 있는 회차 중 최고, 전부 null이면 마지막 회차) |
| `by_question[]` | 비교한 **모든 회차에 있는** 문항만. `scores`(회차별 문항 점수 `questions[].score`, null 없음), `delta_from_previous`, `delta_from_first`(배열의 첫 회차 기준), `comment`. `text`는 "1번 지원동기" 같은 자리표시라 화면에는 백엔드가 저장한 질문 문장을 씁니다 |
| `partial_rounds` | 부분 리포트였던 회차. 이 회차는 `trend.overall`이 null (그래프에서 끊거나 점선) |

**문항 짝 맞추기:** 재면접은 1회차 `replay_log`를 순서대로 재생하므로, 다시 나온 주질문은 1회차와 **같은 `question_id`**를 받습니다(되묻기는 번호를 쓰지 않고, 꼬리질문을 건너뛰어도 그 자리를 대체 질문이 채워 번호가 밀리지 않음). 그래서 모든 회차에서 같은 ID로 짝을 맞추고, 백엔드가 따로 보낼 것은 없습니다.

**비교 대상 문항:** 재면접 회차에서는 `is_replay: true`인 문항만 봅니다. `is_spare_topic`으로 거르지 않습니다(1회차 예비 토픽 질문도 재면접에서 재현되므로 비교 대상). 1회차는 재면접이 아니라 문항이 전부 `is_replay: false`이므로, 다른 회차가 `is_replay`로 다시 낸 question_id만 비교에 씁니다.

---

## 16. 에러 코드 전체

| 코드 | 오는 곳 | 백엔드 처리 |
| --- | --- | --- |
| `INVALID_REQUEST` | HTTP 400 / 없는 경로 404 | 재시도 없음. 요청 버그 |
| `INVALID_CATEGORY` | HTTP 400 | 재시도 없음. `replay_log` 조립 오류 |
| `INVALID_ANSWERS` | HTTP 400 | 재시도 없음. 리포트 `answers` 형식 오류 |
| `UNAUTHORIZED` | HTTP 401 | 비밀번호 확인 |
| `SESSION_NOT_FOUND` | HTTP 404 | 재시도 없음. 세션 ABORTED |
| `SESSION_ENDED` | HTTP 409 | 무시 (중복 제출) |
| `INVALID_QUESTION_ID` | HTTP 400 | 처음이면 세션 유지 · 오류만 알림. 재전송 뒤 이 오류면 ABORTED |
| `REPORT_TOO_SHORT` | HTTP 422 | 재시도 없음. 사용자 안내 |
| `RESUME_PARSE_FAILED` | 폴링 | 재시도 없음. 다른 파일 안내 |
| `LLM_FAILED` | 폴링 | 세션 시작: 세션 실패 처리 · 답변 처리: 1회 재전송 후 ABORTED · 리포트: FAILED |
| `STT_FAILED` | 폴링 | 답변 처리: 1회 재전송 후 재녹음 안내(세션 유지) · 리포트: 재시도 없이 FAILED |
| `CONTENT_FAILED` | 폴링 (리포트) | 새 키로 생성 재요청 1회, 또 실패하면 FAILED |
| `MEDIA_FETCH_FAILED` | 폴링 (리포트) | 새 presigned URL · 새 키로 재요청 1회, 또 실패하면 FAILED |
| `SPEECH_FAILED`, `GAZE_FAILED` | 리포트 축의 `error_code` | 에러 아님. PARTIAL로 저장, 필요하면 14장 재시도 |
| `TTS_FAILED` | 나오지 않음 | `audio_url` null로 정상 진행 |

---

## 17. 백엔드 저장 · 스토리지

### 테이블 (백엔드 dev에 이미 반영됨)

| 테이블 | AI 계약에 필요한 컬럼 |
| --- | --- |
| 세션 | `session_id`(AI가 발급한 문자열), `resume_id`, `company_id`, `persona`, `job_role`, `question_count`, `status`, `retry_of_session_id` |
| 질문 · 답변 로그 | PK `(session_id, question_id)`, `type`, `text`, `audio_url`(null 가능), `category` · `difficulty`(되묻기는 null), `is_spare_topic`, `is_replay`, `reask_of`, `question_number`, `topic_index`, 답변 녹음 · 영상 object key, `answer_is_timeout` |
| 리포트 | 총점, 3축 점수, 상태, 원본 JSON 전체 |

URL은 만료되므로 저장하지 않고 **object key를 저장해 요청할 때 presigned를 발급**합니다.

### presigned 만료

| 용도 | 필요 | 백엔드 설정 |
| --- | --- | --- |
| 이력서 GET (AI 전달) | 10분 이상 | 15분 |
| 답변 녹음 · 영상 GET (AI 리포트 분석) | 30분 이상 | 1시간 |
| 질문 음성 GET (프론트 재생) | 백엔드 판단 | #41에서 결정 |

---

## 18. 진행 상황 (2026-09-27)

| 항목 | 상태 |
| --- | --- |
| 더미 서버 연동 | 가능. AI 레포 main을 받아 `docker compose up` |
| 질문 음성 private S3 + presigned 재생 | 백엔드 이슈 #41 진행 중 |
| S3 버킷 이름 · 리전 | 백엔드 → AI 전달 대기 |
| AI용 S3 업로드 키 (`sessions/*/questions/*` 쓰기만) | 백엔드 → AI 팀장 DM 대기 |
| 버킷 CORS (프론트 도메인 GET) | 백엔드 설정 필요 |
| 실제 AI 서버 주소 · 공유 비밀번호 | AI가 학과 서버에 올린 뒤 DM으로 전달 |
| 실제 모드의 녹화 · 이력서 저장 | S3 (MinIO 불가) |
| 폴링 타임아웃 | 세션 90초 · 답변 60초 반영됨. 리포트 10분은 PR #50 |
| 답변 처리 재전송 · 세션 정리 | PR #43 (이 문서 6장과 같음) |
| 리포트 등록 · 폴링 · 자동 재시도 | PR #49 · #50 (이 문서 12 · 13장과 같음) |
| 리포트 부분 재시도 API | 백엔드에서 PARTIAL 전용으로 둘지 확인 후 별도 작업 (14장) |
| 리포트 상세 · 회차 비교 | 구현 전. 13 · 15장 기준 |

---

## 19. 구버전과 달라진 것

| 구버전 | 최종 |
| --- | --- |
| RAG · FAISS · 문서 인덱싱 API | 없음. 이력서를 통째로 읽음. 문서 등록 즉시 READY |
| `doc_id` = RAG 인덱스 키 | 파싱 캐시용 예약 필드. 지금은 항상 null, 저장 불필요 |
| `GET /ai/companies`, AI가 기업 목록 보관 | 없음. 기업 · 인재상은 백엔드 `company` 테이블, `company_profile_override`로 전달 |
| `company_id: "hyundai_enc"` | 백엔드 PK 문자열 `"17"`. AI는 로그용 |
| 인재상 내용은 백엔드가 저장하지 않음 | 백엔드가 저장 |
| AI 서버는 내부망 · 외부 비노출 | 학과 GPU 서버 + 고정 https 주소 + 비밀번호 헤더 |
| 질문 음성 경로 `tts/{uuid}.mp3` | `sessions/{session_id}/questions/{question_id}.mp3` |
| 질문 음성 S3 URL을 프론트가 그대로 재생 · base64 대안 | 비공개 버킷 + 백엔드가 presigned 발급 (#41) |
| `TTS_FAILED` 500 | 에러로 나오지 않음. `audio_url` null |
| 세션 시작 `LLM_FAILED` → 백엔드가 재전송(새 세션) | AI가 같은 작업 안에서 1회 재시도. 백엔드는 재시도 없이 실패 처리 |
| 답변 처리 `LLM_FAILED` 1회 재시도 | Claude 실패가 아니라 예상 밖 오류. 1회 재전송 후 ABORTED |
| 게이트 한 단계 | 두 단계 (30~49 → 상한 70, 30 미만 → 상한 40) |
| 표시 점수 85 · 70 · 50 · 30 구간 | 20점 단위 (0~19→1 … 80~100→5) |
| 내용 채점 실패 시 임의 점수 | `CONTENT_FAILED`로 전체 실패. 말이 없는 답변은 내용 0점 |
| 리포트 재시도 응답 = 요청한 축만 | 전체 리포트. 통째로 교체 |
| 실패 후 재요청도 같은 멱등 키 | 같은 키는 기존 작업을 그대로 돌려줌(중복 방지). 실패 후 재요청은 시도번호를 올린 새 키 |
| 말하기 지표 = 필러워드 | `hesitation_score` · `speech_rate_cv` · `repetition_count` |
| 회복력 산출식 미정 | 확정 (13장) |
| 축 가중치 미정 | 확정 0.5 · 0.25 · 0.25 |
| 회차 비교 필터 `is_spare_topic` 포함 | `is_replay`만 |
| 회차 비교: 1회차가 끼면 문항별 비교가 빔 · 부분 리포트면 `overall_delta` 0 | 1회차는 재면접 문항과 같은 ID로 짝을 맞춤 · `overall_delta` null · 응답 예시 추가 (2026-11-01) |
| 총평 · 문항 코멘트 없음 | `summary`, `questions[].comment` 추가 (2026-10-01). 기존 필드는 그대로라 저장 방식 변경 없음 |

---

## 부록. 백엔드 `13-report.md` 확인 항목 답

| # | 확인 항목 | 답 |
| --- | --- | --- |
| 1 | 점수 스케일 | 0~100 정수 + 표시 1~5 (13장) |
| 2 | 작업 등록 · 폴링이 질문 생성과 같은가 | 같음. 202 + task_id + `GET /ai/tasks/{task_id}`. 멱등 키 헤더만 추가 |
| 3 | 소요 시간 | 9문항 약 5분, 타임아웃 10분 |
| 4 | stage 목록 | 12장 표 5개 + `progress` |
| 5 | 부분 실패 표현 | `report_status`, `overall.partial` · `axes_failed`, `axes.*.status` |
| 6 | 실패한 축만 재요청 | 가능. `/report/retry` + `axes` (14장) |
| 7 | 회차 비교 | AI가 계산해서 줌. `POST /ai/reports/compare` (15장) |
| 8 | 결과 JSON 스키마 | 13장 |
