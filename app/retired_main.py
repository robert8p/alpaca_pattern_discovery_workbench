from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse


RETIREMENT_VERSION = "canonical-archive-20260906"

app = FastAPI(
    title="Alpaca Pattern Discovery Workbench (retired)",
    version=RETIREMENT_VERSION,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "retired",
        "version": RETIREMENT_VERSION,
        "database": "disabled",
        "archive": "canonical",
    }


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def retired(path: str) -> JSONResponse:
    return JSONResponse(
        status_code=410,
        content={
            "status": "retired",
            "detail": (
                "The Pattern Discovery Workbench runtime is retired. Its immutable "
                "research artefacts are preserved in the canonical archive."
            ),
            "database": "disabled",
            "version": RETIREMENT_VERSION,
        },
    )
