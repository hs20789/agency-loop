"""The development-process report (Agency Mirror의 개발 과정 분석 리포트): section grading, the HTTP flow under the
analysis consent, and the reuse of its gap points by the What-if and feedback prompts (ARCHITECTURE §5.10, §7.2).

The report stays behind the retro blinding gate: like the analysis it runs only after the prediction was stored, on
the same evidence bundle, with consent, inside the per-flow Claude attempt cap — and the failing to find a recorded
item is always phrased as "기록에서 확인되지 않는다", never as work the session developer did not do.
"""

import asyncio
import unittest

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.flow import prompts
from agency_loop.flow.bundle import analysis_bundle, candidate_section
from agency_loop.flow.report import grade_report, review_gaps
from agency_loop.flow.schemas import (
    ACTION_KINDS,
    GAP_CONCERNS,
    PROMPT_ASPECTS,
    REPORT,
    REPORT_SUMMARY_SECTIONS,
    is_valid,
)
from agency_loop.model import Workspace
from agency_loop.providers import mock as provider_mocks
from tests.test_consent import ANSWERS
from tests.test_consent import demo
from tests.test_flow_api import agree, counts, flow_app, mock_providers, run, start, wait_job
from tests.test_parser import ai, human, jsonl, use
from tests.test_web_upload import SA, make_client, upload

SETTINGS = Settings()
REPORT_KEYS = {
    "model", "mock", "overview", "tasks", "changes", "decisions", "tests", "summary_labels", "not_determined",
    "prompt_findings", "gaps", "actions", "dropped_id_count", "stats", "bundle_ids", "bundle_chars",
}
GRADED_KEYS = {"text", "kind", "evidence_ids", "quote", "grade", "grade_label", "invalid_ids"}


def empty_ws():
    return Workspace(id="ws", created_at=0.0, last_used=0.0, session=None, evidence={}, test_runs=[], candidates=[])


def mock_report(bundle, ws, override=None):
    """A deterministic mock report on the demo bundle; `override` names a degenerate fixture variant."""
    user = prompts.report_user(bundle, candidate_section(ws))

    async def call():
        return await provider_mocks.MockClaude(delay=0).generate(
            stage="report", system=prompts.SYSTEM["report"], user=user, schema=REPORT,
            effort="high", max_tokens=100, gate=lambda _p: None,
        )

    llm = asyncio.run(call())
    if override == "unsent_ids":  # schema-valid, but action ids that were never in the bundle
        llm.data["actions"] = [{
            "kind": "ask_ai", "text": "보낸 근거 밖의 항목을 지시하는 제안", "evidence_ids": ["E9999"],
        }]
    if override == "bad_ids":  # a record gap citing an id that was never sent: the app must flag it
        llm.data["gaps"] = [{
            "concern": "missing_verification", "text": "없는 근거를 인용한 보완 포인트",
            "kind": "record", "evidence_ids": ["E8888"], "quote": "없는 원문",
        }]
    return llm


