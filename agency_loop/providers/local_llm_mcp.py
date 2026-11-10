"""Local LLM summary through the course MCP server's `summarize_text` (docs/ARCHITECTURE.md §6.2), over HTTP when
`AGENCY_MCP_URL` is set, otherwise by running the server over stdio.

The course folder is only executed: no API keys in the child env and no bytecode written into it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

from agency_loop.config import Settings
from agency_loop.providers.base import Gate, ProviderError


class McpSummarizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def summarize(self, text: str, *, max_sentences: int = 3, gate: Gate) -> dict:
        s = self.settings
        params = StdioServerParameters(
            command=s.mcp_command,
            args=list(s.mcp_args),
            cwd=s.mcp_cwd or None,
            env={
                "OLLAMA_BASE_URL": s.ollama_chat_url,
                "OLLAMA_MODEL": s.local_llm_model,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PATH": os.environ["PATH"],
            },
        )
        gate("local_llm")
        started = time.monotonic()
        args = {"text": text[:4000], "max_sentences": max_sentences}
        try:
            async with asyncio.timeout(s.mcp_timeout_sec):
                if s.mcp_url:  # the course server runs over HTTP on local-llm-chat; its model is set there
                    async with streamablehttp_client(s.mcp_url) as (read, write, _):
                        result = await _call(read, write, args)
                else:
                    # The child's stderr may echo request text, so it never reaches our log.
                    with open(os.devnull, "w") as devnull:
                        async with stdio_client(params, errlog=devnull) as (read, write):
                            result = await _call(read, write, args)
        except TimeoutError:  # before OSError: TimeoutError is a subclass
            raise ProviderError("timeout", retryable=True, attempted=True) from None
        except OSError:  # the server command could not be started
            raise ProviderError("connection") from None
        except (McpError, ExceptionGroup, httpx.HTTPError):  # unreachable, exited or broke the protocol
            raise ProviderError("connection", retryable=True, attempted=True) from None
        if result.isError:
            raise ProviderError("server_error", retryable=True, attempted=True)

        texts = [c.text for c in result.content if c.type == "text"]
        try:
            data = json.loads(texts[0]) if texts else None
        except ValueError:
            data = None
        summary = data.get("summary") if isinstance(data, dict) else None
        if not isinstance(summary, str):
            raise ProviderError("invalid_output", attempted=True)
        return {
            "model": data.get("model") or s.local_llm_model,
            "summary": summary,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


async def _call(read, write, args: dict):
    async with ClientSession(read, write) as session:
        await session.initialize()
        return await session.call_tool("summarize_text", args)
