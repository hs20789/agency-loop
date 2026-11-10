"""Slice-2 flow over HTTP with delay-0 mock providers: previews, consent, analysis, summary, What-if, feedback
(ARCHITECTURE §5.1, §5.9, §7.2, §7.3)."""

import time
import unittest
from dataclasses import replace

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.flow.schemas import ANALYSIS_SECTIONS, ANSWER_FIELDS
from agency_loop.providers import mock
from agency_loop.providers.base import ProviderError, Providers
from agency_loop.web.app import create_app
from agency_loop.web.views import SUMMARY_LABEL
from tests.test_bundle import hundred_edits
from tests.test_consent import ANSWERS
from tests.test_parser import ai, human, jsonl, use
from tests.test_web_upload import SA, make_client, upload

PROVIDERS = ("anthropic", "local_rag", "clova_rerank", "local_llm")
ZERO = dict.fromkeys(PROVIDERS, 0)
ANALYSIS_KEYS = {
    "model", "mock", "decision_summary", "scope_note", "sections", "section_labels", "stats", "bundle_ids",
    "bundle_chars", "search_state", "search_state_label",
}
GRADED_KEYS = {"text", "kind", "evidence_ids", "quote", "grade", "grade_label", "invalid_ids"}
WHATIF_KEYS = {
    "model", "mock", "tier", "tier_label", "can_generate", "reason", "title", "scenario", "hypothetical_changes",
    "related_evidence_ids", "questions", "question_labels", "dropped_id_count",
}
FEEDBACK_KEYS = {
    "attempt", "model", "mock", "answers", "items", "instruction_review", "instruction_check", "axes", "axes_labels",
    "comparison", "coaching", "explanation", "next_difficulty",
}


class Claude(mock.MockClaude):
    """Records every call as (stage, user message); `fail` maps a stage to an error code raised after the gate."""

    def __init__(self, fail=None):
        super().__init__(delay=0)
        self.fail = dict(fail or {})
        self.calls = []

    async def generate(self, *, stage, user, gate, **kwargs):
        self.calls.append((stage, user))
        if stage in self.fail:
            gate("anthropic")  # a real attempt passes the gate, so it counts toward the caps
            raise ProviderError(self.fail[stage], retryable=True, attempted=True)
        return await super().generate(stage=stage, user=user, gate=gate, **kwargs)


class Summarizer(mock.MockSummarizer):
    def __init__(self, fail=None):
        super().__init__(delay=0)
        self.fail = fail
        self.calls = []

    async def summarize(self, text, *, max_sentences=3, gate):
        self.calls.append(text)
        if self.fail:
            gate("local_llm")
            raise ProviderError(self.fail, retryable=True, attempted=True)
        return await super().summarize(text, max_sentences=max_sentences, gate=gate)


class Embedder(mock.MockEmbedder):
    def __init__(self):
        super().__init__(delay=0)
        self.calls = []

    async def embed(self, texts, *, gate):
        self.calls.append(texts)
        return await super().embed(texts, gate=gate)


class Reranker(mock.MockReranker):
    def __init__(self):
        super().__init__(delay=0)
        self.calls = []

    async def rerank(self, query, documents, *, gate):
        self.calls.append((query, documents))
        return await super().rerank(query, documents, gate=gate)


def mock_providers(claude_fail=None, summary_fail=None):
    """Recording mocks for the analysis-flow providers (reviews come with the next slice)."""
    return Providers(
        claude=Claude(claude_fail), embedder=Embedder(), reranker=Reranker(), summarizer=Summarizer(summary_fail),
        modes=dict.fromkeys(PROVIDERS, "mock"),
    )


def counts(providers):
    """Provider calls so far, refused attempts included."""
    p = providers
    return {"anthropic": len(p.claude.calls), "local_rag": len(p.embedder.calls),
            "clova_rerank": len(p.reranker.calls), "local_llm": len(p.summarizer.calls)}


def flow_app(providers, **overrides):
    return create_app(replace(Settings(), **{"upload_rate_per_10min": 1000, **overrides}), providers)


def start(client, name, candidate_id="c1", choice=None, sa=SA):
    """Upload and select; `choice` also stores a prediction (retro candidates)."""
    upload(client, name)
    assert client.post("/api/select", json={"candidate_id": candidate_id, "self_assessment": sa}).status_code == 200
    if choice is not None:
        assert client.post("/api/prediction", json={"choice": choice, "reason": ""}).status_code == 200


