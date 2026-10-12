#!/usr/bin/env python3
"""Stand-in for nao_driver.py, to test the whole chain without a robot (Python 3).

Same protocol: commands on tcp://127.0.0.1:5555, stop/status on tcp://127.0.0.1:5556.
Actions take (scaled) time and a running walk can be stopped.

  python3 bridge/fake_driver.py --speed 4          # 4x faster than a real NAO
  python3 bridge/fake_driver.py --fail-move 2      # the 2nd walk fails, to test recovery
"""

from __future__ import annotations

import argparse
import json
import logging
import time

import zmq

NEIGHBOURS = {"A": "BD", "B": "ACE", "C": "BF", "D": "AEG", "E": "BDFH", "F": "CEI", "G": "DH", "H": "EGI", "I": "FH"}
log = logging.getLogger("fake_driver")


class FakeDriver:
    def __init__(self, port: int, control_port: int, speed: float, fail_moves: set[int], journal: str | None):
        self.ctx = zmq.Context()
        self.main = self.ctx.socket(zmq.REP)
        self.main.bind(f"tcp://127.0.0.1:{port}")
        self.control = self.ctx.socket(zmq.REP)
        self.control.bind(f"tcp://127.0.0.1:{control_port}")
        self.speed = speed
        self.fail_moves = fail_moves
        self.journal = journal
        self.cell = "A"
        self.battery = 90
        self.moves = 0
        self.busy: str | None = None

    def status(self) -> dict:
        warnings = [f"battery low ({self.battery}%)"] if self.battery < 30 else []
        return {"battery": self.battery, "warnings": warnings, "cell": self.cell}

    def reply(self, ok: bool, error: str | None = None, data: dict | None = None) -> dict:
        st = self.status()
        return {"ok": ok, "error": error, "data": data, "battery": st["battery"], "warnings": st["warnings"]}

    def check_control(self) -> bool:
        stop = False
        while self.control.poll(0):
            msg = json.loads(self.control.recv())
            if msg.get("cmd") == "stop":
                stop = True
                log.warning("STOP during %s", self.busy)
                self.control.send_json({"ok": True, "busy": self.busy})
            else:
                self.control.send_json({"ok": True, "data": {**self.status(), "busy": self.busy},
                                        "battery": self.battery, "warnings": self.status()["warnings"]})
        return stop

    def act(self, seconds: float) -> bool:
        """Spend `seconds` (scaled) on an action; False if it was stopped."""
        end = time.time() + seconds / self.speed
        while time.time() < end:
            if self.check_control():
                return False
            time.sleep(0.02)
        return True

    def record(self, msg: dict, outcome: str) -> None:
        if self.journal:
            with open(self.journal, "a") as f:
                f.write(json.dumps({"t": time.time(), **msg, "outcome": outcome}) + "\n")

    def execute(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        self.busy = cmd
        try:
            if cmd == "move":
                a, b = msg.get("from"), msg.get("to")
                if a not in NEIGHBOURS or b not in NEIGHBOURS.get(a, ""):
                    self.record(msg, "rejected")
                    return self.reply(False, f"{a}->{b} is not a move to an adjacent cell")
                self.moves += 1
                if not self.act(3.0):
                    self.record(msg, "stopped")
                    return self.reply(False, "stopped")
                if self.moves in self.fail_moves:
                    self.record(msg, "failed")
                    return self.reply(False, "robot is not standing after the walk (simulated fall)")
                self.cell = b
                self.battery = max(5, self.battery - 2)
                self.record(msg, "done")
                return self.reply(True, data={"cell": self.cell})
            if cmd == "say":
                ok = self.act(0.5 + 0.04 * len(msg.get("text", "")))
            elif cmd in ("animate", "posture"):
                ok = self.act(2.0)
            elif cmd == "nudge":
                ok = self.act(1.0)
            elif cmd == "sync":
                self.cell = msg.get("cell", self.cell)
                ok = True
            elif cmd in ("eyes", "status", "ping", "stop"):
                ok = True
            else:
                self.record(msg, "rejected")
                return self.reply(False, f"unknown command {cmd!r}")
            self.record(msg, "done" if ok else "stopped")
            return self.reply(ok, None if ok else "stopped")
        finally:
            self.busy = None

    def serve(self) -> None:
        poller = zmq.Poller()
        poller.register(self.main, zmq.POLLIN)
        poller.register(self.control, zmq.POLLIN)
        log.info("fake driver ready (speed x%s)", self.speed)
        while True:
            events = dict(poller.poll(1000))
            if self.control in events:
                self.check_control()
            if self.main in events:
                msg = json.loads(self.main.recv())
                log.info("<- %s", msg)
                self.main.send_json(self.execute(msg))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--control-port", type=int, default=5556)
    p.add_argument("--speed", type=float, default=1.0, help="time scale; 4 = four times faster than real")
    p.add_argument("--fail-move", type=int, action="append", default=[], help="make the Nth walk fail")
    p.add_argument("--journal", help="append every executed command as JSON lines (for tests)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    FakeDriver(args.port, args.control_port, args.speed, set(args.fail_move), args.journal).serve()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
