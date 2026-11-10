"""Independent reviews over HTTP with delay-0 mock reviewers: preconditions, consent, one review per provider, the
model comparison, and failures that keep every earlier result (ARCHITECTURE §5.1, §5.5, §5.8, §7.2, §7.3)."""

import asyncio
import re
import unittest
from dataclasses import replace

from agency_loop import labels
from agency_loop.flow import orchestrator, prompts
from agency_loop.flow.compare import NOTE
from agency_loop.flow.schemas import REVIEW
from agency_loop.model import Job
from agency_loop.providers import mock
from agency_loop.providers.base import ProviderError
from tests.test_flow_api import agree, flow_app, mock_providers, run, start
from tests.test_web_upload import make_client

REVIEW_KEYS = {"model", "mock", "format_ok", "summary", "points"}
POINT_KEYS = {"type", "type_label", "text", "evidence_ids", "quote", "grade", "grade_label", "invalid_ids"}


class Reviewer(mock.MockReviewer):
    """Records (system, user, schema) per call. `fail` is raised after the gate (a sent request that failed),
    `refuse` before it (nothing sent, like the CLOVA pause after a 429), `raw` replaces the JSON reply by plain text."""

    def __init__(self, name):
        super().__init__(name, delay=0)
        self.fail = self.refuse = self.raw = None
        self.calls = []

    async def review(self, *, system, user, schema, gate, on_status=None):
        self.calls.append((system, user, schema))
        if self.refuse:
            raise self.refuse
        result = await super().review(system=system, user=user, schema=schema, gate=gate, on_status=on_status)
        if self.fail:
            raise self.fail
        return replace(result, data=None, text=self.raw) if self.raw else result


def review_providers():
    p = mock_providers()
    p.hcx, p.deepseek = Reviewer("hcx"), Reviewer("deepseek")
    p.modes.update(hcx="mock", deepseek="mock")
    return p


def analyzed(client, name="no_tests.jsonl"):
    """Upload, select c1 (not retro eligible) and run the consented analysis without a summary; returns its result."""
    start(client, name)
    agree(client, local_llm=False)
    return run(client, "/api/analysis")["result"]


def review(client, provider):
    """Previews and consents to the provider's review stage, then runs the review; returns (job view, preview)."""
    preview = agree(client, f"review:{provider}")
    return run(client, "/api/reviews", {"provider": provider}), preview


def refused(client, provider):
    r = client.post("/api/reviews", json={"provider": provider})
    return r.status_code, r.json()["error"]["code"]


def error(code, retryable):
    return {"code": code, "message": labels.ERROR[code], "retryable": retryable}


def groups(comparison):
    """(evidence id, providers) per group, and (provider, statement count) of the statements without a valid id."""
    return {
        **{name: [(g["evidence_id"], g["providers"]) for g in comparison[name]] for name in ("same_evidence", "only_one")},
        "different": [(d["provider"], len(d["statements"])) for d in comparison["different"]],
    }


