# -*- coding: utf-8 -*-
"""NAOqi wrapper for the minimax demo: walk between grid cells, speak, animate, eye LEDs.

Python 2.7 (conda env `nao`, NAOqi SDK 2.8). Talks to the robot with a qi.Session
client. Unlike the older ALProxy API, a qi.Session client opens no listening
port on the laptop, so nothing becomes reachable from eduroam.

`Robot(..., sim=True)` uses fake services, so the driver can be tested without a robot.

Grid frame: the robot starts on the start cell facing the audience (heading 0).
  +x (forward) = towards the audience = next row   (A -> D -> G)   x row_sign
  +y (robot's left) = audience's right = next column (A -> B -> C) x col_sign
Flip --row-sign / --col-sign if the grid is taped the other way round.
"""
from __future__ import division, print_function

import base64
import itertools
import logging
import math
import os
import subprocess
import threading
import time

CELLS = "ABCDEFGHI"
log = logging.getLogger("robot")

# Built-in NAO animations to try per gesture, in order. If none is installed, a
# simple joint gesture is used instead. Check these names on the robot (Choregraphe
# > Animations) and adjust if needed.
ANIMATIONS = {
    "think": ["animations/Stand/Gestures/Thinking_1", "animations/Stand/Gestures/Thinking_8",
              "animations/Stand/Waiting/Think_1"],
    "win": ["animations/Stand/Emotions/Positive/Happy_4", "animations/Stand/Gestures/Enthusiastic_4",
            "animations/Stand/Emotions/Positive/Winner_1"],
    "lose": ["animations/Stand/Emotions/Negative/Sad_1", "animations/Stand/Emotions/Negative/Disappointed_1"],
    "wave": ["animations/Stand/Gestures/Hey_1", "animations/Stand/Gestures/Hey_6"],
}
ANIMATION_TAGS = {"think": "thinking", "win": "happy", "lose": "sad", "wave": "hello"}
LEG_JOINTS = ["LHipYawPitch", "LHipRoll", "LHipPitch", "LKneePitch", "LAnklePitch", "LAnkleRoll",
              "RHipRoll", "RHipPitch", "RKneePitch", "RAnklePitch", "RAnkleRoll"]


class Stopped(Exception):
    """A running action was interrupted by a stop command."""


def cell_rc(cell):
    i = CELLS.index(cell)
    return i // 3, i % 3


