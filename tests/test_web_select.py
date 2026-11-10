"""Selection slice: POST /api/select, GET /api/evidence, retro views, jobs (ARCHITECTURE §4.5, §5.9, §7.2, §7.3)."""

import asyncio
import json
import logging
import time
import unittest
from unittest import mock

from agency_loop.decisions.evidence import visible_evidence_ids
from agency_loop.model import Workspace
from agency_loop.providers.base import ProviderError
from agency_loop.web import views
from agency_loop.web.jobs import JobRunner, update
from tests.test_web_upload import SA, make_app, make_client, upload

EVIDENCE_KEYS = {"id", "kind", "kind_label", "turn", "line", "file", "text", "diff", "later_fix"}
PRECONTEXT_IDS = ["E0010", "E0012", "E0015", "E0018"]  # demo_weather turn 2: request, two edits, the pytest command


def select(client, candidate_id="c1", sa=SA):
    return client.post("/api/select", json={"candidate_id": candidate_id, "self_assessment": sa})


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.client = make_client(self.app)
        upload(self.client, "demo_weather.jsonl")

    def test_retro_candidate_shows_precontext_only(self):
        r = select(self.client, "c1", {"understanding": 2, "verification": 1, "control": 1})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body), {"selected", "tier", "tier_label", "retro", "evidence"})
        self.assertEqual((body["selected"], body["tier"], body["tier_label"]), ("c1", "standard", "표준"))
        retro = body["retro"]
        self.assertEqual(set(retro), {"available", "precontext", "question", "choices"})
        self.assertEqual(retro["precontext"], {
            "request_id": "E0010", "last_request_id": None, "edit_ids": ["E0012", "E0015"],
            "command": {"evidence_id": "E0018", "command": "pytest -q", "runner": "pytest"},
        })
        self.assertEqual([c["value"] for c in retro["choices"]], ["pass", "fail", "unknown"])
        self.assertEqual([e["id"] for e in body["evidence"]], PRECONTEXT_IDS)
        for item in body["evidence"]:
            self.assertEqual(set(item), EVIDENCE_KEYS)
            self.assertIs(item["later_fix"], False)  # turn 2 is fixed later, but the tag would hint at the result
        edit = body["evidence"][1]
        self.assertEqual((edit["kind"], edit["kind_label"], edit["file"], edit["line"]), ("code_edit", "코드 변경", "weather.py", 19))
        self.assertEqual(edit["diff"][0], {"op": "@", "text": "--- weather.py"})
        self.assertEqual({row["op"] for row in edit["diff"][1:]}, {"-", "+"})
        self.assertIsNone(body["evidence"][0]["diff"])
        for text in ("1 failed", "Exit code 1", "NoneType", "실패했습니다"):  # the recorded result stays hidden
            self.assertNotIn(text, r.text)

        view = self.client.get("/api/workspace").json()
        self.assertEqual((view["selected"], view["tier"], view["self_assessment"]),
                         ("c1", "standard", {"understanding": 2, "verification": 1, "control": 1}))
        self.assertEqual(view["evidence_ids"], PRECONTEXT_IDS)
        self.assertEqual(view["retro"], retro)

    def test_evidence_gate(self):
        r = self.client.get("/api/evidence/E0010")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))  # nothing before selection
        select(self.client, "c1")
        for evidence_id in PRECONTEXT_IDS:
            r = self.client.get(f"/api/evidence/{evidence_id}")
            self.assertEqual((r.status_code, r.json()["id"]), (200, evidence_id))
        hidden = [f"E{idx:04d}" for idx in range(42) if f"E{idx:04d}" not in PRECONTEXT_IDS] + ["E9999", "x"]
        for evidence_id in hidden:
            self.assertEqual(self.client.get(f"/api/evidence/{evidence_id}").status_code, 404, evidence_id)

    def test_lock(self):
        self.assertEqual(select(self.client, "c1").status_code, 200)
        for candidate_id in ("c1", "c2"):
            r = select(self.client, candidate_id)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "locked"))
        self.assertEqual(self.client.get("/api/workspace").json()["selected"], "c1")

    def test_strict_validation_does_not_lock(self):
        bad_bodies = [
            {"candidate_id": "c1"},
            {"candidate_id": "c1", "self_assessment": {"understanding": 1, "verification": 1}},
            {"candidate_id": "c1", "self_assessment": {**SA, "control": 3}},
            {"candidate_id": "c1", "self_assessment": {**SA, "control": True}},
            {"candidate_id": "c1", "self_assessment": {**SA, "control": "2"}},
            {"candidate_id": "c1", "self_assessment": {**SA, "extra": 1}},
            ["c1"],
        ]
        for body in bad_bodies:
            r = self.client.post("/api/select", json=body)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_request"), body)
        r = self.client.post("/api/select", content=b"{not json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/select", content=b"[" * (70 * 1024))  # over the JSON body cap
        self.assertEqual(r.status_code, 400)
        r = select(self.client, "c9")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))
        self.assertIsNone(self.client.get("/api/workspace").json()["selected"])
        self.assertEqual(select(self.client, "c1").status_code, 200)

    def test_no_workspace(self):
        client = make_client(self.app)
        r = select(client, "c1")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "no_workspace"))
        self.assertEqual(client.get("/api/evidence/E0000").json()["error"]["code"], "no_workspace")

    def test_ineligible_candidate_shows_window(self):
        r = select(self.client, "c2", {"understanding": 0, "verification": 0, "control": 1})  # turn 4, no test run
        body = r.json()
        self.assertEqual((body["tier"], body["tier_label"]), ("basic", "기본"))
        self.assertEqual(body["retro"], {
            "available": False, "reason": "no_test", "reason_label": "테스트 기록 없음",
            "message": "이 후보는 기록만으로 테스트 결과를 확정할 수 없어 예측 없이 분석으로 넘어갑니다. 이유: 테스트 기록 없음",
        })
        self.assertEqual([e["id"] for e in body["evidence"]], [f"E{idx:04d}" for idx in range(31, 42)])

    def test_stored_prediction_is_read_only_view(self):
        select(self.client, "c1")
        ws = self.app.state.store.get(self.client.cookies["al_ws"])
        ws.prediction = {"choice": "pass", "reason": "", "label": "mismatch", "submitted_at": 0.0}
        view = self.client.get("/api/workspace").json()
        retro = view["retro"]
        self.assertEqual((retro["submitted"], retro["label"], retro["label_text"]), (True, "mismatch", "불일치"))
        self.assertEqual(retro["note"], views.PREDICTION_NOTE)  # the same note as the prediction response
        self.assertEqual(retro["prediction"], {"choice": "pass", "choice_label": "통과", "reason": ""})
        self.assertEqual((retro["reveal"]["result"], retro["reveal"]["evidence_id"]), ("fail", "E0019"))
        self.assertEqual(view["evidence_ids"], [f"E{idx:04d}" for idx in range(10, 20)])  # the analysis window
        later = {e: self.client.get(f"/api/evidence/{e}").json()["later_fix"] for e in ("E0012", "E0015")}
        self.assertEqual(later, {"E0012": True, "E0015": False})  # only weather.py is edited after the window

    def test_workspace_view_lists_the_visible_evidence_in_every_state(self):
        def check(client, expected):
            ws = self.app.state.store.get(client.cookies["al_ws"])
            view = client.get("/api/workspace").json()
            self.assertEqual(visible_evidence_ids(ws), expected)
            self.assertEqual(([item["id"] for item in view["evidence"]], view["evidence_ids"]), (expected, expected))
            for item in view["evidence"]:  # the same items GET /api/evidence serves
                self.assertEqual(item, client.get(f"/api/evidence/{item['id']}").json())
            return ws

        check(self.client, [])  # before selection
        select(self.client, "c1")
        ws = check(self.client, PRECONTEXT_IDS)  # retro eligible, no prediction: pre-context only
        ws.prediction = {"choice": "fail", "reason": "", "label": "match", "submitted_at": 0.0}
        window = [f"E{idx:04d}" for idx in range(10, 20)]
        check(self.client, window)
        ws.results["retrieval"] = {"retrieved": ["E0021", "E0025"]}  # a human request and a later fix
        check(self.client, [*window, "E0021"])
        other = make_client(self.app)
        upload(other, "demo_weather.jsonl")
        select(other, "c2")  # not retro eligible: the analysis window at once
        check(other, [f"E{idx:04d}" for idx in range(31, 42)])

    def test_later_fix_tags_scan_the_session_once(self):
        # A window scan per evidence item made select quadratic: ~100 s on the event loop for a 16 MiB upload.
        def record(role, content):
            return json.dumps({"type": role, "message": {"role": role, "content": content}})

        def edit(i):
            return record("assistant", [{"type": "tool_use", "id": f"t{i}", "name": "Edit",
                                         "input": {"file_path": "a.py", "old_string": "x", "new_string": "y"}}])

        lines = [record("user", "첫 번째 요청입니다"), *map(edit, range(50)), record("user", "두 번째 요청입니다"), edit(50)]
        self.client.post("/api/workspaces", content="\n".join(lines).encode())
        with mock.patch("agency_loop.web.views.analysis_window", wraps=views.analysis_window) as window:
            body = select(self.client, "c1").json()  # turn 1: no test run, a.py edited again in turn 2
        self.assertEqual(window.call_count, 1)
        self.assertEqual(sum(item["later_fix"] for item in body["evidence"]), 50)