def agree(client, stage="analysis", answers=None, **decisions):
    """Previews a stage and consents to each of its providers unless `decisions` says otherwise; returns the preview."""
    body = {"stage": stage} if answers is None else {"stage": stage, "answers": answers}
    preview = client.post("/api/preview", json=body).json()
    choices = {**{e["name"]: True for e in preview["providers"]}, **decisions}
    r = client.post("/api/consent", json={"preview_id": preview["preview_id"], "providers": choices})
    assert r.status_code == 200, r.text
    return preview


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while (view := client.get(f"/api/jobs/{job_id}").json())["status"] not in ("done", "failed"):
        if time.monotonic() > deadline:
            raise AssertionError(f"{view['kind']} job still {view['status']}")
        time.sleep(0.005)
    return view


def run(client, path, body=None):
    """POSTs a job route and waits for its job."""
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text
    return wait_job(client, r.json()["job_id"])


def feedback(client, attempt, answers=ANSWERS):
    preview = agree(client, f"feedback{attempt}", answers)
    return run(client, "/api/feedback", {"attempt": attempt, "preview_id": preview["preview_id"]})


def norm(text):
    return " ".join(text.split())


class FlowApiTest(unittest.TestCase):
    def full_flow(self, client):
        """Analysis with every applicable provider consented, then What-if and both feedbacks; job views by kind."""
        agree(client)
        r = client.post("/api/analysis")
        ids = r.json()
        self.assertEqual((r.status_code, ids["notice"]), (200, None))
        jobs = {"analysis": wait_job(client, ids["job_id"]), "summary": wait_job(client, ids["summary_job_id"])}
        jobs["whatif"] = run(client, "/api/whatif")
        jobs["feedback1"] = feedback(client, 1)
        jobs["feedback2"] = feedback(client, 2)
        for kind, job in jobs.items():
            self.assertEqual((job["kind"], job["status"], job["error"], job["mock"]), (kind, "done", None, True))
            self.assertTrue(job["model"].startswith("mock-"))
            self.assertIs(job["result"]["mock"], True)
        return jobs

    def check_results(self, client, jobs):
        """Shapes of §7.3, verified quotes, bundle-only ids, and the workspace view showing every stored result."""
        analysis = jobs["analysis"]["result"]
        self.assertEqual(set(analysis), ANALYSIS_KEYS)
        self.assertEqual(list(analysis["sections"]), list(ANALYSIS_SECTIONS))
        self.assertEqual(set(analysis["section_labels"]), set(ANALYSIS_SECTIONS))
        statements = [s for section in analysis["sections"].values() for s in section]
        for s in statements:
            self.assertEqual(set(s), GRADED_KEYS)
            self.assertEqual(s["grade_label"], labels.GRADE[s["grade"]])
        stats = analysis["stats"]
        self.assertEqual(stats["total"], len(statements))
        self.assertEqual(sum(stats[grade] for grade in labels.GRADE), len(statements))
        self.assertEqual(stats["citation_unverified"], 0)
        recorded = [s for s in statements if s["grade"] == "recorded"]
        self.assertTrue(recorded)
        for s in recorded:  # the quote is in a cited item the user can open
            texts = [norm(client.get(f"/api/evidence/{i}").json()["text"]) for i in s["evidence_ids"]]
            self.assertTrue(any(norm(s["quote"]) in text for text in texts), s)

        whatif = jobs["whatif"]["result"]
        self.assertEqual(set(whatif), WHATIF_KEYS)
        self.assertIs(whatif["can_generate"], True)
        self.assertEqual(whatif["question_labels"], labels.ANSWER_FIELD)
        self.assertTrue(whatif["related_evidence_ids"])
        self.assertLessEqual(set(whatif["related_evidence_ids"]), set(analysis["bundle_ids"]))

        for attempt in (1, 2):
            result = jobs[f"feedback{attempt}"]["result"]
            self.assertEqual(set(result), FEEDBACK_KEYS)
            self.assertEqual((result["attempt"], result["answers"]), (attempt, ANSWERS))
            self.assertEqual([item["field"] for item in result["items"]], list(ANSWER_FIELDS))
            for item in result["items"]:
                self.assertEqual(item["field_label"], labels.ANSWER_FIELD[item["field"]])
                self.assertLessEqual(set(item["evidence_ids"]), set(analysis["bundle_ids"]))
            self.assertEqual(result["axes_labels"], {a: labels.AXIS_EVIDENCE[v] for a, v in result["axes"].items()})
            self.assertEqual([row["axis"] for row in result["comparison"]], ["understanding", "verification", "control"])
            self.assertEqual(result["instruction_check"]["mentions"][0]["text"], "pytest")  # rule engine, §5.7
        self.assertIsNone(jobs["feedback1"]["result"]["next_difficulty"])
        self.assertEqual(set(jobs["feedback2"]["result"]["next_difficulty"]), {"code", "label", "message"})
        self.assertTrue(jobs["feedback2"]["result"]["explanation"])

        view = client.get("/api/workspace").json()
        results = view["results"]
        self.assertEqual((results["analysis"], results["whatif"]), (analysis, whatif))
        self.assertEqual(results["feedback"], [jobs["feedback1"]["result"], jobs["feedback2"]["result"]])
        self.assertEqual(results["summary"], jobs["summary"]["result"])
        self.assertEqual((results["summary"]["label"], results["summary"]["mock"]), (SUMMARY_LABEL, True))
        self.assertEqual([(j["kind"], j["status"]) for j in view["jobs"]], [
            ("analysis", "done"), ("summary", "done"), ("whatif", "done"), ("feedback1", "done"), ("feedback2", "done"),
        ])
        self.assertEqual(set(view["consents"]), {"analysis", "feedback1", "feedback2"})
        return view

    def test_retro_candidate_end_to_end(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            start(client, "demo_weather.jsonl", "c1", choice="fail")
            jobs = self.full_flow(client)
            view = self.check_results(client, jobs)
            analysis = jobs["analysis"]["result"]
            self.assertEqual((analysis["search_state"], analysis["search_state_label"]), ("skipped", "검색 생략"))
            self.assertEqual(view["results"]["retrieval"],
                             {"state": "skipped", "state_label": "검색 생략", "retrieved": [], "cited": []})
            self.assertEqual(sorted(analysis["bundle_ids"]), [f"E{i:04d}" for i in range(10, 20)])  # the window
            self.assertEqual((view["evidence_ids"], analysis["bundle_chars"] > 0), ([f"E{i:04d}" for i in range(10, 20)], True))
            whatif = jobs["whatif"]["result"]
            self.assertEqual((whatif["tier"], whatif["tier_label"], whatif["dropped_id_count"]), ("standard", "표준", 0))
            status = jobs["feedback1"]["result"]["instruction_check"]["mentions"][0]["status"]
            self.assertEqual(status, "recorded_fail")  # the target run is inside the analysis window
            self.assertEqual(view["calls"], {"anthropic": 4, "local_llm": 1})
            self.assertEqual(counts(p), {**ZERO, "anthropic": 4, "local_llm": 1})
            self.assertEqual([stage for stage, _ in p.claude.calls], ["analysis", "whatif", "feedback1", "feedback2"])
            # One analysis, one What-if, two feedbacks per flow.
            for path, body in (("/api/analysis", None), ("/api/whatif", None),
                               ("/api/feedback", {"attempt": 1, "preview_id": "x"}),
                               ("/api/feedback", {"attempt": 3, "preview_id": "x"})):
                r = client.post(path, json=body)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_submitted"), path)
            self.assertEqual(counts(p)["anthropic"], 4)

    def test_non_retro_candidate_end_to_end(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            start(client, "no_tests.jsonl", "c1", sa={"understanding": 0, "verification": 1, "control": 0})
            jobs = self.full_flow(client)
            view = self.check_results(client, jobs)
            self.assertEqual(jobs["whatif"]["result"]["tier"], "basic")
            self.assertEqual(view["evidence_ids"], [f"E{i:04d}" for i in range(7)])
            self.assertEqual(jobs["feedback1"]["result"]["instruction_check"]["mentions"][0]["status"], "none")
            self.assertEqual(counts(p), {**ZERO, "anthropic": 4, "local_llm": 1})

    def test_related_requests_are_searched_and_cited(self):
        p = mock_providers()
        with make_client(flow_app(p, rag_min_similarity=0.2)) as client:  # bag-of-words mock vectors are far apart
            start(client, "many_requests.jsonl", "c3", choice="pass")
            agree(client)
            analysis = wait_job(client, client.post("/api/analysis").json()["job_id"])["result"]
            view = client.get("/api/workspace").json()
            retrieval = view["results"]["retrieval"]
            self.assertEqual(retrieval, {"state": "done", "state_label": "검색 완료", "retrieved": ["E0012", "E0024"],
                                         "cited": ["E0012", "E0024"]})
            self.assertEqual((analysis["search_state"], analysis["bundle_ids"][-2:]), ("done", ["E0012", "E0024"]))
            self.assertIn('<evidence id="E0012" kind="user_request" turn="3" cited="true">', p.claude.calls[0][1])
            self.assertLessEqual({"E0012", "E0024"}, set(view["evidence_ids"]))  # retrieved requests become visible
            self.assertEqual(counts(p), {"anthropic": 1, "local_rag": 1, "clova_rerank": 1, "local_llm": 1})

    def test_only_consented_providers_are_called(self):
        p = mock_providers()
        with make_client(flow_app(p, rag_min_similarity=0.2)) as client:
            start(client, "many_requests.jsonl", "c3", choice="pass")
            agree(client, clova_rerank=False, local_llm=False)
            r = client.post("/api/analysis").json()
            self.assertIsNone(r["summary_job_id"])
            self.assertEqual(wait_job(client, r["job_id"])["status"], "done")
            view = client.get("/api/workspace").json()
            self.assertEqual((view["results"]["retrieval"]["cited"], view["results"]["summary"]), ([], None))
            self.assertIn('cited="false"', p.claude.calls[0][1])
            self.assertEqual([j["kind"] for j in view["jobs"]], ["analysis"])
            self.assertEqual(counts(p), {**ZERO, "anthropic": 1, "local_rag": 1})

    def test_declined_anthropic_starts_no_analysis(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            start(client, "demo_weather.jsonl", "c2")  # not retro eligible: open right after selection
            preview = agree(client, anthropic=False)
            r = client.post("/api/analysis")
            body = r.json()
            self.assertEqual((r.status_code, body["job_id"], body["notice"]), (200, None, labels.ERROR["consent_missing"]))
            self.assertEqual(wait_job(client, body["summary_job_id"])["status"], "done")  # the summary was consented
            r = client.post("/api/whatif")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))
            self.assertEqual(counts(p), {**ZERO, "local_llm": 1})

            # A later consent to the same preview starts the analysis; the summary does not run twice.
            client.post("/api/consent", json={"preview_id": preview["preview_id"],
                                              "providers": {"anthropic": True, "local_llm": True}})
            body = client.post("/api/analysis").json()
            self.assertIsNone(body["summary_job_id"])
            self.assertEqual(wait_job(client, body["job_id"])["status"], "done")
            self.assertEqual(counts(p), {**ZERO, "anthropic": 1, "local_llm": 1})

    def test_declined_local_llm_starts_no_summary(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            start(client, "no_tests.jsonl")
            agree(client, local_llm=False)
            body = client.post("/api/analysis").json()
            self.assertEqual((body["summary_job_id"], body["notice"]), (None, None))
            wait_job(client, body["job_id"])
            view = client.get("/api/workspace").json()
            self.assertEqual(([j["kind"] for j in view["jobs"]], view["results"]["summary"]), (["analysis"], None))
            self.assertEqual(counts(p), {**ZERO, "anthropic": 1})

    def test_insufficient_evidence_calls_no_model(self):
        p = mock_providers()
        session = jsonl(human("고쳐"), ai(use("t1", "Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})))
        with make_client(flow_app(p)) as client:
            client.post("/api/workspaces", content=session)
            client.post("/api/select", json={"candidate_id": "c1", "self_assessment": SA})
            # Changed with the slice-2 follow-up: no analysis provider applies (Local LLM included), so no preview.
            r = client.post("/api/preview", json={"stage": "analysis"})
            self.assertEqual((r.status_code, r.json()["error"]), (409, {
                "code": "precondition", "message": labels.ERROR["insufficient_evidence"], "retryable": False,
            }))
            for _ in range(2):
                r = client.post("/api/analysis")
                self.assertEqual(r.json(), {"job_id": None, "summary_job_id": None,
                                            "notice": labels.ERROR["insufficient_evidence"]})
            for path in ("/api/whatif", "/api/summary"):
                r = client.post(path)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"), path)
            view = client.get("/api/workspace").json()
            self.assertEqual(view["results"]["retrieval"],
                             {"state": "insufficient", "state_label": "근거 부족", "retrieved": [], "cited": []})
            self.assertEqual((counts(p), view["jobs"], view["calls"], view["consents"]), (ZERO, [], {}, {}))

    def test_a_capped_analysis_sends_exactly_what_the_preview_listed(self):
        p = mock_providers()
        session = hundred_edits(request="시간 제한을 넣어 주세요 " + "가" * 4000)
        with make_client(flow_app(p, bundle_max_chars=2000)) as client:  # drops 99 edits and cuts the request
            client.post("/api/workspaces", content=session)
            client.post("/api/select", json={"candidate_id": "c1", "self_assessment": SA})
            client.post("/api/prediction", json={"choice": "fail"})
            preview = agree(client)
            claude = next(e for e in preview["providers"] if e["name"] == "anthropic")
            ids = client.post("/api/analysis").json()
            analysis = wait_job(client, ids["job_id"])
            self.assertEqual((analysis["status"], analysis["error"]), ("done", None))  # check_send passed
            self.assertEqual(wait_job(client, ids["summary_job_id"])["status"], "done")
            self.assertEqual(p.claude.calls[0][1], claude["payload_text"])
            result = analysis["result"]
            self.assertEqual(result["bundle_ids"], [i["evidence_id"] for i in claude["items"]])
            self.assertEqual(result["bundle_ids"], ["E0000", "E0101", "E0102", "E0103"])
            self.assertLessEqual(result["bundle_chars"], 2000)
            self.assertEqual((result["stats"]["citation_unverified"], result["stats"]["recorded"] > 0), (0, True))
            self.assertEqual(run(client, "/api/whatif")["status"], "done")  # under the same consent
            self.assertEqual(feedback(client, 1)["status"], "done")
            self.assertEqual(counts(p), {**ZERO, "anthropic": 3, "local_llm": 1})

    def test_what_if_and_feedback_need_the_anthropic_consent(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            start(client, "no_tests.jsonl")
            preview = agree(client)
            wait_job(client, client.post("/api/analysis").json()["job_id"])
            client.post("/api/consent", json={"preview_id": preview["preview_id"], "providers": {"anthropic": False}})
            r = client.post("/api/whatif")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "consent_missing"))
            agree(client)
            run(client, "/api/whatif")

            r = client.post("/api/feedback", json={"attempt": 1, "preview_id": "never-previewed"})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "consent_missing"))
            declined = agree(client, "feedback1", ANSWERS, anthropic=False)
            r = client.post("/api/feedback", json={"attempt": 1, "preview_id": declined["preview_id"]})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "consent_missing"))
            newer = client.post("/api/preview", json={"stage": "feedback1", "answers": ANSWERS}).json()
            r = client.post("/api/feedback", json={"attempt": 1, "preview_id": newer["preview_id"]})  # not consented yet
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "consent_missing"))
            self.assertEqual([stage for stage, _ in p.claude.calls], ["analysis", "whatif"])

    def test_request_validation_and_preconditions(self):
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            start(client, "no_tests.jsonl")
            for body in ({"stage": "whatif"}, {"stage": ["analysis"]}, {}, ["analysis"],
                         {"stage": "feedback1", "answers": {"expected_behavior": "a"}}):
                r = client.post("/api/preview", json=body)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_request"), body)
            r = client.post("/api/preview", json={"stage": "feedback1", "answers": ANSWERS})  # no What-if yet
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))

            preview = client.post("/api/preview", json={"stage": "analysis"}).json()
            self.assertEqual(set(preview), {"preview_id", "stage", "stage_label", "providers"})
            self.assertEqual(preview["stage_label"], "심층 분석")
            for body in ({"preview_id": preview["preview_id"], "providers": {"anthropic": "yes"}},
                         {"preview_id": preview["preview_id"], "providers": {"hcx": True}}, []):
                r = client.post("/api/consent", json=body)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_request"), body)
            r = client.post("/api/consent", json={"preview_id": "unknown", "providers": {}})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))

            for body in ({"attempt": "1", "preview_id": "x"}, {"attempt": True, "preview_id": "x"},
                         {"attempt": 0, "preview_id": "x"}, {"attempt": 1}, {"attempt": 1, "preview_id": 5}, None):
                r = client.post("/api/feedback", json=body)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_request"), body)
            for attempt in (1, 2):  # no What-if yet
                r = client.post("/api/feedback", json={"attempt": attempt, "preview_id": "x"})
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"), attempt)
            r = client.post("/api/whatif")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))
            self.assertEqual((counts(p), client.get("/api/workspace").json()["consents"]), (ZERO, {}))

    def test_jobs_and_previews_are_workspace_scoped(self):
        p = mock_providers()
        app = flow_app(p)
        with make_client(app) as alice, make_client(app) as bob:
            start(alice, "no_tests.jsonl")
            preview = agree(alice)
            job_id = alice.post("/api/analysis").json()["job_id"]
            wait_job(alice, job_id)
            start(bob, "no_tests.jsonl")
            r = bob.get(f"/api/jobs/{job_id}")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))
            r = bob.post("/api/consent", json={"preview_id": preview["preview_id"], "providers": {"anthropic": True}})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))
            body = bob.post("/api/analysis").json()  # Alice's consent is not Bob's
            self.assertEqual((body["job_id"], body["notice"]), (None, labels.ERROR["consent_missing"]))
            self.assertEqual(bob.get("/api/workspace").json()["jobs"], [])
            self.assertEqual(alice.get(f"/api/jobs/{job_id}").json()["status"], "done")


if __name__ == "__main__":
    unittest.main()
