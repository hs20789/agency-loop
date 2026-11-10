"""BGE-M3 embeddings from the local-rag class server's Ollama /api/embed (docs/ARCHITECTURE.md §6.2)."""

from __future__ import annotations

import httpx

from agency_loop.config import Settings
from agency_loop.providers.base import Gate, ProviderError

CHUNK_CHARS = 4000  # num_ctx 4096 tokens with truncate=false: longer inputs are split and their vectors averaged


class OllamaEmbedder:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.model = settings.embed_model

    async def embed(self, texts: list[str], *, gate: Gate) -> list[list[float]]:
        if not texts:
            return []
        if not self.settings.ollama_embed_url:
            raise ProviderError("disabled")
        chunks: list[str] = []
        spans = []  # (first, end) chunk index range of each text
        for text in texts:
            pieces = [text[i : i + CHUNK_CHARS] for i in range(0, max(len(text), 1), CHUNK_CHARS)]
            spans.append((len(chunks), len(chunks) + len(pieces)))
            chunks += pieces
        body = {
            "model": self.settings.embed_model,
            "input": chunks,
            "truncate": False,
            "keep_alive": "5m",
            "options": {"num_ctx": 4096},
        }
        gate("local_rag")
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=120) as client:
                resp = await client.post(f"{self.settings.ollama_embed_url.rstrip('/')}/api/embed", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ProviderError("connection", retryable=True) from None
        except httpx.TimeoutException:
            raise ProviderError("timeout", retryable=True, attempted=True) from None
        except httpx.TransportError:
            raise ProviderError("connection", retryable=True, attempted=True) from None
        if resp.status_code >= 500:
            raise ProviderError("server_error", retryable=True, attempted=True, status=resp.status_code)
        if resp.status_code != 200:
            raise ProviderError("bad_request", attempted=True, status=resp.status_code)

        try:
            vectors = resp.json()["embeddings"]
            dim = len(vectors[0])
            if not dim or len(vectors) != len(chunks) or any(len(v) != dim for v in vectors):
                raise ValueError("embedding count or dimension mismatch")
            return [[sum(col) / (end - first) for col in zip(*vectors[first:end], strict=True)] for first, end in spans]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("invalid_output", attempted=True) from None
