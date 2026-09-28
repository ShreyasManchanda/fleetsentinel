"""`python -m fs_api`: run the API. Uses a Selector event loop so psycopg async works on Windows too."""
from __future__ import annotations

import asyncio
import os

import uvicorn

from .main import create


def run() -> None:
    config = uvicorn.Config(create(), host=os.environ.get("FS_API_HOST", "0.0.0.0"), port=int(os.environ.get("FS_API_PORT", "8000")),  # noqa: S104
                            log_level="info", loop="none", proxy_headers=True)
    asyncio.run(uvicorn.Server(config).serve(), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    run()