class ReviewApiTest(unittest.TestCase):
    def test_both_reviews_and_the_comparison(self):
        p = review_providers()
        app = flow_app(p)
        with make_client(app) as client:
            analysis = analyzed(client)
            hcx, hcx_preview = review(client, "hcx")
            first = client.get("/api/workspace").json()["results"]["comparison"]
            deepseek, deepseek_preview = review(client, "deepseek")
            view = client.get("/api/workspace").json()

            claude_texts = [analysis["decision_summary"], analysis["scope_note"],
                            *(s["text"] for section in analysis["sections"].values() for s in section)]
            for job, reviewer, preview in ((hcx, p.hcx, hcx_preview), (deepseek, p.deepseek, deepseek_preview)):
                name, result = reviewer.name, job["result"]
                self.assertEqual((job["kind"], job["status"], job["error"], job["mock"]),
                                 (f"review:{name}", "done", None, True))
                self.assertEqual(set(result), REVIEW_KEYS)
                self.assertEqual((result["model"], result["mock"], result["format_ok"]), (reviewer.model, True, True))
                self.assertTrue(result["summary"])
                for point in result["points"]:
                    self.assertEqual(set(point), POINT_KEYS)
                    self.assertEqual(point["type_label"], labels.POINT_TYPE[point["type"]])
                    self.assertEqual(point["grade_label"], labels.GRADE[point["grade"]])
                # Sent exactly as previewed: the bundle Claude got, and nothing Claude wrote.
                [(system, user, schema)] = reviewer.calls
                self.assertEqual((system, schema), (prompts.SYSTEM["review"], REVIEW))
                self.assertEqual(user, preview["providers"][0]["payload_text"])
                self.assertEqual(re.findall(r'<evidence id="(\w+)"', user), analysis["bundle_ids"])
                for text in claude_texts:
                    self.assertNotIn(text, user)

            # Only the providers with results take part: after the HCX-007 review, Claude and HCX-007.
            self.assertEqual(groups(first), {
                "same_evidence": [("E0000", ["anthropic", "hcx"]), ("E0002", ["anthropic", "hcx"])],
                "only_one": [("E0001", ["anthropic"])],
                "different": [("anthropic", 2)],
            })
            comparison = view["results"]["comparison"]
            self.assertEqual(groups(comparison), {
                "same_evidence": [("E0000", ["anthropic", "hcx"]), ("E0002", ["anthropic", "hcx", "deepseek"])],
                "only_one": [("E0001", ["anthropic"])],
                "different": [("anthropic", 2), ("deepseek", 1)],  # DeepSeek's missing_evidence point cites nothing
            })
            [hcx_issue, _], [deepseek_issue, deepseek_missing] = hcx["result"]["points"], deepseek["result"]["points"]
            self.assertEqual(comparison["same_evidence"][1]["statements"]["deepseek"], [deepseek_issue])
            self.assertEqual(comparison["same_evidence"][1]["statements"]["hcx"], [hcx_issue])
            self.assertEqual(comparison["different"][1]["statements"], [deepseek_missing])
            self.assertEqual((comparison["note"], comparison["labels"]), (NOTE, labels.COMPARISON))

            results = view["results"]
            self.assertEqual((results["analysis"], results["reviews"]),
                             (analysis, {"hcx": hcx["result"], "deepseek": deepseek["result"]}))
            self.assertEqual(set(view["consents"]), {"analysis", "review:hcx", "review:deepseek"})
            self.assertEqual(view["calls"], {"anthropic": 1, "hcx:review:hcx": 1, "deepseek:review:deepseek": 1})
            self.assertEqual(app.state.caps.daily_usage()["clova"]["used"], 1)  # HCX-007 counts toward CLOVA

            for name in ("hcx", "deepseek"):  # one review per provider
                self.assertEqual(refused(client, name), (409, "already_submitted"))
            self.assertEqual((len(p.hcx.calls), len(p.deepseek.calls)), (1, 1))

    def test_preconditions_and_consent(self):
        p = review_providers()
        with make_client(flow_app(p)) as client:
            start(client, "demo_weather.jsonl", "c1")  # retro eligible and no prediction yet: blinded
            self.assertEqual(refused(client, "hcx"), (409, "precondition"))
            client.post("/api/prediction", json={"choice": "pass"})
            for stage in ("review:hcx", "review:deepseek"):  # no Claude analysis yet
                r = client.post("/api/preview", json={"stage": stage})
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"), stage)
                self.assertEqual(refused(client, stage.split(":")[1]), (409, "precondition"), stage)
            agree(client, local_llm=False)
            run(client, "/api/analysis")

            for body in ({"provider": "anthropic"}, {"provider": ["hcx"]}, {}, ["hcx"], None):
                r = client.post("/api/reviews", json=body)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_request"), body)
            self.assertEqual(refused(client, "hcx"), (409, "consent_missing"))  # never previewed
            agree(client, "review:hcx", hcx=False)
            agree(client, "review:deepseek")  # one stage's consent never covers the other provider
            self.assertEqual(refused(client, "hcx"), (409, "consent_missing"))
            view = client.get("/api/workspace").json()
            self.assertEqual((p.hcx.calls, p.deepseek.calls), ([], []))
            self.assertEqual((view["results"]["reviews"], view["results"]["comparison"]), (None, None))
            self.assertEqual((view["calls"], [j["kind"] for j in view["jobs"]]), ({"anthropic": 1}, ["analysis"]))

    def test_a_failed_review_keeps_the_analysis_and_the_other_review(self):
        cases = (  # (code, failing provider, retryable) as the live adapters raise them once the request went out
            ("provider_rate_limited", "hcx", True),  # CLOVA 429: never repeated automatically
            ("timeout", "hcx", True),
            ("server_error", "hcx", True),
            ("budget_exceeded", "deepseek", False),
            ("halted", "deepseek", False),
            ("ledger_missing", "deepseek", False),
            ("connection", "deepseek", True),
        )
        for code, name, retryable in cases:
            with self.subTest(code=code):
                p = review_providers()
                other = "deepseek" if name == "hcx" else "hcx"
                with make_client(flow_app(p)) as client:
                    analysis = analyzed(client)
                    kept = review(client, other)[0]["result"]
                    before = client.get("/api/workspace").json()["results"]
                    getattr(p, name).fail = ProviderError(code, retryable=retryable)
                    failed = review(client, name)[0]
                    # The request went out, so the flow's one call for this reviewer is used up: no retry offered.
                    self.assertEqual((failed["status"], failed["result"], failed["error"]),
                                     ("failed", None, error(code, False)))
                    results = client.get("/api/workspace").json()["results"]
                    self.assertEqual(results, before)  # the comparison too
                    self.assertEqual((results["analysis"], results["reviews"]), (analysis, {other: kept}))
                    self.assertEqual(len(getattr(p, name).calls), 1)  # no automatic retry

    def test_a_review_may_run_again_only_after_a_failure(self):
        p = review_providers()
        with make_client(flow_app(p)) as client:
            analyzed(client)
            # CLOVA is paused after a 429: refused before the gate, so nothing was sent or counted.
            p.hcx.refuse = ProviderError("provider_rate_limited", retryable=True)
            self.assertEqual(review(client, "hcx")[0]["error"], error("provider_rate_limited", True))
            p.hcx.refuse = None
            retried = run(client, "/api/reviews", {"provider": "hcx"})  # the consent still holds
            self.assertEqual((retried["status"], retried["result"]["format_ok"]), ("done", True))
            self.assertEqual(refused(client, "hcx"), (409, "already_submitted"))

            # A request that went out and failed used this flow's one DeepSeek call: the retry stops at the gate.
            p.deepseek.fail = ProviderError("timeout")
            self.assertEqual(review(client, "deepseek")[0]["error"], error("timeout", False))
            p.deepseek.fail = None
            capped = run(client, "/api/reviews", {"provider": "deepseek"})
            self.assertEqual((capped["status"], capped["error"]), ("failed", error("cap_exceeded", False)))
            view = client.get("/api/workspace").json()
            self.assertEqual(view["calls"], {"anthropic": 1, "hcx:review:hcx": 1, "deepseek:review:deepseek": 1})
            self.assertEqual((list(view["results"]["reviews"]), view["results"]["reviews"]["hcx"]),
                             (["hcx"], retried["result"]))
            # Only the newest attempt per kind is kept.
            self.assertEqual([(j["kind"], j["status"]) for j in view["jobs"]], [
                ("analysis", "done"), ("review:hcx", "done"), ("review:deepseek", "failed"),
            ])

    def test_hcx_counts_toward_the_server_wide_daily_clova_cap(self):
        p = review_providers()
        app = flow_app(p, clova_daily_calls=1)
        with make_client(app) as alice, make_client(app) as bob:
            analyzed(alice)
            self.assertEqual(review(alice, "hcx")[0]["status"], "done")
            analysis = analyzed(bob)
            kept = review(bob, "deepseek")[0]["result"]  # DeepSeek has no daily cap
            failed = review(bob, "hcx")[0]
            self.assertEqual((failed["status"], failed["error"]), ("failed", error("daily_cap", False)))
            view = bob.get("/api/workspace").json()
            self.assertEqual((view["results"]["analysis"], view["results"]["reviews"]), (analysis, {"deepseek": kept}))
            self.assertEqual(view["calls"], {"anthropic": 1, "deepseek:review:deepseek": 1})
            self.assertEqual(app.state.caps.daily_usage()["clova"], {"used": 1, "limit": 1})

    def test_the_pipeline_itself_refuses_without_consent(self):
        p = review_providers()
        app = flow_app(p)
        with make_client(app) as client:
            analyzed(client)
            review(client, "deepseek")
            ws = app.state.store.get(client.cookies["al_ws"])
            before = client.get("/api/workspace").json()["results"]
            job = Job(id="direct", kind="review:hcx")
            with self.assertRaises(ProviderError) as cm:  # the route's consent check bypassed
                asyncio.run(orchestrator.review(job, ws, app.state.settings, p, app.state.caps, "hcx"))
            self.assertEqual(cm.exception.code, "consent_missing")
            self.assertEqual((p.hcx.calls, client.get("/api/workspace").json()["results"]), ([], before))

    def test_a_reply_in_an_unchecked_format_keeps_its_raw_text(self):
        p = review_providers()
        p.hcx.raw = "검토 의견: 형식이 깨진 {JSON"
        with make_client(flow_app(p)) as client:
            analyzed(client)
            job = review(client, "hcx")[0]
            self.assertEqual((job["status"], job["result"]), ("done", {
                "model": "mock-hcx-007", "mock": True, "format_ok": False, "summary": "", "points": [], "text": p.hcx.raw,
            }))
            self.assertEqual(groups(client.get("/api/workspace").json()["results"]["comparison"]), {
                "same_evidence": [],
                "only_one": [("E0000", ["anthropic"]), ("E0001", ["anthropic"]), ("E0002", ["anthropic"])],
                "different": [("anthropic", 2)],
            })


if __name__ == "__main__":
    unittest.main()
