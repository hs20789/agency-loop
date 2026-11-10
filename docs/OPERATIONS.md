# Agency Loop 운영·검증 기록

> 프로젝트 소개와 빠른 시작은 [README](../README.md)에 있습니다. 이 문서는 실행·설정·시험을 실제로 확인한 기록과 운영 세부 사항입니다.

AI 코딩 에이전트 개발 세션(Claude Code JSONL)에서 중요한 의사결정과 검증되지 않은 가정을 찾아, 개발자가 그 결정을 이해·검증·통제하는 연습을 하도록 돕는 웹 서비스의 MVP입니다.

- 제품 의도와 계획: [`PROJECT.md`](../PROJECT.md) · 개발 계약(모듈, API, 규칙): [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) · 합성 세션 설명: [`fixtures/README.md`](../fixtures/README.md)
- 이 문서의 명령은 모두 2026-11-10에 이 서버의 `/root/agency-loop`에서 직접 실행했고, 명령마다 결과를 적었습니다. 하지 않은 일은 명령 없이 글로만 적고 '확인하지 않음'으로 표시했습니다.

## 개요

사용자가 Claude Code 세션 JSONL 파일 하나(32MiB 이하)를 올리면, 규칙 엔진이 분석에 필요한 항목만 남기고 비밀값·개인정보를 가린 뒤 세션 속 AI가 내린 의사결정 후보를 찾습니다. 사용자는 후보 하나를 골라 결과를 보기 전에 스스로 판단해 보고, 근거가 붙은 모델 분석, 가정 상황 연습, 피드백을 차례로 받습니다.

1. **Agency Mirror — 의사결정 분석 + 개발 과정 분석 리포트.** 규칙 엔진이 결과와 무관한 신호(예외 처리, 외부 호출, 입력 검증, 의존성·설정 변경, AI 설명의 가정·대안 표현 등)로 후보를 최대 3개 고릅니다. 사용자는 후보와 자기평가(이해·검증·통제 각 3단계)를 입력하고, 조건이 맞으면 결정 직후 테스트가 기록상 통과했는지 실패했는지 예측한 뒤 결과를 봅니다(기록 확인 예측). 이어서 Claude가 근거 ID를 붙여 결정 이유·대안·검증되지 않은 가정·위험·확인할 점을 분석하고, 규칙 엔진이 인용을 원문과 대조해 진술마다 근거 등급을 붙입니다. 심층 분석이 끝나면 같은 근거로 **개발 과정 분석 리포트**(개발 세션 요약 → 주요 기술적 의사결정 = 심층 분석 → 프롬프트 개선 포인트 → 판단·검증 보완 포인트 → 다음 개발을 위한 개선 행동)를 자동으로 만듭니다. 리포트는 세션 개발자와 현재 사용자를 다른 사람으로 다루고, "기록에서 확인되지 않는다"와 실제로 하지 않았다는 말을 구분하며, 점수나 역량 등급을 매기지 않습니다. 원하면 HCX-007·DeepSeek의 독립 검토를 받아 인용 근거 ID 기준으로 비교합니다.
2. **What-if Lab — 가정 상황.** Claude가 실제 코드와 요구사항에 근거한 가정 상황을 자기평가로 정한 난이도(기본/표준/심화)로 만들고, 사용자는 예상 결과·잠재 문제·AI에게 줄 지시·검증 방법을 씁니다. 개발 과정 리포트가 있으면 그 판단·검증 보완 포인트가 가정 상황 만들기에 참고되고, 없으면 그대로 진행합니다. 실행된 적 없는 상황이므로 정답이나 일치 표시가 없습니다.
3. **Adaptive Feedback — 적응형 피드백.** 항목별로 잘한 점·보완할 점·힌트를 주고, 한 번 다시 제출하면 최종 피드백을 줍니다. 개발 과정 리포트의 보완 포인트는 답변 전송 미리보기에 함께 표시되어 동의한 뒤 피드백에 반영됩니다. 답변 속 AI 지시가 언급한 테스트가 세션에 있는지는 규칙 엔진이 확인하고, 자기평가와 답변에 드러난 행동을 점수 없이 나란히 보여 주며, 다음 난이도는 글로만 제안합니다.

이 서비스는 로그 요약기나 코딩 퀴즈가 아닙니다. 점수·정답률·능력 판정·모델 다수결을 쓰지 않고, 테스트를 실행한 기록을 통과로 보지 않으며(통과·실패는 규칙 엔진만 판정), 개발자가 나중에 고친 코드를 정답으로 쓰거나 모델에 보내지 않습니다. 어떤 내용도 제공자별 전송 미리보기와 동의 없이는 서버 밖으로 나가지 않습니다. 수업 기간에는 공개·합성 연습 세션만 처리합니다.

## 현재 구현 상태 (2026-11-10)

- **구현됨·로컬 시험 완료**: 이 서버 안에서 모의 제공자(mock)로 자동 시험을 통과했고, 표시한 것은 직접 실행해서도 확인했습니다. 네트워크 호출은 없습니다.
- **모의 서버로만 시험**: 실제 제공자용 코드(live 어댑터)를 `127.0.0.1`의 모의 HTTP 서버(`tests/mock_servers.py`)나 가짜 MCP 서버에 붙여서만 시험했습니다. 실제 서비스의 응답 형식·지연·과금과 같은지는 모릅니다.
- **실제 서버 연결 필요**: 승인 후 실제 외부 서비스·수업 서버·공개 데이터에 연결해야 확인할 수 있습니다. 아직 한 번도 호출하지 않았습니다.

