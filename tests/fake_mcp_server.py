"""Fake course MCP server for local_llm_mcp tests: stdio FastMCP with the course `summarize_text` shape.

Test hooks: FAKE_MCP_SLEEP=<seconds> delays every call (timeout test); text starting with "RAISE" fails the tool.
The summary reports what the child saw (text length, max_sentences, sorted env var names) so tests can check them.
"""

import asyncio
import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("fake-local-llm")


@server.tool()
async def summarize_text(text: str, max_sentences: int = 3) -> dict:
    if text.startswith("RAISE"):
        raise RuntimeError("fake tool failure")
    await asyncio.sleep(float(os.environ.get("FAKE_MCP_SLEEP", "0")))
    env = ",".join(sorted(os.environ))
    summary = f"len={len(text)} sentences={max_sentences} env={env} head={text[:20]}"
    return {"model": "fake-qwen", "summary": summary, "max_sentences": max_sentences, "elapsed_ms": 1}


if __name__ == "__main__":
    port = os.environ.get("FAKE_MCP_HTTP_PORT")  # HTTP mode, like the course server on local-llm-chat
    if port:
        server.settings.host, server.settings.port = "127.0.0.1", int(port)
        server.run(transport="streamable-http")
    else:
        server.run()