async def _wait(job):
    for _ in range(200):
        if job.status in ("done", "failed"):
            return
        await asyncio.sleep(0.005)


def _workspace():
    return Workspace(id="ws", created_at=0.0, last_used=0.0, session=None, evidence={}, test_runs=[], candidates=[])


class JobRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_done_with_status_updates(self):
        runner, ws = JobRunner(), _workspace()
        seen = []

        async def work(job):
            seen.append(job.status)
            update(job, "streaming", "응답을 받는 중")
            job.model = "mock-claude"
            return {"ok": True}

        job = runner.start(ws, "analysis", work)
        self.assertEqual((ws.jobs[job.id], job.status), (job, "queued"))
        await _wait(job)
        self.assertEqual((seen, job.status, job.progress, job.result, job.error), (["running"], "done", "응답을 받는 중",
                                                                                  {"ok": True}, None))

    async def test_failures_keep_a_safe_error(self):
        runner, ws = JobRunner(), _workspace()

        async def provider_error(_job):
            raise ProviderError("timeout", retryable=True)

        async def bug(_job):
            raise KeyError("SESSION-TEXT-SHOULD-NOT-BE-LOGGED")

        first = runner.start(ws, "analysis", provider_error)
        with self.assertLogs("agency_loop.web.jobs", logging.ERROR) as logs:
            second = runner.start(ws, "summary", bug)
            await _wait(first)
            await _wait(second)
        self.assertEqual((first.status, first.error), ("failed", {"code": "timeout", "message": "응답 시간이 초과되었습니다.",
                                                                  "retryable": True}))
        self.assertEqual((second.status, second.error["code"], second.error["retryable"]), ("failed", "internal", False))
        self.assertNotIn("SESSION-TEXT", "\n".join(logs.output))

    async def test_cancel_on_workspace_removal(self):
        runner, ws = JobRunner(), _workspace()
        started = asyncio.Event()

        async def slow(_job):
            started.set()
            await asyncio.sleep(30)
            return {}

        job = runner.start(ws, "summary", slow)
        await started.wait()
        runner.cancel(ws)
        await asyncio.sleep(0.01)
        self.assertNotEqual(job.status, "done")
        self.assertEqual(runner._tasks, {})


class JobEndpointTest(unittest.TestCase):
    def test_job_view_is_workspace_scoped(self):
        app = make_app()
        with make_client(app) as owner, make_client(app) as other:
            upload(owner, "demo_weather.jsonl")
            upload(other, "demo_weather.jsonl")
            ws = app.state.store.get(owner.cookies["al_ws"])

            async def work(job):
                job.model = "mock-claude"
                return {"answer": 1}

            async def start():
                return app.state.jobs.start(ws, "summary", work).id

            job_id = owner.portal.call(start)
            deadline = time.monotonic() + 5
            while (view := owner.get(f"/api/jobs/{job_id}").json())["status"] != "done" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(set(view), {"id", "kind", "status", "status_label", "progress", "model", "mock", "result",
                                         "error", "updated_at"})
            self.assertEqual((view["status_label"], view["mock"], view["result"]), ("완료", True, {"answer": 1}))
            listed = owner.get("/api/workspace").json()["jobs"]
            self.assertEqual([j["id"] for j in listed], [job_id])
            self.assertNotIn("result", listed[0])
            r = other.get(f"/api/jobs/{job_id}")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