| 기능 | 구현됨·로컬 시험 완료 | 모의 서버로만 시험 | 실제 서버 연결 필요 | 관련 시험 |
|---|---|---|---|---|
| ① 세션 업로드(32MiB 상한, 형식 오류, 깨진 줄 수, IP당 업로드 제한, 세션 파일 찾는 방법 안내, 시연 파일 버튼) | ○ 32MiB(33,554,432바이트)는 통과, +1바이트 → 413 직접 확인 | — | — | `test_web_upload`, `test_parser` |
| ① 비식별화와 분석 가능 범위 표시 | ○ 합성 세션 기준 | — | — | `test_redact`, `test_scope`, `test_security` |
| ② 의사결정 후보 최대 3개(카드에 세션 원문 없음) | ○ | — | — | `test_candidates` |
| ② 후보 선택·자기평가(선택 즉시 고정), 난이도 변환 | ○ | — | — | `test_web_select`, `test_selfassess` |
| ② 기록 확인 예측·결과 가림·결과 공개(pytest·unittest·Jest) | ○ 결과만 다른 두 세션의 제출 전 응답이 같음, 결과 표식이 응답·로그에 없음 | — | — | `test_testruns`, `test_retro`, `test_blinding_flow`, `test_security` |
| ② 근거 카드·코드/로그 뷰어·'이후 수정 있음(정답 아님)' 태그 | ○ | — | — | `test_evidence`, `test_web_select`, UI 스모크 |
| ② 제공자별 전송 미리보기·동의, 보내기 직전 비밀값 검사 | ○ 거절한 제공자 호출 0회 | ○ 예측·동의 전 전송 0건 | — | `test_consent`, `test_flow_api`, `test_flow_failures`, `test_live_e2e` |
| ② 관련 요청 검색(BGE-M3)·인용 근거 선택(CLOVA 리랭커) | ○ 모의 임베딩은 단어 기반 가짜 벡터라 의미 검색이 아님 | ○ | ○ local-rag bge-m3, 리랭커, 유사도 기준값(0.35) 보정 | `test_retrieval`, `test_providers_embed`, `test_providers_clova` |
| ② 심층 분석(Claude Ⓐ)·인용 검증·근거 등급 | ○ 모의 응답 | ○ 스트리밍, 재시도, 거절, 출력 한도 | ○ 키·Opus 5.5 접근, 지연, 실제 인용 정확도 | `test_citations`, `test_bundle`, `test_prompts`, `test_providers_anthropic`, `test_live_e2e` |
| ② 개발 과정 분석 리포트(Claude Ⓔ: 세션 요약·프롬프트 분석·보완 포인트·개선 행동, 예측·결과 공개 뒤에만 생성, 동의는 심층 분석 단계 재사용, 보완 포인트는 What-if·피드백에 재사용) | ○ 모의 응답 | ○ 동일 스키마·제한 | ○ 실제 리포트 품질 | `test_report`, UI 스모크 |
| ② Local LLM 요약(화면 표시용, MCP) | ○ 모의 응답 | ○ 가짜 MCP 서버, 가짜 Ollama에 붙인 수업 `server.py`(opt-in) | ○ local-llm-chat Ollama 요약, 모델 측정 | `test_providers_mcp`, `test_flow_failures`, `test_course_mcp_optin` |
| ② 선택적 독립 검토(HCX-007·DeepSeek)·모델 비교 | ○ 모의 응답 | ○ CLOVA 429, Mistral 402 포함 | ○ HCX-007 JSON 출력·생각 강도, DeepSeek 추론 토큰·과금 | `test_reviews_api`, `test_compare`, `test_mistral`, `test_live_e2e` |
| ③ 가정 상황 What-if 생성(Claude Ⓑ, 리포트 보완 포인트 참고) | ○ 모의 응답 | ○ | ○ 실제 응답 품질·지연 | `test_flow_api`, `test_report`, `test_prompts`, `test_live_e2e` |
| ③ 피드백·재제출(Claude Ⓒ·Ⓓ), AI 지시 점검, 자기평가 비교, 다음 난이도(보완 포인트는 미리보기·동의 후 반영) | ○ 모의 응답(점검·비교·난이도는 규칙 엔진) | ○ | ○ 실제 피드백 품질 | `test_flow_api`, `test_report`, `test_instruction`, `test_selfassess`, `test_live_e2e` |
| 호출 상한(흐름당·하루·동시) | ○ | ○ Claude 429·5xx 재시도 횟수 | — | `test_caps`, `test_flow_failures`, `test_live_e2e` |
| DeepSeek 비용 장부(앱 상한 $12) | ○ 동시 프로세스·끊긴 줄·재시작, CLI 직접 실행 | ○ 402·4xx·5xx·시간 초과 | ○ Mistral 콘솔 사용량과 대조 | `test_ledger`, `test_mistral` |
| 작업 상태 표시, 실패해도 기존 결과 유지, 다시 시도(흐름당 호출 상한이 남아 있을 때만 버튼 표시, 실패한 작업은 종류별 최신 1건만 보관) | ○ | ○ | — | `test_flow_failures`, `test_caps`, `test_reviews_api`, `test_live_e2e`, UI 스모크 |
| 작업 공간 분리·삭제·60분 자동 삭제 | ○ 삭제 후 조회 404 직접 확인 | — | — | `test_security`, `test_web_upload` |
| 한 페이지 3단계 화면(대화상자, 라이트·다크 테마, 모의 응답 배지, 리포트 5개 섹션 자동 시작) | ○ Node 가짜 DOM 스모크 214개(실제 브라우저 점검은 ① 업로드 도움말만) | — | — | `tests/ui/ui_smoke.mjs` |
| 다른 컴퓨터의 브라우저에서 접속(공인 주소) | — | — | ○ ACG 규칙·0.0.0.0 바인딩 승인 후 | — |
| 실제·공개 Claude Code 세션 처리 | — 합성 세션으로만 시험 | — | ○ Trace Commons 다운로드 승인 후 | `test_fixtures`, `test_parser` |