class ReportGradingTest(unittest.TestCase):
    def setUp(self):
        self.ws = demo()
        self.bundle = analysis_bundle(self.ws, SETTINGS)
        self.items = self.bundle.items

    def test_labels_schemas_and_prompts_agree(self):
        self.assertEqual(set(labels.PROMPT_ASPECT), set(PROMPT_ASPECTS))
        self.assertEqual(set(labels.GAP_CONCERN), set(GAP_CONCERNS))
        self.assertEqual(set(labels.ACTION_KIND), set(ACTION_KINDS))
        self.assertEqual(list(labels.REPORT_SUMMARY), list(REPORT_SUMMARY_SECTIONS))
        self.assertIn("판정하지 말고", prompts.SYSTEM["report"])
        self.assertIn("기록에서는 확인되지 않는다", prompts.SYSTEM["report"])
        self.assertIn("점수, 등급, 역량 판정, 순위를 매기지 마세요", prompts.SYSTEM["report"])
        self.assertIn("<gaps>는 이 세션에서 앞서 찾은 판단·검증 보완 포인트(해석)입니다", prompts.SYSTEM["whatif"])

    def test_mock_report_cites_verifiable_evidence(self):
        llm = mock_report(self.bundle, self.ws)
        self.assertTrue(is_valid(llm.data, REPORT))
        sections, stats, dropped = grade_report(llm.data, self.items)
        self.assertEqual((stats["citation_unverified"], dropped), (0, 0))
        self.assertGreaterEqual(stats["recorded"], 2)
        for name in REPORT_SUMMARY_SECTIONS:
            for statement in sections[name]:
                self.assertEqual(set(statement), GRADED_KEYS)
        self.assertEqual(len(sections["gaps"]), 1)
        self.assertEqual(len(sections["prompt_findings"]), 1)
        self.assertEqual(len(sections["actions"]), 2)
        for finding in sections["prompt_findings"]:
            self.assertEqual(finding["aspect_label"], labels.PROMPT_ASPECT[finding["aspect"]])
            self.assertEqual(set(finding["observation"]), GRADED_KEYS)
            self.assertTrue(finding["suggestion"])
        for gap in sections["gaps"]:
            self.assertEqual(gap["concern_label"], labels.GAP_CONCERN[gap["concern"]])
            self.assertEqual(set(gap), {*GRADED_KEYS, "concern", "concern_label"})
            self.assertIn("기록에 없다는 뜻은 아닙니다", gap["text"])  # possibility phrasing, never an accusation
        for action in sections["actions"]:
            self.assertEqual(action["kind_label"], labels.ACTION_KIND[action["kind"]])
            self.assertLessEqual(set(action["evidence_ids"]), {i.evidence_id for i in self.items})
        self.assertEqual(stats["total"], len(sections["tasks"]) + len(sections["changes"])
                         + len(sections["decisions"]) + len(sections["tests"])
                         + len(sections["prompt_findings"]) + len(sections["gaps"]))

    def test_unsent_action_ids_are_dropped_and_counted(self):
        llm = mock_report(self.bundle, self.ws, "unsent_ids")
        sections, stats, dropped = grade_report(llm.data, self.items)
        self.assertEqual(dropped, 1)
        # The action stays (its suggestion is still usable); its unresolvable id is dropped from the chips.
        self.assertEqual([(a["text"], a["evidence_ids"]) for a in sections["actions"]],
                         [("보낸 근거 밖의 항목을 지시하는 제안", [])])
        self.assertEqual(stats["total"], 6)  # 4 summary statements + the finding observation + the gap

    def test_unsent_gap_ids_are_citation_unverified(self):
        llm = mock_report(self.bundle, self.ws, "bad_ids")
        sections, stats, _ = grade_report(llm.data, self.items)
        self.assertEqual([g["grade"] for g in sections["gaps"]], ["citation_unverified"])
        self.assertEqual(stats["citation_unverified"], 1)

    def test_review_gaps_compacts_only_valid_ids_and_stays_absent_without_a_report(self):
        sections, _, _ = grade_report(mock_report(self.bundle, self.ws).data, self.items)
        self.ws.results["report"] = {"gaps": sections["gaps"]}
        sent = {i.evidence_id for i in self.items}
        gaps = review_gaps(self.ws, sent)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(set(gaps[0]), {"concern", "concern_label", "text", "kind", "evidence_ids"})
        self.assertLessEqual(set(gaps[0]["evidence_ids"]), sent)
        empty = empty_ws()
        self.assertEqual(review_gaps(empty, sent), [])  # no report stored: nothing to reuse, the flow goes on


