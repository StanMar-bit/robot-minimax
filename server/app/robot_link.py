"""Server side of the robot bridge: a command queue that the bridge long-polls.

Two channels:
  main      commands that run one after another (move, say, eyes, ...).
            A command stays queued until the bridge posts its result, so a lost
            HTTP response just means the command is delivered again.
  priority  only `stop`. Polled by a separate bridge thread, so it can
            interrupt a walk that is in progress.

Command ids are based on the wall clock in milliseconds and always increase, so
they keep increasing after a server restart. The bridge remembers the last id
it started and never runs the same id twice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

log = logging.getLogger("robot")

OFFLINE_AFTER_S = 35.0
BUSY_GRACE_S = 120.0
CHANNELS = ("main", "priority")


class RobotLink:
    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self.mode = "sim"  # 'robot' | 'sim'
        self.queues: dict[str, list[dict[str, Any]]] = {c: [] for c in CHANNELS}
        self.delivered_upto: dict[str, int] = {c: 0 for c in CHANNELS}
        self.waiters: dict[int, asyncio.Future] = {}
        self._cond = asyncio.Condition()
        self._last_id = 0
        self.last_seen = 0.0
        self.busy: dict[str, Any] | None = None  # command currently executing
        self.battery: int | None = None
        self.warnings: list[str] = []
        self.driver_ok: bool | None = None
        self.last_command: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.on_change = on_change or (lambda: None)

    # ids and status --------------------------------------------------------

    def _new_id(self) -> int:
        self._last_id = max(self._last_id + 1, int(time.time() * 1000))
        return self._last_id

    @property
    def online(self) -> bool:
        now = time.time()
        if now - self.last_seen < OFFLINE_AFTER_S:
            return True
        return bool(self.busy and now - self.busy["delivered_at"] < BUSY_GRACE_S)

    def touch(self) -> None:
        was = self.online
        self.last_seen = time.time()
        if not was:
            log.info("bridge online")
            self.on_change()

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "online": self.online,
            "last_seen": self.last_seen or None,
            "battery": self.battery,
            "warnings": self.warnings,
            "driver_ok": self.driver_ok,
            "busy": {k: self.busy[k] for k in ("cmd_id", "cmd")} if self.busy else None,
            "queued": len(self.queues["main"]),
            "last_command": self.last_command,
            "last_result": self.last_result,
        }

    # sending ---------------------------------------------------------------

    async def send(self, cmd: str, channel: str = "main", **args: Any) -> int:
        cmd_id = self._new_id()
        item = {"cmd_id": cmd_id, "cmd": cmd, "args": args, "queued_at": time.time()}
        self.waiters[cmd_id] = asyncio.get_running_loop().create_future()
        async with self._cond:
            self.queues[channel].append(item)
            self._cond.notify_all()
        log.info("queued %s %s %s", cmd_id, cmd, args)
        self.on_change()
        return cmd_id

    async def wait(self, cmd_id: int, timeout: float | None = None) -> dict[str, Any]:
        fut = self.waiters.get(cmd_id)
        if fut is None:
            return {"cmd_id": cmd_id, "status": "failed", "error": "unknown command"}
        return await asyncio.wait_for(asyncio.shield(fut), timeout)

    def resolve(self, cmd_id: int, result: dict[str, Any]) -> None:
        fut = self.waiters.pop(cmd_id, None)
        if fut and not fut.done():
            fut.set_result(result)

    async def stop(self) -> int:
        """Drop every queued command except the one running, then send stop on the priority channel."""
        await self.cancel_pending("stopped by admin")
        return await self.send("stop", channel="priority")

    async def cancel_pending(self, reason: str) -> None:
        """Cancel queued main-channel commands. The command the bridge is executing is kept."""
        busy_id = self.busy["cmd_id"] if self.busy else None
        async with self._cond:
            keep = []
            for item in self.queues["main"]:
                if item["cmd_id"] == busy_id:
                    keep.append(item)
                else:
                    self.resolve(item["cmd_id"], {"cmd_id": item["cmd_id"], "status": "failed", "error": reason})
            self.queues["main"] = keep
        self.on_change()

    # bridge side -----------------------------------------------------------

    async def next_command(self, last: int, channel: str = "main", timeout: float = 25.0) -> dict[str, Any] | None:
        """Long-poll: return the first queued command with an id above `last`, or None after `timeout`."""
        if channel not in CHANNELS:
            raise ValueError(channel)
        self.touch()
        deadline = time.monotonic() + timeout
        async with self._cond:
            while True:
                for item in self.queues[channel]:
                    if item["cmd_id"] > last:
                        self.delivered_upto[channel] = max(self.delivered_upto[channel], item["cmd_id"])
                        if channel == "main":
                            self.busy = {**item, "delivered_at": time.time()}
                        self.last_command = {"cmd_id": item["cmd_id"], "cmd": item["cmd"], "args": item["args"],
                                             "at": time.time()}
                        self.on_change()
                        return {"cmd_id": item["cmd_id"], "cmd": item["cmd"], "args": item["args"]}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.touch()
                    return None
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except asyncio.TimeoutError:
                    pass

    async def post_result(self, payload: dict[str, Any]) -> None:
        self.touch()
        cmd_id = int(payload["cmd_id"])
        self._update_health(payload)
        result = {
            "cmd_id": cmd_id,
            "status": payload.get("status", "failed"),
            "error": payload.get("error"),
            "data": payload.get("data"),
        }
        async with self._cond:
            for channel in CHANNELS:
                q = self.queues[channel]
                if not any(i["cmd_id"] == cmd_id for i in q):
                    continue
                # The main channel runs in order: a result for cmd_id settles everything before it.
                done = [i for i in q if i["cmd_id"] <= cmd_id] if channel == "main" else [i for i in q if i["cmd_id"] == cmd_id]
                self.queues[channel] = [i for i in q if i not in done]
                for i in done:
                    if i["cmd_id"] != cmd_id:
                        self.resolve(i["cmd_id"], {"cmd_id": i["cmd_id"], "status": "failed", "error": "skipped by bridge"})
        if self.busy and self.busy["cmd_id"] <= cmd_id:
            self.busy = None
        self.last_result = {**result, "at": time.time()}
        if result["status"] != "done":
            log.warning("command %s failed: %s", cmd_id, result["error"])
        self.resolve(cmd_id, result)
        self.on_change()

    async def heartbeat(self, payload: dict[str, Any]) -> None:
        self.touch()
        self._update_health(payload)
        self.on_change()

    def _update_health(self, payload: dict[str, Any]) -> None:
        if payload.get("battery") is not None:
            self.battery = int(payload["battery"])
        if payload.get("warnings") is not None:
            self.warnings = list(payload["warnings"])
        if payload.get("driver_ok") is not None:
            self.driver_ok = bool(payload["driver_ok"])
