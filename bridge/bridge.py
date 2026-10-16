#!/usr/bin/env python3
"""Robot bridge on the TA laptop (Python 3).

Long-polls the game server over HTTPS (outbound only, bearer token) and forwards
each command to nao_driver.py over ZMQ on localhost. Nothing on the robot network
has to be reachable from the internet.

Threads:
  main       GET /bridge/next -> validate -> driver (REQ :5555) -> POST /bridge/result, one command at a time.
             A `say` marked `hall` is first rendered to audio on the robot and uploaded
             (POST /bridge/audio), so the board plays it through the hall speakers as the robot speaks.
  priority   GET /bridge/next?channel=priority -> `stop` straight to the driver's control socket (:5556),
             so it can interrupt a walk that is running
  heartbeat  driver status (battery, temperatures) -> POST /bridge/heartbeat every few seconds

Every command has a cmd_id. The id of a command is written to a state file before
it starts, so a command is never run twice, also not after a bridge restart.

  SERVER_URL=https://minimax.example.nl BRIDGE_TOKEN=... python3 bridge/bridge.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import base64
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
import zmq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
try:  # reuse the game rules from the server when the repository is complete
    from app.game.engine import CELLS, is_adjacent
except ImportError:  # pragma: no cover - bridge copied on its own
    CELLS = "ABCDEFGHI"
    _N = {"A": "BD", "B": "ACE", "C": "BF", "D": "AEG", "E": "BDFH", "F": "CEI", "G": "DH", "H": "EGI", "I": "FH"}

    def is_adjacent(a: str, b: str) -> bool:
        return a in _N and b in _N[a]

log = logging.getLogger("bridge")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ANIMATIONS = ("think", "win", "lose", "wave", "dance", "handshake")
# How long the driver may take per command before the bridge gives up on it.
TIMEOUTS = {"move": 60, "say": 45, "render": 20, "animate": 30, "posture": 30, "nudge": 20, "eyes": 5, "sync": 5, "stop": 10}


def validate(cmd: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Turn a server command into a driver message, or explain why it is refused."""
    if cmd == "move":
        a, b = args.get("from"), args.get("to")
        if not all(isinstance(c, str) and len(c) == 1 and c in CELLS for c in (a, b)):
            return None, "cells must be A-I"
        if not is_adjacent(a, b):
            return None, f"{a} and {b} are not adjacent"
        return {"cmd": "move", "from": a, "to": b}, None
    if cmd == "say":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            return None, "text must be 1-300 characters"
        return {"cmd": "say", "text": text}, None
    if cmd == "animate":
        if args.get("name") not in ANIMATIONS:
            return None, "unknown animation"
        return {"cmd": "animate", "name": args["name"]}, None
    if cmd == "eyes":
        if not HEX.match(str(args.get("color", ""))):
            return None, "color must be #rrggbb"
        return {"cmd": "eyes", "color": args["color"]}, None
    if cmd == "posture":
        if args.get("posture") not in ("stand", "rest", "lie"):
            return None, "posture must be stand, rest or lie"
        return {"cmd": "posture", "posture": args["posture"]}, None
    if cmd == "nudge":
        try:
            dx, dy, dt = (float(args.get(k, 0)) for k in ("dx", "dy", "dtheta"))
        except (TypeError, ValueError):
            return None, "nudge values must be numbers"
        if max(abs(dx), abs(dy)) > 0.15 or abs(dt) > 45:
            return None, "nudge too large (max 0.15 m, 45 degrees)"
        return {"cmd": "nudge", "dx": dx, "dy": dy, "dtheta": dt}, None
    if cmd == "sync":
        cell = args.get("cell")
        if not isinstance(cell, str) or len(cell) != 1 or cell not in CELLS:
            return None, "cell must be A-I"
        msg = {"cmd": "sync", "cell": cell}
        if args.get("heading") is not None:
            msg["heading"] = float(args["heading"])
        return msg, None
    if cmd == "stop":
        return {"cmd": "stop"}, None
    return None, f"unknown command {cmd!r}"


