"""Integration test: real uvicorn server, Socket.IO clients and the bridge HTTP API."""

import asyncio
import os
import socket
import threading
import time

import httpx
import pytest
import socketio
import uvicorn

os.environ.setdefault("MINIMAX_NO_APP", "1")

from app.main import Config, create_app  # noqa: E402

ADMIN = "admin-token"
BRIDGE = "bridge-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = free_port()
    cfg = Config(admin_token=ADMIN, bridge_token=BRIDGE, device_hash_secret="s",
                 db_path=str(tmp_path_factory.mktemp("db") / "t.db"), static_dir="",
                 state_interval=0.05, status_interval=0.5, bridge_poll_timeout=2,
                 settings={"think_min_s": 0, "think_max_s": 1, "sim_move_s": 0, "step_ms": 20})
    app = create_app(cfg)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


class Client:
    def __init__(self, url, role="phone", token=None, device="dev"):
        self.url, self.role, self.token, self.device = url, role, token, device
        self.sio = socketio.AsyncClient(reconnection=False)
        self.state = None
        self.tree = None
        self.status = None
        self.searches = []
        self.sio.on("state", self._on_state)
        self.sio.on("tree", lambda d: setattr(self, "tree", d))
        self.sio.on("status", lambda d: setattr(self, "status", d))
        self.sio.on("search", lambda d: self.searches.append(d))

    def _on_state(self, data):
        self.state = data

    async def connect(self):
        auth = {"role": self.role}
        if self.token:
            auth["token"] = self.token
        await self.sio.connect(self.url, auth=auth, transports=["websocket"])
        return await self.sio.call("hello", {"device_id": self.device}, timeout=5)

    async def admin(self, _name, **args):
        res = await self.sio.call(f"admin:{_name}", args, timeout=5)
        assert res["ok"], res
        return res.get("result")

    async def wait(self, cond, timeout=5):
        deadline = time.monotonic() + timeout
        while not (self.state and cond(self.state)):
            assert time.monotonic() < deadline, f"timeout; last state phase={self.state and self.state['phase']}"
            await asyncio.sleep(0.02)
        return self.state


async def test_healthz_and_pages(server):
    async with httpx.AsyncClient(base_url=server) as http:
        r = await http.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"]
        for page in ("/board", "/play"):
            r = await http.get(page)
            assert r.status_code == 503  # frontend not built in this test


async def test_admin_token_required(server):
    bad = socketio.AsyncClient(reconnection=False)
    with pytest.raises(socketio.exceptions.ConnectionError):
        await bad.connect(server, auth={"role": "admin", "token": "wrong"}, transports=["websocket"])
    phone = Client(server, device="p0")
    await phone.connect()
    res = await phone.sio.call("admin:start", {}, timeout=5)
    assert res == {"ok": False, "error": "not an admin"}
    await phone.sio.disconnect()


async def test_full_round_over_sockets(server):
    admin = Client(server, "admin", ADMIN, "adm")
    board = Client(server, "board", device="board")
    phones = [Client(server, device=f"phone-{i}") for i in range(5)]
    await admin.connect()
    await board.connect()
    for p in phones:
        await p.connect()
    await admin.admin("new_game", variant="1", class_side="MAX")
    await board.wait(lambda s: s["phase"] == "LOBBY" and s["ply"] == 0)
    assert board.tree["tree"]["root"] == "A" and len(board.tree["tree"]["nodes"]) == 23
    await admin.admin("start")
    st = await phones[0].wait(lambda s: s["phase"] == "VOTING")
    rid = st["round"]["id"]
    assert st["round"]["options"] == ["B", "D"] and st["clients"]["phones"] >= 5

    acks = [await p.sio.call("vote", {"round_id": rid, "choice": c}, timeout=5) for p, c in zip(phones, "DDDBD")]
    assert all(a["ok"] for a in acks)
    # a phone that reconnects gets its vote back
    again = await phones[3].sio.call("hello", {"device_id": "phone-3"}, timeout=5)
    assert again["round_id"] == rid and again["choice"] == "B"
    await board.wait(lambda s: s["round"] and s["round"]["total"] == 5)
    assert board.state["round"]["counts"] is None  # blind voting

    await admin.admin("close")
    st = await board.wait(lambda s: s["phase"] == "VOTING" and s["ply"] == 2, timeout=8)
    assert st["path"] == ["A", "D", "E"]
    assert board.searches and board.searches[-1]["best"] == "E"
    assert admin.status and "bridge" in admin.status

    # illegal and stale votes are rejected
    bad = await phones[0].sio.call("vote", {"round_id": rid, "choice": "B"}, timeout=5)
    assert not bad["ok"]
    await admin.admin("override", cell="H")
    st = await board.wait(lambda s: s["phase"] == "GAME_OVER")
    assert st["result"]["value"] == 1

    export = await admin.admin("export")
    assert export["rounds"][0]["counts"] == {"B": 1, "D": 4}
    deleted = await admin.admin("delete_votes")
    assert deleted["deleted"] >= 5

    for c in [admin, board, *phones]:
        await c.sio.disconnect()


