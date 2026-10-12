#!/usr/bin/env python3
"""Load test: N simulated phones vote while some of them disconnect and reconnect.

Checks (section 16 of the plan):
  - every phone's final vote is counted exactly once per round (compared with the server's export);
  - a reconnecting phone gets its own vote back;
  - latency to the board: server state -> board receipt, and last vote -> board shows the total.

Run against a local server (the board latency uses the server clock, so it is exact
on one machine and only indicative across machines):

  server/.venv/bin/python tools/loadtest.py --url http://127.0.0.1:8000 --admin-token ... --clients 150

It starts a new game (variant 2) and changes the vote time and thinking time, so do not
run it against a lecture in progress.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
import uuid

import socketio


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(p / 100 * (len(values) - 1))))
    return values[k]


def summary(name: str, values: list[float]) -> str:
    if not values:
        return f"{name}: no samples"
    return (f"{name}: n={len(values)} p50={statistics.median(values):.0f} ms "
            f"p95={pct(values, 95):.0f} ms max={max(values):.0f} ms")


class Board:
    def __init__(self, url: str) -> None:
        self.url = url
        self.sio = socketio.AsyncClient(reconnection=True)
        self.state: dict | None = None
        self.latency: list[float] = []
        self.sio.on("state", self.on_state)

    def on_state(self, s: dict) -> None:
        self.latency.append(time.time() * 1000 - s["server_time"])
        self.state = s

    async def start(self) -> None:
        await self.sio.connect(self.url, auth={"role": "board"}, transports=["websocket"])
        await self.sio.call("hello", {"device_id": "loadtest-board"})

    async def wait(self, cond, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while not (self.state and cond(self.state)):
            if time.monotonic() > deadline:
                raise TimeoutError(f"board wait timed out (phase {self.state and self.state['phase']})")
            await asyncio.sleep(0.01)
        return self.state


class Phone:
    def __init__(self, url: str, polling: bool) -> None:
        self.url = url
        self.device = str(uuid.uuid4())
        self.transports = ["polling"] if polling else ["websocket"]
        self.sio: socketio.AsyncClient | None = None

    async def connect(self) -> dict:
        self.sio = socketio.AsyncClient(reconnection=False)
        await self.sio.connect(self.url, auth={"role": "phone"}, transports=self.transports, wait_timeout=20)
        return await self.sio.call("hello", {"device_id": self.device}, timeout=20)

    async def disconnect(self, abrupt: bool = False) -> None:
        """abrupt=True drops the connection without a goodbye, like a phone that goes to sleep.
        (The Python client also needs this for long-polling: a clean disconnect waits for the open poll.)"""
        if not self.sio or not self.sio.connected:
            return
        if abrupt or self.transports == ["polling"]:
            await self.sio.eio.disconnect(abort=True)
        else:
            await self.sio.disconnect()

    async def vote(self, round_id: int, choice: str) -> tuple[dict, float]:
        t0 = time.perf_counter()
        res = await self.sio.call("vote", {"round_id": round_id, "choice": choice}, timeout=20)
        return res, (time.perf_counter() - t0) * 1000


async def run(args: argparse.Namespace) -> int:
    failures: list[str] = []
    admin = socketio.AsyncClient()
    await admin.connect(args.url, auth={"role": "admin", "token": args.admin_token}, transports=["websocket"])
    await admin.call("hello", {"device_id": "loadtest-admin"})

    async def adm(cmd: str, **kw):
        res = await admin.call(f"admin:{cmd}", kw, timeout=20)
        if not res.get("ok"):
            raise RuntimeError(f"admin {cmd} failed: {res}")
        return res.get("result")

    board = Board(args.url)
    await board.start()
    phones = [Phone(args.url, polling=i < args.clients * args.polling_share) for i in range(args.clients)]
    t0 = time.perf_counter()
    await asyncio.gather(*(p.connect() for p in phones))
    print(f"{len(phones)} phones connected in {time.perf_counter() - t0:.1f} s "
          f"({sum(p.transports == ['polling'] for p in phones)} on long-polling)")

    await adm("mode", mode="sim")
    await adm("settings", vote_seconds=600, think_min_s=0, think_max_s=1, sim_move_s=0.2, tie_break="first")
    rounds_done = 0
    ack_ms: list[float] = []
    to_board_ms: list[float] = []
    expected_counts: dict[int, dict[str, int]] = {}

    while rounds_done < args.rounds:
        old_game = board.state["game"]["id"] if board.state else None
        await adm("new_game", variant="2", class_side="MAX")
        await adm("start")
        while rounds_done < args.rounds:
            st = await board.wait(lambda s: s["game"]["id"] != old_game and s["phase"] in ("VOTING", "GAME_OVER") and
                                  (s["phase"] == "GAME_OVER" or (s["round"] and not s["round"]["closed"])))
            if st["phase"] == "GAME_OVER":
                break
            rnd = st["round"]
            rid, options = rnd["id"], rnd["options"]
            final: dict[str, str] = {}

            async def phone_round(p: Phone) -> None:
                await asyncio.sleep(random.uniform(0, args.spread))
                choice = random.choice(options)
                res, ms = await p.vote(rid, choice)
                ack_ms.append(ms)
                if not res.get("ok"):
                    failures.append(f"vote rejected: {res}")
                    return
                final[p.device] = choice
                if random.random() < args.churn:
                    await p.disconnect(abrupt=random.random() < 0.5)
                    await asyncio.sleep(random.uniform(0.2, 1.5))
                    hello = await p.connect()
                    if hello.get("round_id") != rid or hello.get("choice") != final[p.device]:
                        failures.append(f"reconnect lost the vote: {hello} expected {final[p.device]}")
                    if random.random() < 0.5:
                        choice = random.choice(options)
                        res, ms = await p.vote(rid, choice)
                        ack_ms.append(ms)
                        if res.get("ok"):
                            final[p.device] = choice
                        else:
                            failures.append(f"re-vote rejected: {res}")

            await asyncio.gather(*(phone_round(p) for p in phones))
            last_ack = time.perf_counter()
            try:
                await board.wait(lambda s: s["round"] and s["round"]["id"] == rid and s["round"]["total"] == len(final),
                                 timeout=10)
                to_board_ms.append((time.perf_counter() - last_ack) * 1000)
            except TimeoutError:
                failures.append(f"board never showed {len(final)} votes (saw {board.state['round']['total']})")
            counts = {o: 0 for o in options}
            for c in final.values():
                counts[c] += 1
            expected_counts[rid] = counts
            await adm("close")
            rounds_done += 1
            print(f"round {rounds_done}: {len(final)} votes {counts}")
            await board.wait(lambda s: not (s["round"] and s["round"]["id"] == rid) or s["phase"] == "GAME_OVER")

    export = await adm("export")
    by_id = {r["id"]: r for r in export["rounds"]}
    for rid, counts in expected_counts.items():
        got = by_id.get(rid, {}).get("counts")
        if got != counts:
            failures.append(f"round {rid}: server counted {got}, phones cast {counts}")
    print(summary("vote ack (phone -> server -> phone)", ack_ms))
    print(summary("state broadcast (server -> board)", board.latency))
    print(summary("last vote -> board shows total", to_board_ms))
    if board.latency and pct(board.latency, 95) > 500:
        failures.append("board latency p95 above 500 ms")
    for p in phones:
        await p.disconnect()
    await board.sio.disconnect()
    await admin.disconnect()
    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for f in failures[:20]:
            print("  -", f)
        return 1
    print("\nOK: every vote counted exactly once")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--admin-token", required=True)
    p.add_argument("--clients", type=int, default=150)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--churn", type=float, default=0.2, help="share of phones that disconnect and reconnect per round")
    p.add_argument("--polling-share", type=float, default=0.25, help="share of phones on HTTP long-polling")
    p.add_argument("--spread", type=float, default=3.0, help="votes are spread over this many seconds")
    sys.exit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