def norm_angle(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


def grid_displacement(frm, to, cell_size, row_sign=1, col_sign=1):
    """Displacement (x, y) in metres in the grid frame (heading 0 = facing the audience)."""
    r0, c0 = cell_rc(frm)
    r1, c1 = cell_rc(to)
    return (r1 - r0) * cell_size * row_sign, (c1 - c0) * cell_size * col_sign


def to_robot_frame(x, y, heading):
    """Rotate a grid-frame vector into the robot's frame, given the robot's heading."""
    c, s = math.cos(heading), math.sin(heading)
    return c * x + s * y, -s * x + c * y


def hex_to_rgb_int(color):
    color = color.lstrip("#")
    return int(color, 16)


class Robot(object):
    def __init__(self, ip="192.168.50.10", port=9559, cell_size=0.5, walk="strafe", row_sign=1, col_sign=1,
                 sim=False, volume=None, move_config=None, temp_warn=70.0, battery_warn=30,
                 odometry_log=None, sim_speed=1.0, ssh_user="nao", ssh_password=None):
        self.ip, self.port = ip, port
        self.ssh_user, self.ssh_password = ssh_user, ssh_password
        self._render_ids = itertools.count(1)
        self.cell_size = cell_size
        self.walk = walk
        self.row_sign, self.col_sign = row_sign, col_sign
        self.volume = volume
        self.move_config = move_config or []
        self.temp_warn = temp_warn
        self.battery_warn = battery_warn
        self.odometry_log = odometry_log
        self.sim = sim
        self.cell = "A"
        self.heading = 0.0
        self._status_cache = (0.0, None)
        if sim:
            self.s = SimServices(speed=sim_speed)
        else:
            self.s = QiServices(ip, port)

    # setup -------------------------------------------------------------------

    def setup(self):
        """Autonomous Life off, wake up, StandInit. The fall manager stays enabled (the default)."""
        s = self.s
        try:
            if s.life.getState() != "disabled":
                s.life.setState("disabled")
        except Exception as e:  # noqa: BLE001 - not fatal
            log.warning("could not disable Autonomous Life: %s", e)
        s.motion.wakeUp()
        s.posture.goToPosture("StandInit", 0.5)
        try:
            s.tts.setLanguage("English")
        except Exception as e:  # noqa: BLE001
            log.warning("could not set the speech language: %s", e)
        if self.volume is not None:
            try:
                s.audio.setOutputVolume(int(self.volume))
            except Exception as e:  # noqa: BLE001
                log.warning("could not set the volume: %s", e)
        log.info("robot ready: awake, StandInit, Autonomous Life %s", s.life.getState())

    # waiting with stop checks -------------------------------------------------

    def _wait(self, fut, poll, timeout, on_stop=None):
        """Wait for a qi future. `poll()` returns True when a stop was requested."""
        t0 = time.time()
        while not fut.isFinished():
            if poll and poll():
                if on_stop:
                    on_stop()
                try:
                    fut.cancel()
                except Exception:  # noqa: BLE001
                    pass
                raise Stopped("stopped")
            if time.time() - t0 > timeout:
                if on_stop:
                    on_stop()
                raise RuntimeError("timed out after %d s" % timeout)
            time.sleep(0.05)
        if fut.hasError():
            raise RuntimeError(fut.error())
        return fut.value()

    def _ensure_standing(self):
        if not self.s.motion.robotIsWakeUp():
            self.s.motion.wakeUp()
            self.s.posture.goToPosture("StandInit", 0.5)

    def _move_to(self, x, y, theta, poll, timeout=40):
        if abs(x) < 1e-4 and abs(y) < 1e-4 and abs(theta) < 1e-3:
            return
        if self.move_config:
            fut = self.s.motion.moveTo(x, y, theta, self.move_config, _async=True)
        else:
            fut = self.s.motion.moveTo(x, y, theta, _async=True)
        self._wait(fut, poll, timeout, on_stop=self.s.motion.stopMove)

    # commands ------------------------------------------------------------------

    def move(self, frm, to, poll=None):
        """Walk from cell `frm` to cell `to`. Validation happens in the bridge and the driver."""
        self._ensure_standing()
        X, Y = grid_displacement(frm, to, self.cell_size, self.row_sign, self.col_sign)
        before = self._odometry()
        if self.walk == "turn":
            # Option A: turn towards the target, walk forward, turn back to face the audience.
            target = math.atan2(Y, X)
            dist = math.hypot(X, Y)
            self._move_to(0, 0, norm_angle(target - self.heading), poll)
            self.heading = target
            self._move_to(dist, 0, 0, poll)
            self._move_to(0, 0, norm_angle(0.0 - self.heading), poll)
            self.heading = 0.0
        else:
            # Option B: keep facing the audience and step sideways/forward/back.
            x, y = to_robot_frame(X, Y, self.heading)
            self._move_to(x, y, 0, poll)
        family = self._posture_family()
        if family and family != "Standing":
            raise RuntimeError("robot is not standing after the walk (posture: %s)" % family)
        self.cell = to
        after = self._odometry()
        self._log_odometry(frm, to, X, Y, before, after)
        return {"cell": self.cell, "heading_deg": round(math.degrees(self.heading), 1)}

    def nudge(self, dx, dy, dtheta_deg, poll=None):
        self._ensure_standing()
        self._move_to(dx, dy, math.radians(dtheta_deg), poll, timeout=20)
        return {"cell": self.cell}

    def sync(self, cell, heading_deg=None):
        """Tell the driver where the robot is. Heading 0 means facing the audience."""
        self.cell = cell
        if heading_deg is not None:
            self.heading = math.radians(heading_deg)
        return {"cell": self.cell, "heading_deg": round(math.degrees(self.heading), 1)}

    def say(self, text, poll=None):
        try:
            fut = self.s.aspeech.say(text, _async=True)
        except Exception as e:  # noqa: BLE001 - fall back to plain speech
            log.warning("animated speech failed (%s), using ALTextToSpeech", e)
            fut = self.s.tts.say(text, _async=True)
        self._wait(fut, poll, 60, on_stop=self.s.tts.stopAll)

    def render(self, text):
        """Speak `text` into a WAV file on the robot and return it base64-encoded, for the
        board in the hall. Same voice and settings as say(). Returns {"wav": None} in sim mode."""
        if self.sim:
            return {"wav": None}
        path = "/tmp/hall-%d-%d.wav" % (os.getpid(), next(self._render_ids))
        t0 = time.time()
        self.s.tts.sayToFile(text, path)
        t1 = time.time()
        wav = self._ssh("cat %s && rm -f %s" % (path, path))
        log.info("rendered %d bytes of speech in %.2f s, fetched in %.2f s", len(wav), t1 - t0, time.time() - t1)
        return {"wav": base64.b64encode(wav).decode("ascii")}

    def _ssh(self, command):
        """Run a command on the robot over SSH and return its output. The connection stays open
        for 10 minutes (ControlPersist), so later calls skip the SSH handshake."""
        ssh = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5",
               "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/nao-ssh-%r@%h:%p", "-o", "ControlPersist=600",
               "%s@%s" % (self.ssh_user, self.ip), command]
        env = None
        if self.ssh_password:
            ssh = ["sshpass", "-e"] + ssh
            env = dict(os.environ, SSHPASS=self.ssh_password)
        return subprocess.check_output(ssh, env=env)

    def animate(self, name, poll=None):
        for path in ANIMATIONS.get(name, []):
            try:
                fut = self.s.anim.run(path, _async=True)
                self._wait(fut, poll, 30)
                return {"animation": path}
            except Stopped:
                raise
            except Exception as e:  # noqa: BLE001 - try the next one
                log.info("animation %s not available: %s", path, e)
        tag = ANIMATION_TAGS.get(name)
        if tag:
            try:
                fut = self.s.anim.runTag(tag, _async=True)
                self._wait(fut, poll, 30)
                return {"animation": "tag:" + tag}
            except Stopped:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("animation tag %s not available: %s", tag, e)
        self._gesture(name, poll)
        return {"animation": "gesture:" + name}

    def _gesture(self, name, poll):
        """Fallback gestures with plain joint interpolation; these work on every NAO."""
        m = self.s.motion
        if name == "think":
            names, angles, times = ["HeadPitch", "HeadYaw"], [[0.25, 0.0], [0.4, 0.0]], [[0.8, 2.2], [0.8, 2.2]]
        elif name == "win":
            names = ["LShoulderPitch", "RShoulderPitch", "LShoulderRoll", "RShoulderRoll"]
            angles = [[-1.2, 1.4], [-1.2, 1.4], [0.3, 0.2], [-0.3, -0.2]]
            times = [[1.0, 2.4]] * 4
        elif name == "lose":
            names, angles, times = ["HeadPitch"], [[0.45, 0.0]], [[1.0, 2.5]]
        else:  # wave
            names = ["RShoulderPitch", "RShoulderRoll", "RElbowRoll", "RElbowYaw"]
            angles = [[-0.9, -0.9, -0.9, 1.4], [-0.3, -0.3, -0.3, -0.2], [1.0, 0.4, 1.0, 0.4], [1.3, 1.3, 1.3, 1.3]]
            times = [[0.8, 1.3, 1.8, 2.8]] * 4
        fut = m.angleInterpolation(names, angles, times, True, _async=True)
        self._wait(fut, poll, 15)

    def eyes(self, color):
        self.s.leds.fadeRGB("FaceLeds", hex_to_rgb_int(color), 0.3)

    def posture(self, which, poll=None):
        if which == "rest":
            self.s.motion.rest()
        else:
            self.s.motion.wakeUp()
            fut = self.s.posture.goToPosture("StandInit", 0.5, _async=True)
            self._wait(fut, poll, 30)

    def stop(self):
        """Stop walking and stand still. Also stops speech."""
        try:
            self.s.motion.stopMove()
        finally:
            try:
                self.s.tts.stopAll()
            except Exception:  # noqa: BLE001
                pass
            try:
                if self.s.motion.robotIsWakeUp():
                    self.s.posture.goToPosture("StandInit", 0.5)
            except Exception as e:  # noqa: BLE001
                log.warning("StandInit after stop failed: %s", e)

    # status ----------------------------------------------------------------------

    def status(self, max_age=5.0):
        """Battery and warnings, cached for a few seconds so every reply can carry them."""
        t, cached = self._status_cache
        if cached and time.time() - t < max_age:
            return cached
        warnings = []
        battery = None
        try:
            battery = int(self.s.battery.getBatteryCharge())
            if battery < self.battery_warn:
                warnings.append("battery low (%d%%)" % battery)
        except Exception as e:  # noqa: BLE001
            warnings.append("battery unknown: %s" % e)
        try:
            keys = ["Device/SubDeviceList/%s/Temperature/Sensor/Value" % j for j in LEG_JOINTS]
            temps = self.s.memory.getListData(keys)
            for joint, temp in zip(LEG_JOINTS, temps):
                if temp is not None and temp >= self.temp_warn:
                    warnings.append("%s hot (%d C)" % (joint, temp))
        except Exception as e:  # noqa: BLE001
            log.debug("temperature read failed: %s", e)
        out = {"battery": battery, "warnings": warnings, "cell": self.cell,
               "heading_deg": round(math.degrees(self.heading), 1)}
        self._status_cache = (time.time(), out)
        return out

    def _posture_family(self):
        try:
            return self.s.posture.getPostureFamily()
        except Exception:  # noqa: BLE001
            return None

    def _odometry(self):
        try:
            return self.s.motion.getRobotPosition(True)
        except Exception:  # noqa: BLE001
            return None

    def _log_odometry(self, frm, to, X, Y, before, after):
        """For the drift test: expected displacement vs. what the robot's odometry measured."""
        if not self.odometry_log or not before or not after:
            return
        dx, dy = after[0] - before[0], after[1] - before[1]
        header = not os.path.exists(self.odometry_log)
        with open(self.odometry_log, "a") as f:
            if header:
                f.write("time,walk,from,to,expected_x,expected_y,odom_dx,odom_dy,odom_dtheta_deg\n")
            f.write("%s,%s,%s,%s,%.3f,%.3f,%.3f,%.3f,%.1f\n" % (
                time.strftime("%d-%m-%Y %H:%M:%S"), self.walk, frm, to, X, Y, dx, dy,
                math.degrees(norm_angle(after[2] - before[2]))))