class DriverClient:
    """ZMQ REQ client. A REQ socket is stuck after a missing reply, so it is recreated on timeout."""

    def __init__(self, ctx: zmq.Context, addr: str) -> None:
        self.ctx, self.addr = ctx, addr
        self.sock: zmq.Socket | None = None

    def request(self, msg: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        if self.sock is None:
            self.sock = self.ctx.socket(zmq.REQ)
            self.sock.setsockopt(zmq.LINGER, 0)
            self.sock.connect(self.addr)
        self.sock.send_json(msg)
        if self.sock.poll(int(timeout_s * 1000)):
            return self.sock.recv_json()
        self.sock.close()
        self.sock = None
        return {"ok": False, "error": f"driver did not answer within {timeout_s:.0f} s (is nao_driver.py running?)"}


class State:
    """Persisted ids: which command was started last, and which finished last."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_started = 0
        self.last_done = 0
        self.last_result: dict[str, Any] | None = None
        try:
            data = json.loads(path.read_text())
            self.last_started = int(data.get("last_started", 0))
            self.last_done = int(data.get("last_done", 0))
            self.last_result = data.get("last_result")
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_started": self.last_started, "last_done": self.last_done,
                                   "last_result": self.last_result}))
        os.replace(tmp, self.path)

    def start(self, cmd_id: int) -> None:
        self.last_started = cmd_id
        self.save()

    def finish(self, result: dict[str, Any]) -> None:
        self.last_done = result["cmd_id"]
        self.last_result = result
        self.save()


class Server:
    def __init__(self, url: str, token: str, stop: threading.Event) -> None:
        self.url = url.rstrip("/")
        self.stop = stop
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self.http.headers["User-Agent"] = "robot-minimax-bridge"

    def next(self, last: int, channel: str = "main") -> dict[str, Any] | None:
        r = self.http.get(f"{self.url}/bridge/next", params={"last": last, "channel": channel}, timeout=(10, 40))
        if r.status_code == 204:
            return None
        if r.status_code == 401:
            raise PermissionError("the server rejected BRIDGE_TOKEN")
        r.raise_for_status()
        return r.json()

    def post_audio(self, cmd_id: int, audio: bytes, content_type: str) -> bool:
        """One try only: a late clip is worse than none."""
        try:
            r = self.http.post(f"{self.url}/bridge/audio", params={"cmd_id": cmd_id}, data=audio,
                               headers={"Content-Type": content_type}, timeout=(5, 10))
            if r.status_code < 300:
                return True
            log.warning("POST /bridge/audio -> HTTP %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("POST /bridge/audio failed: %s", e)
        return False

    def post(self, path: str, payload: dict[str, Any], retry: bool = True) -> bool:
        delay = 1.0
        while not self.stop.is_set():
            try:
                r = self.http.post(f"{self.url}{path}", json=payload, timeout=(10, 20))
                if r.status_code < 300:
                    return True
                log.warning("POST %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
                if r.status_code in (400, 401, 422):
                    return False
            except requests.RequestException as e:
                log.warning("POST %s failed: %s", path, e)
            if not retry:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 10)
        return False


class HallAudio:
    """Speech for the hall speakers: rendered on the robot in its own voice, MP3 when ffmpeg is
    available (a tenth of the WAV size), and kept per text so a repeated line costs nothing."""

    def __init__(self, server: Server, driver: DriverClient) -> None:
        self.server, self.driver = server, driver
        self.cache: dict[str, tuple[bytes, str]] = {}
        self.ffmpeg = shutil.which("ffmpeg")
        if not self.ffmpeg:
            log.info("ffmpeg not found; the hall gets WAV audio")

    def send(self, cmd_id: int, text: str) -> None:
        t0 = time.time()
        clip = self.cache.get(text)
        if clip is None:
            reply = self.driver.request({"cmd": "render", "text": text}, TIMEOUTS["render"])
            wav = (reply.get("data") or {}).get("wav") if reply.get("ok") else None
            if not wav:
                log.warning("no hall audio for %s: %s", cmd_id, reply.get("error") or "the driver has no robot voice")
                return
            clip = self._encode(base64.b64decode(wav))
            if len(self.cache) > 300:
                self.cache.clear()
            self.cache[text] = clip
        if self.server.post_audio(cmd_id, *clip):
            log.info("hall audio for %s: %d KB in %.2f s", cmd_id, len(clip[0]) // 1024, time.time() - t0)

    def _encode(self, wav: bytes) -> tuple[bytes, str]:
        if self.ffmpeg:
            try:
                mp3 = subprocess.run([self.ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-b:a", "64k",
                                      "-f", "mp3", "pipe:1"], input=wav, capture_output=True, timeout=10, check=True)
                return mp3.stdout, "audio/mpeg"
            except (subprocess.SubprocessError, OSError) as e:
                log.warning("ffmpeg failed (%s); sending WAV", e)
        return wav, "audio/wav"


def poll_loop(name: str, server: Server, channel: str, last: int, handle, stop: threading.Event) -> None:
    delay = 1.0
    while not stop.is_set():
        try:
            cmd = server.next(last, channel)
            delay = 1.0
        except PermissionError as e:
            log.error("%s: %s. Retrying in 30 s.", name, e)
            stop.wait(30)
            continue
        except (requests.RequestException, ValueError) as e:
            log.warning("%s: server unreachable (%s); retrying in %.0f s", name, e.__class__.__name__, delay)
            stop.wait(delay)
            delay = min(delay * 2, 10)
            continue
        if cmd is None:
            continue
        last = max(last, handle(cmd))


def main_channel(server: Server, driver: DriverClient, state: State, stop: threading.Event) -> None:
    hall = HallAudio(server, driver)
    if state.last_started > state.last_done:
        cid = state.last_started
        log.warning("command %s was running when the bridge stopped; reporting it as failed", cid)
        result = {"cmd_id": cid, "status": "failed",
                  "error": "bridge restarted while this command ran; check the robot's position"}
        server.post("/bridge/result", result)
        state.finish(result)

    def handle(cmd: dict[str, Any]) -> int:
        cid = int(cmd["cmd_id"])
        if cid <= state.last_started:
            # Delivered again, e.g. because our result got lost. Never run a command twice.
            if state.last_result and state.last_result.get("cmd_id") == cid:
                result = state.last_result
            else:
                result = {"cmd_id": cid, "status": "failed", "error": "already started earlier; not repeated"}
            log.info("command %s already handled; resending result", cid)
            server.post("/bridge/result", result)
            return cid
        state.start(cid)
        msg, err = validate(cmd.get("cmd", ""), cmd.get("args") or {})
        t0 = time.time()
        if err:
            reply = {"ok": False, "error": f"refused by bridge: {err}"}
        else:
            log.info("-> %s %s", cid, msg)
            if msg["cmd"] == "say" and (cmd.get("args") or {}).get("hall"):
                hall.send(cid, msg["text"])
            reply = driver.request(msg, TIMEOUTS.get(msg["cmd"], 20))
        result = {
            "cmd_id": cid,
            "status": "done" if reply.get("ok") else "failed",
            "error": reply.get("error"),
            "battery": reply.get("battery"),
            "warnings": reply.get("warnings"),
            "data": reply.get("data"),
        }
        log.info("<- %s %s in %.1f s%s", cid, result["status"], time.time() - t0,
                 f": {result['error']}" if result["error"] else "")
        state.finish(result)
        server.post("/bridge/result", result)
        return cid

    poll_loop("main", server, "main", state.last_started, handle, stop)


def priority_channel(server: Server, control: DriverClient, stop: threading.Event) -> None:
    def handle(cmd: dict[str, Any]) -> int:
        cid = int(cmd["cmd_id"])
        log.warning("STOP %s", cid)
        reply = control.request({"cmd": "stop"}, TIMEOUTS["stop"])
        server.post("/bridge/result", {"cmd_id": cid, "status": "done" if reply.get("ok") else "failed",
                                       "error": reply.get("error")})
        return cid

    poll_loop("priority", server, "priority", 0, handle, stop)


def heartbeat(server: Server, control: DriverClient, interval: float, stop: threading.Event) -> None:
    while not stop.is_set():
        reply = control.request({"cmd": "status"}, 4)
        payload = {"driver_ok": bool(reply.get("ok")), "battery": reply.get("battery"),
                   "warnings": reply.get("warnings") if reply.get("ok") else [reply.get("error") or "driver down"]}
        server.post("/bridge/heartbeat", payload, retry=False)
        stop.wait(interval)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=os.environ.get("SERVER_URL", "http://localhost:8000"))
    p.add_argument("--token", default=os.environ.get("BRIDGE_TOKEN"))
    p.add_argument("--driver", default=os.environ.get("DRIVER_ADDR", "tcp://127.0.0.1:5555"))
    p.add_argument("--control", default=os.environ.get("DRIVER_CONTROL_ADDR", "tcp://127.0.0.1:5556"))
    p.add_argument("--state-file", default=os.environ.get(
        "BRIDGE_STATE", str(Path.home() / ".cache" / "robot-minimax" / "bridge-state.json")))
    p.add_argument("--heartbeat", type=float, default=10.0, help="seconds between status reports")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.token:
        p.error("BRIDGE_TOKEN (or --token) is required")
    if args.server.startswith("http://") and not re.match(r"http://(localhost|127\.0\.0\.1)", args.server):
        log.warning("SERVER_URL is plain http; use https outside of local testing")

    stop = threading.Event()
    ctx = zmq.Context()
    state = State(Path(args.state_file))
    log.info("server %s, driver %s, state %s (last command %s)", args.server, args.driver, args.state_file,
             state.last_started or "-")
    try:
        r = requests.get(f"{args.server.rstrip('/')}/healthz", timeout=10)
        log.info("server health: %s", r.json())
    except (requests.RequestException, ValueError) as e:
        log.warning("server not reachable yet: %s", e)

    threads = [
        threading.Thread(target=priority_channel, args=(Server(args.server, args.token, stop),
                                                        DriverClient(ctx, args.control), stop), daemon=True),
        threading.Thread(target=heartbeat, args=(Server(args.server, args.token, stop),
                                                 DriverClient(ctx, args.control), args.heartbeat, stop), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        main_channel(Server(args.server, args.token, stop), DriverClient(ctx, args.driver), state, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
