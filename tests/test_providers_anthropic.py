import json
import logging
import os
import time
import unittest
from unittest import mock

from agency_loop.config import Settings
from agency_loop.flow.schemas import ANALYSIS
from agency_loop.providers import anthropic_claude
from agency_loop.providers.anthropic_claude import ClaudeClient
from agency_loop.providers.base import ProviderError
from tests.mock_servers import ANALYSIS_JSON, ANTHROPIC_MODEL, MockApiServer, unused_url


class ClaudeClientTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockApiServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.server.reset()
        self.gates = []
        self.statuses = []
        patcher = mock.patch.object(anthropic_claude, "RETRY_DELAY_SEC", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, base_url=None, api_key="test-key"):
        return ClaudeClient(Settings(anthropic_base_url=base_url or self.server.base_url), api_key=api_key)

    async def call(self, client=None, *, gate=None, **kwargs):
        args = {
            "stage": "analysis", "system": "시스템 지시", "user": "<task>분석</task>", "schema": ANALYSIS,
            "effort": "high", "max_tokens": 32000,
        }
        return await (client or self.client()).generate(
            **{**args, **kwargs}, gate=gate or self.gates.append, on_status=lambda s, _m: self.statuses.append(s)
        )

    async def assert_error(self, code, *scenarios, client=None):
        if scenarios:
            self.server.scenarios["anthropic"] = list(scenarios)
        with self.assertRaises(ProviderError) as cm:
            await self.call(client)
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    @property
    def requests(self):
        return self.server.calls("anthropic")

    async def test_success_sends_the_contract_request(self):
        result = await self.call()

        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual((request["method"], request["path"]), ("POST", "/v1/messages"))
        headers = request["headers"]
        self.assertTrue(headers.get("x-api-key") == "test-key" or headers.get("authorization") == "Bearer test-key")
        body = request["body"]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["max_tokens"], 32000)
        self.assertEqual(body["system"], "시스템 지시")
        self.assertEqual(body["messages"], [{"role": "user", "content": "<task>분석</task>"}])
        self.assertIs(body["stream"], True)
        self.assertEqual(body["output_config"]["effort"], "high")
        self.assertEqual(body["output_config"]["format"], {"type": "json_schema", "schema": ANALYSIS})
        for key in ("thinking", "temperature", "top_p", "top_k"):
            self.assertNotIn(key, body)

        self.assertEqual((result.provider, result.model), ("anthropic", ANTHROPIC_MODEL))  # model from the response
        self.assertEqual(result.data, ANALYSIS_JSON)
        self.assertEqual(json.loads(result.text), ANALYSIS_JSON)
        self.assertEqual((result.stop_reason, result.attempts, result.mock), ("end_turn", 1, False))
        self.assertEqual(result.usage, {"input_tokens": 120, "output_tokens": 45})
        self.assertEqual(self.gates, ["anthropic"])
        self.assertEqual(self.statuses, ["streaming", "validating"])
        self.assertFalse(logging.getLogger("anthropic").isEnabledFor(logging.DEBUG))  # SDK debug logs carry prompts

    async def test_effort_and_max_tokens_follow_the_call(self):
        await self.call(stage="feedback1", effort="medium", max_tokens=12000)
        body = self.requests[0]["body"]
        self.assertEqual((body["output_config"]["effort"], body["max_tokens"]), ("medium", 12000))

    async def test_refusal_and_max_tokens_are_not_retried(self):
        for scenario, code, retryable in (("refusal", "refusal", False), ("max_tokens", "incomplete", True)):
            with self.subTest(scenario):
                self.server.reset()
                self.gates.clear()
                error = await self.assert_error(code, scenario)
                self.assertEqual((error.retryable, error.attempted), (retryable, True))
                self.assertEqual((len(self.requests), self.gates), (1, ["anthropic"]))

    async def test_unparseable_or_off_schema_output_is_invalid_output(self):
        for scenario, text in (("invalid_json", None), ("success", json.dumps({"decision_summary": "만"}))):
            with self.subTest(scenario):
                self.server.reset()
                if text:
                    self.server.claude_text = text
                await self.assert_error("invalid_output", scenario)
                self.assertEqual(len(self.requests), 1)

    async def test_429_is_retried_once_after_retry_after(self):
        self.server.scenarios["anthropic"] = ["429", "success"]
        self.server.retry_after = "0.3"
        started = time.monotonic()
        result = await self.call()
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual((result.attempts, len(self.requests)), (2, 2))
        self.assertEqual(self.gates, ["anthropic", "anthropic"])  # the gate runs before the retry too

    def test_retry_after_is_honoured_up_to_20_seconds(self):
        retry_after = anthropic_claude._retry_after
        self.assertEqual((retry_after("7"), retry_after("20")), (7.0, 20.0))
        self.assertIsNone(retry_after("21"))
        for value in (None, "Wed, 21 Oct 2015 07:28:00 GMT"):  # missing or date form: default wait (patched to 0)
            self.assertEqual(retry_after(value), 0)

    async def test_429_twice_reports_rate_limit(self):
        error = await self.assert_error("provider_rate_limited", "429")
        self.assertEqual((error.status, error.retryable), (429, True))
        self.assertEqual((len(self.requests), len(self.gates)), (2, 2))

    async def test_429_with_long_retry_after_is_not_retried(self):
        self.server.retry_after = "30"
        await self.assert_error("provider_rate_limited", "429", "success")
        self.assertEqual((len(self.requests), len(self.gates)), (1, 1))

    async def test_5xx_and_in_stream_errors_are_retried_once(self):
        for scenario in ("500", "529", "stream_error"):
            with self.subTest(scenario):
                self.server.reset()
                self.gates.clear()
                error = await self.assert_error("server_error", scenario)
                self.assertTrue(error.retryable)
                self.assertEqual((len(self.requests), len(self.gates)), (2, 2))
        self.server.reset()
        self.server.scenarios["anthropic"] = ["529", "success"]
        self.assertEqual((await self.call()).attempts, 2)

    async def test_client_errors_are_not_retried(self):
        for scenario, code in (("400", "bad_request"), ("404", "bad_request"), ("401", "auth"), ("403", "auth")):
            with self.subTest(scenario):
                self.server.reset()
                self.gates.clear()
                error = await self.assert_error(code, scenario)
                self.assertEqual((error.status, error.retryable), (int(scenario), False))
                self.assertEqual((len(self.requests), self.gates), (1, ["anthropic"]))

    async def test_connection_error_is_retried_once(self):
        error = await self.assert_error("connection", client=self.client(base_url=unused_url()))
        self.assertTrue(error.retryable)
        self.assertEqual(self.gates, ["anthropic", "anthropic"])

    async def test_gate_refusal_stops_before_any_request(self):
        def gate(provider):
            raise ProviderError("cap_exceeded")

        with self.assertRaises(ProviderError) as cm:
            await self.call(gate=gate)
        self.assertEqual(cm.exception.code, "cap_exceeded")
        self.assertEqual(self.requests, [])

    async def test_gate_refusal_on_the_retry_stops_the_retry(self):
        self.server.scenarios["anthropic"] = ["500", "success"]

        def gate(provider):
            self.gates.append(provider)
            if len(self.gates) == 2:
                raise ProviderError("daily_cap")

        with self.assertRaises(ProviderError) as cm:
            await self.call(gate=gate)
        self.assertEqual(cm.exception.code, "daily_cap")
        self.assertEqual(len(self.requests), 1)

    async def test_missing_api_key_sends_nothing(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            await self.assert_error("auth", client=self.client(api_key=None))
        self.assertEqual((self.requests, self.gates), ([], []))

    async def test_api_key_is_read_from_the_environment_at_call_time(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": " env-test-key\n"}):
            await self.call(self.client(api_key=None))
        headers = self.requests[0]["headers"]
        self.assertIn("env-test-key", (headers.get("x-api-key"), headers.get("authorization", "")[7:]))


if __name__ == "__main__":
    unittest.main()
