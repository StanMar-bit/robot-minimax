"""HTTP API for the robot bridge on the TA laptop (outbound long-polling, bearer token).

  GET  /bridge/next?last=<cmd_id>&channel=main|priority   blocks up to 25 s, returns one command or 204
  POST /bridge/result     {cmd_id, status: "done"|"failed", error, battery, warnings, data}
  POST /bridge/heartbeat  {battery, warnings, driver_ok}
  POST /bridge/audio?cmd_id=<cmd_id>   raw audio of a say command (audio/mpeg or audio/wav), for the hall
"""

from __future__ import annotations

import hmac
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .robot_link import CHANNELS, RobotLink


class ResultIn(BaseModel):
    cmd_id: int
    status: str = Field(pattern="^(done|failed)$")
    error: str | None = None
    battery: int | None = None
    warnings: list[str] | None = None
    data: dict[str, Any] | None = None


class HeartbeatIn(BaseModel):
    battery: int | None = None
    warnings: list[str] | None = None
    driver_ok: bool | None = None


MAX_AUDIO_BYTES = 4 * 1024 * 1024
AUDIO_TYPES = ("audio/mpeg", "audio/wav")


def make_router(link: RobotLink, token: str, poll_timeout: float = 25.0,
                on_audio: Callable[[int, bytes, str], None] | None = None) -> APIRouter:
    router = APIRouter(prefix="/bridge", tags=["bridge"])

    def check(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {token}"
        if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="bad bridge token")

    @router.get("/next", dependencies=[Depends(check)])
    async def next_command(last: int = 0, channel: str = Query("main"),
                           timeout: float = Query(poll_timeout, ge=0, le=30)):
        if channel not in CHANNELS:
            raise HTTPException(status_code=400, detail="unknown channel")
        cmd = await link.next_command(last, channel, timeout)
        if cmd is None:
            return Response(status_code=204)
        return cmd

    @router.post("/result", dependencies=[Depends(check)])
    async def result(body: ResultIn):
        await link.post_result(body.model_dump())
        return {"ok": True}

    @router.post("/audio", dependencies=[Depends(check)])
    async def audio(request: Request, cmd_id: int = Query(...)):
        content_type = request.headers.get("content-type", "")
        if content_type not in AUDIO_TYPES:
            raise HTTPException(status_code=415, detail=f"audio must be one of {AUDIO_TYPES}")
        body = await request.body()
        if not body or len(body) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="audio missing or too large")
        if on_audio:
            on_audio(cmd_id, body, content_type)
        return {"ok": True}

    @router.post("/heartbeat", dependencies=[Depends(check)])
    async def heartbeat(body: HeartbeatIn):
        await link.heartbeat(body.model_dump())
        return {"ok": True, "mode": link.mode}

    return router
