"""FastAPI + Socket.IO server: phone, board and admin pages, real-time state, bridge API.

Run with ONE worker; all game state lives in memory:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import socketio
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .bridge_api import make_router
from .db import Database
from .game.flow import Flow, Settings
from .robot_link import RobotLink

log = logging.getLogger("server")

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
ANIMATIONS = ("think", "win", "lose", "wave")
NO_CACHE = {"Cache-Control": "no-cache"}


@dataclass
class Config:
    admin_token: str = ""
    bridge_token: str = ""
    device_hash_secret: str = ""
    db_path: str = "data/minimax.db"
    static_dir: str = ""
    public_url: str = ""
    robot_mode: str = "sim"
    cors_origins: list[str] | None = None
    state_interval: float = 0.25   # max rate of state broadcasts (seconds)
    status_interval: float = 2.0
    bridge_poll_timeout: float = 25.0
    restore: bool = True
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        here = Path(__file__).resolve().parent
        default_static = next((str(p) for p in (here.parent / "static", here.parents[1] / "web" / "dist") if p.is_dir()), "")
        cors = os.environ.get("CORS_ORIGINS")
        return cls(
            admin_token=os.environ.get("ADMIN_TOKEN", ""),
            bridge_token=os.environ.get("BRIDGE_TOKEN", ""),
            device_hash_secret=os.environ.get("DEVICE_HASH_SECRET", ""),
            db_path=os.environ.get("DB_PATH", "data/minimax.db"),
            static_dir=os.environ.get("STATIC_DIR", default_static),
            public_url=os.environ.get("PUBLIC_URL", ""),
            robot_mode=os.environ.get("ROBOT_MODE", "sim"),
            cors_origins=[o.strip() for o in cors.split(",") if o.strip()] if cors else None,
        )


def _token(value: str, name: str) -> str:
    if value:
        return value
    generated = secrets.token_urlsafe(18)
    log.warning("%s not set; generated one for this run: %s", name, generated)
    return generated


class Hub:
    """Socket.IO side: clients, coalesced broadcasts, admin commands."""

    def __init__(self, sio: socketio.AsyncServer, cfg: Config) -> None:
        self.sio = sio
        self.cfg = cfg
        self.flow: Flow | None = None
        self.robot: RobotLink | None = None
        self.db: Database | None = None
        self.clients: dict[str, dict[str, Any]] = {}
        self._state_pending = False
        self._status_pending = False
        self._last_state = 0.0
        self._vote_times: dict[str, list[float]] = {}

    # Emitter protocol used by Flow ---------------------------------------------

    def state(self) -> None:
        if self._state_pending:
            return
        self._state_pending = True
        delay = max(0.0, self.cfg.state_interval - (time.monotonic() - self._last_state))
        asyncio.get_running_loop().call_later(delay, lambda: asyncio.ensure_future(self._flush_state()))

    async def _flush_state(self) -> None:
        self._state_pending = False
        self._last_state = time.monotonic()
        await self.sio.emit("state", self.snapshot())

    def tree(self) -> None:
        asyncio.ensure_future(self.sio.emit("tree", self.tree_payload(), room="boards"))

    def search(self, payload: dict[str, Any]) -> None:
        asyncio.ensure_future(self.sio.emit("search", payload, room="boards"))

    def hall_audio(self, cmd_id: int, audio: bytes, content_type: str) -> None:
        """The robot's speech for one say command; the board plays it through the hall speakers."""
        if self.flow.settings.hall_audio:
            payload = {"cmd_id": cmd_id, "audio": audio, "type": content_type}
            asyncio.ensure_future(self.sio.emit("speak", payload, room="boards"))

    def status(self) -> None:
        if self._status_pending:
            return
        self._status_pending = True
        asyncio.get_running_loop().call_later(0.1, lambda: asyncio.ensure_future(self._flush_status()))

    async def _flush_status(self) -> None:
        self._status_pending = False
        await self.sio.emit("status", self.status_payload(), room="admins")

    # payloads --------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        snap = self.flow.snapshot()
        snap["clients"] = self.counts()
        return snap

    def tree_payload(self) -> dict[str, Any]:
        return {"tree": self.flow.tree(), "search": self.flow.search, "server_time": int(time.time() * 1000)}

    def counts(self) -> dict[str, int]:
        phones = {c["hash"] for c in self.clients.values() if c["role"] == "phone" and c.get("hash")}
        return {
            "phones": len(phones),
            "boards": sum(1 for c in self.clients.values() if c["role"] == "board"),
            "admins": sum(1 for c in self.clients.values() if c["role"] == "admin"),
        }

    def status_payload(self) -> dict[str, Any]:
        rnd = self.flow.round
        quiz = self.flow.quiz
        # Admins see live counts even while voting is blind for everyone else.
        return {
            "server_time": int(time.time() * 1000),
            "bridge": self.robot.status(),
            "clients": self.counts(),
            "round_votes": len(rnd.votes) if rnd else 0,
            "round_counts": rnd.counts() if rnd else None,
            "quiz_votes": len(quiz.votes) if quiz else 0,
            "quiz_counts": quiz.counts() if quiz else None,
            "db_path": self.cfg.db_path,
        }

    def rate_ok(self, sid: str, limit: int = 8) -> bool:
        now = time.monotonic()
        times = [t for t in self._vote_times.get(sid, []) if now - t < 1.0]
        times.append(now)
        self._vote_times[sid] = times
        return len(times) <= limit


