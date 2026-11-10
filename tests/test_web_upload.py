"""Upload slice: POST /api/workspaces, GET/DELETE /api/workspace, caps, rate, TTL (ARCHITECTURE §7.1-§7.3)."""

import json
import re
import time
import unittest
from dataclasses import replace
from unittest import mock

from starlette.testclient import TestClient

from agency_loop.config import Settings
from agency_loop.ingest.parser import parse_session
from agency_loop.web.app import STATIC_DIR as STATIC
from agency_loop.web.app import create_app
from tests import test_parser as tp
from tests.helpers import FAKE_SECRETS, load_bytes

HOST = "127.0.0.1:8000"
ORIGIN = f"http://{HOST}"
MIB = 1024 * 1024
SCOPE_KEYS = {
    "format", "human_requests", "has_code_edits", "has_test_records", "masked", "masked_labels", "dropped",
    "bad_lines", "notices",
}
CARD_KEYS = {
    "id", "rank", "turn", "title", "category", "category_label", "signals", "files", "lines", "evidence_level",
    "evidence_level_label", "has_test_record", "retro_available",
}
WORKSPACE_KEYS = {
    "scope", "candidates", "selected", "tier", "tier_label", "self_assessment", "retro", "evidence_ids", "evidence",
    "consents", "results", "jobs", "calls", "expires_in_sec",
}
SA = {"understanding": 1, "verification": 1, "control": 1}


def make_app(**overrides):
    """Mock providers only; a generous upload rate unless a test sets its own."""
    return create_app(replace(Settings(), **{"upload_rate_per_10min": 1000, **overrides}))


def make_client(app, **kwargs):
    """A browser on the page: allowed Host and same-origin Origin on every request."""
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, **kwargs)


def upload(client, name):
    return client.post("/api/workspaces", content=load_bytes(name))


