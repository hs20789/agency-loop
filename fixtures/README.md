# 합성 세션 fixture

`fixtures/sessions/*.jsonl`은 이 프로젝트의 테스트와 데모를 위해 직접 만든 **합성 데이터**입니다. 실제 개발 세션, 실제 사람의 정보, 실제 비밀값은 들어 있지 않습니다.

- 형식은 흔히 관찰되는 Claude Code 세션 JSONL 구조를 본떠 만들었습니다. **공식 스키마가 아니며, 실제 세션 파일과의 호환성은 확인하지 않았습니다.**
- 파일은 `fixtures/make_fixtures.py`가 만듭니다. JSONL을 직접 고치지 말고 스크립트를 고친 뒤 다시 생성하세요. 같은 스크립트는 항상 같은 바이트를 만듭니다(`tests/test_fixtures.py`가 확인).

```bash
cd /root/agency-loop
.venv/bin/python fixtures/make_fixtures.py
.venv/bin/python -m unittest tests.test_fixtures -v
```

테스트에서는 `tests/helpers.py`의 `FIXTURES_DIR`, `load_bytes(name)`, `fixture_names()`, `FAKE_SECRETS`, `PAIR_MARKERS`를 씁니다.

## 공통 구조

- 주제: 외부 날씨 API(`requests`)에서 예보를 받아 출력하는 작은 Python CLI. 작업 폴더(`cwd`)는 `/home/student/weather-app`.
- user/assistant/system 기록마다 파서가 버려야 할 메타데이터가 있습니다: `parentUuid`, `uuid`, `sessionId`, `cwd`, `version`, `gitBranch`, `userType`, `isSidechain`, `timestamp`. assistant 기록에는 `requestId`, `message.id`, `message.model`, `message.usage`도 있습니다. `summary`, `file-history-snapshot` 기록은 관찰된 고유 모양을 따르므로 이 메타데이터가 없습니다.
- assistant API 응답 하나는 내용 블록마다 한 줄씩 나뉘어 기록됩니다(같은 `message.id`). demo_weather 36번째 줄만 여러 블록(text + Edit)을 한 줄에 담았습니다.
- Edit·Write·Read 입력 경로는 `/home/student/weather-app/...` 절대 경로입니다.
- `tool_result.content`는 문자열 또는 `{type: "text"}` 블록 목록입니다. 도구 결과 user 기록에는 `toolUseResult`가 붙습니다(Bash: `stdout`, `stderr`, `interrupted`, `isImage`. Edit·Write·Read·TodoWrite는 각자 관찰된 모양).
- 실패한 Bash: `is_error: true`, 내용이 `Exit code 1\n`으로 시작합니다. 성공한 Bash: `is_error: false`, 종료 코드 줄이 없습니다.
- 도메인은 예약 도메인(`*.example`, `example.com`, `example.org`)만, IP는 문서용 대역(`203.0.113.0/24`)과 `127.0.0.1`만 씁니다.

## 파일별 용도

"예상 결과"는 ARCHITECTURE §3·§4 규칙으로 계산한 값이며, 실제 `ingest`·`decisions` 모듈 테스트가 확인합니다.

- 결정 단위(코드 변경이 있는 턴)마다 기록 확인 예측 가능 여부·이유·기록된 결과: `tests/test_testruns.py`의 `README_OUTCOMES`, `tests/test_retro.py`, `tests/test_candidates.py`(후보 순서, `later_fix`)
- 마스킹·FAKE 값: `tests/test_redact.py`, `tests/test_parser.py` / 깨진 줄·빈 세션: `tests/test_parser.py`, `tests/test_web_upload.py`
- 짝 fixture의 제출 전 응답 동일성과 마커 비노출: `tests/test_security.py`, `tests/test_evidence.py`, `tests/test_retro.py`

