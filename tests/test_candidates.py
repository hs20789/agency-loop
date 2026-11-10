"""agency_loop.decisions.candidates: units, result-independent signals, ranking, levels (ARCHITECTURE §4.3)."""

import dataclasses
import unittest

from agency_loop.config import Settings
from agency_loop.decisions.candidates import analyze_session, file_kind
from agency_loop.ingest.parser import parse_session
from tests.helpers import load_bytes
from tests.test_parser import ai, human, jsonl, result, text, use


def candidates_of(*records):
    return analyze_session(parse_session(jsonl(*records)), Settings())[2]


def edit(tool_id, path, old="x = 1", new="x = 2"):
    return use(tool_id, "Edit", {"file_path": path, "old_string": old, "new_string": new})


class DemoWeatherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidates = analyze_session(parse_session(load_bytes("demo_weather.jsonl")), Settings())[2]

    def test_top_three_by_score(self):
        self.assertEqual([(c.id, c.rank, c.turn) for c in self.candidates], [("c1", 1, 2), ("c2", 2, 4), ("c3", 3, 1)])

    def test_turn_two_decision(self):
        c = self.candidates[0]
        self.assertEqual((c.request_idx, c.edit_idxs, c.decision_idx, c.files),
                         (10, [12, 15], 15, ["weather.py", "tests/test_weather.py"]))
        self.assertEqual(c.signals, [
            "try_except", "input_check", "http_call", "timeout_retry", "assumption_language", "alternative_language",
            "new_function",
        ])
        self.assertEqual(c.category, "external_call")  # ties with assumption (5); tie order picks external_call
        self.assertAlmostEqual(c.score, 16 + 17 / 20)
        self.assertEqual(c.evidence_ids, [f"E{idx:04d}" for idx in range(10, 16)])
        self.assertEqual((c.evidence_level, c.has_test_record, c.later_fix), ("sufficient", True, True))
        self.assertTrue(c.retro.eligible)
        self.assertEqual((c.retro.target.use_idx, c.retro.target.result), (18, "fail"))

    def test_turn_four_changes_dependencies_and_config(self):
        c = self.candidates[1]
        self.assertEqual(c.files, ["config.toml", "requirements.txt", "weather.py"])
        self.assertEqual(c.signals, [
            "try_except", "timeout_retry", "file_or_db_io", "dependency_change", "config_change", "new_function",
        ])
        # error_handling, external_call and dependency_config all sum to 3; the tie order picks error_handling.
        self.assertEqual(c.category, "error_handling")
        self.assertEqual((c.has_test_record, c.retro.reason, c.later_fix), (False, "no_test", False))


class ResultIndependenceTest(unittest.TestCase):
    def test_pair_candidates_differ_only_in_the_target(self):
        def analyzed(name):
            return analyze_session(parse_session(load_bytes(name)), Settings())[2]

        def without_target(candidates):
            return [dataclasses.replace(c, retro=dataclasses.replace(c.retro, target=None)) for c in candidates]

        fail, passed = analyzed("pair_fail.jsonl"), analyzed("pair_pass.jsonl")
        self.assertEqual(without_target(fail), without_target(passed))
        self.assertTrue(all(c.retro.eligible for c in fail + passed))
        self.assertEqual([(c.turn, c.retro.target.result) for c in fail], [(2, "fail"), (1, "pass")])
        self.assertEqual([(c.turn, c.retro.target.result) for c in passed], [(2, "pass"), (1, "pass")])

    def test_tool_result_text_never_makes_a_signal(self):
        [c] = candidates_of(
            human("파일 읽고 고쳐 줘"),
            ai(use("t1", "Read", {"file_path": "w.py"})),
            result("t1", "try:\n    x()\nexcept ValueError:\n    raise"),
            ai(edit("t2", "w.py")),
            result("t2", "try:\n    requests.get(url, timeout=3)\nexcept Exception:\n    pass"),
        )
        self.assertEqual((c.signals, c.category), ([], "design_choice"))

    def test_events_after_the_decision_are_ignored(self):
        [c] = candidates_of(
            human("값을 바꿔 줘"),
            ai(edit("t1", "w.py")),
            result("t1", "ok"),
            ai(text("API가 빠르다고 가정했고 대신 캐시를 쓰지 않았습니다."), use("t2", "Bash", {"command": "pip install requests"})),
            ai(use("t3", "Edit", {"file_path": "README.md", "old_string": "a", "new_string": "try:"})),
        )
        self.assertEqual((c.signals, c.decision_idx), ([], 1))