async def test_bridge_api(server):
    admin = Client(server, "admin", ADMIN, "adm2")
    await admin.connect()
    async with httpx.AsyncClient(base_url=server, timeout=10) as http:
        r = await http.get("/bridge/next", params={"timeout": 0.1})
        assert r.status_code == 401
        h = {"Authorization": f"Bearer {BRIDGE}"}
        # drain anything queued by earlier tests
        last = 0
        while True:
            r = await http.get("/bridge/next", params={"timeout": 0.1, "last": last}, headers=h)
            if r.status_code == 204:
                break
            last = r.json()["cmd_id"]
            await http.post("/bridge/result", json={"cmd_id": last, "status": "done"}, headers=h)
        res = await admin.admin("robot", cmd="say", text="Hello class")
        r = await http.get("/bridge/next", params={"timeout": 1, "last": last}, headers=h)
        assert r.status_code == 200
        cmd = r.json()
        assert cmd["cmd"] == "say" and cmd["args"] == {"text": "Hello class", "hall": True} and cmd["cmd_id"] == res["cmd_id"]
        # not acknowledged yet: polling again with the same `last` returns it again
        r2 = await http.get("/bridge/next", params={"timeout": 0.1, "last": last}, headers=h)
        assert r2.json()["cmd_id"] == cmd["cmd_id"]
        r = await http.post("/bridge/result", json={"cmd_id": cmd["cmd_id"], "status": "done", "battery": 64,
                                                    "warnings": ["LHipPitch hot"]}, headers=h)
        assert r.status_code == 200
        r = await http.get("/bridge/next", params={"timeout": 0.1, "last": cmd["cmd_id"]}, headers=h)
        assert r.status_code == 204
        r = await http.post("/bridge/heartbeat", json={"battery": 63, "driver_ok": True}, headers=h)
        assert r.json()["ok"]
        await asyncio.sleep(0.7)
        assert admin.status["bridge"]["online"] and admin.status["bridge"]["battery"] == 63
        # stop goes to the priority channel
        await admin.admin("robot", cmd="stop")
        r = await http.get("/bridge/next", params={"timeout": 1, "channel": "priority"}, headers=h)
        assert r.json()["cmd"] == "stop"
        # validation of admin robot commands
        res = await admin.sio.call("admin:robot", {"cmd": "nudge", "dx": 2}, timeout=5)
        assert not res["ok"]
        res = await admin.sio.call("admin:robot", {"cmd": "animate", "name": "moonwalk"}, timeout=5)
        assert not res["ok"]
        r = await http.get("/admin/api/export", params={"token": "nope"})
        assert r.status_code == 401
        r = await http.get("/admin/api/export", params={"token": ADMIN})
        assert r.status_code == 200 and "games" in r.json()
    await admin.sio.disconnect()


async def test_board_phone_count_follows_connects(server):
    board = Client(server, "board", device="count-board")
    await board.connect()
    before = (await board.wait(lambda s: True))["clients"]["phones"]
    phone = Client(server, device="count-phone")
    await phone.connect()
    await board.wait(lambda s: s["clients"]["phones"] == before + 1)
    await phone.sio.disconnect()
    await board.wait(lambda s: s["clients"]["phones"] == before)
    await board.sio.disconnect()
