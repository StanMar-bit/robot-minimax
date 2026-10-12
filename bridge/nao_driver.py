#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""NAO driver: executes robot commands from bridge.py. Python 2.7 (conda env `nao`), NAOqi SDK 2.8.

It only talks to the robot and to localhost; all networking to the game server
happens in bridge.py (Python 3).

  tcp://127.0.0.1:5555  REP, commands, one at a time; the reply is sent when the action has finished
  tcp://127.0.0.1:5556  REP, control: {"cmd": "stop"} and {"cmd": "status"}, answered while a walk runs

Messages are JSON, e.g. {"cmd": "move", "from": "A", "to": "D"} -> {"ok": true, "battery": 81, "warnings": []}

Run:
  conda run -n nao python bridge/nao_driver.py --nao-ip 192.168.50.10
  conda run -n nao python bridge/nao_driver.py --sim          # no robot: fake NAOqi services
"""
from __future__ import print_function

import argparse
import json
import logging
import os
import re
import sys
import time

import zmq

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_NAME = "pynaoqi-python2.7-2.8.6.23-linux64"
CELLS = "ABCDEFGHI"
NEIGHBOURS = {
    "A": "BD", "B": "ACE", "C": "BF", "D": "AEG", "E": "BDFH", "F": "CEI", "G": "DH", "H": "EGI", "I": "FH",
}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ANIMATION_NAMES = ("think", "win", "lose", "wave")

log = logging.getLogger("nao_driver")


def add_sdk_to_path(explicit=None):
    """Find the NAOqi SDK: --sdk, $NAOQI_SDK, ~/nao/<sdk>, or the copy next to the project."""
    candidates = [explicit, os.environ.get("NAOQI_SDK"),
                  os.path.expanduser(os.path.join("~", "nao", SDK_NAME)),
                  os.path.join(HERE, "..", "..", SDK_NAME)]
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "lib", "python2.7", "site-packages")):
            sys.path.append(os.path.join(c, "lib", "python2.7", "site-packages"))
            return os.path.abspath(c)
    raise SystemExit("NAOqi SDK not found. Set NAOQI_SDK or pass --sdk (looked in: %s)"
                     % ", ".join(str(c) for c in candidates if c))


def validate(msg):
    """Second line of defence; bridge.py validates the same things first."""
    cmd = msg.get("cmd")
    if cmd == "move":
        a, b = msg.get("from"), msg.get("to")
        if a not in NEIGHBOURS or b not in NEIGHBOURS:
            return "cells must be A-I"
        if b not in NEIGHBOURS[a]:
            return "%s and %s are not adjacent" % (a, b)
    elif cmd == "say":
        if not isinstance(msg.get("text"), (str, type(u""))) or not msg["text"].strip() or len(msg["text"]) > 300:
            return "text must be 1-300 characters"
    elif cmd == "animate":
        if msg.get("name") not in ANIMATION_NAMES:
            return "unknown animation"
    elif cmd == "eyes":
        if not HEX.match(str(msg.get("color", ""))):
            return "color must be #rrggbb"
    elif cmd == "posture":
        if msg.get("posture") not in ("stand", "rest"):
            return "posture must be stand or rest"
    elif cmd == "nudge":
        try:
            dx, dy, dt = float(msg.get("dx", 0)), float(msg.get("dy", 0)), float(msg.get("dtheta", 0))
        except (TypeError, ValueError):
            return "nudge values must be numbers"
        if max(abs(dx), abs(dy)) > 0.15 or abs(dt) > 45:
            return "nudge too large"
    elif cmd == "sync":
        if msg.get("cell") not in NEIGHBOURS:
            return "cell must be A-I"
    elif cmd not in ("status", "ping", "stop"):
        return "unknown command %r" % cmd
    return None


class Driver(object):
    def __init__(self, robot, port=5555, control_port=5556):
        self.robot = robot
        self.ctx = zmq.Context()
        self.main = self.ctx.socket(zmq.REP)
        self.main.bind("tcp://127.0.0.1:%d" % port)
        self.control = self.ctx.socket(zmq.REP)
        self.control.bind("tcp://127.0.0.1:%d" % control_port)
        self.busy = None

    def reply(self, ok, error=None, data=None):
        out = {"ok": ok, "error": error, "data": data}
        try:
            st = self.robot.status()
            out["battery"] = st.get("battery")
            out["warnings"] = st.get("warnings")
        except Exception as e:  # noqa: BLE001
            out["warnings"] = ["status unavailable: %s" % e]
        return out

    def check_control(self):
        """Answer pending control requests. Returns True when a stop was requested."""
        stop = False
        while self.control.poll(0):
            try:
                msg = json.loads(self.control.recv())
            except ValueError:
                self.control.send(json.dumps({"ok": False, "error": "bad json"}))
                continue
            cmd = msg.get("cmd")
            if cmd == "stop":
                stop = True
                log.warning("STOP requested%s", " during %s" % self.busy if self.busy else "")
                if not self.busy:
                    try:
                        self.robot.stop()
                    except Exception as e:  # noqa: BLE001
                        log.error("stop failed: %s", e)
                self.control.send(json.dumps({"ok": True, "busy": self.busy}))
            elif cmd in ("status", "ping"):
                data = self.robot.status()
                data["busy"] = self.busy
                self.control.send(json.dumps({"ok": True, "data": data, "battery": data.get("battery"),
                                              "warnings": data.get("warnings")}))
            else:
                self.control.send(json.dumps({"ok": False, "error": "unknown control command"}))
        return stop

    def execute(self, msg):
        err = validate(msg)
        if err:
            return self.reply(False, err)
        cmd = msg["cmd"]
        poll = self.check_control
        r = self.robot
        self.busy = cmd
        t0 = time.time()
        try:
            if cmd == "move":
                data = r.move(msg["from"], msg["to"], poll)
            elif cmd == "say":
                data = r.say(msg["text"], poll)
            elif cmd == "animate":
                data = r.animate(msg["name"], poll)
            elif cmd == "eyes":
                data = r.eyes(msg["color"])
            elif cmd == "posture":
                data = r.posture(msg["posture"], poll)
            elif cmd == "nudge":
                data = r.nudge(float(msg.get("dx", 0)), float(msg.get("dy", 0)), float(msg.get("dtheta", 0)), poll)
            elif cmd == "sync":
                data = r.sync(msg["cell"], msg.get("heading"))
            elif cmd == "stop":
                data = r.stop()
            else:  # status / ping
                data = r.status(max_age=0)
            log.info("%s done in %.1f s", cmd, time.time() - t0)
            return self.reply(True, data=data)
        except Exception as e:  # noqa: BLE001 - report every failure to the server
            name = type(e).__name__
            log.error("%s failed after %.1f s: %s: %s", cmd, time.time() - t0, name, e)
            if name == "Stopped":
                try:
                    r.stop()  # settle in StandInit
                except Exception as e2:  # noqa: BLE001
                    log.error("stop failed: %s", e2)
            return self.reply(False, "stopped" if name == "Stopped" else "%s: %s" % (name, e))
        finally:
            self.busy = None

    def serve(self):
        poller = zmq.Poller()
        poller.register(self.main, zmq.POLLIN)
        poller.register(self.control, zmq.POLLIN)
        log.info("driver listening on 127.0.0.1 (commands + control)")
        while True:
            events = dict(poller.poll(1000))
            if self.control in events:
                self.check_control()
            if self.main in events:
                raw = self.main.recv()
                try:
                    msg = json.loads(raw)
                    log.info("<- %s", raw[:200])
                    out = self.execute(msg)
                except ValueError:
                    out = {"ok": False, "error": "bad json"}
                self.main.send(json.dumps(out))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nao-ip", default=os.environ.get("NAO_IP", "192.168.50.10"))
    p.add_argument("--nao-port", type=int, default=9559)
    p.add_argument("--port", type=int, default=5555, help="command socket (localhost)")
    p.add_argument("--control-port", type=int, default=5556, help="stop/status socket (localhost)")
    p.add_argument("--sdk", help="path to the NAOqi SDK (default: $NAOQI_SDK or ~/nao/%s)" % SDK_NAME)
    p.add_argument("--sim", action="store_true", help="no robot: fake NAOqi services")
    p.add_argument("--sim-speed", type=float, default=1.0, help="simulation speed factor")
    p.add_argument("--cell-size", type=float, default=0.5, help="grid cell size in metres")
    p.add_argument("--walk", choices=("strafe", "turn"), default="strafe",
                   help="strafe: always face the audience (option B); turn: turn, walk, turn back (option A)")
    p.add_argument("--row-sign", type=int, choices=(1, -1), default=1, help="-1 if row A-B-C is nearest the audience")
    p.add_argument("--col-sign", type=int, choices=(1, -1), default=1, help="-1 if column A-D-G is on the audience's right")
    p.add_argument("--start-cell", default="A")
    p.add_argument("--volume", type=int, help="speaker volume 0-100")
    p.add_argument("--max-step-x", type=float, help="ALMotion MaxStepX (m), e.g. 0.04")
    p.add_argument("--step-frequency", type=float, help="ALMotion MaxStepFrequency (0-1)")
    p.add_argument("--odometry-log", help="append expected vs. odometry per move to this CSV (drift test)")
    p.add_argument("--no-setup", action="store_true", help="skip Autonomous Life off / wake up / StandInit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.sim:
        sdk = add_sdk_to_path(args.sdk)
        log.info("NAOqi SDK: %s", sdk)
    sys.path.insert(0, HERE)
    from robot import Robot  # noqa: E402

    move_config = []
    if args.max_step_x:
        move_config.append(["MaxStepX", args.max_step_x])
    if args.step_frequency:
        move_config.append(["MaxStepFrequency", args.step_frequency])

    while True:
        log.info("connecting to %s", "simulated NAO" if args.sim else "NAO at %s:%d" % (args.nao_ip, args.nao_port))
        try:
            robot = Robot(args.nao_ip, args.nao_port, cell_size=args.cell_size, walk=args.walk,
                          row_sign=args.row_sign, col_sign=args.col_sign, sim=args.sim, volume=args.volume,
                          move_config=move_config, odometry_log=args.odometry_log, sim_speed=args.sim_speed)
            break
        except RuntimeError as e:
            log.error("%s. Retrying in 5 s (Ctrl+C to quit).", e)
            time.sleep(5)
    robot.sync(args.start_cell.upper(), 0)
    if not args.no_setup:
        robot.setup()
    st = robot.status(max_age=0)
    log.info("battery %s%%, warnings: %s", st.get("battery"), st.get("warnings") or "none")
    Driver(robot, args.port, args.control_port).serve()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