| 파일 | 내용 | 예상 결과 |
|---|---|---|
| `demo_weather.jsonl` | 주 데모, 사람 요청 4개. ① CLI와 테스트 작성 → `pytest -q` 3 passed ② timeout·오류 처리. AI 설명에 가정("보통 5초 안에 응답한다고 가정")과 선택("예외 대신 None 반환") 표현 → weather.py·테스트 수정 → `pytest -q` 실패(`1 failed, 3 passed in 0.42s`, `/home/student/weather-app/weather.py`가 든 traceback) ③ "실패한 테스트 고쳐줘" → 이후 수정 → 4 passed ④ config.toml 작성, requirements.txt·weather.py 수정. 끝에 결과를 누설하는 summary 기록 | 2턴: 기록 확인 예측 가능(기록된 실패), `later_fix`. 1턴·3턴: 예측 가능(통과). 4턴: `no_test`. 후보는 최대 3개라 신호가 적은 3턴은 카드에 없을 수 있습니다 |
| `pair_fail.jsonl`, `pair_pass.jsonl` | 2턴 결정 직후 첫 `pytest -q` 결과 한 줄(22번째 줄)만 다릅니다. 실패: `is_error: true`, `Exit code 1`, `1 failed, 3 passed in 0.40s`, `MARKER-ONLY-IN-FAIL-OUTPUT-7Q2`. 통과: `is_error: false`, 종료 코드 줄 없음, `4 passed in 0.40s`, `MARKER-ONLY-IN-PASS-OUTPUT-3K9`. 이후 3턴 "README 정리해줘"는 README.md만 고칩니다. summary 없음. 두 대상 출력에는 마스킹될 값이 없습니다 | 두 파일 모두 2턴 후보 예측 가능. 제출 전 응답은 무작위 ID를 빼면 바이트 단위로 같아야 하고, 마커는 제출 전 응답과 서버 로그에 나오면 안 됩니다 |
| `no_tests.jsonl` | 오류 처리 수정만 있고 테스트 명령이 없습니다 | `no_test` |
| `truncated_output.jsonl` | 결정 수정 뒤 `pytest -v` 출력에 `... [1200 lines truncated] ...`, 요약 줄 없음, `Exit code 1` | `truncated` |
| `no_exit_code.jsonl` | 대상 결과가 `is_error: true`이고 `Exit code` 줄이 없습니다. 요약 줄(`1 failed, 3 passed in 0.38s`)은 일부러 남겼습니다(아래 참고) | `no_exit_code` |
| `code_changed.jsonl` | 결정 수정(weather.py) → README.md 수정(코드 파일이 아니라 결정이 아님) → `pytest -q` | `code_changed` (§4.4 "어떤 파일이든" 규칙) |
| `unsupported_runner.jsonl` | Go 파일 수정 → `go test ./...` | `unsupported_runner` |
| `secrets.jsonl` | 사람 메시지, AI 설명, Write(.env.example)·Edit(settings.py) 입력, Bash 명령, Bash 출력 곳곳에 FAKE 비밀값과 개인정보 | 마스킹 테스트용. 후보는 `no_test` |
| `many_requests.jsonl` | 요구사항이 담긴 사람 요청 8개("응답은 3초 안에", "API 키는 환경변수로", "섭씨로 표시", "실패하면 한국어로 안내" 등). 1~6턴 수정, 7턴 `pytest -q` 6 passed, 8턴 README | 검색 최소 요청 수(6) 이상. 1~5턴 `code_changed`, 6턴 예측 가능(통과) |
| `malformed.jsonl` | 정상 기록과 깨진 줄 정확히 2개: 2번째 줄 `42`(객체 아님), 6번째 줄 잘린 JSON | `bad_lines == [2, 6]`. 잘린 줄의 문구 "이 줄은 중간에 잘렸습니다"는 어디에도 남으면 안 됩니다 |
| `meta_only.jsonl` | summary, system, `isMeta` 기록만 있습니다 | 쓸 수 있는 이벤트 없음(`no_events`) |
| `runners.jsonl` | ① `python -m unittest` 통과(`Ran 5 tests in 0.010s`, `OK`) ② unittest 실패(`FAILED (failures=1)`, `Exit code 1`) ③ `npm test` Jest 실패(`Tests:       1 failed, 5 passed, 6 total`, `Exit code 1`) | 세 후보 모두 예측 가능: 통과, 실패, 실패 |

`no_exit_code.jsonl` 참고: 작업 지시에는 "요약 줄도 없음"으로 되어 있었지만, §4.4 순서에서는 요약 줄이 없으면 `complete=False`라서 `no_exit_code`보다 `truncated`가 먼저 걸립니다(그러면 `truncated_output.jsonl`과 같은 경우가 됩니다). 파일 이름대로 `no_exit_code`가 나오도록 요약 줄을 남겼습니다.

### demo_weather.jsonl의 잡음 기록 (현재 생성 기준 줄 번호)

