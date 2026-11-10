"""Web security: Host/Origin, headers, workspace isolation, retro blinding and secrets (ARCHITECTURE §4.5, §7.1)."""

import contextlib
import logging
import time
import unittest

from starlette.routing import Route
from starlette.testclient import TestClient

from tests.helpers import FAKE_SECRETS, PAIR_MARKERS
from tests.test_web_upload import ORIGIN, SA, make_app, make_client, upload

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@contextlib.contextmanager
def capture_all_logs():
    """Every record of every logger at DEBUG, formatted with arguments and tracebacks."""
    handler = _Collect()
    loggers = [logging.getLogger(), *(x for x in logging.root.manager.loggerDict.values() if isinstance(x, logging.Logger))]
    saved = [(logger, logger.level, logger.disabled) for logger in loggers]
    disabled = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
        logger.addHandler(handler)
    try:
        yield handler.lines
    finally:
        logging.disable(disabled)
        for logger, level, was_disabled in saved:
            logger.setLevel(level)
            logger.disabled = was_disabled
            logger.removeHandler(handler)


class HostOriginTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def test_foreign_host_is_rejected(self):
        client = TestClient(self.app, base_url="http://evil.example:8000", headers={"Origin": "http://evil.example:8000"})
        for method, path in (("GET", "/"), ("GET", "/api/health"), ("POST", "/api/workspaces")):
            r = client.request(method, path, content=b"{}" if method == "POST" else None)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "bad_host"), path)
        self.assertEqual(len(self.app.state.store), 0)

    def test_post_and_delete_need_same_origin(self):
        no_origin = TestClient(self.app, base_url=ORIGIN)
        foreign = TestClient(self.app, base_url=ORIGIN, headers={"Origin": "http://evil.example"})
        for client in (no_origin, foreign):
            for method, path in (("POST", "/api/workspaces"), ("POST", "/api/select"), ("DELETE", "/api/workspace")):
                r = client.request(method, path, content=b"{}" if method == "POST" else None)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "bad_origin"), (method, path))
        self.assertEqual(len(self.app.state.store), 0)
        self.assertEqual(no_origin.get("/api/health").status_code, 200)  # GET needs no Origin
        other_allowed = TestClient(self.app, base_url="http://localhost:8000", headers={"Origin": "http://localhost:8000"})
        self.assertEqual(upload(other_allowed, "no_tests.jsonl").status_code, 201)

    def test_headers_on_every_response(self):
        client = make_client(self.app)

        async def broken(_request):
            raise RuntimeError("bug")

        self.app.router.routes.insert(0, Route("/api/broken", broken))
        responses = {
            "page": client.get("/"),
            "static": client.get("/static/app.js"),
            "health": client.get("/api/health"),
            "not_found": client.get("/api/evidence/E0000"),
            "bad_host": TestClient(self.app, base_url="http://evil.example").get("/api/health"),
            "bad_origin": TestClient(self.app, base_url=ORIGIN).delete("/api/workspace"),
            "server_error": make_client(self.app, raise_server_exceptions=False).get("/api/broken"),
        }
        for name, r in responses.items():
            for header, value in SECURITY_HEADERS.items():
                self.assertEqual(r.headers.get(header), value, (name, header))
            self.assertNotIn("access-control-allow-origin", r.headers)
            if name != "page" and name != "static":
                self.assertEqual(r.headers.get("cache-control"), "no-store", name)
        self.assertEqual(responses["page"].status_code, 200)
        error = responses["server_error"]
        self.assertEqual((error.status_code, error.json()["error"]["code"]), (500, "internal"))
        self.assertEqual(responses["health"].json(), {"ok": True, "modes": {
            "anthropic": "mock", "clova": "mock", "mistral": "mock", "embed": "mock", "local_llm": "mock"}})