확인하지 않은 것:
- 실제 브라우저에서의 화면 점검(배치, 키보드만으로 끝까지 진행, 대비, 화면 낭독기 알림, 다크 테마)은 하지 않았습니다. 단, ① 업로드 화면의 도움말(접힘, 운영체제 전환, 복사 버튼, 시연 파일 버튼)은 2026-11-10 headless Chromium으로 390px·1280px, 라이트·다크, 키보드만으로 점검했습니다. UI 스모크 시험은 실제 `index.html`·`app.js`를 Node의 작은 가짜 DOM에서 돌리는 방식입니다.
- 실제 제공자 응답의 품질·지연·비용(PROJECT.md 9절의 품질 평가)은 측정하지 않았습니다.

PROJECT.md 9절 완료 기준과의 대응:
- 자동 시험·직접 실행으로 확인(모의 응답·모의 서버 기준): #1, #2, #3, #5, #6, #7, #8, #9, #10, #11, #12, #15, #16, #17, #18(이 문서의 명령. 빈 환경에서의 새 설치는 제외).
- 일부만 확인: #4·#14는 API 수준 자동 시험만(두 탭·뒤로 가기·두 브라우저 시험은 안 함), #13은 가짜 MCP 서버의 지연·실패만(★ 실제 Ollama 주소를 닫은 시험은 안 함).
- 미확인: #19(실제 브라우저의 키보드·대비·낭독기), #20~#23(★ 실제 서버와 승인 필요).

구현하지 않은 것(계획상 범위 밖이거나 후순위): 3단계 피드백에 대한 독립 검토, 다음 과제 자동 생성과 여러 세션 추적, 로그인·인증·TLS, Claude Code 외 로그 형식, 사용자 코드·테스트 실행.

## 빠른 시작

### 실제 시연 (`.env` 사용)

`/root/agency-loop/.env`가 준비되어 있습니다(권한 600, git 제외). 들어 있는 설정은 이렇습니다.
- 모든 제공자 실제(live) 모드
- 수업 서버 주소: local-rag BGE-M3, local-llm-chat MCP(HTTP)
- 127.0.0.1:8000 바인딩
- 비용 장부 경로(`var/usage.jsonl`, 2026-11-10 초기화함)

API 키 3개(`ANTHROPIC_API_KEY`, `CLOVA_API_KEY`, `MISTRAL_API_KEY`)만 채우고 실행합니다.

```bash
cd /root/agency-loop
set -a; . ./.env; set +a
.venv/bin/python -m agency_loop        # http://127.0.0.1:8000
```

- 키가 빈 제공자는 요청을 보내기 전에 '인증 정보를 확인할 수 없습니다'로 멈추고 과금되지 않습니다(2026-11-10 확인).
- 수업 서버 두 곳은 키 없이 실제로 동작합니다(2026-11-10 확인).
- 키 없이 화면만 보려면 실행 전에 `export AGENCY_ANTHROPIC_MODE=mock AGENCY_CLOVA_MODE=mock AGENCY_MISTRAL_MODE=mock`을 실행합니다.
- 다른 PC에서 접속하려면 ACG에 TCP 8000 규칙을 추가한 뒤 `.env`의 `AGENCY_HOST`와 `AGENCY_ALLOWED_HOSTS`를 파일 안 설명대로 바꿉니다.

### 개발용

준비물: 시스템 Python 3.12(uv가 Python을 따로 내려받지 않도록 설정됨), uv(확인한 버전 0.13.7), Node.js(문법 검사와 UI 스모크 시험에만 필요, 확인한 버전 22.23.4). 명령은 모두 프로젝트 폴더에서 실행합니다(패키지를 설치하지 않고 폴더에서 바로 실행하며, 비용 장부 기본 경로도 현재 폴더 기준입니다).

```bash
cd /root/agency-loop
uv sync --locked
```

> 확인: 이미 설치된 `.venv`에서 네트워크 없이 `uv sync --locked --offline`으로 실행 → `Resolved 41 packages`, `Checked 38 packages`, 변경 없음. 빈 환경에서 처음 설치하는 경우(PyPI에서 내려받기)는 확인하지 않았습니다.

```bash
.venv/bin/python -m agency_loop
```

> 확인: `Uvicorn running on http://127.0.0.1:8000`, 모든 제공자 mock. Ctrl+C로 정상 종료. 서버 로그에는 시작·종료 줄만 남고 요청 줄은 남지 않습니다.

브라우저에서 <http://127.0.0.1:8000>을 엽니다. 서버는 `127.0.0.1`에만 바인딩하므로 브라우저가 다른 컴퓨터에 있으면 이 주소로 바로 열리지 않습니다. 외부 접속은 아래 '실제 연결 전 승인이 필요한 작업'의 ACG 규칙과 0.0.0.0 바인딩이 필요합니다(SSH 포트 전달로 여는 방법도 있지만 확인하지 않았습니다).

다른 터미널에서 상태와 제공자 모드를 볼 수 있습니다.

```bash
curl -s http://127.0.0.1:8000/api/health
```

