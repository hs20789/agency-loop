"""In-memory workspaces: one per cookie, sliding TTL, server-wide count cap, per-IP upload rate (ARCHITECTURE §7.1)."""

from __future__ import annotations

import secrets
import time
from collections import deque

from agency_loop.config import Settings
from agency_loop.model import Candidate, Evidence, Session, TestRun, Workspace
from agency_loop.web.jobs import JobRunner

RATE_WINDOW_SEC = 600


class WorkspaceStore:
    def __init__(self, settings: Settings, jobs: JobRunner) -> None:
        self.settings = settings
        self.jobs = jobs
        self.clock = time.monotonic  # tests replace this to move time
        self._items: dict[str, Workspace] = {}  # cookie token -> workspace
        self._uploads: dict[str, deque[float]] = {}  # client IP -> upload attempt times

    def __len__(self) -> int:
        return len(self._items)

    def get(self, token: str | None) -> Workspace | None:
        """The live workspace of a cookie value; refreshes its TTL. An expired one is removed on access."""
        ws = self._items.get(token)
        if ws is None:
            return None
        now = self.clock()
        if now - ws.last_used > self.settings.workspace_ttl_sec:
            self.remove(token)
            return None
        ws.last_used = now
        return ws

    def add(
        self, replace: str | None, session: Session, evidence: dict[str, Evidence], test_runs: list[TestRun],
        candidates: list[Candidate],
    ) -> Workspace | None:
        """A new workspace that replaces the caller's previous one; None (old one kept) when the server is full."""
        self.sweep()
        if len(self._items) - (replace in self._items) >= self.settings.max_workspaces:
            return None
        self.remove(replace)
        now = self.clock()
        ws = Workspace(
            id=secrets.token_urlsafe(32), created_at=now, last_used=now, session=session, evidence=evidence,
            test_runs=test_runs, candidates=candidates,
        )
        self._items[ws.id] = ws
        return ws

    def remove(self, token: str | None) -> None:
        ws = self._items.pop(token, None)
        if ws is not None:
            self.jobs.cancel(ws)

    def sweep(self) -> None:
        """Drops expired workspaces and stale rate-limit entries."""
        now = self.clock()
        ttl = self.settings.workspace_ttl_sec
        for token in [t for t, ws in self._items.items() if now - ws.last_used > ttl]:
            self.remove(token)
        self._uploads = {ip: times for ip, times in self._uploads.items() if times and times[-1] > now - RATE_WINDOW_SEC}

    def allow_upload(self, ip: str) -> bool:
        """Counts every upload attempt (accepted or not) per client IP in a sliding 10-minute window."""
        now = self.clock()
        times = self._uploads.setdefault(ip, deque())
        while times and times[0] <= now - RATE_WINDOW_SEC:
            times.popleft()
        if len(times) >= self.settings.upload_rate_per_10min:
            return False
        times.append(now)
        return True
