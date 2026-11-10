"""agency_loop.ingest.scope: ScopeView shape and result independence (ARCHITECTURE §3.4, §7.3)."""

import json
import unittest

from agency_loop.ingest.parser import parse_session
from agency_loop.ingest.scope import build_scope
from agency_loop.model import TestRun
from tests.helpers import load_bytes
from tests.test_parser import ai, human, jsonl, result, use

SCOPE_KEYS = {
    "format", "human_requests", "has_code_edits", "has_test_records", "masked", "masked_labels", "dropped",
    "bad_lines", "notices",
}
NOTICE_CLASS = "수업 기간에는 공개·연습용 세션만 올려 주세요"
NOTICE_CODE = "저장소 전체 코드는 없고 세션에 기록된 코드만 분석합니다"


def recorded_run():
    return TestRun(
        use_idx=3, result_idx=4, command="pytest -q", runner="pytest", exit_code=0, exit_source="is_error_false",
        result="pass", summary_line="1 passed in 0.1s", complete=True, interrupted=False,
    )


class ScopeTest(unittest.TestCase):
    def test_demo_weather_scope(self):
        session = parse_session(load_bytes("demo_weather.jsonl"))
        scope = build_scope(session, [recorded_run()])
        self.assertEqual(set(scope), SCOPE_KEYS)
        self.assertEqual(scope, {
            "format": "claude_code_jsonl",
            "human_requests": 4,
            "has_code_edits": True,
            "has_test_records": True,
            "masked": {"secret": 1, "email": 1, "ip": 1},
            "masked_labels": {"secret": "비밀값", "email": "이메일", "ip": "IP 주소"},
            "dropped": [
                "생각 과정", "이미지", "세션·계정 메타데이터", "세션 요약 줄", "보조 에이전트 기록", "명령·메타 기록",
                "시스템 알림", "기타 도구 입력", "기타 기록",
            ],
            "bad_lines": 0,
            "notices": [NOTICE_CLASS, NOTICE_CODE],
        })
        blob = json.dumps(scope, ensure_ascii=False)
        for event in session.events:  # no session text in the scope
            if event.kind == "user_text":
                self.assertNotIn(event.text, blob)

    def test_bad_lines_notice(self):
        scope = build_scope(parse_session(load_bytes("malformed.jsonl")), [])
        self.assertEqual(scope["bad_lines"], 2)
        self.assertEqual(scope["notices"], [NOTICE_CLASS, "깨진 줄 2개는 제외했습니다", NOTICE_CODE])
        self.assertIs(scope["has_test_records"], False)

    def test_meta_only_scope(self):
        scope = build_scope(parse_session(load_bytes("meta_only.jsonl")), [])
        self.assertEqual((scope["human_requests"], scope["has_code_edits"]), (0, False))
        self.assertEqual(scope["dropped"], ["세션·계정 메타데이터", "세션 요약 줄", "명령·메타 기록", "기타 기록"])

    def test_masked_counts_come_from_text_only(self):
        session = parse_session(load_bytes("secrets.jsonl"))
        scope = build_scope(session, [])
        self.assertEqual(scope["masked"], session.redaction.in_text)
        self.assertNotIn("private_key", scope["masked"])  # appears only in a command output
        self.assertEqual(set(scope["masked_labels"]), set(scope["masked"]))

    def test_tool_outputs_do_not_change_the_scope(self):
        def scope_for(output, is_error):
            return build_scope(parse_session(jsonl(
                human("테스트 돌려 줘"), ai(use("t1", "Bash", {"command": "pytest -q"})), result("t1", output, is_error=is_error),
            )), [])

        quiet = scope_for("1 passed in 0.1s", False)
        noisy = scope_for("Exit code 1\npassword=hunter2hunter2 a@example.com 203.0.113.7\n1 failed in 0.1s", True)
        self.assertEqual(quiet, noisy)
        self.assertEqual((quiet["masked"], quiet["has_code_edits"]), ({}, False))

    def test_code_edits_use_the_candidate_file_filter(self):
        def has_code_edits(*paths):
            edits = [use(f"t{n}", "Edit", {"file_path": p, "old_string": "a", "new_string": "b"})
                     for n, p in enumerate(paths)]
            return build_scope(parse_session(jsonl(human("정리해 줘"), ai(*edits))), [])["has_code_edits"]

        self.assertIs(has_code_edits("README.md"), False)
        self.assertIs(has_code_edits("README.md", "uv.lock", "docs/guide.txt"), False)
        self.assertIs(has_code_edits("README.md", "config.toml"), True)
        self.assertIs(has_code_edits("src/app.py"), True)

    def test_pair_fixtures_give_identical_scope(self):
        fail = build_scope(parse_session(load_bytes("pair_fail.jsonl")), [recorded_run()])
        passed = build_scope(parse_session(load_bytes("pair_pass.jsonl")), [recorded_run()])
        self.assertEqual(json.dumps(fail, ensure_ascii=False), json.dumps(passed, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
