"""HTTP API for the robot bridge on the TA laptop (outbound long-polling, bearer token).

  GET  /bridge/next?last=<cmd_id>&channel=main|priority   blocks up to 25 s, returns one command or 204
  POST /bridge/result     {cmd_id, status: "done"|"failed", error, battery, warnings, data}
  POST /bridge/heartbeat  {battery, warnings, driver_ok}
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
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


def make_router(link: RobotLink, token: str, poll_timeout: float = 25.0) -> APIRouter:
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

    @router.post("/heartbeat", dependencies=[Depends(check)])
    async def heartbeat(body: HeartbeatIn):
        await link.heartbeat(body.model_dump())
        return {"ok": True, "mode": link.mode}

    return router
