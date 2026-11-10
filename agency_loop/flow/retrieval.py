"""Related-request search: BGE-M3 vectors compared in memory, then the reranker's citation choice (ARCHITECTURE §5.9).

Only human requests outside the candidate's analysis window are searched, and nothing is stored. Provider and
consent errors (ProviderError) propagate; the orchestrator records a failed search and goes on without it.
"""

from __future__ import annotations

import math
from typing import Any

from agency_loop import labels
from agency_loop.config import Settings
from agency_loop.flow.bundle import documents_text, evidence_item, retrieval_pool, search_query, unblinded_candidate
from agency_loop.flow.consent import check_send, consented, stage_payload
from agency_loop.model import Workspace
from agency_loop.providers.base import Embedder, Gate, Reranker

RERANK_MAX_DOCS = 10  # the reranker API takes 1-10 documents


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 when either vector is all zeros. Vectors must have the same length."""
    norm = math.hypot(*a) * math.hypot(*b)
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


def top_k(
    query_vec: list[float], pool: list[tuple[str, list[float]]], k: int, threshold: float
) -> list[tuple[str, float]]:
    """At most k (id, score) pairs with score >= threshold, best first; ties keep pool order."""
    scored = [(item_id, cosine(query_vec, vec)) for item_id, vec in pool]
    return sorted((pair for pair in scored if pair[1] >= threshold), key=lambda pair: -pair[1])[:k]


async def retrieve(ws: Workspace, settings: Settings, embedder: Embedder | None, gate: Gate) -> dict[str, Any]:
    """`{state, state_label, retrieved, scores}`. ValueError while blinded or before selection."""
    candidate = unblinded_candidate(ws)
    if candidate.evidence_level == "insufficient":
        return _state("insufficient")
    # A consent is only recorded for an applicable provider, so it also covers the request-count rule.
    if embedder is None or not consented(ws, "analysis", "local_rag"):
        return _state("skipped")
    items, text = stage_payload(ws, "analysis", "local_rag", settings)  # the query, then the pool
    if len(items) < 2:
        return _state("no_results")
    check_send(ws, "analysis", "local_rag", items, text)
    vectors = await embedder.embed([item.text for item in items], gate=gate)
    pool = [(item.evidence_id, vec) for item, vec in zip(items[1:], vectors[1:], strict=True)]
    found = top_k(vectors[0], pool, settings.rag_top_k, settings.rag_min_similarity)
    if not found:
        return _state("no_results")
    return _state("done", [item_id for item_id, _ in found], {item_id: round(score, 4) for item_id, score in found})


async def rerank(ws: Workspace, reranker: Reranker | None, gate: Gate, retrieved: list[str]) -> list[str] | None:
    """Ids the reranker cites among the retrieved requests. None when it did not run (off, not consented, nothing
    retrieved); an empty list when it ran and cited nothing (search state no_results)."""
    unblinded_candidate(ws)
    pool = set(retrieval_pool(ws))
    ids = [i for i in dict.fromkeys(retrieved) if i in pool][:RERANK_MAX_DOCS]
    if reranker is None or not ids or not consented(ws, "analysis", "clova_rerank"):
        return None
    query = search_query(ws)
    docs = [evidence_item(ws.evidence[i]) for i in ids]
    check_send(ws, "analysis", "clova_rerank", [query, *docs], documents_text([query, *docs]))
    cited = await reranker.rerank(query.text, [{"id": d.evidence_id, "doc": d.text} for d in docs], gate=gate)
    return [i for i in dict.fromkeys(cited) if i in ids]


def _state(state: str, retrieved: list[str] | None = None, scores: dict[str, float] | None = None) -> dict[str, Any]:
    return {"state": state, "state_label": labels.SEARCH_STATE[state], "retrieved": retrieved or [], "scores": scores or {}}
