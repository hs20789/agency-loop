"""The whole flow over HTTP with every provider in live mode, against local mock servers (ARCHITECTURE §6.2, §9).

The real adapters talk to tests/mock_servers.py (Anthropic SDK; httpx for CLOVA, Mistral and the BGE-M3 embed) and to
tests/fake_mcp_server.py over MCP stdio. The mocks reply like the deterministic mock providers, so citations verify.
Checked: the request bodies, nothing sent before the prediction or before a consent, the usage ledger, no secret in
what was sent, and failures that keep every earlier result.
"""

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.flow import prompts
from agency_loop.flow.schemas import BY_STAGE, REVIEW
from agency_loop.ledger import Ledger
from agency_loop.providers import anthropic_claude, clova
from agency_loop.providers import mock as mocks
from agency_loop.providers.base import no_gate
from agency_loop.web.app import create_app
from agency_loop.web.views import SUMMARY_LABEL
from tests.helpers import FAKE_SECRETS, PAIR_MARKERS
from tests.mock_servers import ANTHROPIC_MODEL, MISTRAL_USAGE, MockApiServer, unused_url
from tests.test_blinding_flow import MODEL_BOUND
from tests.test_consent import ANSWERS
from tests.test_flow_api import agree, start
from tests.test_web_upload import SA, make_client, strings, upload

TESTS = Path(__file__).resolve().parent
FAKE_MCP = str(TESTS / "fake_mcp_server.py")
KEYS = {"ANTHROPIC_API_KEY": "test-anthropic-key", "CLOVA_API_KEY": "test-clova-key", "MISTRAL_API_KEY": "test-mistral-key"}
CLAUDE_STAGES = ("analysis", "whatif", "feedback1", "feedback2")
CLAUDE_KEYS = {"model", "max_tokens", "system", "messages", "output_config", "stream"}  # no thinking, no sampling
STAGE_OF = {text: stage for stage, text in prompts.SYSTEM.items()}


async def claude_reply(body):
    """What the deterministic mock answers to this request: schema-valid JSON quoting the evidence it was sent."""
    system = body["system"]
    llm = await mocks.MockClaude(delay=0).generate(
        stage=STAGE_OF[system], system=system, user=body["messages"][0]["content"],
        schema=body["output_config"]["format"]["schema"], effort="", max_tokens=0, gate=no_gate,
    )
    return llm.text


def review_reply(name):
    async def reply(body):
        system, user = (message["content"] for message in body["messages"])
        return (await mocks.MockReviewer(name, delay=0).review(system=system, user=user, schema=REVIEW, gate=no_gate)).text

    return reply


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def entry(preview, name):
    return next(e for e in preview["providers"] if e["name"] == name)


def error(code, retryable):
    return {"code": code, "message": labels.ERROR[code], "retryable": retryable}


def wait(client, job_id):
    deadline = time.monotonic() + 30  # the MCP child needs about a second to start
    while (view := client.get(f"/api/jobs/{job_id}").json())["status"] not in ("done", "failed"):
        if time.monotonic() > deadline:
            raise AssertionError(f"{view['kind']} job still {view['status']}")
        time.sleep(0.01)
    return view


def run(client, path, body=None):
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text
    return wait(client, r.json()["job_id"])