> 확인: `{"ok":true,"modes":{"anthropic":"mock","clova":"mock","mistral":"mock","embed":"mock","local_llm":"mock"}}`

### 데모: `fixtures/sessions/demo_weather.jsonl`

합성 세션(외부 날씨 API를 부르는 작은 Python CLI, 사람 요청 4개)으로 전체 흐름을 볼 수 있습니다. 모든 제공자가 mock이면 어디에도 보내지 않습니다. ① 화면의 '시연 파일로 바로 해 보기' 버튼은 이 파일을 `GET /api/demo-session`(이 파일 하나만 내주는 고정 경로)으로 받아 일반 업로드와 같은 방법으로 올립니다.

1. ① 세션 업로드에서 파일을 고르고 '분석 시작'. 분석 가능 범위: 사람 요청 4개, 코드 변경·테스트 기록 있음, 가린 항목 비밀값 1·이메일 1·IP 1, 깨진 줄 0.
2. ② 후보 3개: c1(2턴, 외부 호출, 예측 가능), c2(4턴, 오류 처리, 테스트 기록 없음), c3(1턴, 외부 호출, 예측 가능). c1을 고르고 자기평가를 입력해 '선택 확정'(예: 1·1·2 → 난이도 표준). 이때는 사전 맥락 근거 4개만 보입니다.
3. 2-2 기록 확인 예측을 제출하면 2-3에서 명령·종료 코드(1, 명시)·요약 줄·출력 발췌와 일치/불일치/판단 보류가 나옵니다.
4. 2-4 심층 분석: 전송 미리보기에서 보낼 곳을 고르면 모의 분석(진술 6개: 확인된 기록 3, 모델 추정 2, 검증 필요 1), 모의 Local LLM 요약, '검색 생략'(사람 요청 6개 미만)이 나옵니다. 심층 분석이 끝나면 개발 과정 분석 리포트가 자동으로 만들어져 화면에 개발 세션 요약 → 프롬프트 개선 포인트 → 판단·검증 보완 포인트 → 다음 개발을 위한 개선 행동 순으로 표시됩니다(전송 목적은 심층 분석 미리보기에 함께 적혀 있고, 그 보완 포인트 요약은 What-if·피드백 생성에 재사용됩니다). 원하면 HCX-007·DeepSeek 독립 검토와 모델 비교.
5. ③ What-if 생성 → 네 항목 답변 → 미리보기·동의 → 첫 피드백 → 다시 제출 → 최종 피드백(다음 난이도 제안).
6. 상단 '작업 공간 삭제'.

> 확인 방법: 이 서버에 브라우저가 없어, 위 순서를 화면과 같은 API 호출로 `127.0.0.1:8000`에 실행했습니다. 업로드부터 분석·요약·독립 검토 2개·What-if·피드백 2회까지 모두 완료, 세 번째 피드백은 409, 삭제 후 조회는 404였습니다. 화면 동작은 아래 UI 스모크 시험으로 확인했습니다. 사람 요청이 8개인 `many_requests.jsonl`로는 임베딩 검색이 실행되어 '검색 결과 없음'이 나왔습니다(모의 임베딩은 의미를 보지 않음).

## 제공자 모드와 환경변수

설정은 모두 서버를 시작하는 셸의 환경변수로 받습니다(`agency_loop/config.py`). 앱은 `.env` 파일을 읽지 않으며, [`.env.example`](../.env.example)은 변수 이름과 기본값 예시입니다(키 값은 비어 있음). 쓰는 법: `cp .env.example .env`로 복사해 값을 넣고, `set -a; . ./.env; set +a` 뒤에 서버를 시작합니다.

모드(제공자마다 `mock` / `live` / `off`, 기본 `mock`):
- `mock`: 네트워크 없이 결정적 모의 응답. 결과마다 '모의 응답(개발용)' 배지, 모델 이름 `mock-…`, 전송 미리보기의 목적지는 '모의 응답(실제로 보내지 않음)'.
- `live`: 동의한 내용만 실제 제공자에 보냅니다. **승인 전에는 켜지 않습니다.** 실제 서비스에 live로 연결해 본 적은 아직 없습니다.
- `off`: 호출하지 않습니다. 미리보기에 '꺼져 있음'으로 나오고 동의할 수 없습니다.
- 화면 아래에 제공자별 모드가 표시됩니다. 잘못된 값이면 서버가 시작하지 않습니다(`AGENCY_ANTHROPIC_MODE=maybe`로 확인: `AGENCY_ANTHROPIC_MODE must be one of ('mock', 'live', 'off')`, 종료 코드 1).

```bash
AGENCY_CLOVA_MODE=off AGENCY_MISTRAL_MODE=off .venv/bin/python -m agency_loop
```

> 확인: `/api/health`가 `"clova":"off","mistral":"off"`, 나머지는 `mock`.

| 제공자(용도) | 모드 변수 | live에 필요한 것 |
|---|---|---|
| Anthropic Claude Opus 5.5(심층 분석, What-if, 피드백 2회) | `AGENCY_ANTHROPIC_MODE` | `ANTHROPIC_API_KEY` |
| CLOVA HCX-007(독립 검토)과 CLOVA 리랭커(인용 근거 선택). 한 변수로 함께 켜고 끕니다 | `AGENCY_CLOVA_MODE` | `CLOVA_API_KEY` |
| DeepSeek V4.1 Flash, Mistral 호스팅(독립 검토) | `AGENCY_MISTRAL_MODE` | `MISTRAL_API_KEY`, 비용 장부 파일(아래) |
| BGE-M3 임베딩, local-rag 수업 서버(관련 요청 검색) | `AGENCY_EMBED_MODE` | `AGENCY_OLLAMA_EMBED_URL`(비어 있으면 호출하지 않고 검색 실패로 처리) |
| Local LLM 요약, local-llm-chat 수업 서버(MCP `summarize_text`) | `AGENCY_LOCAL_LLM_MODE` | `AGENCY_MCP_URL`(HTTP로 떠 있는 수업 MCP 주소, 이 경우 모델은 MCP 서버 설정이 정함) 또는 stdio 실행용 `AGENCY_OLLAMA_CHAT_URL`·`AGENCY_MCP_COMMAND` |