def create_app(cfg: Config | None = None) -> socketio.ASGIApp:
    cfg = cfg or Config.from_env()
    cfg.admin_token = _token(cfg.admin_token, "ADMIN_TOKEN")
    cfg.bridge_token = _token(cfg.bridge_token, "BRIDGE_TOKEN")
    if not cfg.device_hash_secret:
        log.warning("DEVICE_HASH_SECRET not set; device hashes will change after a restart")
        cfg.device_hash_secret = secrets.token_hex(16)

    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=cfg.cors_origins if cfg.cors_origins is not None else None,
        ping_interval=20,
        ping_timeout=20,
        max_http_buffer_size=64 * 1024,
    )
    hub = Hub(sio, cfg)
    robot = RobotLink(on_change=hub.status)
    robot.mode = cfg.robot_mode if cfg.robot_mode in ("robot", "sim") else "sim"
    db = Database(cfg.db_path, cfg.device_hash_secret)
    settings = Settings()
    settings.update(cfg.settings)
    flow = Flow(db, robot, hub, settings)
    hub.flow, hub.robot, hub.db = flow, robot, db

    def robot_on_change() -> None:
        hub.status()
        hub.state()

    robot.on_change = robot_on_change

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if cfg.db_path != ":memory:":
            Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
        await db.open()
        restored = cfg.restore and await flow.restore()
        if not restored:
            await flow.new_game("1", "MAX")
        status_task = asyncio.create_task(status_loop())
        log.info("server ready (static: %s, db: %s, robot mode: %s)", cfg.static_dir or "-", cfg.db_path, robot.mode)
        try:
            yield
        finally:
            status_task.cancel()
            flow.shutdown()
            await db.close()

    async def status_loop() -> None:
        was_online = robot.online
        while True:
            await asyncio.sleep(cfg.status_interval)
            if robot.online != was_online:
                was_online = robot.online
                hub.state()
            await sio.emit("status", hub.status_payload(), room="admins")

    api = FastAPI(title="Robot minimax demo", lifespan=lifespan, docs_url=None, redoc_url=None)
    api.state.hub = hub
    api.include_router(make_router(robot, cfg.bridge_token, cfg.bridge_poll_timeout, on_audio=hub.hall_audio))

    @api.get("/healthz")
    async def healthz():
        return {"ok": True, "phase": flow.phase, "bridge_online": robot.online, "clients": hub.counts()}

    @api.get("/admin/api/export")
    async def export(token: str | None = Query(None), authorization: str | None = Header(None)):
        given = token or (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), cfg.admin_token.encode()):
            raise HTTPException(status_code=401, detail="bad admin token")
        return JSONResponse(await db.export(), headers={"Content-Disposition": "attachment; filename=minimax-stats.json"})

    static = Path(cfg.static_dir) if cfg.static_dir else None

    def page(name: str):
        if static and (static / name).is_file():
            return FileResponse(static / name, headers=NO_CACHE)
        return HTMLResponse(f"<p>Frontend not built. Run <code>npm run build</code> in web/ ({name}).</p>", status_code=503)

    @api.get("/")
    async def phone_page():
        return page("index.html")

    @api.get("/board")
    async def board_page():
        return page("board.html")

    @api.get("/admin")
    async def admin_page():
        return page("admin.html")

    if static and (static / "assets").is_dir():
        api.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")
    if static:
        for extra in ("favicon.svg", "robots.txt"):
            if (static / extra).is_file():
                api.add_api_route(f"/{extra}", lambda p=static / extra: FileResponse(p), methods=["GET"])

    # Socket.IO -------------------------------------------------------------------

    @sio.event
    async def connect(sid, environ, auth):
        auth = auth if isinstance(auth, dict) else {}
        role = auth.get("role", "phone")
        if role not in ("phone", "board", "admin"):
            role = "phone"
        if role == "admin":
            token = str(auth.get("token", ""))
            if not hmac.compare_digest(token.encode(), cfg.admin_token.encode()):
                raise socketio.exceptions.ConnectionRefusedError("bad admin token")
        hub.clients[sid] = {"role": role, "hash": None, "team": None}
        await sio.enter_room(sid, role + "s")

    @sio.event
    async def disconnect(sid, *args):
        hub.clients.pop(sid, None)
        hub._vote_times.pop(sid, None)
        hub.status()
        hub.state()  # the board shows the phone count from the state snapshot

    @sio.event
    async def hello(sid, data):
        client = hub.clients.get(sid)
        if client is None:
            return {"ok": False, "error": "not connected"}
        data = data if isinstance(data, dict) else {}
        device_id = str(data.get("device_id") or sid)[:64]
        client["hash"] = db.device_hash(device_id)
        team = data.get("team")
        client["team"] = team if team in ("left", "right") else None
        await sio.emit("state", hub.snapshot(), to=sid)
        if client["role"] in ("board", "admin"):
            await sio.emit("tree", hub.tree_payload(), to=sid)
        if client["role"] == "admin":
            await sio.emit("status", hub.status_payload(), to=sid)
        hub.status()
        hub.state()
        return {"ok": True, "server_time": int(time.time() * 1000), "public_url": cfg.public_url,
                **flow.my_vote(client["hash"])}

    @sio.event
    async def vote(sid, data):
        client = hub.clients.get(sid)
        if not client or not client.get("hash"):
            return {"ok": False, "error": "say hello first"}
        if not hub.rate_ok(sid):
            return {"ok": False, "error": "too many votes"}
        try:
            round_id = int(data["round_id"])
            choice = str(data["choice"]).upper()[:1]
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "bad vote"}
        team = data.get("team") if data.get("team") in ("left", "right") else client.get("team")
        if team:
            client["team"] = team
        ok, err = await flow.vote(client["hash"], round_id, choice, team)
        if ok:
            hub.status()
        return {"ok": ok, "choice": choice if ok else None, "round_id": round_id, "error": err}

    @sio.event
    async def quiz_vote(sid, data):
        client = hub.clients.get(sid)
        if not client or not client.get("hash"):
            return {"ok": False, "error": "say hello first"}
        if not hub.rate_ok(sid):
            return {"ok": False, "error": "too many votes"}
        try:
            quiz_id = int(data["quiz_id"])
            choice = str(data["choice"])[:40]
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "bad vote"}
        ok, err = await flow.quiz_vote(client["hash"], quiz_id, choice)
        if ok:
            hub.status()
        return {"ok": ok, "choice": choice if ok else None, "quiz_id": quiz_id, "error": err}

    # admin commands ----------------------------------------------------------------

    async def robot_command(args: dict[str, Any]) -> Any:
        cmd = args.get("cmd")
        if cmd == "stop":
            return {"cmd_id": await robot.stop()}
        if cmd == "say":
            text = str(args.get("text", "")).strip()[:300]
            if not text:
                raise ValueError("nothing to say")
            return {"cmd_id": await robot.send("say", text=text, hall=flow.settings.hall_audio)}
        if cmd == "animate":
            if args.get("name") not in ANIMATIONS:
                raise ValueError(f"animation must be one of {ANIMATIONS}")
            return {"cmd_id": await robot.send("animate", name=args["name"])}
        if cmd == "eyes":
            color = str(args.get("color", ""))
            if not HEX_COLOR.match(color):
                raise ValueError("color must look like #rrggbb")
            flow._eyes = color  # keep the flow's "last colour sent" in step
            return {"cmd_id": await robot.send("eyes", color=color)}
        if cmd == "posture":
            if args.get("posture") not in ("stand", "rest"):
                raise ValueError("posture must be stand or rest")
            return {"cmd_id": await robot.send("posture", posture=args["posture"])}
        if cmd == "nudge":
            dx, dy, dtheta = (float(args.get(k, 0)) for k in ("dx", "dy", "dtheta"))
            if max(abs(dx), abs(dy)) > 0.15 or abs(dtheta) > 45:
                raise ValueError("nudge too large (max 0.15 m, 45 degrees)")
            return {"cmd_id": await robot.send("nudge", dx=dx, dy=dy, dtheta=dtheta)}
        raise ValueError(f"unknown robot command {cmd!r}")

    async def reset(_args):
        await flow.new_game(flow.setup.name if flow.setup.name in ("1", "2") else None, flow.class_side,
                            sorted(flow.setup.cups) if flow.setup.name == "custom" else None,
                            flow.setup.start if flow.setup.name == "custom" else None)

    admin_commands = {
        "new_game": lambda a: flow.new_game(a.get("variant"), a.get("class_side", "MAX"), a.get("cups"), a.get("start")),
        "reset": reset,
        "start": lambda a: flow.start(),
        "close": lambda a: flow.close_round(),
        "extend": lambda a: flow.extend(float(a.get("seconds", 10))),
        "pause": lambda a: flow.pause_timer(),
        "resume": lambda a: flow.resume_timer(),
        "override": lambda a: flow.override(str(a["cell"])),
        "skip": lambda a: flow.skip_thinking(),
        "settings": lambda a: _settings(a),
        "replay": lambda a: flow.replay_search(a.get("algorithm"), bool(a.get("from_root", True))),
        "clear_search": lambda a: flow.clear_search(),
        "mode": lambda a: flow.set_mode(str(a.get("mode"))),
        "robot": robot_command,
        "robot_at": lambda a: flow.robot_at(str(a["cell"])),
        "retry_move": lambda a: flow.retry_move(),
        "quiz_open": lambda a: flow.quiz_open(a.get("question"), a.get("options"), a.get("answer"), a.get("preset")),
        "quiz_close": lambda a: flow.quiz_close(),
        "quiz_reveal": lambda a: flow.quiz_reveal(),
        "quiz_clear": lambda a: flow.quiz_clear(),
        "export": lambda a: db.export(),
        "delete_votes": lambda a: _delete_votes(),
    }

    async def _settings(changes: dict[str, Any]) -> None:
        settings.update(changes)
        hub.state()

    async def _delete_votes() -> dict[str, int]:
        return {"deleted": await db.delete_votes()}

    def register(name: str, fn):
        async def handler(sid, data=None):
            if hub.clients.get(sid, {}).get("role") != "admin":
                return {"ok": False, "error": "not an admin"}
            try:
                result = await fn(data if isinstance(data, dict) else {})
                log.info("admin %s %s", name, data)
                return {"ok": True, "result": result}
            except (ValueError, KeyError, TypeError) as e:
                return {"ok": False, "error": str(e)}
            except Exception as e:  # noqa: BLE001
                log.exception("admin command %s failed", name)
                return {"ok": False, "error": f"server error: {e}"}

        sio.on(f"admin:{name}", handler)

    for name, fn in admin_commands.items():
        register(name, fn)

    return socketio.ASGIApp(sio, other_asgi_app=api, socketio_path="socket.io")


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


_setup_logging()
app = create_app() if os.environ.get("MINIMAX_NO_APP") != "1" else None