class ReportApiTest(unittest.TestCase):
    def test_report_after_the_analysis_and_reuse_by_the_next_steps(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            start(client, "demo_weather.jsonl", "c1", choice="fail")
            agree(client)
            ids = client.post("/api/analysis").json()
            self.assertEqual(wait_job(client, ids["job_id"])["status"], "done")
            body = client.post("/api/report").json()
            self.assertEqual((body["job_id"] is None, body["notice"]), (False, None))
            job = wait_job(client, body["job_id"])
            self.assertEqual((job["status"], job["kind"], job["mock"]), ("done", "report", True))
            result = job["result"]
            self.assertEqual(set(result), REPORT_KEYS)
            self.assertEqual(result["summary_labels"], dict(labels.REPORT_SUMMARY))
            analysis = client.get("/api/workspace").json()["results"]["analysis"]
            self.assertEqual(result["bundle_ids"], analysis["bundle_ids"])  # the same bundle as Ⓐ (§5.10)
            counted = (sum(len(result[name]) for name in REPORT_SUMMARY_SECTIONS)
                       + len(result["prompt_findings"]) + len(result["gaps"]))
            self.assertEqual(result["stats"]["total"], counted)
            self.assertEqual(result["stats"]["citation_unverified"], 0)
            view = client.get("/api/workspace").json()
            self.assertEqual(view["results"]["report"], result)
            self.assertEqual([j["kind"] for j in view["jobs"]], ["analysis", "summary", "report"])
            self.assertEqual(view["calls"], {"anthropic": 2, "local_llm": 1})
            r = client.post("/api/report")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_submitted"))
            # The What-if waits for a settled report and carries its gap points (conditional reuse, §5.10).
            self.assertEqual(run(client, "/api/whatif")["status"], "done")
            whatif_user = p.claude.calls[2][1]
            self.assertIn("<gaps>", whatif_user)
            self.assertIn(labels.GAP_CONCERN["missing_verification"], whatif_user)
            # The feedback preview lists and consents the gap summary like every other sent item.
            preview = agree(client, "feedback1", ANSWERS)
            items = {i["evidence_id"] for e in preview["providers"] for i in e["items"]}
            self.assertIn("gaps", items)
            feedback = run(client, "/api/feedback", {"attempt": 1, "preview_id": preview["preview_id"]})["result"]
            self.assertEqual(feedback["answers"], ANSWERS)
            self.assertIn("<gaps>", p.claude.calls[3][1])
            self.assertEqual([stage for stage, _ in p.claude.calls],
                             ["analysis", "report", "whatif", "feedback1"])
            self.assertEqual(counts(p), {"anthropic": 4, "local_rag": 0, "clova_rerank": 0, "local_llm": 1})

    def test_without_a_report_the_what_if_runs_without_gap_context(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            start(client, "demo_weather.jsonl", "c1", choice="fail")
            agree(client)
            wait_job(client, client.post("/api/analysis").json()["job_id"])
            self.assertEqual(run(client, "/api/whatif")["status"], "done")
            self.assertNotIn("<gaps>", p.claude.calls[1][1])

    def test_report_needs_the_unblinded_workspace_the_analysis_and_the_consent(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            upload(client, "demo_weather.jsonl")
            client.post("/api/select", json={"candidate_id": "c1", "self_assessment": SA})
            r = client.post("/api/report")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))  # blinded (§4.5)
            client.post("/api/prediction", json={"choice": "fail", "reason": ""})
            r = client.post("/api/report")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))  # no analysis yet
            # A later consent withdrawal from a done analysis is respected: the notice, no call.
            agree(client, local_llm=False)
            wait_job(client, client.post("/api/analysis").json()["job_id"])
            preview = agree(client, anthropic=False)
            body = client.post("/api/report").json()
            self.assertEqual((body["job_id"], body["notice"]), (None, labels.ERROR["consent_missing"]))
            self.assertEqual(len(p.claude.calls), 1)
            client.post("/api/consent", json={"preview_id": preview["preview_id"], "providers": {"anthropic": True}})
            body = client.post("/api/report").json()
            self.assertEqual((body["notice"], wait_job(client, body["job_id"])["status"]), (None, "done"))

    def test_insufficient_evidence_reports_only_the_notice(self):
        p = mock_providers()
        session = jsonl(human("고쳐"), ai(use("t1", "Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})))
        with make_client(flow_app(p)) as client:
            client.post("/api/workspaces", content=session)
            client.post("/api/select", json={"candidate_id": "c1", "self_assessment": SA})
            self.assertEqual(client.post("/api/analysis").json()["notice"], labels.ERROR["insufficient_evidence"])
            for _ in range(2):
                body = client.post("/api/report").json()
                self.assertEqual((body["job_id"], body["notice"]), (None, labels.ERROR["insufficient_evidence"]))
            self.assertEqual(counts(p), {"anthropic": 0, "local_rag": 0, "clova_rerank": 0, "local_llm": 0})

    def test_a_failed_report_keeps_the_flow_and_can_be_retried(self):
        p = mock_providers(claude_fail={"report": "timeout"})
        with make_client(flow_app(p)) as client:
            start(client, "demo_weather.jsonl", "c1", choice="fail")
            agree(client)
            wait_job(client, client.post("/api/analysis").json()["job_id"])
            failed = wait_job(client, client.post("/api/report").json()["job_id"])
            self.assertEqual((failed["status"], failed["error"]["code"]), ("failed", "timeout"))
            view = client.get("/api/workspace").json()
            self.assertIsNone(view["results"]["report"])  # failure keeps every earlier result
            self.assertEqual([j["kind"] for j in view["jobs"]], ["analysis", "summary", "report"])
            # A failed report does not block the What-if: it goes on without the gap points.
            self.assertEqual(run(client, "/api/whatif")["status"], "done")
            self.assertNotIn("<gaps>", p.claude.calls[2][1])
            p.claude.fail = {}  # the provider recovers: the retry must be able to finish
            retried = wait_job(client, client.post("/api/report").json()["job_id"])
            self.assertEqual((retried["status"], retried["result"]["mock"]), ("done", True))
            self.assertEqual([j["kind"] for j in client.get("/api/workspace").json()["jobs"]],
                             ["analysis", "summary", "whatif", "report"])
            self.assertEqual(client.get("/api/workspace").json()["calls"]["anthropic"], 4)  # Ⓐ+Ⓔ+Ⓑ+Ⓔ retry


if __name__ == "__main__":
    unittest.main()
