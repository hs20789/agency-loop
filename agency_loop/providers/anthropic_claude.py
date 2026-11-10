"""Claude through the Anthropic SDK: streaming, structured JSON output (docs/ARCHITECTURE.md §6.2).

One app-level retry for 429 (only when retry-after <= 20 s), 5xx/529 and connection errors; the SDK itself never
retries. The gate runs before every attempt. No thinking or sampling parameters, no model fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import anthropic

from agency_loop.config import Settings
from agency_loop.flow.schemas import is_valid
from agency_loop.providers.base import Gate, LLMResult, ProviderError, Status

# The SDK logs whole request bodies (prompts with session content) at DEBUG; never let them through.
logging.getLogger("anthropic").setLevel(logging.WARNING)

RETRY_DELAY_SEC = 1.0  # wait before the one retry when the server gives no retry-after
MAX_RETRY_AFTER_SEC = 20.0


def _retry_after(value: str | None) -> float | None:
    """Seconds to wait before retrying a 429, or None when the server asks for longer than we wait."""
    try:
        wait = float(value) if value is not None else RETRY_DELAY_SEC
    except ValueError:  # HTTP-date form
        wait = RETRY_DELAY_SEC
    return wait if wait <= MAX_RETRY_AFTER_SEC else None


def _status_error(e: anthropic.APIStatusError) -> tuple[ProviderError, float | None]:
    """Map an HTTP or in-stream API error to (error, wait before the retry or None for no retry)."""
    status, kind = e.status_code, e.type  # in-stream errors arrive on a 200 response, so check error.type too
    if status == 429 or kind == "rate_limit_error":
        error = ProviderError("provider_rate_limited", retryable=True, attempted=True, status=status)
        return error, _retry_after(e.response.headers.get("retry-after"))
    if status >= 500 or kind in ("api_error", "overloaded_error"):
        return ProviderError("server_error", retryable=True, attempted=True, status=status), RETRY_DELAY_SEC
    code = "auth" if status in (401, 403) else "bad_request"
    return ProviderError(code, attempted=True, status=status), None


class ClaudeClient:
    def __init__(self, settings: Settings, *, api_key: str | None = None) -> None:
        self.settings = settings
        self.api_key = api_key

    async def generate(
        self, *, stage: str, system: str, user: str, schema: dict, effort: str, max_tokens: int,
        gate: Gate, on_status: Status | None = None,
    ) -> LLMResult:
        # Explicit key only: without one the SDK would look for profiles and other credentials.
        api_key = (self.api_key if self.api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")).strip()
        if not api_key:
            raise ProviderError("auth")
        started = time.monotonic()
        async with anthropic.AsyncAnthropic(
            # Never None: the SDK would then send to ANTHROPIC_BASE_URL; an empty setting fails closed instead.
            api_key=api_key, max_retries=0, timeout=600, base_url=self.settings.anthropic_base_url
        ) as client:
            for attempt in (1, 2):
                gate("anthropic")
                try:
                    final = await self._stream(client, system, user, schema, effort, max_tokens, on_status)
                    break
                except anthropic.APITimeoutError:
                    raise ProviderError("timeout", retryable=True, attempted=True) from None
                except anthropic.APIConnectionError:
                    error, wait = ProviderError("connection", retryable=True, attempted=True), RETRY_DELAY_SEC
                except anthropic.APIStatusError as e:
                    error, wait = _status_error(e)
                if attempt == 2 or wait is None:
                    raise error
                await asyncio.sleep(wait)

        if final.stop_reason == "refusal":
            raise ProviderError("refusal", attempted=True)
        if final.stop_reason == "max_tokens":
            raise ProviderError("incomplete", retryable=True, attempted=True)
        if on_status:
            on_status("validating", "응답 형식 확인 중")
        text = "".join(block.text for block in final.content if block.type == "text")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not is_valid(data, schema):
            raise ProviderError("invalid_output", attempted=True)
        return LLMResult(
            provider="anthropic",
            model=final.model,
            data=data,
            text=text,
            stop_reason=final.stop_reason,
            usage={"input_tokens": final.usage.input_tokens, "output_tokens": final.usage.output_tokens},
            attempts=attempt,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def _stream(self, client, system, user, schema, effort, max_tokens, on_status):
        async with client.messages.stream(
            model=self.settings.claude_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        ) as stream:
            streaming = False
            async for _ in stream.text_stream:
                if on_status and not streaming:
                    on_status("streaming", "응답 수신 중")
                streaming = True
            return await stream.get_final_message()
