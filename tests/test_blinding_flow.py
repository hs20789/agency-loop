"""Prediction and the retro blinding gate on the slice-2 routes: one prediction, the reveal only in its response,
nothing model-bound before it, and the prediction never reaching a provider (ARCHITECTURE §4.5, §7.2)."""

import json
import logging
import unittest

from agency_loop import labels
from agency_loop.web.views import PREDICTION_NOTE
from tests.helpers import PAIR_MARKERS
from tests.test_consent import ANSWERS
from tests.test_flow_api import ZERO, agree, counts, feedback, flow_app, mock_providers, run, wait_job
from tests.test_security import capture_all_logs
from tests.test_web_upload import SA, make_client, upload

WINDOW = [f"E{i:04d}" for i in range(10, 20)]  # demo_weather c1: its request .. the target result
PREDICTION_KEYS = {"prediction", "reveal", "label", "label_text", "checkpoints", "note", "evidence"}
# Every model-bound request a browser can send; each must be refused while the candidate is blinded.
MODEL_BOUND = [
    ("/api/preview", {"stage": "analysis"}),
    ("/api/preview", {"stage": "review:hcx"}),
    ("/api/preview", {"stage": "feedback1", "answers": ANSWERS}),
    ("/api/consent", {"preview_id": "x", "providers": {"anthropic": True}}),
    ("/api/analysis", None),
    ("/api/report", None),
    ("/api/summary", None),
    ("/api/whatif", None),
    ("/api/feedback", {"attempt": 1, "preview_id": "x"}),
]


def select(client, candidate_id="c1"):
    return client.post("/api/select", json={"candidate_id": candidate_id, "self_assessment": SA})


class PredictionTest(unittest.TestCase):
    def test_labels_and_the_reveal(self):
        reason = "시간 제한 처리가 테스트와 어긋날 것 같다"
        for choice, label, text in (("fail", "match", "일치"), ("pass", "mismatch", "불일치"),
                                    ("unknown", "withheld", "판단 보류")):
            with self.subTest(choice=choice), make_client(flow_app(mock_providers())) as client:  # one workspace each
                upload(client, "demo_weather.jsonl")
                select(client)
                r = client.post("/api/prediction", json={"choice": choice, "reason": reason})
                self.assertEqual((r.status_code, r.headers["cache-control"]), (200, "no-store"))
                body = r.json()
                self.assertEqual(set(body), PREDICTION_KEYS)
                self.assertEqual((body["label"], body["label_text"], body["note"]), (label, text, PREDICTION_NOTE))
                self.assertIn("검증 능력이 높다는 뜻은 아니며", PREDICTION_NOTE)  # matching is not a measure of it
                self.assertEqual(body["prediction"], {"choice": choice, "choice_label": labels.PREDICTION_CHOICE[choice],
                                                      "reason": reason})
                reveal = body["reveal"]
                self.assertEqual((reveal["result"], reveal["exit_code"], reveal["summary_line"], reveal["evidence_id"]),
                                 ("fail", 1, "1 failed, 3 passed in 0.42s", "E0019"))
                self.assertEqual(body["checkpoints"], ["종료 코드: 1 (명시)", "요약 줄: 1 failed, 3 passed in 0.42s"])
                self.assertEqual([item["id"] for item in body["evidence"]], WINDOW)
                retro = client.get("/api/workspace").json()["retro"]  # later reads: the stored copy, read-only
                self.assertEqual({key: retro[key] for key in PREDICTION_KEYS - {"evidence"}},
                                 {key: body[key] for key in PREDICTION_KEYS - {"evidence"}})

    def test_once_and_only_for_the_selected_retro_candidate(self):
        with make_client(flow_app(mock_providers())) as client:
            upload(client, "demo_weather.jsonl")
            r = client.post("/api/prediction", json={"choice": "pass"})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"))  # nothing selected
            select(client)
            for body in ({"choice": "maybe"}, {"choice": ["pass"]}, {"choice": "pass", "reason": 3},
                         {"choice": "pass", "reason": "가" * 1001}, ["pass"], None):
                r = client.post("/api/prediction", json=body)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_request"), body)
            self.assertNotIn("submitted", client.get("/api/workspace").json()["retro"])
            r = client.post("/api/prediction", json={"choice": "pass", "reason": "가" * 1000})
            self.assertEqual(r.status_code, 200)
            r = client.post("/api/prediction", json={"choice": "fail", "reason": ""})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_submitted"))
            self.assertEqual(client.get("/api/workspace").json()["retro"]["prediction"]["choice"], "pass")
        with make_client(flow_app(mock_providers())) as client:
            upload(client, "demo_weather.jsonl")
            select(client, "c2")  # no test after the decision
            r = client.post("/api/prediction", json={"choice": "pass", "reason": ""})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_eligible"))


