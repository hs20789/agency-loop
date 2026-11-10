"""Korean UI labels for every code the API returns. The API sends both the code and its label."""

CATEGORY = {
    "error_handling": "오류 처리",
    "input_validation": "입력 검증",
    "external_call": "외부 호출",
    "dependency_config": "의존성·설정 변경",
    "assumption": "가정이 드러난 결정",
    "design_choice": "설계·구현 방식",
}

# Card titles are fixed templates per category, never free text from the session (blinding).
CATEGORY_TITLE = {
    "error_handling": "오류 처리 방식에 관한 결정",
    "input_validation": "입력 검증에 관한 결정",
    "external_call": "외부 호출 방식에 관한 결정",
    "dependency_config": "의존성·설정 변경에 관한 결정",
    "assumption": "가정을 바탕으로 한 결정",
    "design_choice": "설계·구현 방식에 관한 결정",
}

# Result-independent candidate signals (computed only from requests, AI text and code edits before the decision).
SIGNALS = {
    "try_except": "예외 처리 코드 추가",
    "raise_error": "오류 발생 코드 추가",
    "input_check": "입력값 확인 코드 추가",
    "http_call": "외부 API·네트워크 호출",
    "timeout_retry": "시간 제한·재시도 설정",
    "file_or_db_io": "파일·DB 입출력",
    "dependency_change": "의존성 변경",
    "config_change": "설정 파일 변경",
    "assumption_language": "AI 설명에 가정 표현",
    "alternative_language": "AI 설명에 대안·선택 표현",
    "new_function": "새 함수·클래스 추가",
    "large_change": "큰 코드 변경",
}

EVIDENCE_LEVEL = {"sufficient": "충분", "partial": "부분", "insufficient": "부족"}

EVIDENCE_KIND = {
    "user_request": "사용자 요청",
    "assistant_text": "AI 설명",
    "code_edit": "코드 변경",
    "command": "명령 실행",
    "command_output": "명령 출력",
    "tool_other": "기타 도구 기록",
}

# Statement-level grades (ARCHITECTURE §6.3). Never mix with evidence levels.
GRADE = {
    "recorded": "확인된 기록",
    "inferred": "모델 추정",
    "needs_verification": "검증 필요",
    "citation_unverified": "인용 미확인",
}

TEST_STATUS = {
    "none": "기록 없음",
    "ran_unknown": "실행됨·결과 확인 불가",
    "recorded_pass": "기록된 통과",
    "recorded_fail": "기록된 실패",
    "other_version": "다른 코드 버전의 결과",
}

RETRO_REASON = {
    "no_test": "테스트 기록 없음",
    "unsupported_runner": "지원하지 않는 실행기",
    "code_changed": "이후 코드 변경",
    "no_exit_code": "종료 코드 없음",
    "truncated": "출력 잘림",
    "interrupted": "실행 중단",
    "inconsistent": "종료 코드와 요약 불일치",
}

PREDICTION_CHOICE = {"pass": "통과", "fail": "실패", "unknown": "판단 불가"}
PREDICTION_LABEL = {"match": "일치", "mismatch": "불일치", "withheld": "판단 보류"}

TIER = {"basic": "기본", "standard": "표준", "advanced": "심화"}
AXIS = {"understanding": "이해", "verification": "검증", "control": "통제"}
SELF_RATING = {0: "아직 어렵다", 1: "부분적으로", 2: "할 수 있다"}
AXIS_EVIDENCE = {"shown": "드러남", "partial": "일부", "not_shown": "드러나지 않음"}
NEXT_DIFFICULTY = {"easier": "쉽게", "same": "같게", "harder": "어렵게"}

ANSWER_FIELD = {
    "expected_behavior": "예상 결과",
    "potential_issue": "잠재 문제",
    "ai_instruction": "AI에게 줄 지시",
    "verification": "검증 방법",
}

COMPARISON = {
    "same_evidence": "같은 근거를 지적",
    "only_one": "한쪽만 지적",
    "different": "비교 불가(유효한 근거 ID 없음)",
}

MASKED = {
    "secret": "비밀값",
    "private_key": "개인 키",
    "url_credential": "URL 속 계정 정보",
    "email": "이메일",
    "ip": "IP 주소",
    "phone": "전화번호",
    "home_path": "홈 폴더 경로(사용자 이름)",
}

STAGE = {
    "analysis": "심층 분석",
    "review:hcx": "HCX-007 독립 검토",
    "review:deepseek": "DeepSeek 독립 검토",
    "feedback1": "첫 피드백",
    "feedback2": "최종 피드백",
}

SEARCH_STATE = {
    "skipped": "검색 생략",
    "no_results": "검색 결과 없음",
    "insufficient": "근거 부족",
    "done": "검색 완료",
    "failed": "검색 실패(규칙 엔진 근거로 진행)",
}

JOB_STATUS = {
    "queued": "대기",
    "running": "진행",
    "streaming": "응답 수신 중",
    "validating": "검증 중",
    "done": "완료",
    "failed": "실패",
}

