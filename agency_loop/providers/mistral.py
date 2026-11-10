"""DeepSeek V4.1 Flash review through the Mistral chat completions API (docs/ARCHITECTURE.md §6.2, §6.4).

One non-streaming httpx request per review, no retries, guarded by the usage ledger:
reserve before sending, settle from the reported usage, release only when nothing can have been billed.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

import httpx

from agency_loop.config import Settings
from agency_loop.flow.schemas import is_valid
from agency_loop.ledger import Ledger, LedgerRefused
from agency_loop.providers.base import Gate, LLMResult, ProviderError, Status

log = logging.getLogger(__name__)


def _text(content: object) -> str:
    if isinstance(content, list):  # chunked content (reasoning models): keep text chunks, never thinking
        return "".join(
            c["text"]
            for c in content
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
        )
    return content if isinstance(content, str) else ""


class DeepSeekReviewer:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.api_key = api_key
        self.transport = transport
        self.disabled = False  # set by a 402 (no credit) and kept until the process restarts

    async def review(
        self, *, system: str, user: str, schema: dict, gate: Gate, on_status: Status | None = None
    ) -> LLMResult:
        if self.disabled:
            raise ProviderError("budget_exceeded")
        api_key = (self.api_key if self.api_key is not None else os.environ.get("MISTRAL_API_KEY", "")).strip()
        if not api_key:
            raise ProviderError("auth")
        max_tokens = self.settings.mistral_max_tokens
        body = json.dumps(
            {
                "model": self.settings.mistral_model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "review", "schema": schema, "strict": True},
                },
            },
            ensure_ascii=False,
        ).encode()
        call_id = uuid.uuid4().hex

        # Reserve before the gate: a ledger refusal must not use up the one DeepSeek call this flow allows.
        try:
            reserved = self.ledger.reserve(call_id, len(body), max_tokens)
        except LedgerRefused as e:  # "halted" and "corrupt" both wait for the operator
            raise ProviderError({"cap": "budget_exceeded", "missing": "ledger_missing"}.get(e.code, "halted")) from None
        try:
            gate("deepseek")
        except ProviderError:
            self.ledger.release(call_id)  # refused by the caps: nothing was sent
            raise

        if on_status:
            on_status("running", "DeepSeek 검토를 요청했습니다.")
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=120) as client:
                resp = await client.post(
                    f"{self.settings.mistral_base_url.rstrip('/')}/v1/chat/completions",
                    content=body,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            self.ledger.release(call_id)  # no connection, so the request never left this machine
            raise ProviderError("connection", retryable=True) from None
        except httpx.TimeoutException:
            raise ProviderError("timeout", attempted=True) from None
        except httpx.TransportError:
            raise ProviderError("connection", attempted=True) from None
        elapsed_ms = int((time.monotonic() - started) * 1000)

        status = resp.status_code
        if 400 <= status < 500:
            self.ledger.release(call_id)  # rejected before generation: nothing billed
            if status == 402:
                self.disabled = True
                raise ProviderError("budget_exceeded", attempted=True, status=status)
            if status == 429:
                raise ProviderError("provider_rate_limited", retryable=True, attempted=True, status=status)
            raise ProviderError("auth" if status in (401, 403) else "bad_request", attempted=True, status=status)
        if status != 200:  # 5xx or unexpected: may have been billed, keep the reservation
            raise ProviderError("server_error", attempted=True, status=status)

        try:
            payload = resp.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("invalid_output", attempted=True, status=status) from None  # usage unknown: keep
        if on_status:
            on_status("validating", "응답 형식을 확인하고 있습니다.")

        usage = payload.get("usage")
        try:
            prompt_tokens, completion_tokens = usage["prompt_tokens"], usage["completion_tokens"]
            charged = self.ledger.settle(call_id, prompt_tokens, completion_tokens)
            usage = {
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "charged_micro_usd": charged,
            }
        except (KeyError, TypeError, ValueError, LedgerRefused, OSError):
            # Keep the paid result; the reservation stays counted as spent until an operator reconciles it.
            log.warning("DeepSeek usage not settled, reservation kept (call_id=%s)", call_id)
            usage = {"reserved_micro_usd": reserved, "settled": False}

        text = _text(content)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        return LLMResult(
            provider="deepseek",
            model=payload.get("model") or self.settings.mistral_model,
            data=data if is_valid(data, schema) else None,
            text=text,
            stop_reason=choice.get("finish_reason"),
            usage=usage,
            attempts=1,
            elapsed_ms=elapsed_ms,
        )