| 줄 | 기록 | 기대 처리 |
|---|---|---|
| 1, 2 | `isMeta: true` user 기록(caveat, `<command-name>/clear</command-name>`) | `meta`로 건너뜀 |
| 3, 31 | `file-history-snapshot` | 건너뜀 |
| 5, 17, 33 / 44 | `thinking` 블록 / `redacted_thinking` 블록 | `thinking`으로 버림 |
| 13, 35 | 목록 형식 `tool_result` (나머지는 문자열) | 텍스트를 이어 붙임 |
| 15 | `isSidechain: true` user 기록 | `sidechain`으로 건너뜀(사람 요청으로 세면 턴이 밀림) |
| 16 | 사람 요청 뒤에 붙은 `<system-reminder>...</system-reminder>` | 제거 |
| 28 | `system` 기록(결과를 누설하는 hook 문구) | 건너뜀 |
| 29, 30 | `/cost` 명령과 `<local-command-stdout>` | `command_wrapper` |
| 32 | 이미지 블록 | `image`로 버림 |
| 34, 46 | Read 도구 / TodoWrite 도구(허용 목록 밖, 입력은 `{}`) | `tool_other` 근거 |
| 42 | `[Request interrupted by user]` | `interrupt_marker` |
| 55 | summary "Weather timeout test fixed" | 건너뜀. 결과를 누설하므로 어떤 응답에도 나오면 안 됩니다 |

## FAKE 값 목록

아래 값은 모두 **FAKE**입니다. 실제 서비스의 키, 계정, 사람과 관계없습니다. `tests/helpers.py`의 `FAKE_SECRETS`에 같은 값이 있고, 어떤 출력(API 응답, 모델 전송 내용, 로그)에도 원문이 나오면 안 됩니다.

| FAKE 값 | 종류 | 위치 |
|---|---|---|
| `sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE0000` | API 키 형식 | demo_weather 4턴 사람 메시지 |
| `student@example.com` | 이메일 | demo_weather 4턴 사람 메시지 |
| `203.0.113.7` | IP | demo_weather 4턴 AI 설명, secrets AI 설명·Bash 출력 |
| `/home/student/` | 홈 경로(사용자 이름) | 모든 파일의 `cwd`·도구 경로·도구 출력, secrets AI 설명 |
| `sk-ant-api03-FAKE-secrets-fixture-0000000000` | API 키 형식 | secrets 사람 메시지 |
| `hunter2hunter2` (`password=hunter2hunter2`) | 비밀번호 | secrets 사람 메시지 |
| `alice.kim@example.com` | 이메일 | secrets 사람 메시지·AI 설명·Bash 출력 |
| `010-1234-5678` | 전화번호 | secrets 사람 메시지 |
| `/Users/alice/` | 홈 경로(사용자 이름) | secrets 사람 메시지·AI 설명 |
| `ghp_FAKE0123456789FAKE0123456789FAKE0123` | GitHub 토큰 형식 | secrets AI 설명 |
| `fake-api-key-1234567890` (`API_KEY=...`) | API 키 | secrets `.env.example` Write |
| `admin:pa55word` (`https://admin:pa55word@db.example.com/x`) | URL 자격 증명 | secrets `.env.example` Write·Bash 출력 |
| `ops@example.org` | 이메일 | secrets `.env.example` Write·마지막 AI 설명 |
| `AKIAABCDEFGHIJKLMNOP` (`AWS_ACCESS_KEY_ID = "..."`) | AWS 액세스 키 형식 | secrets settings.py Edit |
| `fake-bearer-token-abcdef123456` (`Authorization: Bearer ...`) | Bearer 토큰 | secrets Bash 명령 |
| `fake-mistral-key-0123456789abcdef` (`MISTRAL_API_KEY=...`) | API 키 | secrets Bash 출력 |
| `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmYWtlLXVzZXItMDAwMSIsIm5hbWUiOiJGQUtFIn0.ZmFrZS1zaWduYXR1cmUtbm90LXJlYWw` (`SESSION_TOKEN=...`) | JWT 형식 | secrets Bash 출력 |
| `FAKEPRIVATEKEYBODYFAKEPRIVATEKEYBODYFAKEPRIVATEKEYBODY0000000000`, `NOTAREALKEYNOTAREALKEYNOTAREALKEYNOTAREALKEY00000000==` (`-----BEGIN PRIVATE KEY-----` 블록 본문) | 개인 키 형식 | secrets Bash 출력 |

마스킹 대상이 아닌 값(참고): `127.0.0.1`, `WEATHER_TOKEN=your_token_here`(secrets, 자리표시자), `export WEATHER_API_KEY=<발급받은 키>`와 `api_key = os.environ.get(...)`(many_requests, 자리표시자), `weather-web@0.1.0`(runners의 npm 버전 표기, 이메일 아님).
