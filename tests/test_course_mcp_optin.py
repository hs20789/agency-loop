"""Opt-in: the real course MCP server behind the live adapter (AGENCY_TEST_COURSE_MCP=1; ARCHITECTURE §6.2).

Starts /root/day05/local-llm-mcp/server.py read-only over MCP stdio, with Ollama replaced by the local mock
(GET /api/tags, POST /api/chat), runs summarize_text end to end and checks that nothing under /root/day05 changed.
"""

import os
import unittest
from pathlib import Path

from agency_loop.config import Settings
from agency_loop.providers.local_llm_mcp import McpSummarizer
from tests.mock_servers import OLLAMA_MODEL, MockApiServer

DAY05 = Path("/root/day05")
COURSE = DAY05 / "local-llm-mcp"
TEXT = "날씨 API 호출에 5초 시간 제한을 넣었습니다. 시간이 지나면 None을 돌려주고 화면에는 안내 문구를 보여 줍니다.\n"


def tree(root):
    """Path -> (mtime_ns, size) of root and everything under it; a created or deleted file also changes its folder."""
    found = {}
    for folder, dirs, files in os.walk(root):
        for path in [folder, *(os.path.join(folder, name) for name in [*dirs, *files])]:
            st = os.lstat(path)
            found[path] = (st.st_mtime_ns, st.st_size)
    return found


@unittest.skipUnless(os.environ.get("AGENCY_TEST_COURSE_MCP") == "1", "set AGENCY_TEST_COURSE_MCP=1 to run")
class CourseMcpTest(unittest.IsolatedAsyncioTestCase):
    async def test_summarize_text_through_the_course_server(self):
        server = MockApiServer()
        self.addCleanup(server.close)
        before = tree(DAY05)
        settings = Settings(
            mcp_command=str(COURSE / ".venv/bin/python"), mcp_args=(str(COURSE / "server.py"),), mcp_cwd=str(COURSE),
            ollama_chat_url=server.base_url, local_llm_model=OLLAMA_MODEL, mcp_timeout_sec=60,
        )
        gates = []

        result = await McpSummarizer(settings).summarize(TEXT, max_sentences=2, gate=gates.append)

        self.assertEqual(gates, ["local_llm"])
        self.assertEqual((result["model"], result["summary"]), (OLLAMA_MODEL, server.ollama_summary))
        self.assertIsInstance(result["elapsed_ms"], int)
        tags, chat = server.calls("ollama")  # the model check, then the summary; nothing else went out
        self.assertEqual(server.requests, [tags, chat])
        self.assertEqual((tags["method"], tags["path"], chat["method"], chat["path"]), ("GET", "/api/tags", "POST", "/api/chat"))
        body = chat["body"]
        self.assertEqual((body["model"], body["stream"], body["think"]), (OLLAMA_MODEL, False, False))
        self.assertEqual(body["messages"][1], {"role": "user", "content": TEXT.strip()})
        self.assertIn("2문장", body["messages"][0]["content"])  # max_sentences reached the course prompt
        self.assertEqual(tree(DAY05), before)


if __name__ == "__main__":
    unittest.main()