API 키(`ANTHROPIC_API_KEY`, `CLOVA_API_KEY`, `MISTRAL_API_KEY`)는 서버 환경변수로만 넣습니다. 각 제공자 클라이언트가 호출할 때만 읽고, 브라우저 코드·API 응답·로그·문서에는 들어가지 않습니다. MCP 자식 프로세스에는 키 없이 `OLLAMA_BASE_URL`(= `AGENCY_OLLAMA_CHAT_URL`), `OLLAMA_MODEL`, `PATH`, `PYTHONDONTWRITEBYTECODE=1`만 넘깁니다. 키가 없으면 요청을 보내기 전에 '인증 정보를 확인할 수 없습니다(서버 환경변수)'로 끝납니다(자동 시험). Anthropic SDK가 따로 읽는 `ANTHROPIC_BASE_URL`은 쓰지 않고 `AGENCY_ANTHROPIC_BASE_URL`만 씁니다. SDK가 모든 요청 헤더에 섞는 `ANTHROPIC_CUSTOM_HEADERS`는 `python -m agency_loop`으로 시작할 때 프로세스 환경에서 지웁니다. 그래도 서버는 필요한 변수만 둔 깨끗한 환경에서 실행하는 것을 권합니다.

그 밖의 `AGENCY_*` 변수(기본값):

| 묶음 | 변수 = 기본값 |
|---|---|
| 웹 | `AGENCY_HOST=127.0.0.1`, `AGENCY_PORT=8000`, `AGENCY_ALLOWED_HOSTS=127.0.0.1:<포트>,localhost:<포트>`(허용 Host 헤더, 쉼표 구분. POST·DELETE의 Origin도 `http://<이 값>`이어야 하며, 값을 주면 기본값을 대체) |
| 작업 공간 | `AGENCY_WORKSPACE_TTL_SEC=3600`(미사용 뒤 삭제, 쓸 때마다 다시 셈), `AGENCY_MAX_WORKSPACES=50`, `AGENCY_UPLOAD_RATE_PER_10MIN=10`(IP당), `AGENCY_BUNDLE_MAX_CHARS=12000`(모델에 보내는 근거 묶음) |
| Claude | `AGENCY_CLAUDE_MODEL=claude-opus-5-5`, `AGENCY_CLAUDE_DAILY_ATTEMPTS=300`, `AGENCY_ANTHROPIC_BASE_URL=https://api.anthropic.com` |
| CLOVA | `AGENCY_CLOVA_BASE_URL=https://clovastudio.stream.ntruss.com`, `AGENCY_HCX_THINKING_EFFORT=low`, `AGENCY_HCX_MAX_COMPLETION_TOKENS=5120`, `AGENCY_CLOVA_DAILY_CALLS=300` |
| Mistral(DeepSeek) | `AGENCY_MISTRAL_BASE_URL=https://api.mistral.ai`, `AGENCY_MISTRAL_MODEL=deepseek-v4-1-flash-1017`, `AGENCY_MISTRAL_MAX_TOKENS=8192`, `AGENCY_MISTRAL_CAP_MICRO_USD=12000000`, `AGENCY_MISTRAL_OUT_UB_TOKENS=128000`, `AGENCY_LEDGER_PATH=var/usage.jsonl` |
| 검색 | `AGENCY_OLLAMA_EMBED_URL=`(빈 값, 예: `http://<local-rag 공인 주소>:11434`), `AGENCY_RAG_MIN_REQUESTS=6`, `AGENCY_RAG_TOP_K=5`, `AGENCY_RAG_MIN_SIMILARITY=0.35` |
| Local LLM | `AGENCY_OLLAMA_CHAT_URL=`(빈 값), `AGENCY_LOCAL_LLM_MODEL=qwen3.5:9b-q8_0`, `AGENCY_MCP_COMMAND=/root/day05/local-llm-mcp/.venv/bin/python`, `AGENCY_MCP_ARGS=/root/day05/local-llm-mcp/server.py`(공백으로 나눔), `AGENCY_MCP_CWD=/root/day05/local-llm-mcp`, `AGENCY_MCP_TIMEOUT_SEC=240` |

환경변수로 바꿀 수 없는 값(코드 고정, 바꾸면 `config.py`와 계약 문서를 함께 고침): 업로드 32MiB(2026-11-10 16MiB에서 상향), 근거 발췌 4,000자, Claude effort·`max_tokens`(Ⓐ high 32,000 · Ⓔ 리포트 high 24,000 · Ⓑ high 24,000 · Ⓒ·Ⓓ medium 12,000), Claude 흐름당 7회 시도, DeepSeek 단가(100만 토큰당 입력 $0.30·출력 $1.20), 동시 분석 2건·Local LLM 동시 1건. 숫자 기본값 대부분은 측정 전 초기값입니다(PROJECT.md 부록 C).

## 비용 장부 (DeepSeek)