class BlindingFlowTest(unittest.TestCase):
    def test_nothing_model_bound_runs_before_the_prediction(self):
        p = mock_providers()
        app = flow_app(p)
        with make_client(app) as client:
            upload(client, "demo_weather.jsonl")
            for selected in (False, True):
                for path, body in MODEL_BOUND:
                    r = client.post(path, json=body)
                    self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "precondition"), (selected, path))
                select(client)
            ws = app.state.store.get(client.cookies["al_ws"])
            self.assertEqual((ws.previews, ws.consents, ws.jobs, ws.calls, ws.results), ({}, {}, {}, {}, {}))
            self.assertEqual(counts(p), ZERO)

    def _responses(self, name):
        """(responses before the prediction, the prediction response) on the turn-2 retro candidate; the flow then
        runs on so its logs are captured too."""
        p = mock_providers()
        with make_client(flow_app(p)) as client:
            bodies = [upload(client, name).text, client.get("/api/workspace").text]
            r = select(client)
            self.assertEqual(r.json()["retro"]["precontext"]["command"]["evidence_id"], "E0018")
            bodies.append(r.text)
            bodies += [client.post(path, json=body).text for path, body in MODEL_BOUND]
            bodies.append(client.get("/api/workspace").text)
            self.assertEqual(counts(p), ZERO)
            revealed = client.post("/api/prediction", json={"choice": "unknown", "reason": ""}).text
            agree(client)
            self.assertEqual(wait_job(client, client.post("/api/analysis").json()["job_id"])["status"], "done")
            self.assertEqual(run(client, "/api/whatif")["status"], "done")
        return bodies, revealed

    def test_pair_markers_stay_hidden_until_the_prediction_response(self):
        with capture_all_logs() as logs:
            logging.getLogger("agency_loop").debug("capture check")
            fail, fail_reveal = self._responses("pair_fail.jsonl")
            passed, pass_reveal = self._responses("pair_pass.jsonl")
        self.assertIn("capture check", "\n".join(logs))
        self.assertEqual(fail, passed)  # the pair differs only in the target result; these bodies hold no random id
        for marker in PAIR_MARKERS:
            for text in fail + passed + logs:
                self.assertNotIn(marker, text)
        self.assertIn("1 failed, 3 passed in 0.40s", fail_reveal)
        self.assertIn("4 passed in 0.40s", pass_reveal)
        self.assertIn(PAIR_MARKERS[0], fail_reveal)  # the output excerpt: revealed only by this response
        self.assertIn(PAIR_MARKERS[1], pass_reveal)

    def test_the_prediction_never_reaches_a_provider(self):
        p = mock_providers()
        reason = "PREDICTION-REASON-MARKER-5Z"
        with make_client(flow_app(p)) as client:
            upload(client, "demo_weather.jsonl")
            select(client)
            client.post("/api/prediction", json={"choice": "fail", "reason": reason})
            previews = [agree(client)]
            ids = client.post("/api/analysis").json()
            wait_job(client, ids["job_id"]), wait_job(client, ids["summary_job_id"])
            run(client, "/api/whatif")
            feedback(client, 1)
            feedback(client, 2)
            previews += [client.post("/api/preview", json={"stage": stage, "answers": ANSWERS}).json()
                         for stage in ("feedback1", "feedback2")]
            self.assertEqual(client.get("/api/workspace").json()["retro"]["prediction"]["reason"], reason)
        sent = [user for _, user in p.claude.calls] + p.summarizer.calls
        self.assertEqual(len(sent), 5)
        self.assertIn("<tier>standard</tier>", p.claude.calls[1][1])  # the What-if gets the tier code only
        for text in sent + [json.dumps(preview, ensure_ascii=False) for preview in previews]:
            self.assertNotIn(reason, text)
            self.assertNotIn("self_rating", text)


if __name__ == "__main__":
    unittest.main()