PROVIDER = {
    "anthropic": "Anthropic Claude",
    "hcx": "CLOVA HCX-007",
    "deepseek": "DeepSeek V4.1 Flash (Mistral)",
    "local_rag": "BGE-M3 임베딩 (local-rag 수업 서버)",
    "clova_rerank": "CLOVA 리랭커",
    "local_llm": "Local LLM 요약 (local-llm-chat 수업 서버, MCP)",
}

ERROR = {
    "too_large": "파일이 32MiB를 넘습니다. 더 작은 세션 파일을 올려 주세요.",
    "bad_format": "Claude Code 세션 JSONL 형식이 아닙니다.",
    "no_events": "분석할 수 있는 대화·코드 기록이 없습니다.",
    "rate_limited": "요청이 너무 많습니다. 잠시 뒤 다시 시도해 주세요.",
    "server_busy": "서버 사용량 제한에 도달했습니다. 잠시 뒤 다시 시도해 주세요.",
    "no_workspace": "작업 공간이 없거나 만료되었습니다. 세션을 다시 올려 주세요.",
    "not_found": "찾을 수 없습니다.",
    "locked": "후보는 이미 선택되어 바꿀 수 없습니다. 다른 후보는 세션을 다시 올려 분석해 주세요.",
    "consent_missing": "전송 동의가 없어 호출하지 않았습니다.",
    "cap_exceeded": "이 흐름의 호출 상한에 도달했습니다.",
    "daily_cap": "서버 사용량 제한(하루 호출 상한)에 도달했습니다.",
    "budget_exceeded": "비용 상한에 도달해 호출하지 않았습니다.",
    "halted": "비용 장부가 운영자 확인을 기다리고 있어 호출하지 않습니다.",
    "secret_detected": "보낼 내용에서 비밀값으로 보이는 문자열이 발견되어 전송을 막았습니다.",
    "refusal": "모델이 이 요청을 처리하지 않았습니다. 이미 찾은 근거는 그대로 유지됩니다.",
    "incomplete": "응답이 출력 한도에 걸려 불완전합니다. 다시 시도할 수 있습니다.",
    "invalid_output": "모델 응답 형식을 확인할 수 없습니다.",
    "timeout": "응답 시간이 초과되었습니다.",
    "connection": "서버에 연결할 수 없습니다.",
    "auth": "인증 정보를 확인할 수 없습니다(서버 환경변수).",
    "provider_rate_limited": "제공자 요청 한도에 걸렸습니다. 같은 요청을 자동으로 반복하지 않습니다.",
    "server_error": "제공자 서버 오류입니다.",
    "bad_request": "제공자가 요청을 거절했습니다.",
    "disabled": "이 제공자는 꺼져 있습니다.",
    "insufficient_evidence": "근거가 부족해 모델을 호출하지 않았습니다.",
    "precondition": "이전 단계를 먼저 완료해 주세요.",
    "bad_host": "허용되지 않은 주소로 들어온 요청입니다.",
    "bad_origin": "이 페이지에서 보낸 요청이 아니어서 처리하지 않았습니다.",
    "invalid_request": "요청 형식이 올바르지 않습니다.",
    "internal": "내부 오류로 작업을 마치지 못했습니다. 이미 얻은 결과는 유지됩니다.",
    "ledger_missing": "비용 장부 파일이 없습니다. 운영자가 서버에서 `python -m agency_loop.ledger init`을 먼저 실행해야 합니다.",
    "not_eligible": "이 후보는 기록 확인 예측 대상이 아닙니다.",
    "already_submitted": "이미 제출했습니다. 제출한 답은 바꿀 수 없습니다.",
    "concurrency": "다른 분석이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.",
}

SECTION = {
    "reasons": "결정 이유",
    "alternatives": "대안",
    "assumptions": "검증되지 않은 가정",
    "risks": "잠재 위험",
    "verification_points": "확인할 점",
}

# Development-process report (기능 1의 개발 과정 분석 리포트): labels for the model's report codes.
REPORT_SUMMARY = {
    "tasks": "요청한 주요 작업",
    "changes": "기록된 코드 변경",
    "decisions": "중요한 기술적 의사결정",
    "tests": "수행된 테스트와 검증",
}

PROMPT_ASPECT = {
    "clarity": "요구사항 명확성",
    "context_constraints": "맥락·제약조건 구체성",
    "exceptions": "예외 상황 고려",
    "verification": "검증·테스트 지시",
    "scope_expectation": "작업 범위와 기대 결과",
}

GAP_CONCERN = {
    "missing_verification": "예외·검증 기록 부족",
    "unclear_rationale": "구현 방식 근거 불명확",
    "requirement_test_gap": "요구사항과 테스트 사이 공백",
    "nonfunctional_risk": "보안·성능·동시성 가능성",
    "post_failure_continuation": "실패 이후 확인 부족 가능성",
}

ACTION_KIND = {
    "ask_ai": "AI에게 추가로 확인 요청",
    "requirements": "요구사항 작성 방법",
    "tests": "테스트·검증 방법",
    "alternatives": "대안 비교 시 확인 조건",
}

POINT_TYPE = {
    "judgment": "판단",
    "issue": "문제",
    "counterexample": "반례",
    "alternative": "대안",
    "missing_evidence": "근거 누락",
}
