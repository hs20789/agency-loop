"""CLOVA Studio: HCX-007 independent review and the reranker, through httpx (docs/ARCHITECTURE.md §6.2).

No retries. A 429 from either API pauses every CLOVA call in this process for 60 s.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import httpx

from agency_loop.config import Settings
from agency_loop.flow.schemas import is_valid, repair
from agency_loop.providers.base import Gate, LLMResult, ProviderError, Status

COOLDOWN_SEC = 60
clock = time.monotonic  # tests replace it
_cooldown_until = 0.0  # server-wide: shared by HcxReviewer and ClovaReranker


def _lenient_json(text: str) -> object:
    """JSON output is not documented for HCX-007: take the first "{" through the last "}". strict=False accepts raw
    control characters inside strings, such as a tab HCX-007 copies verbatim from test output into a quote."""
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start : end + 1], strict=False) if 0 <= start < end else None
    except ValueError:
        return None


class _ClovaClient:
    def __init__(
        self, settings: Settings, *, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.api_key = api_key
        self.transport = transport

    async def _post(self, path: str, body: dict, *, gate: Gate, provider: str, timeout: float) -> dict:
        """One POST; returns `result` of a successful (status.code 20000) response or raises ProviderError."""
        global _cooldown_until
        if clock() < _cooldown_until:
            raise ProviderError("provider_rate_limited", retryable=True)
        api_key = (self.api_key if self.api_key is not None else os.environ.get("CLOVA_API_KEY", "")).strip()
        if not api_key:
            raise ProviderError("auth")
        gate(provider)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        }
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=timeout) as client:
                resp = await client.post(self.settings.clova_base_url.rstrip("/") + path, json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ProviderError("connection", retryable=True) from None
        except httpx.TimeoutException:
            raise ProviderError("timeout", retryable=True, attempted=True) from None
        except httpx.TransportError:
            raise ProviderError("connection", retryable=True, attempted=True) from None

        status = resp.status_code
        try:
            payload = resp.json()
            code = str(payload["status"]["code"])
        except (ValueError, KeyError, TypeError):
            payload, code = {}, ""
        if status == 429 or code.startswith("429"):
            _cooldown_until = clock() + COOLDOWN_SEC
            raise ProviderError("provider_rate_limited", retryable=True, attempted=True, status=status)
        if status in (401, 403):
            raise ProviderError("auth", attempted=True, status=status)
        if status >= 500:
            raise ProviderError("server_error", retryable=True, attempted=True, status=status)
        if status != 200 or code != "20000":
            raise ProviderError("bad_request", attempted=True, status=status)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProviderError("invalid_output", attempted=True, status=status)
        return result


class HcxReviewer(_ClovaClient):
    async def review(
        self, *, system: str, user: str, schema: dict, gate: Gate, on_status: Status | None = None
    ) -> LLMResult:
        started = time.monotonic()
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "topP": 0.8,
            "topK": 0,
            "temperature": 0.5,
            "repetitionPenalty": 1.1,
            "seed": 7,
            "includeAiFilters": False,
            "maxCompletionTokens": self.settings.hcx_max_completion_tokens,
            "thinking": {"effort": self.settings.hcx_thinking_effort},
        }
        result = await self._post(
            f"/v3/chat-completions/{self.settings.hcx_model}", body, gate=gate, provider="hcx", timeout=120
        )
        if on_status:
            on_status("validating", "응답 형식 확인 중")
        message = result.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            raise ProviderError("invalid_output", attempted=True)
        data = repair(_lenient_json(text), schema)  # HCX-007 has no enforced schema and drops empty keys
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return LLMResult(
            provider="hcx",
            model=self.settings.hcx_model,  # the v3 response does not name the model
            data=data if is_valid(data, schema) else None,  # None: UI shows 형식 미확인 with the raw text
            text=text,
            stop_reason=result.get("finishReason"),
            usage={"prompt_tokens": usage.get("promptTokens"), "completion_tokens": usage.get("completionTokens")},
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


class ClovaReranker(_ClovaClient):
    async def rerank(self, query: str, documents: list[dict], *, gate: Gate) -> list[str]:
        """`documents` are {"id", "doc"}; returns the cited ids that were sent, in the reranker's order."""
        docs = [{"id": d["id"], "doc": d["doc"]} for d in documents[:10]]  # the API takes 1-10 documents
        if not docs:
            return []
        body = {"query": query, "documents": docs, "maxTokens": 1024}
        result = await self._post("/v1/api-tools/reranker", body, gate=gate, provider="clova_rerank", timeout=60)
        cited = result.get("citedDocuments")
        sent = {d["id"] for d in docs}
        ids = [c.get("id") for c in cited if isinstance(c, dict)] if isinstance(cited, list) else []
        return list(dict.fromkeys(i for i in ids if isinstance(i, str) and i in sent))  # result.result is never used
