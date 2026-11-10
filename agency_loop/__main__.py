"""Run the web service: `.venv/bin/python -m agency_loop`."""

import os

import uvicorn

from agency_loop.config import Settings

if __name__ == "__main__":
    # The Anthropic SDK merges this variable's headers into every Claude request; the app sends only its own headers.
    os.environ.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    settings = Settings.from_env()
    # access_log off: request lines are not needed and logs must never hold session content.
    uvicorn.run(
        "agency_loop.web.app:create_app", factory=True, host=settings.host, port=settings.port,
        workers=1, access_log=False,
    )
