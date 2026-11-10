"""Mistral (DeepSeek) usage ledger and its operator CLI (docs/ARCHITECTURE.md §6.4).

Append-only JSONL of call metadata only: op, id, micro-USD amounts, token counts, unix time.
Unsettled reservations count as spent, so a crash, timeout or 5xx can only over-count, never under-count.

CLI: python -m agency_loop.ledger init|status|ack <call_id>
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from agency_loop.config import Settings

_LOCK = threading.Lock()  # process-wide, taken before the file lock


class LedgerRefused(Exception):
    """`code` is "cap" | "halted" | "missing" | "corrupt"."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _micro_usd(tokens: int, price_per_mtok: int) -> int:
    return -(-tokens * price_per_mtok // 1_000_000)  # integer ceil


class _State:
    """Totals recomputed from every record of the file."""

    def __init__(self, data: bytes) -> None:
        self.reserved: dict[str, int] = {}
        self.closed: dict[str, int] = {}  # settled or released id -> final charge
        acked: set[str] = set()
        try:
            for line in data.splitlines():
                record = json.loads(line)
                op, call_id = record["op"], record["id"]
                if op == "reserve":
                    self.reserved[call_id] = record["reserved"]
                elif op == "settle":
                    self.closed[call_id] = record["charge"]
                elif op == "release":
                    self.closed[call_id] = 0
                elif op == "ack":
                    acked.add(call_id)
                else:
                    raise ValueError(op)
            self.spent = sum(self.closed.get(i, r) for i, r in self.reserved.items())
            self.unsettled = sum(r for i, r in self.reserved.items() if i not in self.closed)
            self.overruns = [i for i, c in self.closed.items() if c > self.reserved.get(i, 0) and i not in acked]
        except (ValueError, KeyError, TypeError):
            raise LedgerRefused("corrupt") from None

    def require_open(self, call_id: str) -> None:
        if call_id not in self.reserved or call_id in self.closed:
            raise ValueError("unknown or already closed call_id")


class Ledger:
    def __init__(self, path, *, cap_micro_usd: int, price_in: int, price_out: int, out_ub_tokens: int) -> None:
        self.path = os.fspath(path)
        self.cap = cap_micro_usd
        self.price_in = price_in
        self.price_out = price_out
        self.out_ub_tokens = out_ub_tokens

    @staticmethod
    def init(path) -> None:
        """Create an empty ledger. Raises FileExistsError: spend history is never reset."""
        os.makedirs(os.path.dirname(os.fspath(path)) or ".", exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))

    @contextmanager
    def _open(self) -> Iterator[tuple[int, _State]]:
        with _LOCK:
            try:
                # Never O_CREAT: a lost ledger must refuse, not silently restart from zero spend.
                fd = os.open(self.path, os.O_RDWR | os.O_APPEND)
            except FileNotFoundError:
                raise LedgerRefused("missing") from None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                data = os.pread(fd, os.fstat(fd).st_size, 0)
                end = data.rfind(b"\n") + 1
                if end < len(data):  # torn last line from a crash mid-write
                    os.ftruncate(fd, end)
                    os.fsync(fd)
                yield fd, _State(data[:end])
            finally:
                os.close(fd)  # also releases the flock

    @staticmethod
    def _append(fd: int, record: dict) -> None:
        line = json.dumps({**record, "t": int(time.time())}, separators=(",", ":")).encode() + b"\n"
        if os.write(fd, line) != len(line):
            raise OSError("ledger short write")  # the torn line is cut by the next operation
        os.fsync(fd)

    def reserve(self, call_id: str, request_body_bytes: int, max_tokens: int) -> int:
        """Hold the worst-case cost before sending; refuses over the cap or while an overrun is unacknowledged."""
        amount = _micro_usd(request_body_bytes + 4096, self.price_in) + _micro_usd(
            max(max_tokens, self.out_ub_tokens), self.price_out
        )
        with self._open() as (fd, state):
            if call_id in state.reserved:
                raise ValueError("duplicate call_id")
            if state.overruns:
                raise LedgerRefused("halted")
            if state.spent + amount > self.cap:
                raise LedgerRefused("cap")
            self._append(fd, {"op": "reserve", "id": call_id, "reserved": amount})
        return amount

    def settle(self, call_id: str, prompt_tokens: int, completion_tokens: int) -> int:
        """Record the real charge. A charge above the reservation is an overrun and halts reserves until ack."""
        if not all(type(n) is int and n >= 0 for n in (prompt_tokens, completion_tokens)):
            raise ValueError("token counts must be non-negative integers")
        charge = _micro_usd(prompt_tokens, self.price_in) + _micro_usd(completion_tokens, self.price_out)
        with self._open() as (fd, state):
            state.require_open(call_id)
            self._append(
                fd,
                {
                    "op": "settle", "id": call_id, "charge": charge,
                    "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                },
            )
        return charge

    def release(self, call_id: str) -> None:
        """The request was definitely not sent, or was rejected with 4xx: charge 0."""
        with self._open() as (fd, state):
            state.require_open(call_id)
            self._append(fd, {"op": "release", "id": call_id})

    def ack(self, call_id: str) -> None:
        """Operator acknowledges an overrun so reserves can resume."""
        with self._open() as (fd, state):
            if call_id not in state.overruns:
                raise ValueError("no unacknowledged overrun for this call_id")
            self._append(fd, {"op": "ack", "id": call_id})

    def status(self) -> dict:
        with self._open() as (_, state):
            return {
                "spent": state.spent,
                "unsettled": state.unsettled,
                "cap": self.cap,
                "remaining": max(self.cap - state.spent, 0),
                "halted": bool(state.overruns),
                "overruns": state.overruns,
            }


def main(argv: list[str]) -> int:
    settings = Settings.from_env()
    ledger = Ledger(
        settings.ledger_path,
        cap_micro_usd=settings.mistral_cap_micro_usd,
        price_in=settings.mistral_price_in,
        price_out=settings.mistral_price_out,
        out_ub_tokens=settings.mistral_out_ub_tokens,
    )
    try:
        if argv == ["init"]:
            Ledger.init(settings.ledger_path)
            print(f"사용량 장부를 만들었습니다: {settings.ledger_path}")
        elif argv == ["status"]:
            print(json.dumps(ledger.status()))
        elif len(argv) == 2 and argv[0] == "ack":
            ledger.ack(argv[1])
            print(f"초과 사용을 확인했습니다. 다시 호출할 수 있습니다: {argv[1]}")
        else:
            print("사용법: python -m agency_loop.ledger init|status|ack <call_id>", file=sys.stderr)
            return 2
    except FileExistsError:
        print("장부가 이미 있습니다. 기존 사용 기록은 지우지 않습니다.", file=sys.stderr)
        return 1
    except LedgerRefused as e:
        print(f"장부를 사용할 수 없습니다({e.code}). missing이면 먼저 init을 실행하세요.", file=sys.stderr)
        return 1
    except ValueError:  # only ack raises it here
        print(f"확인을 기다리는 초과 사용이 아닙니다: {argv[1]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
