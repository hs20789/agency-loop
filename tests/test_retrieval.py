"""agency_loop.flow.retrieval: similarity, top-k, consented search and reranking (ARCHITECTURE §5.1, §5.9)."""

import unittest
from dataclasses import replace

from agency_loop.config import Settings
from agency_loop.flow.bundle import retrieval_pool
from agency_loop.flow.caps import Caps
from agency_loop.flow.consent import make_preview, record_consent
from agency_loop.flow.retrieval import cosine, rerank, retrieve, top_k
from agency_loop.ingest.parser import parse_session
from agency_loop.providers import mock
from agency_loop.providers.base import ProviderError
from agency_loop.providers.registry import build_providers
from tests.test_consent import ALL_ANALYSIS, demo, many
from tests.test_evidence import PREDICTION, workspace
from tests.test_parser import ai, human, jsonl, result, use

SETTINGS = replace(Settings(), rag_min_similarity=0.2)  # the bag-of-words mock gives low similarities
MOCKS = build_providers(SETTINGS)


def consent(ws, **decisions):
    preview = make_preview(ws, "analysis", SETTINGS, MOCKS)
    record_consent(ws, preview["preview_id"], {**ALL_ANALYSIS, **decisions})


class Untouchable:
    """A provider that fails the test when it is called."""

    async def embed(self, texts, *, gate):
        raise AssertionError("embedder called")

    async def rerank(self, query, documents, *, gate):
        raise AssertionError("reranker called")


class Recorder(mock.MockReranker):
    def __init__(self):
        super().__init__(delay=0)
        self.calls = []

    async def rerank(self, query, documents, *, gate):
        self.calls.append((query, documents))
        return await super().rerank(query, documents, gate=gate)


