import unittest

from agency_loop.config import Settings
from agency_loop.providers.base import ProviderError
from agency_loop.providers.ollama_embed import OllamaEmbedder
from tests.mock_servers import MockApiServer, unused_url


class OllamaEmbedderTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockApiServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.server.reset()
        self.gates = []

    async def embed(self, texts, url=None):
        embedder = OllamaEmbedder(Settings(ollama_embed_url=self.server.base_url + "/" if url is None else url))
        return await embedder.embed(texts, gate=self.gates.append)

    async def assert_error(self, code, call):
        with self.assertRaises(ProviderError) as cm:
            await call
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    async def test_success_sends_the_contract_request(self):
        vectors = await self.embed(["날씨 요청", "캐시"])

        [request] = self.server.calls("embed")
        self.assertEqual((request["method"], request["path"]), ("POST", "/api/embed"))
        self.assertEqual(
            request["body"],
            {
                "model": "bge-m3",
                "input": ["날씨 요청", "캐시"],
                "truncate": False,
                "keep_alive": "5m",
                "options": {"num_ctx": 4096},
            },
        )
        self.assertEqual(vectors, [[5.0, 1.0], [2.0, 1.0]])  # mock server vector = [chunk length, 1.0]
        self.assertEqual(self.gates, ["local_rag"])

    async def test_long_inputs_are_split_and_averaged_in_one_request(self):
        vectors = await self.embed(["a" * 9000, "b", ""])

        [request] = self.server.calls("embed")
        self.assertEqual([len(t) for t in request["body"]["input"]], [4000, 4000, 1000, 1, 0])
        self.assertEqual(vectors, [[3000.0, 1.0], [1.0, 1.0], [0.0, 1.0]])
        self.assertEqual(self.gates, ["local_rag"])

    async def test_no_texts_sends_nothing(self):
        self.assertEqual(await self.embed([]), [])
        self.assertEqual((self.server.requests, self.gates), ([], []))

    async def test_missing_url_is_disabled(self):
        await self.assert_error("disabled", self.embed(["x"], url=""))
        self.assertEqual(self.gates, [])

    async def test_errors(self):
        for scenario, code in (("500", "server_error"), ("404", "bad_request"), ("bad_count", "invalid_output")):
            with self.subTest(scenario):
                self.server.reset()
                self.server.scenarios["embed"] = [scenario]
                error = await self.assert_error(code, self.embed(["x", "y"]))
                self.assertTrue(error.attempted)
        error = await self.assert_error("connection", self.embed(["x"], url=unused_url()))
        self.assertFalse(error.attempted)


if __name__ == "__main__":
    unittest.main()