class LiveE2eTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockApiServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.server.reset()
        self.server.replies = {"anthropic": claude_reply, "hcx": review_reply("hcx"), "mistral": review_reply("deepseek")}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger_path = Path(tmp.name) / "usage.jsonl"
        Ledger.init(self.ledger_path)
        for patcher in (
            mock.patch.dict(os.environ, KEYS),
            mock.patch.object(anthropic_claude, "RETRY_DELAY_SEC", 0),
            mock.patch.object(clova, "_cooldown_until", 0.0),  # a 429 here must not pause CLOVA for later tests
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def app(self, **overrides):
        url = self.server.base_url
        settings = {
            "upload_rate_per_10min": 1000,
            **dict.fromkeys(("anthropic_mode", "clova_mode", "mistral_mode", "embed_mode", "local_llm_mode"), "live"),
            **dict.fromkeys(("anthropic_base_url", "clova_base_url", "mistral_base_url", "ollama_embed_url"), url),
            "ollama_chat_url": url,
            "mcp_command": sys.executable, "mcp_args": (FAKE_MCP,), "mcp_cwd": str(TESTS), "mcp_timeout_sec": 30,
            "ledger_path": str(self.ledger_path),
        }
        return create_app(Settings(**{**settings, **overrides}))

    def apis(self):
        return [r["api"] for r in self.server.requests]

    def ledger_ops(self):
        return [json.loads(line)["op"] for line in self.ledger_path.read_text().splitlines()]

    def analyzed(self, client, name="demo_weather.jsonl", candidate_id="c1", choice="fail"):
        """A consented analysis job (no summary) on a predicted retro candidate."""
        start(client, name, candidate_id, choice=choice)
        agree(client, local_llm=False)
        return run(client, "/api/analysis")

    @staticmethod
    def results(client):
        return client.get("/api/workspace").json()["results"]

    def full_flow(self, name, candidate_id, choice):
        """Prediction -> analysis (search, rerank, Claude) with the summary -> both reviews -> What-if -> two feedbacks,
        every provider consented. Returns (workspace view, consented previews by stage, job views by kind)."""
        app = self.app()
        previews = {}
        with make_client(app) as client:
            start(client, name, candidate_id, choice=choice)
            previews["analysis"] = agree(client)
            # Nothing sent or even attempted (MCP included) before the prediction and the consent.
            self.assertEqual((self.server.requests, client.get("/api/workspace").json()["calls"]), ([], {}))
            ids = client.post("/api/analysis").json()
            jobs = {"analysis": wait(client, ids["job_id"]), "summary": wait(client, ids["summary_job_id"])}
            for provider, api in (("hcx", "hcx"), ("deepseek", "mistral")):
                stage = f"review:{provider}"
                previews[stage] = agree(client, stage)
                self.assertEqual(self.server.calls(api), [])  # not before its own consent
                jobs[stage] = run(client, "/api/reviews", {"provider": provider})
            jobs["whatif"] = run(client, "/api/whatif")
            for attempt in (1, 2):
                stage = f"feedback{attempt}"
                previews[stage] = agree(client, stage, ANSWERS)
                self.assertEqual(len(self.server.calls("anthropic")), attempt + 1)  # not before its own consent
                jobs[stage] = run(client, "/api/feedback", {"attempt": attempt, "preview_id": previews[stage]["preview_id"]})
            view = client.get("/api/workspace").json()

        s = app.state.settings
        for kind, job in jobs.items():
            self.assertEqual((job["status"], job["error"], job["mock"], job["result"]["mock"]), ("done", None, False, False), kind)
        self.assertEqual(
            {kind: job["model"] for kind, job in jobs.items()},
            {**dict.fromkeys(CLAUDE_STAGES, ANTHROPIC_MODEL), "summary": "fake-qwen", "review:hcx": s.hcx_model,
             "review:deepseek": s.mistral_model},
        )
        for e in previews["analysis"]["providers"]:
            self.assertEqual((e["mock"], e["destination"]), (False, self.server.base_url), e["name"])
        analysis = jobs["analysis"]["result"]
        self.assertEqual((analysis["stats"]["citation_unverified"], analysis["stats"]["recorded"] > 0), (0, True))
        self.assertTrue(all(r["format_ok"] for r in view["results"]["reviews"].values()))
        self.assertTrue(view["results"]["comparison"]["same_evidence"])
        summary = view["results"]["summary"]
        self.assertEqual((summary["label"], summary["model"]), (SUMMARY_LABEL, "fake-qwen"))
        for key in KEYS:  # the fake MCP server lists its env names: no API key reached the child
            self.assertNotIn(key, summary["summary"])

        claude = self.server.calls("anthropic")
        for request, stage in zip(claude, CLAUDE_STAGES, strict=True):
            body, headers = request["body"], request["headers"]
            key = headers.get("x-api-key") or headers["authorization"].removeprefix("Bearer ")  # SDK 2.6: Bearer
            self.assertEqual(set(body), CLAUDE_KEYS, stage)
            self.assertEqual(
                (body["model"], body["max_tokens"], body["system"], body["stream"], key),
                (s.claude_model, s.claude_max_tokens[stage], prompts.SYSTEM[stage], True, KEYS["ANTHROPIC_API_KEY"]),
            )
            self.assertEqual(body["output_config"], {
                "effort": s.claude_effort[stage], "format": {"type": "json_schema", "schema": BY_STAGE[stage]},
            })
        user = claude[0]["body"]["messages"][0]["content"]
        self.assertEqual(re.findall(r'<evidence id="(\w+)"', user), analysis["bundle_ids"])
        self.assertIn(f"<tier>{view['tier']}</tier>", claude[1]["body"]["messages"][0]["content"])
        for request, stage in zip(claude[2:], ("feedback1", "feedback2"), strict=True):  # exactly as previewed
            self.assertEqual(request["body"]["messages"], [
                {"role": "user", "content": entry(previews[stage], "anthropic")["payload_text"]},
            ])

        review = {"role": "system", "content": prompts.SYSTEM["review"]}
        [hcx] = self.server.calls("hcx")
        self.assertEqual((hcx["path"], hcx["headers"]["authorization"]),
                         (f"/v3/chat-completions/{s.hcx_model}", f"Bearer {KEYS['CLOVA_API_KEY']}"))
        self.assertEqual(hcx["body"], {
            "messages": [review, {"role": "user", "content": entry(previews["review:hcx"], "hcx")["payload_text"]}],
            "topP": 0.8, "topK": 0, "temperature": 0.5, "repetitionPenalty": 1.1, "seed": 7, "includeAiFilters": False,
            "maxCompletionTokens": s.hcx_max_completion_tokens, "thinking": {"effort": s.hcx_thinking_effort},
        })
        [deepseek] = self.server.calls("mistral")
        self.assertEqual(deepseek["headers"]["authorization"], f"Bearer {KEYS['MISTRAL_API_KEY']}")
        self.assertEqual(deepseek["body"], {
            "model": s.mistral_model,
            "messages": [review, {"role": "user", "content": entry(previews["review:deepseek"], "deepseek")["payload_text"]}],
            "max_tokens": s.mistral_max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "review", "schema": REVIEW, "strict": True}},
        })
        charge = math.ceil(MISTRAL_USAGE["prompt_tokens"] * s.mistral_price_in / 1e6) + math.ceil(
            MISTRAL_USAGE["completion_tokens"] * s.mistral_price_out / 1e6
        )
        status = app.state.providers.deepseek.ledger.status()
        self.assertEqual((self.ledger_ops(), status["spent"], status["unsettled"]), (["reserve", "settle"], charge, 0))

        for request in self.server.requests:
            sent = "\n".join(strings(request["body"]))
            for value in [*FAKE_SECRETS, summary["summary"]]:  # nor the Local LLM summary: it never feeds a call
                self.assertNotIn(value, sent, request["api"])
        return view, previews, jobs

    def test_demo_weather_flow(self):
        view, _, _ = self.full_flow("demo_weather.jsonl", "c1", "fail")
        self.assertEqual(self.apis(), ["anthropic", "hcx", "mistral", "anthropic", "anthropic", "anthropic"])
        self.assertEqual(view["results"]["retrieval"]["state"], "skipped")  # under 6 human requests
        self.assertEqual(view["calls"], {
            "anthropic": 4, "local_llm": 1, "hcx:review:hcx": 1, "deepseek:review:deepseek": 1,
        })

    def test_many_requests_flow_searches_and_reranks(self):
        view, previews, jobs = self.full_flow("many_requests.jsonl", "c3", "pass")
        self.assertEqual(self.apis(), ["embed", "rerank", "anthropic", "hcx", "mistral", "anthropic", "anthropic", "anthropic"])
        [embed] = self.server.calls("embed")
        body = embed["body"]
        self.assertEqual({**body, "input": None},
                         {"model": "bge-m3", "input": None, "truncate": False, "keep_alive": "5m", "options": {"num_ctx": 4096}})
        rag = entry(previews["analysis"], "local_rag")["items"]  # the query and the pool, exactly as previewed
        self.assertEqual([sha(text) for text in body["input"]], [item["sha256"] for item in rag])

        retrieval = view["results"]["retrieval"]
        [rerank] = self.server.calls("rerank")
        docs = rerank["body"]["documents"]
        self.assertLessEqual(len(docs), 10)
        self.assertEqual((set(rerank["body"]), rerank["body"]["maxTokens"]), ({"query", "documents", "maxTokens"}, 1024))
        self.assertEqual([d["id"] for d in docs], retrieval["retrieved"])
        previewed = {item["evidence_id"]: item["sha256"] for item in entry(previews["analysis"], "clova_rerank")["items"]}
        self.assertEqual([sha(rerank["body"]["query"]), *(sha(d["doc"]) for d in docs)],
                         [previewed["query"], *(previewed[d["id"]] for d in docs)])
        self.assertEqual((retrieval["state"], retrieval["cited"]), ("done", retrieval["retrieved"][:1]))  # mock: first doc
        self.assertEqual(jobs["analysis"]["result"]["search_state"], "done")
        self.assertIn(f'<evidence id="{retrieval["cited"][0]}" kind="user_request"',
                      self.server.calls("anthropic")[0]["body"]["messages"][0]["content"])
        self.assertEqual(view["calls"], {
            "local_rag": 1, "clova_rerank": 1, "anthropic": 4, "local_llm": 1, "hcx:review:hcx": 1,
            "deepseek:review:deepseek": 1,
        })

    def test_nothing_is_sent_before_the_prediction(self):
        with make_client(self.app()) as client:
            upload(client, "pair_fail.jsonl")
            client.post("/api/select", json={"candidate_id": "c1", "self_assessment": SA})
            for path, body in [*MODEL_BOUND, ("/api/reviews", {"provider": "hcx"}), ("/api/reviews", {"provider": "deepseek"})]:
                self.assertEqual(client.post(path, json=body).status_code, 409, path)
            self.assertEqual(self.server.requests, [])
            client.post("/api/prediction", json={"choice": "fail"})
            agree(client, local_llm=False)
            self.assertEqual(run(client, "/api/analysis")["status"], "done")
        [request] = self.server.requests  # the target output, marker included, goes out only after the reveal
        self.assertIn(PAIR_MARKERS[0], request["body"]["messages"][0]["content"])

    def test_claude_5xx_twice_fails_the_analysis_and_a_retry_succeeds(self):
        self.server.scenarios["anthropic"] = ["500", "500", "success"]
        with make_client(self.app()) as client:
            failed = self.analyzed(client, "many_requests.jsonl", "c3", "pass")
            self.assertEqual((failed["status"], failed["error"]), ("failed", error("server_error", True)))
            before = self.results(client)
            self.assertEqual((before["retrieval"]["state"], before["analysis"]), ("done", None))  # the search is kept
            retried = run(client, "/api/analysis")
            self.assertEqual((retried["status"], retried["result"]["search_state"]), ("done", "done"))
            self.assertEqual(self.results(client)["retrieval"], before["retrieval"])
            self.assertEqual(client.get("/api/workspace").json()["calls"], {"local_rag": 1, "clova_rerank": 1, "anthropic": 3})
        self.assertEqual(self.apis(), ["embed", "rerank", "anthropic", "anthropic", "anthropic"])  # searched once

    def test_refusal_and_max_tokens_keep_earlier_results(self):
        self.server.scenarios["anthropic"] = ["success", "refusal", "success", "max_tokens"]
        with make_client(self.app()) as client:
            self.analyzed(client)
            before = self.results(client)
            refused = run(client, "/api/whatif")
            self.assertEqual((refused["status"], refused["error"]), ("failed", error("refusal", False)))
            self.assertEqual(self.results(client), before)
            self.assertEqual(run(client, "/api/whatif")["status"], "done")  # the user may ask again
            before = self.results(client)
            preview = agree(client, "feedback1", ANSWERS)
            cut = run(client, "/api/feedback", {"attempt": 1, "preview_id": preview["preview_id"]})
            self.assertEqual((cut["status"], cut["error"]), ("failed", error("incomplete", True)))
            self.assertEqual(self.results(client), before)
        self.assertEqual(self.apis(), ["anthropic"] * 4)  # the adapter retries neither

    def test_clova_429_on_the_review_is_never_repeated(self):
        self.server.scenarios["hcx"] = ["429", "success"]
        with make_client(self.app()) as client:
            self.analyzed(client)
            before = self.results(client)
            agree(client, "review:hcx")
            for _ in range(2):  # a retry while CLOVA is paused sends nothing
                failed = run(client, "/api/reviews", {"provider": "hcx"})
                # The 429 used this flow's one HCX-007 call, so no retry is offered.
                self.assertEqual((failed["status"], failed["error"]), ("failed", error("provider_rate_limited", False)))
            self.assertEqual(self.results(client), before)
            self.assertEqual(client.get("/api/workspace").json()["calls"]["hcx:review:hcx"], 1)
        self.assertEqual(self.apis(), ["anthropic", "hcx"])

    def test_mistral_402_disables_deepseek(self):
        self.server.scenarios["mistral"] = ["402", "success"]
        app = self.app()
        with make_client(app) as client:
            self.analyzed(client)
            before = self.results(client)
            agree(client, "review:deepseek")
            for _ in range(2):  # disabled: the retry sends nothing
                failed = run(client, "/api/reviews", {"provider": "deepseek"})
                self.assertEqual((failed["status"], failed["error"]), ("failed", error("budget_exceeded", False)))
            self.assertEqual(self.results(client), before)
        self.assertTrue(app.state.providers.deepseek.disabled)
        self.assertEqual(self.apis(), ["anthropic", "mistral"])
        self.assertEqual((self.ledger_ops(), app.state.providers.deepseek.ledger.status()["spent"]), (["reserve", "release"], 0))

    def test_embed_server_down_fails_the_search_and_the_analysis_completes(self):
        with make_client(self.app(ollama_embed_url=unused_url())) as client:
            done = self.analyzed(client, "many_requests.jsonl", "c3", "pass")
            self.assertEqual((done["status"], done["result"]["search_state"]), ("done", "failed"))
            self.assertEqual(self.results(client)["retrieval"], {
                "state": "failed", "state_label": labels.SEARCH_STATE["failed"], "retrieved": [], "cited": [],
            })
        self.assertEqual(self.apis(), ["anthropic"])  # nothing to rerank
        self.assertNotIn("cited=", self.server.requests[0]["body"]["messages"][0]["content"])

    def test_an_empty_anthropic_url_never_falls_back_to_the_environment(self):
        app = self.app(anthropic_base_url="")
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": self.server.base_url}), make_client(app) as client:
            start(client, "demo_weather.jsonl", "c1", choice="fail")
            preview = agree(client, local_llm=False)
            self.assertEqual(entry(preview, "anthropic")["destination"], "주소 미설정")  # what the user consented to
            failed = run(client, "/api/analysis")
            self.assertEqual((failed["status"], failed["error"]), ("failed", error("connection", True)))
        self.assertEqual(self.server.requests, [])

    def test_mcp_timeout_fails_only_the_summary(self):
        slow = {"mcp_command": "/usr/bin/env", "mcp_args": ("FAKE_MCP_SLEEP=30", sys.executable, FAKE_MCP), "mcp_timeout_sec": 1}
        with make_client(self.app(**slow)) as client:
            start(client, "demo_weather.jsonl", "c1", choice="fail")
            agree(client)
            ids = client.post("/api/analysis").json()
            summary, analysis = wait(client, ids["summary_job_id"]), wait(client, ids["job_id"])
            # The one Local LLM call of the flow is used up, so the failure offers no retry.
            self.assertEqual((summary["status"], summary["error"]), ("failed", error("timeout", False)))
            self.assertEqual((analysis["status"], analysis["error"]), ("done", None))
            results = self.results(client)
            self.assertEqual((results["summary"], results["analysis"]), ({"error": error("timeout", False)}, analysis["result"]))
            self.assertEqual(run(client, "/api/whatif")["status"], "done")  # the flow goes on


if __name__ == "__main__":
    unittest.main()