DeepSeek(Mistral) 호출만 앱 장부로 비용을 막습니다(Claude·CLOVA는 호출 횟수 상한만 있음). 장부는 `AGENCY_MISTRAL_MODE=live`일 때만 쓰고, mock에서는 읽지도 쓰지도 않습니다.

- 파일: 기본 `var/usage.jsonl`(현재 폴더 기준, git 제외). 서버와 CLI가 같은 경로를 써야 합니다. 한 줄에 기록 하나, 메타데이터만(`op`, 호출 ID, 금액, 토큰 수, 시각), 권한 600. 세션 내용·프롬프트·응답은 쓰지 않습니다. 금액 단위는 마이크로달러(1달러 = 1,000,000)입니다.
- 상한: 앱 누적 세전 **$12**(`AGENCY_MISTRAL_CAP_MICRO_USD=12000000`). 세금·결제 방식을 확인하기 전에는 올리지 않습니다. 앱 밖 사용량도 있으므로 Mistral 콘솔의 잔액·사용량은 따로 확인합니다.

예약 규칙:
1. 보내기 전에 예약합니다. 예약액(마이크로달러) = ⌈(요청 본문 바이트 + 4,096) × 0.3⌉ + ⌈max(`max_tokens`, 128,000) × 1.2⌉. 0.3과 1.2는 토큰당 단가(100만 토큰당 입력 $0.30, 출력 $1.20)입니다. 요청 바이트 수를 입력 토큰 수의 상한으로 쓰고, `max_tokens`(8,192)가 추론 토큰까지 제한하는지 확인되기 전에는 출력을 128k로 잡습니다. 1,000바이트 요청이면 155,129마이크로달러(약 $0.16)입니다.
2. 지출(정산된 호출은 실제 금액, 정산되지 않은 호출은 예약액) + 새 예약액이 상한을 넘으면 보내지 않습니다('비용 상한에 도달해 호출하지 않았습니다').
3. 응답의 `usage.prompt_tokens`·`completion_tokens`로 정산합니다.
4. 확실히 보내지 않은 경우(연결 실패)와 4xx 거절만 0으로 해제합니다. 시간 초과·5xx·읽을 수 없는 응답·usage 없음은 예약액을 지출로 남깁니다(덜 세지 않고 더 셉니다).
5. 정산액이 예약액보다 크면 초과로 기록하고, 운영자가 `ack`할 때까지 모든 예약을 거절합니다.
6. 402(잔액 없음)를 받으면 서버를 다시 시작할 때까지 DeepSeek를 끕니다. 장부 파일이 없으면 init 안내가 나오고, 파일이 손상되면 운영자 확인을 기다립니다.
7. 작업마다 파일을 잠그고 기록은 fsync로 디스크에 확정하며, 끊긴 마지막 줄은 다음 작업에서 잘라 냅니다. 여러 프로세스가 동시에 예약해도 상한을 넘지 않는지 자동 시험으로 확인했습니다.

```bash
.venv/bin/python -m agency_loop.ledger init             # 빈 장부를 권한 600으로 만듦. 이미 있으면 거절(기록을 지우지 않음)
.venv/bin/python -m agency_loop.ledger status           # spent, unsettled, cap, remaining, halted, overruns(마이크로달러)
.venv/bin/python -m agency_loop.ledger ack <call_id>    # status의 overruns에 있는 초과 사용을 확인하고 예약을 다시 허용
```

> 확인: 프로젝트의 `var/usage.jsonl`은 아직 만들지 않았고, `AGENCY_LEDGER_PATH`를 임시 파일로 바꿔 실행했습니다. init 전 status → `장부를 사용할 수 없습니다(missing)…`(종료 코드 1). init → 권한 600 빈 파일. init 다시 → `장부가 이미 있습니다…`(1). status → `{"spent": 0, "unsettled": 0, "cap": 12000000, "remaining": 12000000, "halted": false, "overruns": []}`. 예약 155,129를 240,300으로 정산해 초과를 만든 뒤 status → `"halted": true`(다음 예약 거절). `ack <그 호출 ID>` → `초과 사용을 확인했습니다…`(0). status → `"halted": false`. 인자가 틀리면 사용법을 출력하고 종료 코드 2.

DeepSeek를 live로 켜기 전에 운영자가 프로젝트 폴더에서 `init`을 한 번 실행합니다.

## 시험

시험은 실제 외부 서비스를 부르지 않습니다. live 어댑터는 `127.0.0.1`에서만 도는 모의 HTTP 서버(`tests/mock_servers.py`: Anthropic 스트리밍, CLOVA HCX-007·리랭커, Mistral, Ollama 임베딩·대화)와 가짜 MCP 서버(`tests/fake_mcp_server.py`)로 시험합니다. `tests/test_live_e2e.py`는 모든 제공자를 live로 두고 모의 서버에 붙여 업로드부터 피드백까지 HTTP로 진행하며, 요청 본문, 예측·동의 전 전송 0건, 비용 장부, 보낸 내용에 가짜 비밀값이 없음, 실패해도 이전 결과가 남음을 확인합니다. 시험 데이터는 합성 세션 `fixtures/sessions/*.jsonl`입니다.

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

> 결과(2026-11-10 마지막 실행): `Ran 335 tests`(약 15초), `OK (skipped=1)`. 건너뛴 1개는 아래 opt-in 수업 MCP 시험입니다. `-v`를 붙여도 같은 결과입니다.

```bash
.venv/bin/ruff check .
```

> 결과: `All checks passed!`

```bash
node --check agency_loop/static/app.js
```

