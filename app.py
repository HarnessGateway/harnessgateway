"""The endpoint: the OpenAI Responses API served by FastAPI, the way the platform serves it.

    uvicorn app:app --port 8000

Then any OpenAI SDK with base_url=http://127.0.0.1:8000/v1 works unchanged. The body is validated
with the SDK's `ResponseCreateParams` (in wire.parse_request), the reply is the SDK's `Response`,
the stream is its event classes; nothing of the schema is written here. HG_DATA_DIR keeps the
sessions between runs; without it a temporary directory is used.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import wire


def error(status: int, code: str, message: str, param: str | None = None) -> JSONResponse:
    return JSONResponse({"error": {"type": "invalid_request_error", "code": code, "message": message, "param": param}}, status_code=status)


def create_app(platform: wire.Platform | None = None) -> FastAPI:
    app = FastAPI(title="HarnessGateway", version="0.1.0", docs_url="/reference", redoc_url=None)
    app.state.platform = platform or wire.Platform(Path(os.environ.get("HG_DATA_DIR") or tempfile.mkdtemp(prefix="harnessgateway-")))

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "harnesses": [app.state.platform.manifest["description"].split(":")[0]]}

    @app.post("/v1/responses")
    async def create_response(request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError:
            return error(400, "invalid_value", "the body is not JSON")
        if not isinstance(body, dict):
            return error(400, "invalid_value", "the body is a JSON object")
        try:
            turn, relay = app.state.platform.create(body)
        except wire.Refused as e:
            return JSONResponse(e.body, status_code=404 if e.body["error"]["code"] == "not_found" else 400)
        if body.get("stream"):
            def sse():                               # a sync generator: Starlette iterates it in a thread
                for ev in wire.events(turn, relay):
                    yield f"event: {ev.type}\ndata: {ev.model_dump_json()}\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
        await asyncio.to_thread(lambda: any(False for _ in relay))   # run the turn to its end, off the loop
        return JSONResponse(wire.response_object(turn).model_dump(mode="json"))

    @app.get("/v1/responses/{rid}")
    def get_response(rid: str) -> Any:
        found = app.state.platform.turns.get(rid)
        if not found:
            return error(404, "not_found", f"no response {rid}", "id")
        return JSONResponse(wire.response_object(found[1]).model_dump(mode="json"))

    return app


app = create_app()
