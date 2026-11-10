"""agency_loop.ingest.parser: allowlist extraction, turns, links, skips (ARCHITECTURE §3.1, §3.2)."""

import dataclasses
import json
import unittest

from agency_loop.ingest.parser import TRUNCATION_MARK, parse_session
from tests.helpers import FAKE_SECRETS, fixture_names, load_bytes

CWD = "/home/student/app"


def record(kind, **fields):
    """A record carrying the metadata Claude Code adds; the parser must drop all of it."""
    return {
        "parentUuid": None, "isSidechain": False, "userType": "external", "cwd": CWD, "sessionId": "sess-0001",
        "version": "2.0.14", "gitBranch": "main", "type": kind, "uuid": "uuid-0001",
        "timestamp": "2026-10-20T05:12:03.137Z", **fields,
    }


def human(content, **extra):
    return record("user", message={"role": "user", "content": content}, **extra)


def ai(*blocks, **extra):
    message = {"id": "msg_0001", "role": "assistant", "model": "claude-x", "content": list(blocks), "usage": {"input_tokens": 1}}
    return record("assistant", message=message, requestId="req_0001", **extra)


def text(value):
    return {"type": "text", "text": value}


def use(tool_id, name, tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def result(tool_id, content, *, is_error=False, interrupted=None):
    extra = {} if interrupted is None else {"toolUseResult": {"interrupted": interrupted, "stdout": "raw"}}
    return human([{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}], **extra)


def jsonl(*lines):
    return "".join((line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n" for line in lines).encode()


def dump(session):
    return json.dumps([dataclasses.asdict(event) for event in session.events], ensure_ascii=False)


class InlineParseTest(unittest.TestCase):
    def test_turns_start_at_zero_and_count_human_requests(self):
        session = parse_session(jsonl(
            ai(text("세션을 시작합니다.")),
            human("첫 요청"),
            ai(text("답변"), use("toolu_A", "Bash", {"command": "ls"})),
            result("toolu_A", "a.py"),
            human([text("두 번째 요청")]),
            ai(text("두 번째 답변")),
        ))
        self.assertEqual(
            [(e.kind, e.turn) for e in session.events],
            [("assistant_text", 0), ("user_text", 1), ("assistant_text", 1), ("tool_use", 1), ("tool_result", 1),
             ("user_text", 2), ("assistant_text", 2)],
        )
        self.assertEqual([e.idx for e in session.events], list(range(7)))
        self.assertEqual([e.line for e in session.events], [1, 2, 3, 3, 4, 5, 6])

    def test_tool_ids_are_remapped_in_order_and_results_copy_the_tool(self):
        session = parse_session(jsonl(
            human("요청"),
            ai(use("toolu_X", "Bash", {"command": "pytest -q"}), use("toolu_Y", "Edit", {"file_path": "a.py"})),
            result("toolu_Y", "edited"),
            result("toolu_X", [text("line 1"), text("line 2")], is_error=True, interrupted=False),
            result("toolu_orphan", "?"),
        ))
        uses = [(e.tool_use_id, e.tool) for e in session.events if e.kind == "tool_use"]
        results = [
            (e.tool_use_id, e.tool, e.text, e.is_error, e.interrupted) for e in session.events if e.kind == "tool_result"
        ]
        self.assertEqual(uses, [("tu1", "Bash"), ("tu2", "Edit")])
        self.assertEqual(results, [
            ("tu2", "Edit", "edited", False, None),
            ("tu1", "Bash", "line 1\nline 2", True, False),
            ("tu3", None, "?", False, None),
        ])
        self.assertNotIn("toolu_", dump(session))

    def test_skipped_records_and_dropped_parts_are_counted_and_not_kept(self):
        session = parse_session(jsonl(
            {"type": "summary", "summary": "결과를 누설하는 요약", "leafUuid": "uuid-0001"},
            {"type": "file-history-snapshot", "messageId": "m-1", "snapshot": {}},
            record("system", content="hook: pytest exited with status 1"),
            {"type": "queue-operation", "content": "대기열 내용"},
            human("보조 에이전트 요청", isSidechain=True),
            human("<local-command-caveat>Caveat</local-command-caveat>", isMeta=True),
            human("이전 대화 요약: 테스트가 통과했다", isCompactSummary=True),
            human("<command-name>/cost</command-name>"),
            human("<local-command-stdout>Total cost: $0.21</local-command-stdout>"),
            human("<bash-input>pytest</bash-input>"),
            human([text("[Request interrupted by user for tool use]")]),
            human("<system-reminder>only a reminder</system-reminder>"),
            human([
                text("요청<system-reminder>\nIDE에서 파일을 열었습니다\n</system-reminder>"),
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo"}},
            ]),
            ai({"type": "thinking", "thinking": "숨은 생각"}, {"type": "redacted_thinking", "data": "xyz"},
               use("toolu_T", "TodoWrite", {"todos": [{"content": "할 일 목록"}]})),
        ))
        report = session.report
        self.assertEqual(report.skipped_record_types, {
            "summary": 2, "file-history-snapshot": 1, "system": 1, "other": 1, "sidechain": 1, "meta": 1,
        })
        self.assertEqual(report.dropped, {
            "meta_fields": 11, "command_wrapper": 3, "interrupt_marker": 1, "system_reminder": 2, "image": 1,
            "thinking": 2, "other_tool_input": 1,
        })
        self.assertEqual([(e.kind, e.turn, e.text, e.tool_input) for e in session.events], [
            ("user_text", 1, "요청", None), ("tool_use", 1, "", {}),
        ])
        blob = dump(session)
        for needle in ("누설", "hook", "대기열", "보조 에이전트", "Caveat", "이전 대화", "cost", "bash-input", "Request interrupted",
                       "reminder", "IDE", "iVBOR", "숨은 생각", "xyz", "할 일", "uuid", "sess-0001", "req_0001",
                       "msg_0001", "2026-", "claude-x", "external", "main"):
            self.assertNotIn(needle, blob)

    def test_inputs_are_allowlisted_and_paths_relative_to_the_first_cwd(self):
        other = "/home/student/other"
        session = parse_session(jsonl(
            human("요청"),
            ai(
                use("t1", "Edit", {"file_path": f"{CWD}/src/w.py", "old_string": "a", "new_string": "b", "replace_all": False, "extra": "x"}),
                use("t2", "MultiEdit", {"file_path": f"{CWD}/m.py", "edits": [{"old_string": "x", "new_string": "y", "replace_all": True, "junk": 1}, "no"]}),
                use("t3", "Write", {"file_path": "/etc/hosts", "content": 123}),
                use("t4", "NotebookEdit", {"notebook_path": f"{CWD}/n.ipynb", "new_source": "print(1)", "cell_id": "c1"}),
                use("t5", "Read", {"file_path": f"{CWD}/README.md", "offset": 10}),
                use("t6", "Grep", {"pattern": "def ", "path": CWD, "output_mode": "content"}),
                use("t7", "Glob", {"pattern": "**/*.py", "path": f"{CWD}2/x"}),
                use("t8", "Bash", {"command": f"cat {CWD}/a.txt", "description": "보기", "timeout": 5}),
                use("t9", "WebFetch", {"url": "https://example.com", "prompt": "요약"}),
            ),
            ai(use("t10", "Read", {"file_path": f"{other}/y.py"}), cwd=other),
        ))
        self.assertEqual([e.tool_input for e in session.events if e.kind == "tool_use"], [
            {"file_path": "src/w.py", "old_string": "a", "new_string": "b", "replace_all": False},
            {"file_path": "m.py", "edits": [{"old_string": "x", "new_string": "y", "replace_all": True}]},
            {"file_path": "/etc/hosts"},
            {"notebook_path": "n.ipynb", "new_source": "print(1)"},
            {"file_path": "README.md"},
            {"pattern": "def ", "path": "."},
            {"pattern": "**/*.py", "path": "<HOME>/app2/x"},
            {"command": "cat <HOME>/app/a.txt", "description": "보기"},
            {},
            {"file_path": "<HOME>/other/y.py"},
        ])
        self.assertEqual(session.report.dropped["other_tool_input"], 1)

    def test_outputs_are_redacted_before_the_cut(self):
        key = "-----BEGIN PRIVATE KEY-----\n" + "K" * 80 + "\n-----END PRIVATE KEY-----"
        session = parse_session(jsonl(
            human("요청"), ai(use("t1", "Bash", {"command": "env"})), result("t1", "x" * 20 + key + "y" * 20),
        ), max_output_chars=60)
        self.assertEqual(session.events[-1].text, "x" * 20 + "[PRIVATE_KEY]" + "y" * 20)
        self.assertEqual(session.redaction.in_outputs, {"private_key": 1})
        self.assertEqual(session.redaction.in_text, {})

    def test_long_outputs_keep_head_and_tail(self):
        output = "H" * 50 + "M" * 1000 + "T" * 50
        session = parse_session(jsonl(human("요청"), ai(use("t1", "Bash", {"command": "pytest"})), result("t1", output)),
                                max_output_chars=100)
        self.assertEqual(session.events[-1].text, "H" * 50 + TRUNCATION_MARK + "T" * 50)
        default = parse_session(jsonl(result("t1", "a" * 250_000)))
        self.assertEqual(len(default.events[0].text), 200_000 + len(TRUNCATION_MARK))

    def test_bad_lines_are_reported_by_number_only(self):
        session = parse_session(jsonl(human("요청"), "42", '{"type": "user", "message": "잘린 줄', "", ai(text("답변")), "[1, 2]"))
        self.assertEqual(session.report.bad_lines, [2, 3, 6])
        self.assertEqual((session.report.total_lines, session.report.parsed_records), (6, 2))
        self.assertEqual([e.line for e in session.events], [1, 5])
        self.assertNotIn("잘린 줄", dump(session))

    def test_no_json_object_line_is_bad_format(self):
        for data in (b"", b"\n\n", b"not json\n42\n[1]\n", b"\xff\xfe\x00garbage\n", '"문자열"\n'.encode(), b"[" * 100_000):
            with self.subTest(data=data[:20]), self.assertRaises(ValueError) as caught:
                parse_session(data)
            self.assertEqual(str(caught.exception), "bad_format")

    def test_encoding_and_line_endings(self):
        raw = b'{"type":"user","cwd":"/x","message":{"role":"user","content":"caf\xff\xfe"}}\n'
        self.assertEqual(parse_session(raw).events[0].text, "caf��")
        bom_crlf = b"\xef\xbb\xbf" + jsonl(human("첫 줄 같은 줄"), ai(text("답변"))).replace(b"\n", b"\r\n")
        session = parse_session(bom_crlf)
        self.assertEqual(session.report.bad_lines, [])
        self.assertEqual([e.text for e in session.events], ["첫 줄 같은 줄", "답변"])

    def test_lone_surrogate_escapes_are_replaced(self):
        # json.dumps escapes every surrogate ("\ud800"); json.loads keeps a lone one and joins a valid pair.
        session = parse_session(jsonl(*(json.dumps(r) for r in (
            human("요청 \ud800 끝"),
            ai(text("답변 \udfff"), use("t1", "Bash", {"command": "echo \ud800"}), use("t2", "Wei\udc00rd", {}),
               use("t3", "Edit", {"file_path": f"{CWD}/a\ud800.py", "old_string": "x", "new_string": "y \ud83d\ude00"})),
            result("t1", "out \udbff"),
        ))))
        dump(session).encode("utf-8")  # raises while any surrogate is left
        self.assertEqual([e.text for e in session.events if e.text], ["요청 \ufffd 끝", "답변 \ufffd", "out \ufffd"])
        bash, other, edit = (e for e in session.events if e.kind == "tool_use")
        self.assertEqual((bash.tool_input["command"], other.tool), ("echo \ufffd", "Wei\ufffdrd"))
        self.assertEqual((edit.tool_input["file_path"], edit.tool_input["new_string"]), ("a\ufffd.py", "y \U0001f600"))

    def test_odd_but_valid_records_do_not_crash(self):
        session = parse_session(jsonl(
            {"type": ["user"]}, {"type": {"a": 1}, "cwd": 5}, {"type": "user"}, {"type": "user", "message": "x"},
            {"type": "user", "message": {"content": 7}}, {"type": "assistant", "message": {"content": [1, None, {"type": 2}]}},
            human([{"type": "tool_result", "tool_use_id": ["x"], "content": {"a": 1}, "is_error": "yes"}], toolUseResult="Error"),
            ai(use(None, None, "not a dict"), use("t1", "Edit", {"file_path": 3, "old_string": None, "replace_all": "true"}),
               use("t2", "MultiEdit", {"edits": "nope"})),
            human([text(5), {"type": "text"}]),
        ))
        self.assertEqual([(e.kind, e.tool_use_id, e.tool, e.text, e.tool_input, e.is_error) for e in session.events], [
            ("tool_result", None, None, "", None, None),
            ("tool_use", None, None, "", {}, None),
            ("tool_use", "tu1", "Edit", "", {}, None),
            ("tool_use", "tu2", "MultiEdit", "", {}, None),
        ])

    def test_nothing_usable_gives_no_events(self):
        session = parse_session(jsonl({"type": "summary", "summary": "x"}, human("<command-name>/clear</command-name>")))
        self.assertEqual(session.events, [])


class FixtureParseTest(unittest.TestCase):
    # Metadata keys and values, plus content of every record or block the parser must drop.
    FORBIDDEN = (
        "uuid", "Uuid", "sessionId", "requestId", "msg_01", "req_011C", "toolu_", "/home/student", "2026-",
        "claude-sonnet", "Weather timeout test fixed", "Weather CLI setup", "requests로 예보를 받아", "timeout 기본값과",
        "main이 None을 처리하지 않는다", "비밀값은 저장소에", "The user opened the file", "PostToolUse", "Total cost",
        "Request interrupted", "requests 호출 위치를 찾아", "local-command-caveat", "Conversation compacted",
        "iVBORw0KGgo", "EmwKAhgB", "EqQBCkYI", "config.toml 만드는 중",
    )

    def test_demo_weather_requests_turns_and_links(self):
        session = parse_session(load_bytes("demo_weather.jsonl"))
        self.assertEqual([(e.turn, e.text) for e in session.events if e.kind == "user_text"], [
            (1, "외부 날씨 API로 서울 날씨를 보여 주는 CLI를 만들어 줘"),
            (2, "API가 느리면 멈추지 않게 timeout이랑 오류 처리 넣어줘"),
            (3, "실패한 테스트 고쳐줘"),
            (4, "설정 파일로 API 주소를 빼 줘. 참고로 내 키는 [SECRET_1] 이고 연락처는 [EMAIL_1] 이야"),
        ])
        uses = {e.tool_use_id: e for e in session.events if e.kind == "tool_use"}
        self.assertEqual(list(uses), [f"tu{n}" for n in range(1, len(uses) + 1)])
        results = [e for e in session.events if e.kind == "tool_result"]
        self.assertEqual(len(results), len(uses))
        for event in results:
            self.assertEqual(event.tool, uses[event.tool_use_id].tool)
            self.assertGreater(event.idx, uses[event.tool_use_id].idx)
        failing = [e for e in results if e.is_error]
        self.assertEqual([(e.turn, e.tool) for e in failing], [(2, "Bash")])
        self.assertTrue(failing[0].text.startswith("Exit code 1\n"))
        self.assertIn("<HOME>/weather-app/weather.py", failing[0].text)
        edited = {e.tool_input["file_path"] for e in uses.values() if e.tool in ("Edit", "Write")}
        self.assertEqual(edited, {"weather.py", "tests/test_weather.py", "config.toml", "requirements.txt"})
        self.assertEqual(session.report.skipped_record_types, {
            "meta": 2, "file-history-snapshot": 2, "sidechain": 1, "system": 1, "summary": 1,
        })
        dropped = {k: v for k, v in session.report.dropped.items() if k != "meta_fields"}
        self.assertEqual(dropped, {
            "thinking": 4, "system_reminder": 1, "command_wrapper": 2, "image": 1, "interrupt_marker": 1,
            "other_tool_input": 1,
        })

    def test_events_carry_no_metadata_or_dropped_content(self):
        for name in fixture_names():
            blob = dump(parse_session(load_bytes(name)))
            for needle in self.FORBIDDEN:
                with self.subTest(name=name, needle=needle):
                    self.assertNotIn(needle, blob)

    def test_fake_secrets_never_survive(self):
        for name in fixture_names():
            for event in parse_session(load_bytes(name)).events:
                kept = json.dumps([event.text, event.tool, event.tool_input], ensure_ascii=False)
                for secret in FAKE_SECRETS:
                    with self.subTest(name=name, idx=event.idx, secret=secret):
                        self.assertNotIn(secret, kept)

    def test_malformed_fixture(self):
        session = parse_session(load_bytes("malformed.jsonl"))
        self.assertEqual(session.report.bad_lines, [2, 6])
        self.assertTrue(session.events)
        self.assertNotIn("이 줄은 중간에 잘렸습니다", dump(session))

    def test_meta_only_fixture_has_no_events(self):
        session = parse_session(load_bytes("meta_only.jsonl"))
        self.assertEqual(session.events, [])
        self.assertEqual(session.report.skipped_record_types, {"summary": 1, "meta": 2, "system": 1})

    def test_pair_fixtures_differ_only_in_the_target_output(self):
        fail = parse_session(load_bytes("pair_fail.jsonl"))
        passed = parse_session(load_bytes("pair_pass.jsonl"))
        differing = [a.idx for a, b in zip(fail.events, passed.events, strict=True) if a != b]
        self.assertEqual(len(differing), 1)
        self.assertEqual(fail.events[differing[0]].kind, "tool_result")
        self.assertEqual((fail.report, fail.redaction), (passed.report, passed.redaction))


if __name__ == "__main__":
    unittest.main()