> 결과: 출력 없음, 종료 코드 0.

UI 스모크 시험: 실제 `index.html`·`app.js`를 Node의 작은 가짜 DOM에서 돌려(npm 패키지 없음), 모의 모드로 띄운 서버에 업로드부터 최종 피드백까지, 그리고 거절·실패·다시 시도·근거 부족 경로를 진행합니다.

```bash
AGENCY_PORT=8767 .venv/bin/python -m agency_loop &
curl -s --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8767/api/health
node tests/ui/ui_smoke.mjs http://127.0.0.1:8767
kill %1
```

> 결과: `214 passed, 0 failed`, 서버 정상 종료. 업로드를 여러 번 하므로(IP당 10분 10회 제한) 반복 실행할 때는 서버를 다시 시작합니다.

opt-in 수업 MCP 시험: 수업 폴더의 `server.py`를 그 폴더의 `.venv` Python으로 MCP stdio 실행하고, Ollama 대신 `127.0.0.1`의 모의 서버를 써서 `summarize_text`를 끝까지 호출합니다. 실제 Ollama에는 연결하지 않고, 시험 전후 `/root/day05` 아래 파일 상태가 같은지 확인합니다(수업 폴더는 실행만 하고 바이트코드도 쓰지 않음). 환경변수 없이 돌리면 건너뛰고, 수업 폴더가 없으면 실패합니다.

```bash
AGENCY_TEST_COURSE_MCP=1 .venv/bin/python -m unittest tests.test_course_mcp_optin -v
```

> 결과: `Ran 1 test`(약 0.6초), `OK`.

## 보안과 한계

- **평문 HTTP, 로그인 없음.** TLS가 없어 같은 경로의 누군가가 내용과 작업 공간 쿠키를 엿볼 수 있습니다. 쿠키는 HttpOnly·SameSite=Strict지만 Secure가 아닙니다. 수업 서버(local-rag, local-llm-chat)와의 통신도 공인 IP 평문 HTTP이고 인증이 없습니다.
- **접근 통제는 IP뿐.** 외부에 열면 공유 ACG의 /32 규칙으로 접속 IP만 제한하므로, 허용된 IP에서는 누구나 쓸 수 있습니다. 앱은 Host 헤더(허용 목록 밖 400)와 POST·DELETE의 Origin(403)을 검사하고 CSP 등 보안 헤더와 `/api/*`의 `Cache-Control: no-store`를 붙이며, CORS는 열지 않습니다(Host 400, Origin 없는 POST 403, 헤더는 직접 확인).
- **메모리에만 저장.** 작업 공간(세션, 근거, 결과)은 프로세스 메모리에만 있어 서버를 다시 시작하면 사라집니다. 그래서 워커는 1개입니다. 업로드는 임시 파일 없이 받고, 디스크에 남는 것은 비용 장부 하나입니다. 하루 호출 상한 집계도 메모리라 자정과 재시작 때 0으로 돌아갑니다.
- **60분 자동 삭제.** 마지막 사용 뒤 60분(초기값, 쓸 때마다 다시 셈)이 지나면 지우고, 정리는 1분마다 돕니다. 상단 '작업 공간 삭제'로 바로 지울 수 있습니다. 서버 전체 50개, IP당 10분 10회 업로드로 제한합니다.
- **가리기의 한계.** 정해진 형식만 찾습니다: 알려진 키 형식(`sk-`, `AKIA`, `ghp_`, JWT 등), 이름에 secret·token·password·api key 등이 든 `키=값`(6자 미만 값과 자리표시자 제외), Bearer·Basic 인증값, 개인 키 블록, URL 속 계정 정보, 이메일, IPv4, 한국 전화번호, 홈 폴더 경로. 형식이 다른 비밀값, 사람 이름, 주소, 회사·서버 이름, IPv6, 해외 전화번호 등은 놓칠 수 있습니다. 보내기 직전 검사는 비밀값·개인 키·URL 계정 정보만 다시 봅니다. 그래서 수업 기간에는 공개·합성 세션만 올립니다.
- **형식 확인은 합성 세션으로만.** 파서는 흔히 관찰되는 Claude Code JSONL 구조를 따른 것이며 공식 스키마가 아닙니다. 실제·공개 세션과의 호환성과 가리기 누락률은 확인하지 않았습니다.
- **모의 응답 표시.** 기본값은 모두 mock입니다. 모의 응답은 실제 근거 ID와 원문 일부를 인용하는 결정적·일반적 문장이며 실제 모델 결과가 아닙니다. '모의 응답(개발용)' 배지, `mock-` 모델 이름, 미리보기 목적지 '모의 응답(실제로 보내지 않음)', 화면 아래 모드 표시로 구분합니다. 모의 피드백은 답변 길이·단어 같은 단순 특징으로 만든 것이라 능력 판단이 아닙니다.
- **로그.** 접근 로그를 끄고, 세션 내용·근거·프롬프트·답변·모델 출력·키를 로그에 쓰지 않습니다(위 실행들에서 서버 로그에는 시작·종료 줄만 남음). MCP 자식 프로세스의 stderr는 버립니다.
- **결과 가림의 범위.** 앱의 응답·미리보기·모델 입력만 가립니다. 사용자가 가진 원본 파일, 공개 데이터셋에 실린 결과, 응답 시간 같은 앱 밖 단서는 막지 못합니다.
- **외부 제공자 데이터 정책**(미리보기에도 표시): Anthropic은 API 데이터를 학습에 쓰지 않고 30일 안에 삭제(공식 자료). DeepSeek(Mistral Public Preview)는 약관 §4.3상 입력·출력이 학습에 쓰일 수 있고 학습 거부·ZDR이 적용되지 않음. CLOVA의 데이터 정책은 미확인.

