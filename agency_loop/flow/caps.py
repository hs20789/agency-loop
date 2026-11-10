"""Per-flow attempt counters, server-wide daily caps and concurrency slots (ARCHITECTURE §5.8).

In memory only; one Caps per server, so new uploads never reset the daily totals. Everything runs on the event
loop and no check is separated from its increment by an await.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

from agency_loop.config import Settings
from agency_loop.model import Workspace
from agency_loop.providers.base import Gate, ProviderError

_DAILY_BUCKET = {"anthropic": "anthropic", "hcx": "clova", "clova_rerank": "clova"}
_PER_STAGE = ("hcx", "deepseek")  # one call per stage; every other provider has one per-flow counter


class Caps:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.clock = clock
        self._day: date | None = None
        self._used = {"anthropic": 0, "clova": 0}
        self._running = {"analysis": 0, "local_llm": 0}

    def gate(self, ws: Workspace, stage: str) -> Gate:
        """Gate for the calls of one stage. Each allowed attempt counts in ws.calls ("anthropic", "hcx:<stage>",
        "deepseek:<stage>", "clova_rerank", "local_rag", "local_llm") and in the daily totals; a refused one does not.
        Raises ProviderError("cap_exceeded") for the per-flow limit, ProviderError("daily_cap") for the daily one."""

        def gate(provider: str) -> None:
            key, limit = self._key_limit(provider, stage)
            if ws.calls.get(key, 0) >= limit:
                raise ProviderError("cap_exceeded")
            bucket = _DAILY_BUCKET.get(provider)
            if bucket is not None:
                self._roll_day()
                if self._used[bucket] >= self._limits()[bucket]:
                    raise ProviderError("daily_cap")
                self._used[bucket] += 1
            ws.calls[key] = ws.calls.get(key, 0) + 1

        return gate

    def exhausted(self, ws: Workspace, provider: str, stage: str) -> bool:
        """True when the per-flow limit for this provider (and stage) is used up, so a retry cannot succeed."""
        key, limit = self._key_limit(provider, stage)
        return ws.calls.get(key, 0) >= limit

    def _key_limit(self, provider: str, stage: str) -> tuple[str, int]:
        key = f"{provider}:{stage}" if provider in _PER_STAGE else provider
        return key, self.settings.claude_attempts_per_flow if provider == "anthropic" else 1

    def daily_usage(self) -> dict[str, Any]:
        """Counts only, for health and debugging: `{date, anthropic: {used, limit}, clova: {...}, running}`."""
        self._roll_day()
        limits = self._limits()
        return {
            "date": self._day.isoformat(),
            **{bucket: {"used": used, "limit": limits[bucket]} for bucket, used in self._used.items()},
            "running": dict(self._running),
        }

    def analysis_slot(self) -> contextlib.AbstractContextManager[None]:
        """`with caps.analysis_slot():` around an analysis job; ProviderError("concurrency") when all are busy."""
        return self._slot("analysis", self.settings.analysis_concurrency)

    def local_llm_slot(self) -> contextlib.AbstractContextManager[None]:
        return self._slot("local_llm", self.settings.local_llm_concurrency)

    @contextlib.contextmanager
    def _slot(self, name: str, limit: int) -> Iterator[None]:
        if self._running[name] >= limit:  # non-blocking: the caller tells the user to retry
            raise ProviderError("concurrency", retryable=True)
        self._running[name] += 1
        try:
            yield
        finally:
            self._running[name] -= 1

    def _limits(self) -> dict[str, int]:
        return {"anthropic": self.settings.claude_daily_attempts, "clova": self.settings.clova_daily_calls}

    def _roll_day(self) -> None:
        today = date.fromtimestamp(self.clock())  # local date: the totals reset at local midnight
        if today != self._day:
            self._day, self._used = today, dict.fromkeys(self._used, 0)