class QiServices(object):
    """NAOqi services through a qi.Session client (no listening socket on the laptop)."""

    def __init__(self, ip, port, timeout=10):
        import qi  # from the NAOqi SDK, on sys.path via nao_driver.py
        url = "tcp://%s:%d" % (ip, port)
        self.session = qi.Session()
        fut = self.session.connect(url, _async=True)
        fut.wait(int(timeout * 1000))
        if not fut.isFinished():
            try:
                fut.cancel()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError("no answer from NAO at %s within %d s (is it on and on the robot network? "
                               "try: ping %s)" % (url, timeout, ip))
        if fut.hasError():
            raise RuntimeError("cannot connect to NAO at %s: %s" % (url, fut.error()))
        get = self.session.service
        self.motion = get("ALMotion")
        self.posture = get("ALRobotPosture")
        self.tts = get("ALTextToSpeech")
        self.aspeech = get("ALAnimatedSpeech")
        self.anim = get("ALAnimationPlayer")
        self.leds = get("ALLeds")
        self.life = get("ALAutonomousLife")
        self.battery = get("ALBattery")
        self.memory = get("ALMemory")
        self.audio = get("ALAudioDevice")


# --------------------------------------------------------------------------------
# Simulation: fake services with the same call style, for tests without a robot.


