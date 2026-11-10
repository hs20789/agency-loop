"""Provider contract shared by live adapters, mocks and the orchestrator (docs/ARCHITECTURE.md §6.1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agency_loop import labels


class ProviderError(Exception):
    """Every provider failure. `code` is a key of labels.ERROR; `message` is Korean and safe to show."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        retryable: bool = False,
        attempted: bool = False,
        status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or labels.ERROR.get(code, "요청을 처리하지 못했습니다.")
        self.retryable = retryable
        self.attempted = attempted  # True once an HTTP request may have reached the provider
        self.status = status
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass
class LLMResult:
    provider: str  # "anthropic" | "hcx" | "deepseek"
    model: str  # provider-reported model name ("mock-..." for mocks)
    data: dict[str, Any] | None  # parsed and schema-checked JSON, None when unavailable
    text: str  # raw text output
    stop_reason: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    mock: bool = False
    elapsed_ms: int = 0


Gate = Callable[[str], None]  # gate(provider) before EVERY HTTP attempt; raises ProviderError to stop
Status = Callable[[str, str], None]  # on_status(job_status, korean_progress)


class Claude(Protocol):
    async def generate(
        self, *, stage: str, system: str, user: str, schema: dict, effort: str, max_tokens: int,
        gate: Gate, on_status: Status | None = None,
    ) -> LLMResult: ...


class Reviewer(Protocol):
    async def review(
        self, *, system: str, user: str, schema: dict, gate: Gate, on_status: Status | None = None
    ) -> LLMResult: ...


class Embedder(Protocol):
    async def embed(self, texts: list[str], *, gate: Gate) -> list[list[float]]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, documents: list[dict], *, gate: Gate) -> list[str]: ...


class Summarizer(Protocol):
    async def summarize(self, text: str, *, max_sentences: int = 3, gate: Gate) -> dict: ...


@dataclass
class Providers:
    claude: Claude | None = None
    hcx: Reviewer | None = None
    deepseek: Reviewer | None = None
    embedder: Embedder | None = None
    reranker: Reranker | None = None
    summarizer: Summarizer | None = None
    modes: dict[str, str] = field(default_factory=dict)  # provider -> "mock" | "live" | "off"


def no_gate(_provider: str) -> None:
    """Gate that allows everything; for tests only."""