class IsolationTest(unittest.TestCase):
    def test_clients_cannot_see_each_other(self):
        app = make_app()
        with make_client(app) as alice, make_client(app) as bob:
            upload(alice, "demo_weather.jsonl")
            alice.post("/api/select", json={"candidate_id": "c1", "self_assessment": SA})
            alice_ws = app.state.store.get(alice.cookies["al_ws"])

            async def work(_job):
                return {}

            job_id = alice.portal.call(_start_job, app, alice_ws, work)
            deadline = time.monotonic() + 5
            while alice.get(f"/api/jobs/{job_id}").json()["status"] != "done" and time.monotonic() < deadline:
                time.sleep(0.01)

            # Bob has no workspace: everything is 404.
            for path in ("/api/workspace", "/api/evidence/E0010", f"/api/jobs/{job_id}"):
                self.assertEqual(bob.get(path).json()["error"]["code"], "no_workspace", path)
            # Bob with his own workspace still cannot reach Alice's evidence or job.
            upload(bob, "demo_weather.jsonl")
            self.assertIsNone(bob.get("/api/workspace").json()["selected"])
            self.assertEqual(bob.get("/api/evidence/E0010").json()["error"]["code"], "not_found")
            self.assertEqual(bob.get(f"/api/jobs/{job_id}").json()["error"]["code"], "not_found")
            # A guessed cookie is just an unknown workspace.
            guesser = make_client(app, cookies={"al_ws": "guess"})
            self.assertEqual(guesser.get("/api/workspace").json()["error"]["code"], "no_workspace")
            # Alice is unaffected.
            self.assertEqual(alice.get("/api/evidence/E0010").status_code, 200)
            self.assertEqual(alice.get(f"/api/jobs/{job_id}").status_code, 200)


async def _start_job(app, ws, work):
    return app.state.jobs.start(ws, "summary", work).id


class BlindingTest(unittest.TestCase):
    def _pre_submission_bodies(self, name):
        """Every response a browser can get before submitting a prediction on the retro candidate."""
        app = make_app()
        bodies = []
        with make_client(app) as client:
            r = upload(client, name)
            bodies.append(r.text)
            bodies.append(client.get("/api/workspace").text)
            retro_id = next(card["id"] for card in r.json()["candidates"] if card["turn"] == 2)
            r = client.post("/api/select", json={"candidate_id": retro_id, "self_assessment": SA})
            self.assertTrue(r.json()["retro"]["available"])
            bodies.append(r.text)
            visible = [item["id"] for item in r.json()["evidence"]]
            self.assertEqual(visible, ["E0010", "E0012", "E0015", "E0018"])
            bodies += [client.get(f"/api/evidence/{evidence_id}").text for evidence_id in visible]
            bodies.append(client.get("/api/workspace").text)
            for evidence_id in [f"E{idx:04d}" for idx in range(26)]:  # every event of the pair files
                if evidence_id not in visible:  # E0019 is the target result
                    hidden = client.get(f"/api/evidence/{evidence_id}")
                    self.assertEqual(hidden.status_code, 404, evidence_id)
                    bodies.append(hidden.text)
        return bodies

    def test_pair_fixtures_give_identical_responses(self):
        with capture_all_logs() as logs:
            logging.getLogger("agency_loop").debug("capture check")
            fail = self._pre_submission_bodies("pair_fail.jsonl")
            passed = self._pre_submission_bodies("pair_pass.jsonl")
        self.assertIn("capture check", "\n".join(logs))
        self.assertEqual(fail, passed)
        for marker in PAIR_MARKERS:
            for body in fail + passed + logs:
                self.assertNotIn(marker, body)


class SecretsTest(unittest.TestCase):
    def test_no_fake_secret_in_any_response(self):
        for name in ("demo_weather.jsonl", "secrets.jsonl"):
            app = make_app()
            client = make_client(app)
            bodies = [upload(client, name).text]
            cards = client.get("/api/workspace").json()["candidates"]
            r = client.post("/api/select", json={"candidate_id": cards[0]["id"], "self_assessment": SA})
            bodies.append(r.text)
            bodies += [client.get(f"/api/evidence/{item['id']}").text for item in r.json()["evidence"]]
            bodies.append(client.get("/api/workspace").text)
            for secret in FAKE_SECRETS:
                for body in bodies:
                    self.assertNotIn(secret, body, (name, secret))


if __name__ == "__main__":
    unittest.main()
