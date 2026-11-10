"""Background jobs: one asyncio task per job, status kept on the workspace's Job (ARCHITECTURE §5.9).

`work(job)` reports progress with `update(job, status, progress)` (fits the providers' `on_status`), may set
`job.model`, and returns the result dict. A ProviderError becomes job.error; earlier results stay where the
work stored them.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from agency_loop.model import Job, Workspace
from agency_loop.providers.base import ProviderError
from agency_loop.web import views

log = logging.getLogger(__name__)

Work = Callable[[Job], Awaitable[dict[str, Any]]]


def update(job: Job, status: str, progress: str | None = None) -> None:
    job.status = status
    if progress is not None:
        job.progress = progress
    job.updated_at = time.time()


class JobRunner:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}  # job id -> running task, for cancellation on workspace removal

    def start(self, ws: Workspace, kind: str, work: Work) -> Job:
        """Registers a queued job on `ws` and runs `work(job)` as a task on the running event loop."""
        now = time.time()
        job = Job(id=secrets.token_urlsafe(16), kind=kind, created_at=now, updated_at=now)
        ws.jobs[job.id] = job
        task = asyncio.create_task(self._run(job, work))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(job.id, None))
        return job

    def cancel(self, ws: Workspace) -> None:
        """Stops every running job of a removed workspace, so nothing keeps calling providers for it."""
        for job_id in ws.jobs:
            task = self._tasks.pop(job_id, None)
            if task is not None:
                task.cancel()

    async def _run(self, job: Job, work: Work) -> None:
        update(job, "running")
        try:
            job.result = await work(job)
        except ProviderError as exc:
            job.error = exc.to_dict()
            update(job, "failed")
        except Exception as exc:
            # Type name only: exception text can carry session content.
            log.error("job %s failed: %s", job.kind, type(exc).__name__)
            job.error = views.error_dict("internal")
            update(job, "failed")
        else:
            update(job, "done")
