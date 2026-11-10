import json
import math
import multiprocessing
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from agency_loop.ledger import Ledger, LedgerRefused

ROOT = Path(__file__).resolve().parents[1]
PRICE_IN, PRICE_OUT, OUT_UB = 300_000, 1_200_000, 128_000
# 1000-byte body, max_tokens 8192 -> out_ub 128000 (ARCHITECTURE §6.4 formula, float ceil as an independent check)
R = math.ceil(5096 * PRICE_IN / 1e6) + math.ceil(128_000 * PRICE_OUT / 1e6)


def make(path, cap=10**9):
    return Ledger(path, cap_micro_usd=cap, price_in=PRICE_IN, price_out=PRICE_OUT, out_ub_tokens=OUT_UB)


def _hammer(path, cap, barrier, attempts):  # runs in a spawned process
    ledger = make(path, cap)
    barrier.wait(timeout=30)
    for _ in range(attempts):
        call_id = uuid.uuid4().hex
        try:
            ledger.reserve(call_id, 1000, 8192)
        except LedgerRefused as e:
            if e.code != "cap":
                raise
        else:
            ledger.release(call_id)  # keeps the ledger at the cap boundary, where a race would overspend


class LedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "usage.jsonl"
        Ledger.init(self.path)

    def test_init_creates_a_private_file_once(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        make(self.path).reserve("a", 1000, 8192)
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            Ledger.init(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reserve_amount(self):
        ledger = make(self.path)
        self.assertEqual(R, 1529 + 153_600)
        self.assertEqual(ledger.reserve("a", 1000, 8192), R)
        # max_tokens above the upper bound is reserved as is
        self.assertEqual(
            ledger.reserve("b", 0, 200_000), math.ceil(4096 * PRICE_IN / 1e6) + math.ceil(200_000 * PRICE_OUT / 1e6)
        )

    def test_settle_and_release(self):
        ledger = make(self.path)
        ledger.reserve("a", 1000, 8192)
        ledger.reserve("b", 1000, 8192)
        charge = math.ceil(1001 * PRICE_IN / 1e6) + math.ceil(501 * PRICE_OUT / 1e6)
        self.assertEqual(ledger.settle("a", 1001, 501), charge)
        ledger.release("b")
        status = ledger.status()
        self.assertEqual((status["spent"], status["unsettled"], status["halted"]), (301 + 602, 0, False))
        with self.assertRaises(ValueError):  # a closed call is final
            ledger.release("a")
        with self.assertRaises(ValueError):
            ledger.settle("unknown", 1, 1)
        with self.assertRaises(ValueError):
            ledger.settle("b", 1.5, 1)
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([r["op"] for r in records], ["reserve", "reserve", "settle", "release"])
        for r in records:  # metadata only
            self.assertLessEqual(set(r), {"op", "id", "reserved", "charge", "prompt_tokens", "completion_tokens", "t"})

    def test_cap_counts_unsettled_reservations(self):
        ledger = make(self.path, cap=2 * R)
        ledger.reserve("a", 1000, 8192)
        ledger.reserve("b", 1000, 8192)
        self.assertEqual(
            ledger.status(),
            {"spent": 2 * R, "unsettled": 2 * R, "cap": 2 * R, "remaining": 0, "halted": False, "overruns": []},
        )
        with self.assertRaises(LedgerRefused) as cm:
            ledger.reserve("c", 1000, 8192)
        self.assertEqual(cm.exception.code, "cap")
        ledger.release("a")
        ledger.reserve("c", 1000, 8192)
        self.assertEqual(ledger.status()["spent"], 2 * R)

    def test_overrun_halts_until_ack(self):
        ledger = make(self.path)
        reserved = ledger.reserve("a", 1000, 8192)
        self.assertGreater(ledger.settle("a", 0, 200_000), reserved)
        status = ledger.status()
        self.assertEqual((status["halted"], status["overruns"]), (True, ["a"]))
        with self.assertRaises(LedgerRefused) as cm:
            ledger.reserve("b", 1000, 8192)
        self.assertEqual(cm.exception.code, "halted")
        with self.assertRaises(ValueError):
            ledger.ack("b")
        ledger.ack("a")
        self.assertFalse(ledger.status()["halted"])
        ledger.reserve("b", 1000, 8192)

    def test_torn_last_line_is_repaired(self):
        ledger = make(self.path)
        ledger.reserve("a", 1000, 8192)
        good = self.path.read_bytes()
        with open(self.path, "ab") as f:
            f.write(b'{"op":"reserve","id":"b","res')
        self.assertEqual(ledger.status()["spent"], R)
        self.assertEqual(self.path.read_bytes(), good)
        ledger.reserve("b", 1000, 8192)
        self.assertEqual(ledger.status()["spent"], 2 * R)

        only_torn = self.dir / "torn.jsonl"
        only_torn.write_bytes(b'{"op":"res')
        self.assertEqual(make(only_torn).status()["spent"], 0)
        self.assertEqual(only_torn.read_bytes(), b"")

    def test_corrupt_line_is_refused(self):
        self.path.write_bytes(b'{"op":"reserve","id":"a","reserved":1}\nnot json\n')
        with self.assertRaises(LedgerRefused) as cm:
            make(self.path).reserve("b", 1000, 8192)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_restart_keeps_state(self):
        first = make(self.path)
        first.reserve("a", 1000, 8192)
        first.reserve("b", 1000, 8192)
        first.settle("a", 1000, 500)
        second = make(self.path)
        self.assertEqual(second.status(), first.status())
        self.assertEqual(second.status()["spent"], 300 + 600 + R)
        second.settle("b", 1000, 500)
        with self.assertRaises(ValueError):  # ids are never reused
            second.reserve("a", 1000, 8192)

    def test_missing_file_is_refused_and_never_recreated(self):
        self.path.unlink()
        ledger = make(self.path)
        calls = [
            lambda: ledger.reserve("a", 1000, 8192),
            lambda: ledger.settle("a", 1, 1),
            lambda: ledger.release("a"),
            lambda: ledger.ack("a"),
            ledger.status,
        ]
        for call in calls:
            with self.assertRaises(LedgerRefused) as cm:
                call()
            self.assertEqual(cm.exception.code, "missing")
        self.assertFalse(self.path.exists())

    def test_concurrent_processes_never_exceed_the_cap(self):
        cap = 2 * R + R // 2  # room for two open reservations
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(8)
        procs = [
            ctx.Process(target=_hammer, args=(str(self.path), cap, barrier, 150), daemon=True) for _ in range(8)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
        self.assertEqual([p.exitcode for p in procs], [0] * 8)
        held, reserves = {}, 0
        for line in self.path.read_text().splitlines():  # replay: the cap held at every point in time
            record = json.loads(line)
            if record["op"] == "reserve":
                self.assertLessEqual(sum(held.values()) + record["reserved"], cap)
                held[record["id"]] = record["reserved"]
                reserves += 1
            else:
                del held[record["id"]]
        self.assertGreater(reserves, 8)
        self.assertEqual(make(self.path, cap).status()["spent"], 0)

    def test_cli(self):
        path = self.dir / "cli" / "usage.jsonl"

        def run(*args):
            return subprocess.run(
                [sys.executable, "-m", "agency_loop.ledger", *args],
                cwd=ROOT, env={"AGENCY_LEDGER_PATH": str(path)}, capture_output=True, text=True, timeout=60,
            )

        self.assertEqual(run("status").returncode, 1)  # missing
        self.assertEqual(run("init").returncode, 0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(run("init").returncode, 1)
        status = run("status")
        self.assertEqual(status.returncode, 0)
        self.assertEqual(
            json.loads(status.stdout),
            {"spent": 0, "unsettled": 0, "cap": 12_000_000, "remaining": 12_000_000, "halted": False, "overruns": []},
        )
        self.assertEqual(run("ack", "nope").returncode, 1)
        self.assertEqual(run().returncode, 2)

        ledger = Ledger(path, cap_micro_usd=12_000_000, price_in=PRICE_IN, price_out=PRICE_OUT, out_ub_tokens=OUT_UB)
        ledger.reserve("x", 0, 0)
        ledger.settle("x", 0, 10**6)
        self.assertEqual(run("ack", "x").returncode, 0)
        self.assertFalse(json.loads(run("status").stdout)["halted"])


if __name__ == "__main__":
    unittest.main()
