from __future__ import annotations

import uvicorn

from .app import create_app


app = create_app()


def run() -> None:
    uvicorn.run(
        "endless_task.api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