class SimilarityTest(unittest.TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(cosine([1.0, 0.0], [2.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 3.0]), 0.0)
        self.assertAlmostEqual(cosine([1.0, 1.0], [-1.0, -1.0]), -1.0)
        self.assertEqual(cosine([0.0, 0.0], [1.0, 2.0]), 0.0)
        with self.assertRaises(ValueError):
            cosine([1.0], [1.0, 2.0])

    def test_top_k_threshold_order_and_ties(self):
        pool = [("a", [1.0, 0.0]), ("b", [0.0, 1.0]), ("c", [1.0, 1.0]), ("d", [1.0, 0.0]), ("e", [-1.0, 0.0])]
        found = top_k([1.0, 0.0], pool, 3, 0.5)
        self.assertEqual([i for i, _ in found], ["a", "d", "c"])  # ties keep pool order
        self.assertAlmostEqual(found[2][1], 0.7071, places=4)
        self.assertEqual([i for i, _ in top_k([1.0, 0.0], pool, 1, 0.5)], ["a"])
        self.assertEqual(top_k([1.0, 0.0], pool, 5, 1.01), [])
        self.assertEqual(top_k([1.0, 0.0], [], 5, 0.0), [])


class RetrieveTest(unittest.IsolatedAsyncioTestCase):
    async def test_consented_search_over_the_pool(self):
        ws, caps = many(), Caps(SETTINGS)
        consent(ws)
        found = await retrieve(ws, SETTINGS, mock.MockEmbedder(delay=0), caps.gate(ws, "analysis"))
        self.assertEqual(set(found), {"state", "state_label", "retrieved", "scores"})
        self.assertEqual((found["state"], found["state_label"]), ("done", "검색 완료"))
        self.assertEqual(found["retrieved"], ["E0012", "E0024"])  # deterministic mock vectors
        self.assertLessEqual(set(found["retrieved"]), set(retrieval_pool(ws)))
        scores = [found["scores"][i] for i in found["retrieved"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(score >= SETTINGS.rag_min_similarity for score in scores))
        self.assertEqual(ws.calls, {"local_rag": 1})

        top_one = await retrieve(ws, replace(SETTINGS, rag_top_k=1), mock.MockEmbedder(delay=0), lambda _p: None)
        self.assertEqual(top_one["retrieved"], ["E0012"])
        strict = await retrieve(ws, replace(SETTINGS, rag_min_similarity=0.99), mock.MockEmbedder(delay=0), lambda _p: None)
        self.assertEqual((strict["state"], strict["retrieved"], strict["scores"]), ("no_results", [], {}))

    async def test_skipped_without_consent_or_provider_and_never_called(self):
        ws, caps = many(), Caps(SETTINGS)
        consent(ws, local_rag=False)
        gate = caps.gate(ws, "analysis")
        for embedder in (Untouchable(), None):
            self.assertEqual((await retrieve(ws, SETTINGS, embedder, gate))["state"], "skipped")
        small = demo()  # 4 human requests: the preview made local_rag not applicable, so its consent is false
        consent(small)
        self.assertEqual((await retrieve(small, SETTINGS, Untouchable(), caps.gate(small, "analysis")))["state"], "skipped")
        self.assertEqual((ws.calls, small.calls), ({}, {}))

    async def test_refused_while_blinded_and_insufficient_evidence(self):
        blinded = many()
        blinded.prediction = None
        with self.assertRaises(ValueError):
            await retrieve(blinded, SETTINGS, Untouchable(), lambda _p: None)
        with self.assertRaises(ValueError):
            await rerank(blinded, Untouchable(), lambda _p: None, ["E0012"])
        session = parse_session(jsonl(human("고쳐"), ai(use("t1", "Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}))))
        weak = workspace(session, selected="c1")
        found = await retrieve(weak, SETTINGS, Untouchable(), lambda _p: None)
        self.assertEqual((found["state"], found["state_label"]), ("insufficient", "근거 부족"))

    async def test_no_requests_outside_the_window(self):
        records = [human("첫 요청: 시간 제한을 넣어 주세요"),
                   ai(use("t1", "Edit", {"file_path": "w.py", "old_string": "a", "new_string": "b"})), result("t1", "ok")]
        records += [human(f"{n}번째 요청입니다") for n in range(2, 7)]
        records += [ai(use("t2", "Bash", {"command": "pytest -q"})), result("t2", "1 passed in 0.1s")]
        ws = workspace(parse_session(jsonl(*records)), selected="c1", prediction=PREDICTION)
        self.assertEqual(retrieval_pool(ws), [])  # the window runs to the test result in the last turn
        consent(ws)
        self.assertEqual((await retrieve(ws, SETTINGS, Untouchable(), lambda _p: None))["state"], "no_results")

    async def test_a_secret_in_the_pool_blocks_the_embedding_call(self):
        ws = many()
        ws.evidence["E0043"] = replace(ws.evidence["E0043"], excerpt="README의 키는 sk-ant-api03-RAWRAWRAWRAWRAW0000")
        consent(ws)
        with self.assertRaises(ProviderError) as cm:
            await retrieve(ws, SETTINGS, Untouchable(), lambda _p: None)
        self.assertEqual(cm.exception.code, "secret_detected")


class RerankTest(unittest.IsolatedAsyncioTestCase):
    async def test_cites_among_retrieved_pool_requests(self):
        ws, caps = many(), Caps(SETTINGS)
        consent(ws)
        reranker = Recorder()
        cited = await rerank(ws, reranker, caps.gate(ws, "analysis"), ["E0012", "E0024", "E0029", "E0031", "E0012"])
        self.assertEqual(cited, ["E0012", "E0024"])
        query, documents = reranker.calls[0]
        self.assertEqual([d["id"] for d in documents], ["E0012", "E0024"])  # window items and duplicates dropped
        self.assertEqual(documents[0]["doc"], ws.evidence["E0012"].excerpt)
        self.assertTrue(query.startswith(ws.evidence["E0029"].excerpt))
        self.assertEqual(ws.calls, {"clova_rerank": 1})

    async def test_none_when_it_does_not_run_and_empty_when_nothing_is_cited(self):
        ws = many()
        consent(ws, clova_rerank=False)
        self.assertIsNone(await rerank(ws, Untouchable(), lambda _p: None, ["E0012"]))
        consent(ws)
        self.assertIsNone(await rerank(ws, None, lambda _p: None, ["E0012"]))
        self.assertIsNone(await rerank(ws, Untouchable(), lambda _p: None, []))
        self.assertIsNone(await rerank(ws, Untouchable(), lambda _p: None, ["E0029", "E0045"]))  # not pool requests

        class NoCitation:
            async def rerank(self, query, documents, *, gate):
                gate("clova_rerank")
                return ["E9999"]  # an id that was not sent is never returned

        self.assertEqual(await rerank(ws, NoCitation(), lambda _p: None, ["E0012"]), [])

    async def test_at_most_ten_documents(self):
        records = [human(f"{n}번째 요청: 캐시와 도시 이름 처리") for n in range(1, 13)]
        records += [human("같은 도시를 다시 물으면 캐시를 써 줘"),
                    ai(use("t1", "Edit", {"file_path": "w.py", "old_string": "a", "new_string": "b"}))]
        ws = workspace(parse_session(jsonl(*records)), selected="c1")
        consent(ws)
        reranker = Recorder()
        await rerank(ws, reranker, lambda _p: None, retrieval_pool(ws))
        self.assertEqual(len(retrieval_pool(ws)), 12)
        self.assertEqual(len(reranker.calls[0][1]), 10)


if __name__ == "__main__":
    unittest.main()
