"""Robot bridge: unit tests, and the whole chain server -> bridge.py -> driver without a robot.

The end-to-end test runs twice: with fake_driver.py (Python 3) and with the real
nao_driver.py in --sim mode under Python 2.7 (conda env `nao`), when that env exists.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import zmq

os.environ.setdefault("MINIMAX_NO_APP", "1")

from app.main import Config, create_app  # noqa: E402

from .test_server import Client, free_port  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_DIR = ROOT / "bridge"
NAO_PY = Path(os.environ.get("NAO_PYTHON", Path.home() / "anaconda3" / "envs" / "nao" / "bin" / "python"))

spec = importlib.util.spec_from_file_location("bridge_mod", BRIDGE_DIR / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

spec = importlib.util.spec_from_file_location("robot_mod", BRIDGE_DIR / "robot.py")
robot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(robot)


# unit ----------------------------------------------------------------------------


def test_validate_commands():
    assert bridge.validate("move", {"from": "A", "to": "D"}) == ({"cmd": "move", "from": "A", "to": "D"}, None)
    assert bridge.validate("move", {"from": "A", "to": "E"})[1] == "A and E are not adjacent"
    assert bridge.validate("move", {"from": None, "to": "B"})[1] == "cells must be A-I"
    assert bridge.validate("move", {"from": "AB", "to": "B"})[1] == "cells must be A-I"
    assert bridge.validate("say", {"text": "x" * 301})[1]
    assert bridge.validate("say", {"text": "Hello"})[0] == {"cmd": "say", "text": "Hello"}
    assert bridge.validate("eyes", {"color": "red"})[1]
    assert bridge.validate("nudge", {"dx": 0.2})[1]
    assert bridge.validate("nudge", {"dx": 0.05, "dtheta": -10})[0] == {"cmd": "nudge", "dx": 0.05, "dy": 0.0, "dtheta": -10.0}
    assert bridge.validate("animate", {"name": "dab"})[1]
    for name in ("dance", "handshake"):
        assert bridge.validate("animate", {"name": name}) == ({"cmd": "animate", "name": name}, None)
    assert bridge.validate("animate", {"name": "backflip"})[1]
    assert bridge.validate("posture", {"posture": "lie"}) == ({"cmd": "posture", "posture": "lie"}, None)
    assert bridge.validate("posture", {"posture": "handstand"})[1]
    assert bridge.validate("selfdestruct", {})[1]


# NAO joint limits (radians) for every joint the routines use.
JOINT_LIMITS = {
    "HeadYaw": (-2.0857, 2.0857), "HeadPitch": (-0.6720, 0.5149),
    "LShoulderPitch": (-2.0857, 2.0857), "RShoulderPitch": (-2.0857, 2.0857),
    "LShoulderRoll": (-0.3142, 1.3265), "RShoulderRoll": (-1.3265, 0.3142),
    "LElbowYaw": (-2.0857, 2.0857), "RElbowYaw": (-2.0857, 2.0857),
    "LElbowRoll": (-1.5446, -0.0349), "RElbowRoll": (0.0349, 1.5446),
    "RWristYaw": (-1.8238, 1.8238), "RHand": (0.0, 1.0),
}


@pytest.mark.parametrize("frames", [robot.dance_frames(), robot.handshake_frames()], ids=["dance", "handshake"])
def test_routine_keyframes_are_valid(frames):
    names, angles, times = robot.keyframes(frames)
    assert set(names) <= set(JOINT_LIMITS), "routines move only the head and arms"
    for name, a, t in zip(names, angles, times):
        lo, hi = JOINT_LIMITS[name]
        assert all(lo <= x <= hi for x in a), name
        assert t[0] > 0 and all(t1 > t0 for t0, t1 in zip(t, t[1:])), name
    assert max(t[-1] for t in times) < 20


def test_mirror_swaps_sides():
    assert robot.mirror({"LShoulderRoll": 0.6, "RElbowYaw": 1.2, "LShoulderPitch": 1.0, "HeadYaw": 0.3}) == {
        "RShoulderRoll": -0.6, "LElbowYaw": -1.2, "RShoulderPitch": 1.0, "HeadYaw": -0.3}


@pytest.mark.parametrize("name", robot.ROUTINES)
def test_routines_run_on_sim_robot_and_end_standing(name):
    r = robot.Robot(sim=True, sim_speed=50)
    r.setup()
    assert r.animate(name) == {"animation": "routine:" + name}
    calls = [(svc, m) for svc, m, _ in r.s.calls]
    assert ("motion", "angleInterpolation") in calls
    assert calls[-1] == ("posture", "goToPosture") and r.s.calls[-1][2][0] == "StandInit"
    assert not any(svc == "anim" for svc, _ in calls), "routines do not depend on built-in animations"


def test_lie_down_and_get_up_again():
    r = robot.Robot(sim=True, sim_speed=50)
    r.setup()
    r.posture("lie")
    assert r.s.posture.getPostureFamily() == "LyingBack"
    r.animate("dance")  # gets up by itself first
    gotos = [args[0] for svc, m, args in r.s.calls if (svc, m) == ("posture", "goToPosture")]
    assert gotos[-3:] == ["LyingBack", "StandInit", "StandInit"]
    r.posture("lie")
    r.posture("stand")
    assert r.s.posture.getPostureFamily() == "Standing"


def test_routine_can_be_stopped():
    r = robot.Robot(sim=True, sim_speed=1)
    with pytest.raises(robot.Stopped):
        r.animate("dance", poll=lambda: True)


def test_state_file_roundtrip(tmp_path):
    st = bridge.State(tmp_path / "s.json")
    assert st.last_started == 0
    st.start(5)
    st2 = bridge.State(tmp_path / "s.json")
    assert st2.last_started == 5 and st2.last_done == 0
    st2.finish({"cmd_id": 5, "status": "done"})
    st3 = bridge.State(tmp_path / "s.json")
    assert st3.last_done == 5 and st3.last_result["status"] == "done"


def test_driver_client_timeout_recreates_socket():
    ctx = zmq.Context()
    port = free_port()
    rep = ctx.socket(zmq.REP)
    rep.bind(f"tcp://127.0.0.1:{port}")
    client = bridge.DriverClient(ctx, f"tcp://127.0.0.1:{port}")
    out = client.request({"cmd": "ping"}, 0.2)  # nobody answers
    assert not out["ok"] and "did not answer" in out["error"] and client.sock is None
    rep.recv()  # the first request arrived but was never answered
    rep.close()
    rep = ctx.socket(zmq.REP)
    rep.setsockopt(zmq.LINGER, 0)
    port2 = free_port()
    rep.bind(f"tcp://127.0.0.1:{port2}")
    client.addr = f"tcp://127.0.0.1:{port2}"

    def answer():
        msg = json.loads(rep.recv())
        rep.send_json({"ok": True, "echo": msg})

    t = threading.Thread(target=answer)
    t.start()
    assert client.request({"cmd": "ping"}, 2) == {"ok": True, "echo": {"cmd": "ping"}}
    t.join()
    rep.close()
    ctx.destroy(linger=0)


# end to end ----------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    port = free_port()
    cfg = Config(admin_token="adm", bridge_token="brg", device_hash_secret="s", db_path=str(tmp_path / "e2e.db"),
                 static_dir="", state_interval=0.05, status_interval=0.5, bridge_poll_timeout=2,
                 settings={"think_min_s": 0, "think_max_s": 1, "sim_move_s": 0, "step_ms": 20})
    srv = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def driver_cmd(kind, port, cport, journal):
    if kind == "fake":
        return [sys.executable, str(BRIDGE_DIR / "fake_driver.py"), "--speed", "10", "--port", str(port),
                "--control-port", str(cport), "--journal", str(journal)]
    return [str(NAO_PY), str(BRIDGE_DIR / "nao_driver.py"), "--sim", "--sim-speed", "10", "--port", str(port),
            "--control-port", str(cport)]


@pytest.fixture(params=["fake", "nao_sim"])
def chain(request, server, tmp_path):
    if request.param == "nao_sim" and not NAO_PY.exists():
        pytest.skip(f"Python 2.7 env not found at {NAO_PY}")
    port, cport = free_port(), free_port()
    journal = tmp_path / "journal.jsonl"
    logs = {name: open(tmp_path / f"{name}.log", "w") for name in ("driver", "bridge")}
    driver = subprocess.Popen(driver_cmd(request.param, port, cport, journal), stdout=logs["driver"],
                              stderr=subprocess.STDOUT, cwd=str(tmp_path))
    br = subprocess.Popen([sys.executable, str(BRIDGE_DIR / "bridge.py"), "--server", server, "--token", "brg",
                           "--driver", f"tcp://127.0.0.1:{port}", "--control", f"tcp://127.0.0.1:{cport}",
                           "--state-file", str(tmp_path / "bridge-state.json"), "--heartbeat", "0.5"],
                          stdout=logs["bridge"], stderr=subprocess.STDOUT)
    yield {"server": server, "journal": journal, "kind": request.param, "tmp": tmp_path}
    for proc in (br, driver):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    for f in logs.values():
        f.close()
    if os.environ.get("SHOW_BRIDGE_LOGS"):
        for name in logs:
            print(f"--- {name} ---\n" + (tmp_path / f"{name}.log").read_text())


async def test_full_game_through_bridge(chain):
    admin = Client(chain["server"], "admin", "adm", "adm")
    phone = Client(chain["server"], device="ph")
    board = Client(chain["server"], "board", device="bd")
    clips = []
    board.sio.on("speak", clips.append)
    await admin.connect()
    await phone.connect()
    await board.connect()
    # wait until the bridge reports in, with battery from the driver
    deadline = time.monotonic() + 15
    while not (admin.status and admin.status["bridge"]["online"] and admin.status["bridge"]["battery"]):
        assert time.monotonic() < deadline, f"bridge never came online: {admin.status}"
        await asyncio.sleep(0.1)
    await admin.admin("mode", mode="robot")
    await admin.admin("new_game", variant="1", class_side="MAX")
    await admin.admin("start")
    st = await phone.wait(lambda s: s["phase"] == "VOTING")
    assert (await phone.sio.call("vote", {"round_id": st["round"]["id"], "choice": "D"}, timeout=5))["ok"]
    await admin.admin("close")
    st = await phone.wait(lambda s: s["phase"] == "VOTING" and s["ply"] == 2, timeout=30)
    assert st["path"] == ["A", "D", "E"] and st["robot"]["cell"] == "E"
    await phone.sio.call("vote", {"round_id": st["round"]["id"], "choice": "H"}, timeout=5)
    await admin.admin("close")
    st = await phone.wait(lambda s: s["phase"] == "GAME_OVER", timeout=30)
    assert st["result"]["owner"] == "class"
    # the bridge reported every result; nothing is left in the queue
    deadline = time.monotonic() + 20
    while admin.status["bridge"]["queued"] or admin.status["bridge"]["busy"]:
        assert time.monotonic() < deadline, admin.status["bridge"]
        await asyncio.sleep(0.2)
    if chain["kind"] == "fake":
        lines = [json.loads(line) for line in chain["journal"].read_text().splitlines()]
        moves = [(e["from"], e["to"]) for e in lines if e["cmd"] == "move"]
        assert moves == [("A", "D"), ("D", "E"), ("E", "H")]
        said = [e["text"] for e in lines if e["cmd"] == "say"]
        assert said[0] == "You chose D. Let me walk there."
        assert any(t.startswith("I evaluated") for t in said)
        assert said[-1] == "You win. Well played!"
        assert all(e["outcome"] == "done" for e in lines)
        # every line was rendered for the hall before the robot said it, and reached the board as audio
        assert [e["text"] for e in lines if e["cmd"] == "render"] == said
        assert len(clips) == len(said) and all(c["audio"] and c["type"] in ("audio/mpeg", "audio/wav") for c in clips)
    await admin.sio.disconnect()
    await phone.sio.disconnect()
    await board.sio.disconnect()


async def test_stop_interrupts_walk_and_admin_recovers(chain):
    admin = Client(chain["server"], "admin", "adm", "adm2")
    phone = Client(chain["server"], device="ph2")
    await admin.connect()
    await phone.connect()
    deadline = time.monotonic() + 15
    while not (admin.status and admin.status["bridge"]["online"]):
        assert time.monotonic() < deadline
        await asyncio.sleep(0.1)
    await admin.admin("mode", mode="robot")
    await admin.admin("settings", speech=False)  # straight to the walk
    await admin.admin("new_game", variant="2", class_side="MAX")
    await admin.admin("start")
    st = await phone.wait(lambda s: s["phase"] == "VOTING")
    await phone.sio.call("vote", {"round_id": st["round"]["id"], "choice": "D"}, timeout=5)
    await admin.admin("close")
    await phone.wait(lambda s: s["phase"] == "ROBOT_MOVING")
    # wait until the walk is actually running on the driver, then stop it
    deadline = time.monotonic() + 10
    while not (admin.status["bridge"]["busy"] and admin.status["bridge"]["busy"]["cmd"] == "move"):
        assert time.monotonic() < deadline, admin.status["bridge"]
        await asyncio.sleep(0.02)
    await admin.admin("robot", cmd="stop")
    st = await phone.wait(lambda s: s["robot"]["error"] is not None, timeout=10)
    assert "stopped" in st["robot"]["error"] and st["phase"] == "ROBOT_MOVING"
    # the TA puts the robot on D and tells the server
    await admin.admin("robot_at", cell="D")
    st = await phone.wait(lambda s: s["phase"] in ("THINKING", "ROBOT_MOVING", "VOTING") and s["ply"] >= 2, timeout=30)
    assert st["path"][:2] == ["A", "D"]
    await admin.admin("settings", speech=True)
    await admin.sio.disconnect()
    await phone.sio.disconnect()