def strings(value):
    """Every string leaf of a JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def fake_clock(app, start=1000.0):
    now = [start]
    app.state.store.clock = lambda: now[0]
    return now


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.client = make_client(self.app)

    def test_demo_weather_upload(self):
        r = upload(self.client, "demo_weather.jsonl")
        self.assertEqual(r.status_code, 201)
        cookie = r.headers["set-cookie"]
        self.assertTrue(cookie.startswith("al_ws="))
        for flag in ("httponly", "samesite=strict", "path=/"):
            self.assertIn(flag, cookie.lower())
        body = r.json()
        self.assertEqual(set(body), {"scope", "candidates", "expires_in_sec"})
        self.assertEqual(set(body["scope"]), SCOPE_KEYS)
        self.assertEqual(body["scope"]["human_requests"], 4)
        self.assertIn("수업 기간에는 공개·연습용 세션만 올려 주세요", body["scope"]["notices"])
        self.assertEqual(body["expires_in_sec"], 3600)

        cards = body["candidates"]
        self.assertTrue(1 <= len(cards) <= 3)
        for card in cards:
            self.assertEqual(set(card), CARD_KEYS)
        first = cards[0]
        self.assertEqual((first["id"], first["rank"], first["turn"]), ("c1", 1, 2))
        self.assertEqual((first["title"], first["category_label"]), ("외부 호출 방식에 관한 결정", "외부 호출"))
        self.assertEqual(first["files"], ["weather.py", "tests/test_weather.py"])
        self.assertEqual(first["lines"], [19, 22])
        self.assertEqual((first["evidence_level"], first["evidence_level_label"]), ("sufficient", "충분"))
        self.assertIs(first["has_test_record"], True)
        self.assertIs(first["retro_available"], True)
        self.assertIn({"code": "try_except", "label": "예외 처리 코드 추가"}, first["signals"])
        no_test = next(card for card in cards if card["turn"] == 4)
        self.assertEqual((no_test["has_test_record"], no_test["retro_available"]), (False, False))

        # Cards carry no session text: no human request appears in any card value.
        requests = [e.text for e in parse_session(load_bytes("demo_weather.jsonl")).events if e.kind == "user_text"]
        leaves = list(strings(cards))
        blob = json.dumps(cards, ensure_ascii=False)
        for text in requests:
            self.assertFalse(any(text in leaf for leaf in leaves), text)
            self.assertNotIn(text[:12], blob)
        for secret in FAKE_SECRETS:
            self.assertNotIn(secret, r.text)

    def test_size_cap(self):
        cap = 32 * MIB
        self.assertEqual((Settings().max_upload_bytes, cap), (33_554_432, 33_554_432))
        over = b"x" * (cap + 1)
        r = self.client.post("/api/workspaces", content=over)  # declared Content-Length
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "too_large"))
        self.assertIn("32MiB", r.json()["error"]["message"])
        r = self.client.post("/api/workspaces", content=iter([over[: 16 * MIB], over[16 * MIB :]]))  # chunked
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "too_large"))
        at_cap = over[:cap]  # exactly at the cap is read and parsed, declared or chunked
        r = self.client.post("/api/workspaces", content=at_cap)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "bad_format"))
        r = self.client.post("/api/workspaces", content=iter([at_cap[: 16 * MIB], at_cap[16 * MIB :]]))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "bad_format"))
        self.assertNotIn("set-cookie", r.headers)

    def test_frontend_precheck_uses_the_same_cap(self):
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        found = re.search(r"const MAX_UPLOAD_BYTES = (\d+) \* 1024 \* 1024;", js)
        self.assertEqual(int(found[1]) * MIB, Settings().max_upload_bytes)
        self.assertNotIn("16MiB", js)
        self.assertIn("32MiB", (STATIC / "index.html").read_text(encoding="utf-8"))

    def test_demo_session_download(self):
        r = self.client.get("/api/demo-session")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, load_bytes("demo_weather.jsonl"))
        self.assertTrue(r.headers["content-type"].startswith("application/x-ndjson"))
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", r.headers)
        # The browser posts the same bytes like a user file.
        self.assertEqual(self.client.post("/api/workspaces", content=r.content).status_code, 201)
        # One fixed file: no path, query or method reaches another one.
        for path in ("/api/demo-session/secrets.jsonl", "/api/demo-session/..%2Fsecrets.jsonl", "/api/demo-sessions"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.get("/api/demo-session?name=secrets.jsonl").content, r.content)
        self.assertEqual(self.client.post("/api/demo-session").status_code, 405)

    def test_malformed_lines_are_counted(self):
        r = upload(self.client, "malformed.jsonl")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["scope"]["bad_lines"], 2)
        self.assertNotIn("이 줄은 중간에 잘렸습니다", r.text)

    def test_bad_format_and_no_events(self):
        for content in (b"\x00\xff\xfe garbage\nnot json\n", b"", b"[1, 2]\n42\n"):
            r = self.client.post("/api/workspaces", content=content)
            self.assertEqual(r.status_code, 400, content)
            self.assertEqual(r.json()["error"], {"code": "bad_format", "message": "Claude Code 세션 JSONL 형식이 아닙니다.",
                                                 "retryable": False})
        r = upload(self.client, "meta_only.jsonl")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "no_events"))
        self.assertEqual(len(self.app.state.store), 0)

    def test_lone_surrogates_never_reach_a_response(self):
        from tests.test_flow_api import agree, wait_job  # here: test_flow_api imports this module

        lone, replaced = chr(0xD800), "\N{REPLACEMENT CHARACTER}"  # json.loads keeps an escaped "\ud800" as is
        session = tp.jsonl(*(json.dumps(record) for record in (  # ensure_ascii: the file holds the escape
            tp.human(f"날씨 API 호출에 timeout을 넣어 주세요 {lone}"),
            tp.ai(tp.text(f"timeout을 5초로 두고 requests.Timeout을 잡겠습니다. 보통 5초면 충분하다고 가정합니다 {lone}")),
            tp.ai(tp.use("t1", "Edit", {"file_path": f"w{lone}.py", "old_string": "a", "new_string": f"timeout=5 {lone}"})),
            tp.ai(tp.use("t2", "Bash", {"command": "pytest -q"})),
            tp.result("t2", f"Exit code 1\nE {lone}\n1 failed in 0.10s", is_error=True),
        )))
        with make_client(self.app) as client:
            r = client.post("/api/workspaces", content=session)
            self.assertEqual((r.status_code, r.json()["candidates"][0]["files"]), (201, [f"w{replaced}.py"]))
            responses = [client.get("/api/workspace"),
                         client.post("/api/select", json={"candidate_id": "c1", "self_assessment": SA}),
                         client.post("/api/prediction", json={"choice": "fail"})]
            agree(client)  # the preview carries every visible text
            ids = client.post("/api/analysis").json()
            jobs = [wait_job(client, ids["job_id"]), wait_job(client, ids["summary_job_id"])]
            view = client.get("/api/workspace")
            responses += [view, *(client.get(f"/api/evidence/{i}") for i in view.json()["evidence_ids"])]
        self.assertEqual({r.status_code for r in responses}, {200})
        self.assertEqual([job["status"] for job in jobs], ["done", "done"])
        texts = [item["text"] for item in view.json()["evidence"]]
        self.assertEqual(len(texts), 5)
        self.assertFalse(any(lone in text for text in texts))
        self.assertEqual(texts[-1], f"Exit code 1\nE {replaced}\n1 failed in 0.10s")  # the target output, revealed

    def test_rate_limit_per_ip(self):
        app = make_app(upload_rate_per_10min=2)
        now = fake_clock(app)
        client = make_client(app)
        self.assertEqual([upload(client, "no_tests.jsonl").status_code for _ in range(2)], [201, 201])
        r = upload(client, "no_tests.jsonl")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "rate_limited"))
        self.assertIs(r.json()["error"]["retryable"], True)
        other_ip = make_client(app, client=("10.0.0.2", 50000))
        self.assertEqual(upload(other_ip, "no_tests.jsonl").status_code, 201)
        now[0] += 601
        self.assertEqual(upload(client, "no_tests.jsonl").status_code, 201)

    def test_workspace_cap_and_replacement(self):
        app = make_app(max_workspaces=1)
        first, second = make_client(app), make_client(app)
        self.assertEqual(upload(first, "no_tests.jsonl").status_code, 201)
        old_cookie = first.cookies["al_ws"]
        r = upload(second, "no_tests.jsonl")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "server_busy"))
        # The caller's own workspace does not count against the cap; a new upload replaces it.
        self.assertEqual(upload(first, "demo_weather.jsonl").status_code, 201)
        self.assertNotEqual(first.cookies["al_ws"], old_cookie)
        self.assertEqual(len(app.state.store), 1)
        self.assertIsNone(app.state.store.get(old_cookie))
        self.assertEqual(first.get("/api/workspace").json()["scope"]["human_requests"], 4)

    def test_workspace_view_and_delete(self):
        self.assertEqual(self.client.get("/api/workspace").json()["error"]["code"], "no_workspace")
        upload(self.client, "demo_weather.jsonl")
        view = self.client.get("/api/workspace").json()
        self.assertEqual(set(view), WORKSPACE_KEYS)
        self.assertEqual((view["selected"], view["retro"], view["evidence_ids"], view["evidence"], view["jobs"]),
                         (None, None, [], [], []))
        self.assertEqual(set(view["results"]), {"analysis", "report", "summary", "retrieval", "reviews", "comparison",
                                                "whatif", "feedback"})
        r = self.client.delete("/api/workspace")
        self.assertEqual(r.status_code, 204)
        self.assertIn('al_ws=""', r.headers["set-cookie"])
        self.assertEqual(len(self.app.state.store), 0)
        r = self.client.get("/api/workspace")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "no_workspace"))


class ExpiryTest(unittest.TestCase):
    def test_sliding_ttl_with_injected_clock(self):
        app = make_app(workspace_ttl_sec=100)
        now = fake_clock(app)
        client = make_client(app)
        upload(client, "demo_weather.jsonl")
        now[0] += 99
        self.assertEqual(client.get("/api/workspace").status_code, 200)  # access refreshes the TTL
        now[0] += 99
        self.assertEqual(client.get("/api/workspace").status_code, 200)
        now[0] += 101
        r = client.get("/api/workspace")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "no_workspace"))
        self.assertEqual(len(app.state.store), 0)

    def test_sweep_removes_expired(self):
        app = make_app(workspace_ttl_sec=100)
        now = fake_clock(app)
        upload(make_client(app), "no_tests.jsonl")
        upload(make_client(app), "no_tests.jsonl")
        now[0] += 50
        app.state.store.sweep()
        self.assertEqual(len(app.state.store), 2)
        now[0] += 51
        app.state.store.sweep()
        self.assertEqual(len(app.state.store), 0)

    def test_lifespan_sweeper(self):
        app = make_app(workspace_ttl_sec=100)
        now = fake_clock(app)
        with mock.patch("agency_loop.web.app.SWEEP_INTERVAL_SEC", 0.01), make_client(app) as client:
            upload(client, "no_tests.jsonl")
            now[0] += 101
            deadline = time.monotonic() + 5
            while len(app.state.store) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(app.state.store), 0)


if __name__ == "__main__":
    unittest.main()
