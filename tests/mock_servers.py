"""Local mock HTTP servers for the live provider adapters (docs/ARCHITECTURE.md §9). Nothing leaves 127.0.0.1.

`MockApiServer` serves, on one ephemeral port:
- Anthropic Messages SSE  POST /v1/messages
- CLOVA HCX-007           POST /v3/chat-completions/{model}
- CLOVA reranker          POST /v1/api-tools/reranker
- Mistral chat            POST /v1/chat/completions
- Ollama embed            POST /api/embed
- Ollama chat             GET /api/tags, POST /api/chat (api "ollama", for the course MCP server)

`server.scenarios[api]` ("anthropic" | "hcx" | "rerank" | "mistral" | "embed") is a list of scenario names consumed
one per request; the last one repeats. `server.replies[api]` ("anthropic" | "hcx" | "mistral"), when set, is an async
function of the request body that returns the reply text instead of the fixed one. Every request is logged in
`server.requests` as {"api", "method", "path", "headers" (lowercase keys), "body" (parsed JSON or None)}.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Awaitable, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

ANTHROPIC_MODEL = "claude-opus-5-5-20261101"  # reported model; differs from the requested one on purpose

ANALYSIS_JSON = {
    "decision_summary": "요약",
    "reasons": [{"text": "이유", "kind": "record", "evidence_ids": ["E0001"], "quote": "날씨"}],
    "alternatives": [],
    "assumptions": [],
    "risks": [],
    "verification_points": [],
    "scope_note": "",
}
REVIEW_JSON = {
    "summary": "검토 요약",
    "points": [{"type": "issue", "text": "확인 필요", "evidence_ids": ["E0001"], "quote": ""}],
}

# Anthropic scenario -> (HTTP status, error.type). Anything else streams a 200 SSE response.
ANTHROPIC_ERRORS = {
    "400": (400, "invalid_request_error"),
    "401": (401, "authentication_error"),
    "403": (403, "permission_error"),
    "404": (404, "not_found_error"),
    "429": (429, "rate_limit_error"),
    "500": (500, "api_error"),
    "529": (529, "overloaded_error"),
}
CLOVA_ERRORS = {"400": (400, "40000"), "401": (401, "40100"), "429": (429, "42901"), "500": (500, "50000")}
MISTRAL_USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
OLLAMA_MODEL = "test-qwen:4b"  # listed by GET /api/tags


def unused_url() -> str:
    """Base URL of a port nothing listens on (connection refused)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def _sse(text: str, stop_reason: str, *, fail: bool = False) -> str:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_mock", "type": "message", "role": "assistant", "model": ANTHROPIC_MODEL, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 120, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
    ]
    events += [
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i : i + 40]}}
        for i in range(0, len(text), 40)
    ]
    if fail:  # in-stream error after a 200 status line
        events.append({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    else:
        events += [
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 45},
            },
            {"type": "message_stop"},
        ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


def _authorized(request: Request) -> bool:
    bearer = request.headers.get("authorization", "")
    return bool(request.headers.get("x-api-key") or (bearer.startswith("Bearer ") and bearer[7:].strip()))


class MockApiServer:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.reset()
        app = Starlette(
            routes=[
                Route("/v1/messages", self._messages, methods=["POST"]),
                Route("/v3/chat-completions/{model}", self._hcx, methods=["POST"]),
                Route("/v1/api-tools/reranker", self._rerank, methods=["POST"]),
                Route("/v1/chat/completions", self._mistral, methods=["POST"]),
                Route("/api/embed", self._embed, methods=["POST"]),
                Route("/api/tags", self._tags, methods=["GET"]),
                Route("/api/chat", self._chat, methods=["POST"]),
            ]
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off"))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("mock server did not start")
            time.sleep(0.01)

    def reset(self) -> None:
        """Default scenarios and payloads; clears the request log."""
        self.requests.clear()
        self.scenarios = {
            "anthropic": ["success"], "hcx": ["success"], "rerank": ["success"], "mistral": ["success"],
            "embed": ["success"],
        }
        self.replies: dict[str, Callable[[dict], Awaitable[str]]] = {}
        self.claude_text = json.dumps(ANALYSIS_JSON, ensure_ascii=False)  # streamed on success
        self.retry_after = "0"  # sent with Anthropic 429s
        self.hcx_content = f"검토 결과입니다.\n```json\n{json.dumps(REVIEW_JSON, ensure_ascii=False)}\n```"
        self.rerank_cited: list[str] | None = None  # None = cite the first document
        self.mistral_content = json.dumps(REVIEW_JSON, ensure_ascii=False)
        self.ollama_summary = "모의 요약입니다. 시간 제한을 넣은 변경입니다."

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(10)

    def calls(self, api: str) -> list[dict]:
        return [r for r in self.requests if r["api"] == api]

    async def _log(self, api: str, request: Request) -> dict | None:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        self.requests.append(
            {"api": api, "method": request.method, "path": request.url.path, "headers": dict(request.headers), "body": body}
        )
        return body

    def _next(self, api: str) -> str:
        queue = self.scenarios[api]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def _reply(self, api: str, body: dict | None, default: str) -> str:
        reply = self.replies.get(api)
        return await reply(body) if reply else default

    async def _messages(self, request: Request) -> Response:
        body = await self._log("anthropic", request)
        scenario = self._next("anthropic") if _authorized(request) else "401"
        if scenario in ANTHROPIC_ERRORS:
            status, kind = ANTHROPIC_ERRORS[scenario]
            headers = {"retry-after": self.retry_after} if status == 429 else None
            body = {"type": "error", "error": {"type": kind, "message": "mock error"}}
            return JSONResponse(body, status_code=status, headers=headers)
        full = await self._reply("anthropic", body, self.claude_text)
        text, stop_reason = {
            "success": (full, "end_turn"),
            "refusal": ("", "refusal"),
            "max_tokens": (full[: len(full) // 2], "max_tokens"),
            "invalid_json": ("{not json", "end_turn"),
            "stream_error": (full[:20], "end_turn"),
        }[scenario]
        sse = _sse(text, stop_reason, fail=scenario == "stream_error")
        return Response(sse, media_type="text/event-stream", headers={"request-id": "req_mock"})

    async def _clova(self, api: str, request: Request) -> tuple[str, dict | None, Response | None]:
        body = await self._log(api, request)
        scenario = self._next(api) if _authorized(request) else "401"
        if scenario not in CLOVA_ERRORS:
            return scenario, body, None
        status, code = CLOVA_ERRORS[scenario]
        return scenario, body, JSONResponse({"status": {"code": code, "message": "mock error"}}, status_code=status)

    async def _hcx(self, request: Request) -> Response:
        scenario, body, error = await self._clova("hcx", request)
        if error:
            return error
        if scenario == "status_error":  # HTTP 200 with a non-success status code
            return JSONResponse({"status": {"code": "40001", "message": "mock error"}})
        full = await self._reply("hcx", body, self.hcx_content)
        content = {"success": full, "not_json": "JSON이 아닌 검토 문장입니다."}[scenario]
        return JSONResponse(
            {
                "status": {"code": "20000", "message": "OK"},
                "result": {
                    "message": {"role": "assistant", "content": content, "thinkingContent": "숨겨진 추론"},
                    "finishReason": "stop",
                    "created": 1,
                    "seed": 7,
                    "usage": {"promptTokens": 120, "completionTokens": 40, "totalTokens": 160},
                },
            }
        )

    async def _rerank(self, request: Request) -> Response:
        _, body, error = await self._clova("rerank", request)
        if error:
            return error
        docs = body["documents"]
        cited = self.rerank_cited if self.rerank_cited is not None else [d["id"] for d in docs[:1]]
        return JSONResponse(
            {
                "status": {"code": "20000", "message": "OK"},
                "result": {
                    "result": "모의 생성 답변",
                    "citedDocuments": [{"id": doc_id, "doc": "..."} for doc_id in cited],
                    "usage": {"promptTokens": 50, "completionTokens": 10, "totalTokens": 60},
                },
            }
        )

    async def _mistral(self, request: Request) -> Response:
        body = await self._log("mistral", request)
        scenario = self._next("mistral") if _authorized(request) else "401"
        if scenario.isdigit():  # "402", "429", "500", ...: that HTTP status
            return JSONResponse({"object": "error", "message": "mock error"}, status_code=int(scenario))
        content = await self._reply("mistral", body, self.mistral_content)
        return JSONResponse(
            {
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": MISTRAL_USAGE,
            }
        )

    async def _tags(self, request: Request) -> Response:
        await self._log("ollama", request)
        return JSONResponse({"models": [{"name": OLLAMA_MODEL}]})

    async def _chat(self, request: Request) -> Response:
        body = await self._log("ollama", request)
        return JSONResponse(
            {
                "model": body["model"],
                "message": {"role": "assistant", "content": self.ollama_summary},
                "done": True,
                "done_reason": "stop",
            }
        )

    async def _embed(self, request: Request) -> Response:
        body = await self._log("embed", request)
        scenario = self._next("embed")
        if scenario in ("404", "500"):
            return JSONResponse({"error": "mock error"}, status_code=int(scenario))
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        vectors = [[float(len(text)), 1.0] for text in inputs]  # [chunk length, 1.0]: makes chunk means checkable
        if scenario == "bad_count":
            vectors = vectors[:-1]
        return JSONResponse({"model": body["model"], "embeddings": vectors, "prompt_eval_count": len(inputs)})
