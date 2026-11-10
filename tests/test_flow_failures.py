"""Failure handling of the slice-2 jobs: one failed job keeps earlier results, retries within the caps, a failed
search, a busy server, and the last secret check before any send (ARCHITECTURE §5.1, §5.8, §5.9)."""

import unittest
from dataclasses import replace

from agency_loop import labels
from agency_loop.providers.base import ProviderError
from tests.test_consent import ANSWERS
from tests.test_flow_api import ZERO, agree, counts, flow_app, mock_providers, run, start, wait_job
from tests.test_web_upload import make_client


def error(code, retryable=True):
    return {"code": code, "message": labels.ERROR[code], "retryable": retryable}


class BrokenEmbedder:
    async def embed(self, texts, *, gate):
        gate("local_rag")
        raise ProviderError("connection", retryable=True, attempted=True)


class FailureTest(unittest.TestCase):
    def test_a_lone_surrogate_in_a_json_body_is_rejected(self):
        with make_client(flow_app(mock_providers())) as client:
            start(client, "demo_weather.jsonl", "c1")
            body = b'{"choice": "pass", "reason": "\\ud800"}'  # parses, but could never be serialized again
            r = client.post("/api/prediction", content=body, headers={"content-type": "application/json"})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_request"))
            self.assertEqual(client.get("/api/workspace").status_code, 200)
            r = client.post("/api/prediction", json={"choice": "pass", "reason": ""})
            self.assertEqual(r.status_code, 200)  # nothing was stored by the rejected body

    def test_what_if_server_error_keeps_the_analysis_and_can_be_retried(self):
        p = mock_providers(claude_fail={"whatif": "server_error"})
        with make_client(flow_app(p)) as client:
            start(client, "demo_weather.jsonl", "c1", choice="pass")
            agree(client)
            analysis = wait_job(client, client.post("/api/analysis").json()["job_id"])
            failed = run(client, "/api/whatif")
            self.assertEqual((failed["status"], failed["result"], failed["error"]), ("failed", None, error("server_error")))
            view = client.get("/api/workspace").json()
            self.assertEqual((view["results"]["analysis"], view["results"]["whatif"]), (analysis["result"], None))
            r = client.post("/api/preview", json={"stage": "feedback1", "answers": ANSWERS})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))  # no task yet

            p.claude.fail.clear()
            retried = run(client, "/api/whatif")
            self.assertEqual((retried["status"], retried["result"]["can_generate"]), ("done", True))
            view = client.get("/api/workspace").json()
            self.assertEqual((view["results"]["analysis"], view["calls"]["anthropic"]), (analysis["result"], 3))
            # The retry replaced the failed job: only the newest attempt per kind is kept.
            self.assertEqual([j["status"] for j in view["jobs"] if j["kind"] == "whatif"], ["done"])

    def test_summary_failure_does_not_affect_the_analysis(self):
        p = mock_providers(summary_fail="timeout")
        with make_client(flow_app(p)) as client:
            start(client, "no_tests.jsonl")
            agree(client)
            ids = client.post("/api/analysis").json()
            summary, analysis = wait_job(client, ids["summary_job_id"]), wait_job(client, ids["job_id"])
            # The one Local LLM call of the flow is used up, so the failure offers no retry.
            timeout = error("timeout", retryable=False)
            self.assertEqual((summary["status"], summary["error"]), ("failed", timeout))
            self.assertEqual((analysis["status"], analysis["error"]), ("done", None))
            results = client.get("/api/workspace").json()["results"]
            self.assertEqual((results["summary"], results["analysis"]), ({"error": timeout}, analysis["result"]))
            self.assertEqual(run(client, "/api/whatif")["status"], "done")

    def test_a_failed_summary_can_be_retried(self):
        p = mock_providers()
        app = flow_app(p)
        with make_client(app) as client:
            start(client, "no_tests.jsonl")
            r = client.post("/api/summary")  # the first summary starts with the analysis
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))
            preview = agree(client)
            with app.state.caps.local_llm_slot():  # the class server is busy: the summary fails before any call
                ids = client.post("/api/analysis").json()
                busy = wait_job(client, ids["summary_job_id"])
            self.assertEqual((busy["status"], busy["error"]), ("failed", error("concurrency")))
            wait_job(client, ids["job_id"])
            client.post("/api/consent", json={"preview_id": preview["preview_id"], "providers": {"anthropic": True}})
            r = client.post("/api/summary")  # the Local LLM consent was withdrawn
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "consent_missing"))
            agree(client)
            retried = run(client, "/api/summary")
            self.assertEqual((retried["status"], retried["error"]), ("done", None))
            view = client.get("/api/workspace").json()
            self.assertEqual((view["results"]["summary"], view["calls"]), (retried["result"], {"anthropic": 1, "local_llm": 1}))
            r = client.post("/api/summary")  # only after a failure
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_submitted"))
            self.assertEqual(len(p.summarizer.calls), 1)

    def test_a_summary_retry_still_counts_toward_the_local_llm_cap(self):
        p = mock_providers(summary_fail="timeout")
        with make_client(flow_app(p)) as client:
            start(client, "no_tests.jsonl")
            agree(client)
            ids = client.post("/api/analysis").json()
            # After its gate: the call is used up, so the error is not retryable (the route still accepts a retry).
            self.assertEqual(wait_job(client, ids["summary_job_id"])["error"], error("timeout", retryable=False))
            wait_job(client, ids["job_id"])
            p.summarizer.fail = None
            capped = run(client, "/api/summary")
            self.assertEqual((capped["status"], capped["error"]), ("failed", error("cap_exceeded", retryable=False)))
            view = client.get("/api/workspace").json()
            self.assertEqual((view["calls"]["local_llm"], view["results"]["summary"]),
                             (1, {"error": error("cap_exceeded", retryable=False)}))
            self.assertEqual(len(p.summarizer.calls), 2)  # the retry stopped at the gate

    def test_claude_attempts_are_capped_per_flow(self):
        p = mock_providers(claude_fail={"analysis": "server_error"})
        with make_client(flow_app(p)) as client:
            start(client, "no_tests.jsonl")
            agree(client, local_llm=False)
            for attempt in range(1, 8):  # a failed analysis may be re-run; the 7th failure leaves no attempt
                self.assertEqual(run(client, "/api/analysis")["error"], error("server_error", retryable=attempt < 7))
            capped = run(client, "/api/analysis")
            self.assertEqual((capped["status"], capped["error"]), ("failed", error("cap_exceeded", retryable=False)))
            view = client.get("/api/workspace").json()
            self.assertEqual((view["calls"], view["results"]["analysis"]), ({"anthropic": 7}, None))
            self.assertEqual(len(p.claude.calls), 8)  # the 8th attempt stopped at the gate

    def test_feedback_failure_keeps_the_consented_answers_for_a_retry(self):
        p = mock_providers(claude_fail={"feedback1": "incomplete"})
        with make_client(flow_app(p)) as client:
            start(client, "no_tests.jsonl")
            agree(client)
            wait_job(client, client.post("/api/analysis").json()["job_id"])
            run(client, "/api/whatif")
            preview = agree(client, "feedback1", ANSWERS)
            body = {"attempt": 1, "preview_id": preview["preview_id"]}
            self.assertEqual(run(client, "/api/feedback", body)["error"], error("incomplete"))
            self.assertEqual(client.get("/api/workspace").json()["results"]["feedback"], [])
            r = client.post("/api/feedback", json={"attempt": 2, "preview_id": preview["preview_id"]})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))
            p.claude.fail.clear()
            retried = run(client, "/api/feedback", body)
            self.assertEqual((retried["status"], retried["result"]["answers"]), ("done", ANSWERS))
            self.assertEqual(p.claude.calls[-1][1], p.claude.calls[-2][1])  # the same consented payload

    def test_a_failed_search_goes_on_with_the_rule_engine_evidence(self):
        p = mock_providers()
        p.embedder = BrokenEmbedder()
        with make_client(flow_app(p, rag_min_similarity=0.2)) as client:
            start(client, "many_requests.jsonl", "c3", choice="pass")
            agree(client)
            analysis = wait_job(client, client.post("/api/analysis").json()["job_id"])
            self.assertEqual((analysis["status"], analysis["result"]["search_state"]), ("done", "failed"))
            self.assertEqual(client.get("/api/workspace").json()["results"]["retrieval"], {
                "state": "failed", "state_label": labels.SEARCH_STATE["failed"], "retrieved": [], "cited": [],
            })
            self.assertEqual(len(p.reranker.calls), 0)

    def test_a_busy_server_fails_the_analysis_as_retryable(self):
        p = mock_providers()
        with make_client(flow_app(p, analysis_concurrency=0)) as client:
            start(client, "no_tests.jsonl")
            agree(client, local_llm=False)
            busy = run(client, "/api/analysis")
            self.assertEqual((busy["status"], busy["error"]), ("failed", error("concurrency")))
            self.assertEqual(counts(p), ZERO)

    def test_a_raw_secret_in_visible_evidence_blocks_every_send(self):
        p = mock_providers()
        app = flow_app(p)
        with make_client(app) as client:
            start(client, "no_tests.jsonl")
            ws = app.state.store.get(client.cookies["al_ws"])
            request = ws.evidence["E0000"]  # visible: the candidate's request
            ws.evidence["E0000"] = replace(request, excerpt=request.excerpt + " 키는 sk-ant-api03-RAWRAWRAWRAWRAW0000")
            agree(client)
            ids = client.post("/api/analysis").json()
            for job_id in (ids["job_id"], ids["summary_job_id"]):
                job = wait_job(client, job_id)
                self.assertEqual((job["status"], job["error"]), ("failed", error("secret_detected", False)), job["kind"])
            self.assertEqual((counts(p), client.get("/api/workspace").json()["calls"]), (ZERO, {}))


if __name__ == "__main__":
    unittest.main()