## 실제 연결 전 승인이 필요한 작업

아래 작업은 사용자 승인 전에는 하지 않습니다. 어느 제공자든 live로 바꾸는 것도 승인 뒤에 합니다. 이 문서를 쓰면서 아래 작업은 하나도 하지 않았습니다.

1. **수업 서버 시작과 Local LLM 측정** [서버 시작·명령·모델 다운로드 승인, 수업 실습 시간 외]. PROJECT.md 10절(사용자 확인 사항)에 따르면 local-llm-chat·local-rag 서버는 꺼져 있습니다. 읽기 전용 확인(버전·CPU·RAM·디스크·모델) → `qwen3:4b-instruct-2507-q4_K_M` 기준선 → `qwen3.5:9b-q8_0`을 같은 입력·설정으로 측정(지연, 메모리, 요약 충실도, MCP 190초 제한) → 최종 모델 결정(9B Q8_0 → 9B q4_K_M → 4B). local-rag의 bge-m3 호출을 확인하고 유사도 기준값(0.35)을 보정합니다. 그 뒤 `AGENCY_EMBED_MODE`·`AGENCY_LOCAL_LLM_MODE`를 live로 하고 주소 변수를 넣습니다.
2. **과금 API 시험 호출** [과금 호출·외부 전송 승인, 공개·연습 데이터만]. Claude(키·Opus 5.5 접근·한도, 단계별 지연·출력 토큰), CLOVA(리랭커, HCX-007 생각 강도·최대 토큰·JSON 출력), DeepSeek(추론 설정, `max_tokens`가 추론 토큰을 제한하는지. 제한하면 장부의 출력 예약 기준 128k를 낮출 수 있음). DeepSeek 전에 장부 `init`이 필요합니다.
3. **Mistral 콘솔** [콘솔 확인·변경 승인]. 결제 방식·세금·$15의 성격·잔액·사용량·자동 충전 확인, 가능하면 전용 워크스페이스·키와 세전 월 $13 한도, 새 키로 DeepSeek 접근 재확인. 기존 워크스페이스 한도는 낮추지 않습니다.
4. **공유 ACG에 인바운드 TCP 8000 규칙 추가** [ACG 변경 승인]. 사용자 공인 IP/32만(강사 IP/32는 필요할 때 별도 승인), 0.0.0.0/0은 쓰지 않습니다. 공유 ACG라 모든 수업 서버에 적용되므로 다른 서버의 8000번 사용 여부를 먼저 확인하고, 변경 전후 SSH·Ollama·RAG 접속을 확인합니다. 기존 서버 간 규칙은 건드리지 않고, 프로젝트가 끝나면 추가한 규칙만 지웁니다.
5. **0.0.0.0 바인딩** [4번 규칙을 추가한 뒤에만]. `AGENCY_HOST=0.0.0.0`으로 바꾸고 `AGENCY_ALLOWED_HOSTS`에 `<claude-code 공인 주소>:8000`을 넣습니다(브라우저가 보내는 Host와 같아야 함). 평문 HTTP·인증 없음 한계는 그대로입니다. 이 설정으로는 실행해 보지 않았습니다.
6. **Trace Commons 다운로드** [다운로드 승인]. 실제 세션 형식을 확인하고 파서 호환성을 보정합니다. 화면에 출처를 표시합니다(CC BY 4.0). SWE-chat은 이용 조건을 확인한 뒤에 씁니다.

## 디렉터리 구조

```
agency-loop/
├── agency_loop/
│   ├── __main__.py      웹 서비스 실행(uvicorn, 워커 1개, 접근 로그 끔)
│   ├── config.py        AGENCY_* 환경변수 → Settings
│   ├── model.py         공유 데이터 타입
│   ├── labels.py        코드별 한국어 라벨과 오류 문구
│   ├── ledger.py        DeepSeek 비용 장부와 CLI
│   ├── ingest/          JSONL 파싱(허용 목록), 가리기, 분석 가능 범위
│   ├── decisions/       근거, 테스트 실행 판정, 후보, 기록 확인 예측
│   ├── flow/            미리보기·동의, 근거 묶음, 프롬프트·스키마, 인용 검증, 모델 비교, 자기평가,
│   │                    지시 점검, 검색, 호출 상한, 작업 파이프라인
│   ├── providers/       Claude·CLOVA·Mistral·Ollama 임베딩·MCP 어댑터, 모의 제공자, 레지스트리
│   ├── web/             Starlette 앱과 라우트, 보안 검사, 업로드, 작업 공간, 작업 실행, 응답 직렬화
│   └── static/          index.html, app.css, app.js(한 페이지 3단계 화면), fonts.css·fonts/(MaruBuri), img/(장식 SVG)
├── docs/ARCHITECTURE.md 개발 계약
├── fixtures/            합성 세션(sessions/*.jsonl), 생성 스크립트, 설명(README.md)
├── tests/               unittest, 모의 HTTP 서버, 가짜 MCP 서버, ui/ui_smoke.mjs
├── scripts/             (비어 있음)
├── var/                 비용 장부 기본 위치(usage.jsonl, git 제외, 아직 없음)
├── PROJECT.md           제품 기획
├── README.md            프로젝트 소개
├── docs/OPERATIONS.md   이 문서
├── pyproject.toml, uv.lock, .python-version
└── .env.example         환경변수 이름과 기본값 예시(키 값 없음)
```