class SignalsTest(unittest.TestCase):
    def test_text_and_commands_before_the_decision(self):
        [c] = candidates_of(
            human("requests 설치하고 호출 코드 넣어 줘"),
            ai(text("Probably the API is fast, so I chose requests instead of urllib."),
               use("t1", "Bash", {"command": "uv add requests"})),
            result("t1", "Resolved 1 package"),
            ai(edit("t2", "w.py")),
        )
        self.assertEqual(c.signals, ["dependency_change", "assumption_language", "alternative_language"])
        self.assertEqual(c.category, "assumption")

    def test_added_line_signals_skip_unchanged_lines(self):
        old = "def fetch(url):\n    return requests.get(url, timeout=5)"
        [c] = candidates_of(
            human("검증 추가해 줘"),
            ai(use("t1", "MultiEdit", {"file_path": "w.py", "edits": [
                {"old_string": old, "new_string": old + "\n\n\ndef check(value):\n    if not value:\n        raise ValueError(value)"},
                {"old_string": "", "new_string": "data = json.load(handle)"},
            ]})),
        )
        self.assertEqual(c.signals, ["raise_error", "input_check", "file_or_db_io", "new_function"])
        self.assertEqual(c.category, "error_handling")  # raise_error 2 ties input_check 2; tie order picks error_handling
        self.assertAlmostEqual(c.score, 2 + 2 + 1 + 1 + 6 / 20)

    def test_large_change_and_changed_lines_in_the_score(self):
        big = "\n".join(f"line_{n} = {n}" for n in range(40))
        cands = candidates_of(
            human("작은 수정입니다"), ai(edit("t1", "a.py")),
            human("큰 수정입니다"), ai(use("t2", "Write", {"file_path": "b.py", "content": big})),
        )
        self.assertEqual([(c.turn, c.signals) for c in cands], [(2, ["large_change"]), (1, [])])
        self.assertEqual([c.score for c in cands], [1 + 2, 2 / 20])
        self.assertEqual(cands[0].category, "design_choice")

    def test_file_kinds(self):
        paths = {
            "app/main.py": "code", "src/file_lock.py": "code", "notebook.ipynb": "code", "setup.py": "manifest",
            "requirements-dev.txt": "manifest", "pyproject.toml": "manifest", "package.json": "manifest",
            "Dockerfile": "config", "docker-compose.prod.yml": "config", ".env.example": "config",
            "config/app.yaml": "config", "C:\\proj\\Makefile": "config",
            "README.md": None, "docs/guide.txt": None, "pnpm-lock.yaml": None, "package-lock.json": None,
            "uv.lock": None, "requirements-lock.txt": None, ".env": None, "": None,
        }
        self.assertEqual({path: file_kind(path) for path in paths}, paths)

    def test_docs_and_lock_files_are_not_decisions(self):
        cands = candidates_of(
            human("README 정리해 줘"), ai(use("t1", "Edit", {"file_path": "README.md", "old_string": "a", "new_string": "b"})),
            human("lock 파일 갱신해 줘"), ai(use("t2", "Write", {"file_path": "pnpm-lock.yaml", "content": "lockfileVersion: 9"})),
        )
        self.assertEqual(cands, [])


class RankingTest(unittest.TestCase):
    def test_at_most_three_and_ties_go_to_the_earlier_turn(self):
        records = []
        for n in range(1, 5):
            records += [human(f"{n}번째 수정 요청입니다"), ai(edit(f"t{n}", f"m{n}.py"))]
        cands = candidates_of(*records)
        self.assertEqual([(c.id, c.rank, c.turn, c.files) for c in cands],
                         [("c1", 1, 1, ["m1.py"]), ("c2", 2, 2, ["m2.py"]), ("c3", 3, 3, ["m3.py"])])

    def test_evidence_level_counts_request_and_explanation(self):
        explanation = text("네, 수정하겠습니다. 이 설명은 사십 자를 넘도록 조금 길게 씁니다. 결과와는 무관합니다.")
        cands = candidates_of(
            human("고쳐"), ai(edit("t1", "a.py")),
            human("고쳐"), ai(explanation, edit("t2", "b.py")),
            human("timeout 처리 넣어 줘요"), ai(explanation, edit("t3", "c.py")),
        )
        self.assertEqual({c.turn: c.evidence_level for c in cands}, {1: "insufficient", 2: "partial", 3: "sufficient"})

    def test_settings_cap_the_evidence_excerpts(self):
        session = parse_session(load_bytes("demo_weather.jsonl"))
        evidence, test_runs, cands = analyze_session(session, Settings(evidence_excerpt_chars=30))
        self.assertTrue(all(len(item.excerpt) <= 30 for item in evidence.values()))
        self.assertEqual(len(test_runs), 3)
        self.assertEqual([c.turn for c in cands], [2, 4, 1])


if __name__ == "__main__":
    unittest.main()