class _SimFuture(object):
    def __init__(self, duration, value=None, error=None, on_cancel=None):
        self._end = time.time() + duration
        self._value, self._error = value, error
        self._cancelled = threading.Event()
        self._on_cancel = on_cancel

    def isFinished(self):  # noqa: N802 - qi naming
        return self._cancelled.is_set() or time.time() >= self._end

    def hasError(self):  # noqa: N802
        return self._error is not None

    def error(self):
        return self._error

    def value(self):
        return self._value

    def cancel(self):
        self._cancelled.set()


class _SimService(object):
    def __init__(self, name, sim):
        self._name, self._sim = name, sim

    def __getattr__(self, method):
        def call(*args, **kwargs):
            asynchronous = kwargs.pop("_async", False)
            duration, value, error = self._sim.handle(self._name, method, args)
            if asynchronous:
                return _SimFuture(duration, value, error)
            if error:
                raise RuntimeError(error)
            time.sleep(min(duration, 0.2))
            return value
        return call


class SimServices(object):
    """Pretends to be a NAO. Walks take time proportional to distance, scaled by `speed`."""

    def __init__(self, speed=1.0):
        self.speed = speed
        self.calls = []
        self.awake = False
        self.life_state = "solitary"
        self.battery_level = 92
        self.lock = threading.Lock()
        for name in ("motion", "posture", "tts", "aspeech", "anim", "leds", "life", "battery", "memory", "audio"):
            setattr(self, name, _SimService(name, self))

    def handle(self, service, method, args):
        with self.lock:
            self.calls.append((service, method, args))
        s = 1.0 / max(self.speed, 0.01)
        if service == "motion":
            if method == "moveTo":
                x, y, theta = args[0], args[1], args[2]
                self.battery_level = max(5, self.battery_level - 1)
                return (0.6 + 2.0 * math.hypot(x, y) + 0.6 * abs(theta)) * s, None, None
            if method == "robotIsWakeUp":
                return 0, self.awake, None
            if method == "wakeUp":
                self.awake = True
                return 0.3 * s, None, None
            if method == "rest":
                self.awake = False
                return 0.3 * s, None, None
            if method == "getRobotPosition":
                return 0, [0.0, 0.0, 0.0], None
            if method == "angleInterpolation":
                return 1.5 * s, None, None
            return 0, None, None
        if service == "posture":
            if method == "getPostureFamily":
                return 0, "Standing", None
            return 0.5 * s, True, None
        if service in ("tts", "aspeech") and method == "say":
            return (0.4 + 0.05 * len(args[0])) * s, None, None
        if service == "anim":
            if method == "run" and args[0].endswith("_1"):
                return 1.5 * s, None, None
            return 0, None, "animation %s not found" % (args[0] if args else "?")
        if service == "life":
            if method == "getState":
                return 0, self.life_state, None
            if method == "setState":
                self.life_state = args[0]
            return 0, None, None
        if service == "battery":
            return 0, self.battery_level, None
        if service == "memory" and method == "getListData":
            return 0, [41.0] * len(args[0]), None
        return 0, None, None
