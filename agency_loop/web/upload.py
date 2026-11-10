"""Raw request body reading with a byte cap, in memory only (no temp files) (ARCHITECTURE §7.2)."""

from __future__ import annotations

from starlette.requests import Request


class TooLarge(Exception):
    """The declared or streamed body size passed the cap."""


async def read_body(request: Request, limit: int) -> bytes:
    """The whole body; raises TooLarge on a Content-Length over `limit` before reading, or as soon as the stream
    passes it (chunked uploads have no Content-Length)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise TooLarge
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise TooLarge
        chunks.append(chunk)
    return b"".join(chunks)
