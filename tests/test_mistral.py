import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from agency_loop.config import Settings
from agency_loop.flow.schemas import REVIEW
from agency_loop.ledger import Ledger
from agency_loop.providers.base import ProviderError
from agency_loop.providers.mistral import DeepSeekReviewer

S = Settings(mistral_base_url="https://mistral.test/")
REVIEW_JSON = {
    "summary": "검토 요약",
    "points": [{"type": "issue", "text": "확인 필요", "evidence_ids": ["E0001"], "quote": ""}],
}


def completion(content=None, *, with_usage=True):
    body = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "model": "deepseek-v4-1-flash-1017",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(REVIEW_JSON) if content is None else content},
                "finish_reason": "stop",
            }
        ],
    }
    if with_usage:
        body["usage"] = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
    return body


def reservation_for(request):
    return math.ceil((len(request.content) + 4096) * S.mistral_price_in / 1e6) + math.ceil(
        max(S.mistral_max_tokens, S.mistral_out_ub_tokens) * S.mistral_price_out / 1e6
    )


class DeepSeekReviewerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "usage.jsonl"
        Ledger.init(self.path)
        self.ledger = self.make_ledger(S.mistral_cap_micro_usd)
        self.events = []  # ("gate", provider) and ("request", httpx.Request) in call order

    def make_ledger(self, cap):
        return Ledger(
            self.path, cap_micro_usd=cap, price_in=S.mistral_price_in, price_out=S.mistral_price_out,
            out_ub_tokens=S.mistral_out_ub_tokens,
        )

    def gate(self, provider):
        self.events.append(("gate", provider))

    def reviewer(self, respond, *, ledger=None, api_key="test-key"):
        def handler(request):
            self.events.append(("request", request))
            return respond(request)

        return DeepSeekReviewer(S, ledger or self.ledger, api_key=api_key, transport=httpx.MockTransport(handler))

    async def call(self, reviewer, **kwargs):
        return await reviewer.review(system="시스템 지시", user="<task>검토</task>", schema=REVIEW, gate=self.gate, **kwargs)

    @property
    def requests(self):
        return [e[1] for e in self.events if e[0] == "request"]

    async def assert_error(self, reviewer, code):
        with self.assertRaises(ProviderError) as cm:
            await self.call(reviewer)
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    async def test_success_sends_one_contract_request_and_settles(self):
        held_at_send = []

        def respond(request):
            held_at_send.append(self.ledger.status()["unsettled"])
            return httpx.Response(200, json=completion())

        statuses = []
        result = await self.call(self.reviewer(respond), on_status=lambda s, _msg: statuses.append(s))

        self.assertEqual([e[0] for e in self.events], ["gate", "request"])
        self.assertEqual(self.events[0][1], "deepseek")
        request = self.requests[0]
        self.assertEqual(held_at_send, [reservation_for(request)])  # reserved before sending, from the body size
        self.assertEqual((request.method, str(request.url)), ("POST", "https://mistral.test/v1/chat/completions"))
        self.assertEqual(request.headers["authorization"], "Bearer test-key")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(
            json.loads(request.content),
            {
                "model": S.mistral_model,
                "messages": [
                    {"role": "system", "content": "시스템 지시"},
                    {"role": "user", "content": "<task>검토</task>"},
                ],
                "max_tokens": S.mistral_max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "review", "schema": REVIEW, "strict": True},
                },
            },
        )

        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(result.model, "deepseek-v4-1-flash-1017")
        self.assertEqual(result.data, REVIEW_JSON)
        self.assertEqual(result.text, json.dumps(REVIEW_JSON))
        self.assertEqual((result.stop_reason, result.attempts, result.mock), ("stop", 1, False))
        charge = math.ceil(1000 * S.mistral_price_in / 1e6) + math.ceil(500 * S.mistral_price_out / 1e6)
        self.assertEqual(result.usage, {"prompt_tokens": 1000, "completion_tokens": 500, "charged_micro_usd": charge})
        status = self.ledger.status()
        self.assertEqual((status["spent"], status["unsettled"]), (charge, 0))
        self.assertEqual(statuses, ["running", "validating"])

    async def test_output_outside_the_schema_keeps_text_and_still_settles(self):
        for content in ("JSON이 아님", json.dumps({"summary": "points 없음"})):
            with self.subTest(content=content):
                result = await self.call(self.reviewer(lambda r, c=content: httpx.Response(200, json=completion(c))))
                self.assertIsNone(result.data)
                self.assertEqual(result.text, content)
                self.assertEqual(self.ledger.status()["unsettled"], 0)

    async def test_chunked_content_keeps_text_chunks_only(self):
        chunks = [
            {"type": "thinking", "thinking": [{"type": "text", "text": "숨겨야 할 추론"}]},
            {"type": "text", "text": json.dumps(REVIEW_JSON)},
        ]
        result = await self.call(self.reviewer(lambda r: httpx.Response(200, json=completion(chunks))))
        self.assertEqual(result.data, REVIEW_JSON)
        self.assertNotIn("숨겨야 할 추론", result.text)

    async def test_missing_usage_keeps_the_reservation_and_reports_it(self):
        reviewer = self.reviewer(lambda r: httpx.Response(200, json=completion(with_usage=False)))
        with self.assertLogs("agency_loop.providers.mistral", "WARNING") as logs:
            result = await self.call(reviewer)
        self.assertEqual(result.data, REVIEW_JSON)
        reserved = reservation_for(self.requests[0])
        self.assertEqual(result.usage, {"reserved_micro_usd": reserved, "settled": False})
        status = self.ledger.status()
        self.assertEqual((status["spent"], status["unsettled"]), (reserved, reserved))
        for text in ("검토 요약", "시스템 지시", "test-key"):  # logs carry metadata only
            self.assertNotIn(text, "\n".join(logs.output))

    async def test_unreadable_200_body_keeps_the_reservation(self):
        reviewer = self.reviewer(lambda r: httpx.Response(200, text="<html>proxy</html>"))
        await self.assert_error(reviewer, "invalid_output")
        self.assertEqual(self.ledger.status()["unsettled"], reservation_for(self.requests[0]))

    async def test_402_releases_and_disables_the_reviewer(self):
        reviewer = self.reviewer(lambda r: httpx.Response(402, json={"message": "no credit"}))
        error = await self.assert_error(reviewer, "budget_exceeded")
        self.assertEqual((error.status, error.attempted), (402, True))
        self.assertEqual(self.ledger.status()["spent"], 0)
        self.assertTrue(reviewer.disabled)
        await self.assert_error(reviewer, "budget_exceeded")
        self.assertEqual([e[0] for e in self.events], ["gate", "request"])  # second call: no gate, no request

    async def test_4xx_releases_and_never_retries(self):
        cases = [
            (400, "bad_request"), (401, "auth"), (403, "auth"), (404, "bad_request"), (422, "bad_request"),
            (429, "provider_rate_limited"),
        ]
        for status, code in cases:
            with self.subTest(status=status):
                self.events.clear()
                error = await self.assert_error(self.reviewer(lambda r, s=status: httpx.Response(s, json={})), code)
                self.assertEqual(error.status, status)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.ledger.status()["spent"], 0)

    async def test_5xx_keeps_the_reservation(self):
        for status in (500, 503):
            with self.subTest(status=status):
                self.events.clear()
                before = self.ledger.status()["unsettled"]
                error = await self.assert_error(self.reviewer(lambda r, s=status: httpx.Response(s)), "server_error")
                self.assertEqual(error.status, status)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.ledger.status()["unsettled"] - before, reservation_for(self.requests[0]))

    async def test_connect_failures_release(self):
        for exc in (httpx.ConnectError, httpx.ConnectTimeout):
            with self.subTest(exc=exc.__name__):
                self.events.clear()

                def respond(request, exc=exc):
                    raise exc("unreachable", request=request)

                error = await self.assert_error(self.reviewer(respond), "connection")
                self.assertFalse(error.attempted)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.ledger.status()["spent"], 0)

    async def test_failures_after_sending_keep_the_reservation(self):
        cases = [
            (httpx.ReadTimeout, "timeout"), (httpx.WriteTimeout, "timeout"),
            (httpx.RemoteProtocolError, "connection"), (httpx.ReadError, "connection"),
        ]
        for exc, code in cases:
            with self.subTest(exc=exc.__name__):
                self.events.clear()
                before = self.ledger.status()["unsettled"]

                def respond(request, exc=exc):
                    raise exc("lost", request=request)

                error = await self.assert_error(self.reviewer(respond), code)
                self.assertTrue(error.attempted)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.ledger.status()["unsettled"] - before, reservation_for(self.requests[0]))

    async def test_ledger_refusal_sends_nothing(self):
        def respond(request):
            return httpx.Response(200, json=completion())

        with self.subTest("cap"):
            await self.assert_error(self.reviewer(respond, ledger=self.make_ledger(1000)), "budget_exceeded")
        with self.subTest("halted"):
            self.ledger.reserve("x", 0, 0)
            self.ledger.settle("x", 0, 10**6)  # overrun
            await self.assert_error(self.reviewer(respond), "halted")
        with self.subTest("missing"):
            self.path.unlink()
            error = await self.assert_error(self.reviewer(respond), "ledger_missing")
            self.assertIn("python -m agency_loop.ledger init", error.message)  # tells the operator what to run
            self.assertFalse(self.path.exists())
        with self.subTest("corrupt"):
            self.path.write_bytes(b"not a ledger record\n")
            await self.assert_error(self.reviewer(respond), "halted")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.events, [])  # the ledger refuses before the gate, so no per-flow call is used up

    async def test_gate_refusal_releases_the_reservation(self):
        def gate(provider):
            raise ProviderError("cap_exceeded")

        reviewer = self.reviewer(lambda r: httpx.Response(200, json=completion()))
        with self.assertRaises(ProviderError) as cm:
            await reviewer.review(system="s", user="u", schema=REVIEW, gate=gate)
        self.assertEqual(cm.exception.code, "cap_exceeded")
        self.assertEqual(self.requests, [])
        status = self.ledger.status()
        self.assertEqual((status["spent"], status["unsettled"]), (0, 0))

    async def test_missing_api_key_reserves_and_sends_nothing(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("MISTRAL_API_KEY", None)
            await self.assert_error(self.reviewer(lambda r: httpx.Response(200), api_key=None), "auth")
        self.assertEqual((self.events, self.path.read_bytes()), ([], b""))

    async def test_api_key_is_read_from_the_environment_at_call_time(self):
        reviewer = self.reviewer(lambda r: httpx.Response(200, json=completion()), api_key=None)
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": " env-test-key\n"}):
            await self.call(reviewer)
        self.assertEqual(self.requests[0].headers["authorization"], "Bearer env-test-key")


if __name__ == "__main__":
    unittest.main()
