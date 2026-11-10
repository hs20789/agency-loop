import asyncio
import os
import re
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS

from agency_loop.config import Settings
from agency_loop.providers.base import ProviderError
from agency_loop.providers.local_llm_mcp import McpSummarizer

TESTS = Path(__file__).resolve().parent
FAKE = str(TESTS / "fake_mcp_server.py")
REQUIRED_ENV = {"OLLAMA_BASE_URL", "OLLAMA_MODEL", "PYTHONDONTWRITEBYTECODE", "PATH"}


def settings(**overrides):
    base = {
        "mcp_command": sys.executable,
        "mcp_args": (FAKE,),
        "mcp_cwd": str(TESTS),
        "ollama_chat_url": "http://ollama.test:11434",
        "local_llm_model": "fake-model",
        "mcp_timeout_sec": 30,
    }
    return Settings(**{**base, **overrides})


class McpSummarizerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gates = []

    async def summarize(self, text, **overrides):
        return await McpSummarizer(settings(**overrides)).summarize(text, max_sentences=2, gate=self.gates.append)

    async def assert_error(self, code, call):
        with self.assertRaises(ProviderError) as cm:
            await call
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    async def test_calls_summarize_text_with_capped_text(self):
        result = await self.summarize("가" * 5000)

        self.assertEqual(set(result), {"model", "summary", "elapsed_ms"})
        self.assertEqual(result["model"], "fake-qwen")
        self.assertIn("len=4000 sentences=2", result["summary"])
        self.assertIsInstance(result["elapsed_ms"], int)
        self.assertEqual(self.gates, ["local_llm"])

    async def test_child_env_has_no_secrets(self):
        secrets = {"ANTHROPIC_API_KEY": "sk-ant-sentinel", "CLOVA_API_KEY": "sentinel", "MISTRAL_API_KEY": "sentinel"}
        with mock.patch.dict(os.environ, secrets):
            result = await self.summarize("환경 확인")

        names = set(re.search(r"env=(\S*)", result["summary"]).group(1).split(","))
        self.assertLessEqual(REQUIRED_ENV, names)
        # mcp's stdio_client always adds its safe defaults; Python may add LC_CTYPE by locale coercion.
        self.assertLessEqual(names, REQUIRED_ENV | set(DEFAULT_INHERITED_ENV_VARS) | {"LC_CTYPE"})
        self.assertFalse(names & set(secrets))

    async def test_overall_timeout(self):
        started = time.monotonic()
        overrides = {"mcp_command": "/usr/bin/env", "mcp_args": ("FAKE_MCP_SLEEP=30", sys.executable, FAKE)}
        error = await self.assert_error("timeout", self.summarize("느린 요약", mcp_timeout_sec=1, **overrides))
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(error.retryable)

    async def test_tool_error(self):
        error = await self.assert_error("server_error", self.summarize("RAISE 실패"))
        self.assertTrue(error.attempted)
        self.assertEqual(self.gates, ["local_llm"])

    async def test_server_that_cannot_start_or_exits(self):
        await self.assert_error("connection", self.summarize("x", mcp_command=str(TESTS / "no-such-python")))
        await self.assert_error("connection", self.summarize("x", mcp_args=("-c", "raise SystemExit(3)")))

    async def test_http_transport_when_a_url_is_set(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {"PATH": os.environ["PATH"], "FAKE_MCP_HTTP_PORT": str(port)}
        proc = subprocess.Popen([sys.executable, FAKE], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            url = f"http://127.0.0.1:{port}/mcp"
            for _ in range(100):  # wait for the server to listen
                with socket.socket() as sock:
                    if sock.connect_ex(("127.0.0.1", port)) == 0:
                        break
                await asyncio.sleep(0.1)
            # The stdio command is broken on purpose: the URL must win.
            result = await self.summarize("HTTP 요약", mcp_url=url, mcp_command=str(TESTS / "no-such-python"))
            self.assertEqual(result["model"], "fake-qwen")
            self.assertIn("len=7 sentences=2", result["summary"])
            self.assertEqual(self.gates, ["local_llm"])
        finally:
            proc.terminate()
            proc.wait(10)
        await self.assert_error("connection", self.summarize("x", mcp_url=url))  # nothing listens any more

    async def test_gate_refusal_starts_nothing(self):
        def gate(provider):
            raise ProviderError("cap_exceeded")

        summarizer = McpSummarizer(settings(mcp_command=str(TESTS / "no-such-python")))
        with self.assertRaises(ProviderError) as cm:
            await summarizer.summarize("x", gate=gate)
        self.assertEqual(cm.exception.code, "cap_exceeded")


if __name__ == "__main__":
    unittest.main()
