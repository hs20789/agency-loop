"""agency_loop.flow.caps: per-flow and daily counters with an injected clock, concurrency slots (ARCHITECTURE §5.8)."""

import json
import time
import unittest
from dataclasses import replace

from agency_loop.config import Settings
from agency_loop.flow.caps import Caps
from agency_loop.model import Workspace
from agency_loop.providers.base import ProviderError


def ws():
    return Workspace(id="ws", created_at=0.0, last_used=0.0, session=None, evidence={}, test_runs=[], candidates=[])


def local(*fields):
    """Unix time of a local wall-clock moment, so the midnight checks hold in any time zone."""
    return time.mktime((*fields, 0, 0, -1))


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def code(call):
    try:
        call()
    except ProviderError as exc:
        return exc.code
    return None


class PerFlowTest(unittest.TestCase):
    def test_claude_attempts_per_flow(self):
        caps, flow = Caps(Settings()), ws()
        gate = caps.gate(flow, "analysis")
        for _ in range(7):  # 5 model calls of the flow (ⒶⒺⒷⒸⒹ) + 2 spare retries
            gate("anthropic")
        self.assertEqual(code(lambda: gate("anthropic")), "cap_exceeded")
        self.assertEqual(code(lambda: caps.gate(flow, "feedback2")("anthropic")), "cap_exceeded")  # shared by stages
        self.assertEqual(flow.calls, {"anthropic": 7})  # refused attempts are not counted
        self.assertEqual(caps.daily_usage()["anthropic"]["used"], 7)
        self.assertIsNone(code(lambda: caps.gate(ws(), "analysis")("anthropic")))  # another flow has its own count

    def test_one_call_each_for_the_others(self):
        caps, flow = Caps(Settings()), ws()
        for provider, stage in (("hcx", "review:hcx"), ("deepseek", "review:deepseek"), ("clova_rerank", "analysis"),
                                ("local_rag", "analysis"), ("local_llm", "analysis")):
            gate = caps.gate(flow, stage)
            self.assertIsNone(code(lambda gate=gate, p=provider: gate(p)))
            self.assertEqual(code(lambda gate=gate, p=provider: gate(p)), "cap_exceeded", provider)
        self.assertIsNone(code(lambda: caps.gate(flow, "review:other")("hcx")))  # HCX counts per stage
        self.assertEqual(flow.calls, {
            "hcx:review:hcx": 1, "deepseek:review:deepseek": 1, "clova_rerank": 1, "local_rag": 1, "local_llm": 1,
            "hcx:review:other": 1,
        })

    def test_exhausted_matches_what_the_gate_would_do(self):
        caps, flow = Caps(replace(Settings(), claude_attempts_per_flow=2)), ws()
        for provider, stage, allowed in (("anthropic", "analysis", 2), ("hcx", "review:hcx", 1), ("local_llm", "analysis", 1)):
            gate = caps.gate(flow, stage)
            for _ in range(allowed):
                self.assertFalse(caps.exhausted(flow, provider, stage), provider)
                gate(provider)
            self.assertTrue(caps.exhausted(flow, provider, stage), provider)
            self.assertEqual(code(lambda gate=gate, p=provider: gate(p)), "cap_exceeded", provider)
        self.assertFalse(caps.exhausted(flow, "hcx", "review:other"))  # per stage


class DailyTest(unittest.TestCase):
    def test_daily_caps_reset_at_local_midnight_and_not_on_new_uploads(self):
        clock = Clock(local(2026, 11, 8, 23, 59, 30))
        caps = Caps(replace(Settings(), claude_daily_attempts=3), clock=clock)
        first = caps.gate(ws(), "analysis")
        first("anthropic"), first("anthropic")
        new_upload = ws()  # a new workspace never resets the server-wide totals
        caps.gate(new_upload, "analysis")("anthropic")
        self.assertEqual(code(lambda: caps.gate(new_upload, "whatif")("anthropic")), "daily_cap")
        self.assertEqual(new_upload.calls, {"anthropic": 1})
        clock.now = local(2026, 11, 8, 23, 59, 59)
        self.assertEqual(code(lambda: caps.gate(ws(), "analysis")("anthropic")), "daily_cap")
        clock.now = local(2026, 11, 9, 0, 0, 1)
        self.assertIsNone(code(lambda: caps.gate(new_upload, "whatif")("anthropic")))
        self.assertEqual(caps.daily_usage()["anthropic"], {"used": 1, "limit": 3})
        self.assertEqual(caps.daily_usage()["date"], "2026-11-09")

    def test_clova_total_counts_hcx_and_reranker_and_deepseek_has_none(self):
        caps = Caps(replace(Settings(), clova_daily_calls=2), clock=Clock(local(2026, 11, 8, 12, 0, 0)))
        caps.gate(ws(), "review:hcx")("hcx")
        caps.gate(ws(), "analysis")("clova_rerank")
        self.assertEqual(code(lambda: caps.gate(ws(), "review:hcx")("hcx")), "daily_cap")
        self.assertEqual(code(lambda: caps.gate(ws(), "analysis")("clova_rerank")), "daily_cap")
        for _ in range(5):
            caps.gate(ws(), "review:deepseek")("deepseek")
            caps.gate(ws(), "analysis")("local_rag")
        self.assertEqual(caps.daily_usage(), {
            "date": "2026-11-08", "anthropic": {"used": 0, "limit": 300}, "clova": {"used": 2, "limit": 2},
            "running": {"analysis": 0, "local_llm": 0},
        })

    def test_per_flow_is_checked_before_daily_and_neither_counts_a_refusal(self):
        caps, flow = Caps(replace(Settings(), clova_daily_calls=1)), ws()
        caps.gate(flow, "review:hcx")("hcx")
        self.assertEqual(code(lambda: caps.gate(flow, "review:hcx")("hcx")), "cap_exceeded")
        self.assertEqual(code(lambda: caps.gate(ws(), "review:hcx")("hcx")), "daily_cap")
        self.assertEqual(caps.daily_usage()["clova"]["used"], 1)
        json.dumps(caps.daily_usage())  # health/debug output: plain counts


class SlotTest(unittest.TestCase):
    def test_slots_are_non_blocking(self):
        caps = Caps(Settings())
        with caps.analysis_slot(), caps.analysis_slot():
            self.assertEqual(code(lambda: caps.analysis_slot().__enter__()), "concurrency")
            self.assertEqual(caps.daily_usage()["running"]["analysis"], 2)
            with caps.local_llm_slot():  # a separate pool
                self.assertEqual(code(lambda: caps.local_llm_slot().__enter__()), "concurrency")
        with caps.analysis_slot():
            pass
        self.assertEqual(caps.daily_usage()["running"], {"analysis": 0, "local_llm": 0})

    def test_a_failing_job_releases_its_slot(self):
        caps = Caps(replace(Settings(), analysis_concurrency=1))
        with self.assertRaises(RuntimeError), caps.analysis_slot():
            raise RuntimeError("job failed")
        with caps.analysis_slot(), self.assertRaises(ProviderError) as cm:
            caps.analysis_slot().__enter__()
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("concurrency", True))
        self.assertEqual(caps.daily_usage()["running"]["analysis"], 0)


if __name__ == "__main__":
    unittest.main()
