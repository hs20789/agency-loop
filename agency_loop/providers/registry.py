"""Builds the provider set from the per-provider modes in Settings (docs/ARCHITECTURE.md §6)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agency_loop.config import Settings
from agency_loop.providers import mock
from agency_loop.providers.anthropic_claude import ClaudeClient
from agency_loop.providers.base import Providers
from agency_loop.providers.clova import ClovaReranker, HcxReviewer
from agency_loop.providers.local_llm_mcp import McpSummarizer
from agency_loop.providers.ollama_embed import OllamaEmbedder

if TYPE_CHECKING:
    from agency_loop.ledger import Ledger


def _pick(mode: str, live: Callable[[], Any], fake: Callable[[], Any]) -> Any:
    return live() if mode == "live" else fake() if mode == "mock" else None  # "off" -> None


def build_providers(settings: Settings, ledger: Ledger | None = None) -> Providers:
    """`modes` is keyed by the gate/consent provider names (labels.PROVIDER)."""

    def deepseek() -> Any:
        from agency_loop.ledger import Ledger
        from agency_loop.providers.mistral import DeepSeekReviewer

        if ledger is not None:
            return DeepSeekReviewer(settings, ledger)
        book = Ledger(
            settings.ledger_path,
            cap_micro_usd=settings.mistral_cap_micro_usd,
            price_in=settings.mistral_price_in,
            price_out=settings.mistral_price_out,
            out_ub_tokens=settings.mistral_out_ub_tokens,
        )
        return DeepSeekReviewer(settings, book)

    s = settings
    return Providers(
        claude=_pick(s.anthropic_mode, lambda: ClaudeClient(s), mock.MockClaude),
        hcx=_pick(s.clova_mode, lambda: HcxReviewer(s), lambda: mock.MockReviewer("hcx")),
        deepseek=_pick(s.mistral_mode, deepseek, lambda: mock.MockReviewer("deepseek")),
        embedder=_pick(s.embed_mode, lambda: OllamaEmbedder(s), mock.MockEmbedder),
        reranker=_pick(s.clova_mode, lambda: ClovaReranker(s), mock.MockReranker),
        summarizer=_pick(s.local_llm_mode, lambda: McpSummarizer(s), mock.MockSummarizer),
        modes={
            "anthropic": s.anthropic_mode,
            "hcx": s.clova_mode,
            "deepseek": s.mistral_mode,
            "local_rag": s.embed_mode,
            "clova_rerank": s.clova_mode,
            "local_llm": s.local_llm_mode,
        },
    )
