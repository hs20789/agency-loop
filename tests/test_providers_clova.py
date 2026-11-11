import os
import unittest
import uuid
from unittest import mock

from agency_loop.config import Settings
from agency_loop.flow.schemas import REVIEW
from agency_loop.providers import clova
from agency_loop.providers.base import ProviderError
from agency_loop.providers.clova import ClovaReranker, HcxReviewer
from tests.mock_servers import REVIEW_JSON, MockApiServer, unused_url

DOCS = [{"id": "E0001", "doc": "날씨 API 시간 제한"}, {"id": "E0002", "doc": "응답 캐시"}]


class ClovaTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockApiServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.server.reset()
        self.gates = []
        self.now = 1000.0
        for name, value in (("clock", lambda: self.now), ("_cooldown_until", 0.0)):
            patcher = mock.patch.object(clova, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self, base_url=None):
        return Settings(clova_base_url=(base_url or self.server.base_url) + "/")

    def hcx(self, base_url=None, api_key="test-key"):
        return HcxReviewer(self.settings(base_url), api_key=api_key)

    def reranker(self, base_url=None):
        return ClovaReranker(self.settings(base_url), api_key="test-key")

    async def review(self, reviewer=None, **kwargs):
        return await (reviewer or self.hcx()).review(
            system="시스템 지시", user="<task>검토</task>", schema=REVIEW, gate=self.gates.append, **kwargs
        )

    async def rerank(self, documents=DOCS, reranker=None):
        return await (reranker or self.reranker()).rerank("날씨 API 시간 제한", documents, gate=self.gates.append)

    async def assert_error(self, code, call):
        with self.assertRaises(ProviderError) as cm:
            await call
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    def assert_clova_headers(self, headers):
        self.assertEqual(headers["authorization"], "Bearer test-key")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(uuid.UUID(headers["x-ncp-clovastudio-request-id"]).version, 4)

    async def test_hcx_success_sends_the_contract_request(self):
        statuses = []
        result = await self.review(on_status=lambda s, _m: statuses.append(s))

        [request] = self.server.calls("hcx")
        self.assertEqual((request["method"], request["path"]), ("POST", "/v3/chat-completions/HCX-007"))
        self.assert_clova_headers(request["headers"])
        self.assertEqual(
            request["body"],
            {
                "messages": [
                    {"role": "system", "content": "시스템 지시"},
                    {"role": "user", "content": "<task>검토</task>"},
                ],
                "topP": 0.8,
                "topK": 0,
                "temperature": 0.5,
                "repetitionPenalty": 1.1,
                "seed": 7,
                "includeAiFilters": False,
                "maxCompletionTokens": 5120,
                "thinking": {"effort": "low"},
            },
        )
        self.assertEqual((result.provider, result.model, result.mock), ("hcx", "HCX-007", False))
        self.assertEqual(result.data, REVIEW_JSON)  # parsed leniently out of prose and a code fence
        self.assertEqual(result.text, self.server.hcx_content)
        self.assertNotIn("숨겨진 추론", result.text)
        self.assertEqual((result.stop_reason, result.attempts), ("stop", 1))
        self.assertEqual(result.usage, {"prompt_tokens": 120, "completion_tokens": 40})
        self.assertEqual((self.gates, statuses), (["hcx"], ["validating"]))

    async def test_hcx_output_outside_the_schema_keeps_the_text(self):
        for scenario, content in (("not_json", None), ("success", '{"summary": "points 없음"}')):
            with self.subTest(scenario):
                self.server.reset()
                self.server.scenarios["hcx"] = [scenario]
                if content:
                    self.server.hcx_content = content
                result = await self.review()
                self.assertIsNone(result.data)
                self.assertTrue(result.text)

    async def test_hcx_reply_without_empty_keys_is_repaired(self):
        # Seen from the live HCX-007: a point with no quote key at all, and an extra key the schema does not allow.
        self.server.scenarios["hcx"] = ["success"]
        self.server.hcx_content = (
            '{"summary": "오류 처리 구현이 드러나지 않음", "points": [{"type": "missing_evidence", '
            '"text": "오류 처리 파일의 구현과 테스트가 부족해 검토 불가", "evidence_ids": [], "confidence": "low"}]}'
        )
        result = await self.review()
        self.assertEqual(result.data["points"], [{
            "type": "missing_evidence", "text": "오류 처리 파일의 구현과 테스트가 부족해 검토 불가",
            "evidence_ids": [], "quote": "",
        }])
        # Only empty-able keys are filled: a point without its text is still outside the schema.
        self.server.reset()
        self.server.scenarios["hcx"] = ["success"]
        self.server.hcx_content = '{"summary": "s", "points": [{"type": "issue", "evidence_ids": [], "quote": ""}]}'
        self.assertIsNone((await self.review()).data)

    async def test_hcx_reply_with_a_raw_tab_in_a_quote_is_read(self):
        # Seen from the live HCX-007: mutation-test output copied into a quote with its tab, which strict JSON refuses.
        self.server.scenarios["hcx"] = ["success"]
        self.server.hcx_content = (
            '{"summary": "테스트가 복원됨", "points": [{"type": "issue", "text": "변이가 검출됨", '
            '"evidence_ids": ["E0001"], "quote": "KILLED\tarchived category blocks keeping it on update"}]}'
        )
        result = await self.review()
        self.assertEqual(result.data["points"][0]["quote"], "KILLED\tarchived category blocks keeping it on update")

    async def test_hcx_errors_are_mapped_and_never_retried(self):
        cases = [
            ("400", "bad_request", False), ("401", "auth", False), ("500", "server_error", True),
            ("status_error", "bad_request", False),
        ]
        for scenario, code, retryable in cases:
            with self.subTest(scenario):
                self.server.reset()
                self.gates.clear()
                self.server.scenarios["hcx"] = [scenario]
                error = await self.assert_error(code, self.review())
                self.assertEqual((error.retryable, error.attempted), (retryable, True))
                self.assertEqual((len(self.server.calls("hcx")), self.gates), (1, ["hcx"]))

    async def test_429_pauses_all_clova_calls_for_60_seconds(self):
        self.server.scenarios["hcx"] = ["429", "success"]
        error = await self.assert_error("provider_rate_limited", self.review())
        self.assertEqual((error.status, error.retryable), (429, True))

        self.now += 59
        await self.assert_error("provider_rate_limited", self.review())
        await self.assert_error("provider_rate_limited", self.rerank())
        self.assertEqual(len(self.server.requests), 1)  # nothing sent during the pause
        self.assertEqual(self.gates, ["hcx"])  # and no gate call, so no cap is spent

        self.now += 1
        self.assertEqual((await self.review()).data, REVIEW_JSON)
        self.assertEqual(await self.rerank(), ["E0001"])

    async def test_reranker_429_also_pauses_hcx(self):
        self.server.scenarios["rerank"] = ["429"]
        await self.assert_error("provider_rate_limited", self.rerank())
        await self.assert_error("provider_rate_limited", self.review())
        self.assertEqual(self.server.calls("hcx"), [])

    async def test_connection_error(self):
        error = await self.assert_error("connection", self.review(self.hcx(base_url=unused_url())))
        self.assertFalse(error.attempted)
        self.assertEqual(self.gates, ["hcx"])

    async def test_missing_api_key_sends_nothing(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLOVA_API_KEY", None)
            await self.assert_error("auth", self.review(self.hcx(api_key=None)))
        self.assertEqual((self.server.requests, self.gates), ([], []))

    async def test_reranker_sends_the_contract_request_and_returns_cited_ids(self):
        self.server.rerank_cited = ["E0002", "E9999", "E0001", "E0002"]  # unknown and duplicate ids are dropped
        self.assertEqual(await self.rerank(), ["E0002", "E0001"])

        [request] = self.server.calls("rerank")
        self.assertEqual((request["method"], request["path"]), ("POST", "/v1/api-tools/reranker"))
        self.assert_clova_headers(request["headers"])
        self.assertEqual(request["body"], {"query": "날씨 API 시간 제한", "documents": DOCS, "maxTokens": 1024})
        self.assertEqual(self.gates, ["clova_rerank"])

    async def test_reranker_sends_only_id_and_doc_of_at_most_10_documents(self):
        docs = [{"id": f"E{i:04d}", "doc": f"문서 {i}", "sha256": "x"} for i in range(12)]
        await self.rerank(docs)
        sent = self.server.calls("rerank")[0]["body"]["documents"]
        self.assertEqual(sent, [{"id": d["id"], "doc": d["doc"]} for d in docs[:10]])

    async def test_reranker_with_no_documents_sends_nothing(self):
        self.assertEqual(await self.rerank([]), [])
        self.assertEqual((self.server.requests, self.gates), ([], []))

    async def test_reranker_errors(self):
        for scenario, code in (("500", "server_error"), ("401", "auth"), ("400", "bad_request")):
            with self.subTest(scenario):
                self.server.reset()
                self.server.scenarios["rerank"] = [scenario]
                await self.assert_error(code, self.rerank())


if __name__ == "__main__":
    unittest.main()
