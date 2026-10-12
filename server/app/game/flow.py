"""Game flow: phases, voting rounds, timers and the robot's turns.

    LOBBY --start--> VOTING --close/timeout--> ROBOT_MOVING --done--> THINKING --> ROBOT_MOVING --done--> VOTING ...
                                                                                              '--> GAME_OVER

The server is authoritative. Phones and the board only display snapshots and
send votes. Every automatic step has an admin override.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .engine import MAX, MIN, VARIANTS, IllegalMove, Setup, State, make_setup, other, replay
from .search import build_tree, search

log = logging.getLogger("flow")

LOBBY = "LOBBY"
VOTING = "VOTING"
THINKING = "THINKING"
ROBOT_MOVING = "ROBOT_MOVING"
GAME_OVER = "GAME_OVER"

COLORS = {MAX: "#2563eb", MIN: "#ea580c"}
SIDES = ("MAX", "MIN", "BOTH")
SPOKEN = {1: "plus one", 0: "zero", -1: "minus one"}


class Emitter(Protocol):
    def state(self) -> None: ...
    def tree(self) -> None: ...
    def search(self, payload: dict[str, Any]) -> None: ...


@dataclass
class Settings:
    vote_seconds: int = 20
    auto_timer: bool = True          # False: rounds open without a countdown, admin closes them
    tie_break: str = "random"        # 'random' | 'first' | 'admin'
    algorithm: str = "minimax"       # 'minimax' | 'alphabeta'
    step_ms: int = 350               # board animation speed per trace event
    think_min_s: float = 3.0
    think_max_s: float = 20.0
    values: str = "hidden"           # board fog: 'hidden' | 'leaves' | 'all'
    live_votes: bool = False         # show counts during voting (blind by default)
    sim_move_s: float = 1.6          # no-robot mode: board animation time per move
    speech: bool = True

    CHOICES = {"tie_break": ("random", "first", "admin"), "algorithm": ("minimax", "alphabeta"),
               "values": ("hidden", "leaves", "all")}
    LIMITS = {"vote_seconds": (3, 600), "step_ms": (20, 3000), "think_min_s": (0, 60),
              "think_max_s": (1, 120), "sim_move_s": (0, 10)}

    def update(self, changes: dict[str, Any]) -> None:
        for key, value in changes.items():
            if key not in self.__dataclass_fields__:
                raise ValueError(f"unknown setting {key!r}")
            current = getattr(self, key)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, (int, float)):
                value = type(current)(value)
                lo, hi = self.LIMITS.get(key, (None, None))
                if lo is not None and not lo <= value <= hi:
                    raise ValueError(f"{key} must be between {lo} and {hi}")
            if key in self.CHOICES and value not in self.CHOICES[key]:
                raise ValueError(f"{key} must be one of {self.CHOICES[key]}")
            setattr(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Round:
    id: int
    ply: int
    player: str
    team: str                        # 'class' | 'left' | 'right'
    options: list[str]
    ends_at: float | None            # epoch seconds, None while paused
    remaining: float | None = None   # seconds left while paused
    votes: dict[str, str] = field(default_factory=dict)  # device hash -> choice
    closed: bool = False
    winner: str | None = None
    tie: list[str] | None = None     # set when waiting for the admin to break a tie

    def counts(self) -> dict[str, int]:
        c = {o: 0 for o in self.options}
        for choice in self.votes.values():
            c[choice] = c.get(choice, 0) + 1
        return c

    def public(self, show_counts: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "ply": self.ply,
            "player": self.player,
            "team": self.team,
            "options": self.options,
            "ends_at": int(self.ends_at * 1000) if self.ends_at else None,
            "remaining_ms": int(self.remaining * 1000) if self.remaining is not None else None,
            "paused": self.ends_at is None and not self.closed,
            "closed": self.closed,
            "winner": self.winner,
            "tie": self.tie,
            "total": len(self.votes),
            "counts": self.counts() if (show_counts or self.closed) else None,
        }


@dataclass
class Quiz:
    id: int
    question: str
    options: list[str]
    answer: str | None
    open: bool = True
    revealed: bool = False
    votes: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        c = {o: 0 for o in self.options}
        for choice in self.votes.values():
            c[choice] = c.get(choice, 0) + 1
        return c

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "options": self.options,
            "open": self.open,
            "revealed": self.revealed,
            "total": len(self.votes),
            "counts": None if self.open else self.counts(),
            "answer": self.answer if self.revealed else None,
        }


_last_id = 0


def new_id() -> int:
    """Increasing ids based on the clock, unique across restarts."""
    global _last_id
    _last_id = max(_last_id + 1, int(time.time() * 1000))
    return _last_id


class Flow:
    def __init__(self, db, robot, emit: Emitter, settings: Settings | None = None) -> None:
        self.db = db
        self.robot = robot
        self.emit = emit
        self.settings = settings or Settings()
        self.setup: Setup = VARIANTS["1"]
        self.class_side = "MAX"
        self.state = State(self.setup)
        self.game_id: int | None = None
        self.phase = LOBBY
        self.round: Round | None = None
        self.quiz: Quiz | None = None
        self.result: dict[str, Any] | None = None
        self.moving: dict[str, Any] | None = None
        self.search: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []
        self.robot_cell: str = self.setup.start
        self.robot_error: str | None = None
        self.restored = False
        self._tree_cache: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None
        self._timer: asyncio.Task | None = None
        self._skip = asyncio.Event()
        self._move_done: asyncio.Future | None = None
        self._eyes: str | None = None  # last eye colour sent, to skip repeats

    # helpers ---------------------------------------------------------------

    def owner(self, player: str) -> str:
        """Who decides the moves for `player`: 'class', 'robot', 'left' or 'right'."""
        if self.class_side == "BOTH":
            return "left" if player == MAX else "right"
        return "class" if player == self.class_side else "robot"

    def robot_player(self) -> str | None:
        if self.class_side == "BOTH":
            return None
        return other(self.class_side)

    async def _db(self, method: str, *args: Any) -> Any:
        if self.db is None:
            return None
        try:
            return await getattr(self.db, method)(*args)
        except Exception:  # noqa: BLE001 - the demo must go on without the database
            log.exception("database call %s failed", method)
            return None

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        task.add_done_callback(_log_task_error)
        self._task = task
        return task

    def _cancel_task(self) -> None:
        current = asyncio.current_task()
        for t in (self._task, self._timer):
            if t and not t.done() and t is not current:
                t.cancel()
        self._task = None
        self._timer = None
        if self._move_done and not self._move_done.done():
            self._move_done.cancel()
        self._move_done = None

    async def _robot(self, cmd: str, **args: Any) -> int | None:
        """Queue a robot command, only in robot mode."""
        if self.robot.mode != "robot":
            return None
        if cmd == "say" and not self.settings.speech:
            return None
        if cmd == "eyes":
            if args.get("color") == self._eyes:
                return None
            self._eyes = args.get("color")
        return await self.robot.send(cmd, **args)

    def tree(self) -> dict[str, Any]:
        if self._tree_cache is None:
            self._tree_cache = build_tree(State(self.setup))
        return self._tree_cache

    # snapshot --------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        terminal = s.is_terminal()
        search_summary = None
        if self.search:
            search_summary = {k: v for k, v in self.search.items() if k != "trace"}
        return {
            "server_time": int(time.time() * 1000),
            "phase": self.phase,
            "game": {
                "id": self.game_id,
                "setup": self.setup.to_dict(),
                "class_side": self.class_side,
                "owners": {MAX: self.owner(MAX), MIN: self.owner(MIN)},
                "colors": COLORS,
                "restored": self.restored,
                "root_value": self.tree()["value"],
            },
            "path": list(s.path),
            "cell": s.cell,
            "ply": s.ply,
            "to_move": None if terminal else s.to_move,
            "owner": None if terminal else self.owner(s.to_move),
            "options": [] if terminal else s.legal_moves(),
            "round": self.round.public(self.settings.live_votes) if self.round else None,
            "moving": self.moving,
            "result": self.result,
            "search": search_summary,
            "history": self.history,
            "settings": self.settings.to_dict(),
            "robot": {"mode": self.robot.mode, "online": self.robot.online, "cell": self.robot_cell,
                      "error": self.robot_error},
            "quiz": self.quiz.public() if self.quiz else None,
        }

    # game lifecycle --------------------------------------------------------

    async def new_game(self, variant: str | None = "1", class_side: str = "MAX",
                       cups: list[str] | None = None, start: str | None = None) -> None:
        if class_side not in SIDES:
            raise ValueError(f"class_side must be one of {SIDES}")
        setup = make_setup(variant, cups, start)
        self._cancel_task()
        await self.robot.cancel_pending("new game")
        if self.game_id and self.phase != GAME_OVER:
            await self._db("finish_game", self.game_id, None, "abandoned")
        self.setup = setup
        self.class_side = class_side
        self.state = State(setup)
        self.game_id = new_id()
        self.phase = LOBBY
        self.round = None
        self.result = None
        self.moving = None
        self.search = None
        self.history = []
        self.robot_cell = setup.start
        self.robot_error = None
        self.restored = False
        self._tree_cache = None
        self._eyes = None
        await self._db("create_game", self.game_id, setup.to_dict(), class_side)
        await self._robot("sync", cell=setup.start)
        log.info("new game %s: %s, class plays %s", self.game_id, setup.to_dict(), class_side)
        self.emit.tree()
        self.emit.state()

    async def restore(self) -> bool:
        """Load the active game from the database by replaying its moves. Resumes in LOBBY."""
        game = await self._db("active_game")
        if not game:
            return False
        try:
            if game["variant"] in VARIANTS:
                setup = VARIANTS[game["variant"]]
            else:
                setup = make_setup(None, game["cups"].split(","), game["start_cell"])
            state = replay(setup, [m["to_cell"] for m in game["moves"]])
        except (ValueError, IllegalMove):
            log.exception("could not restore game %s", game.get("id"))
            return False
        self.setup = setup
        self.class_side = game["class_side"] if game["class_side"] in SIDES else "MAX"
        self.state = state
        self.game_id = game["id"]
        self.history = [{"ply": m["ply"], "player": m["player"], "from": m["from_cell"], "to": m["to_cell"],
                         "source": m["source"]} for m in game["moves"]]
        self.phase = LOBBY
        self.robot_cell = state.cell
        self.restored = True
        self._tree_cache = None
        log.info("restored game %s at %s", self.game_id, state.node_id)
        return True

    async def start(self) -> None:
        """LOBBY -> the first (or, after a restore, the next) turn."""
        if self.phase != LOBBY:
            return
        self.restored = False
        await self._advance()

    async def _advance(self) -> None:
        if self.state.is_terminal():
            await self._game_over()
            return
        owner = self.owner(self.state.to_move)
        if owner == "robot":
            self._spawn(self._robot_turn())
        else:
            await self._open_round()

    async def _game_over(self) -> None:
        s = self.state
        value = s.utility()
        winner = MAX if value == 1 else MIN if value == -1 else None
        self.phase = GAME_OVER
        self.round = None
        self.result = {"value": value, "winner": winner, "owner": self.owner(winner) if winner else None,
                       "kind": s.terminal_kind(), "cell": s.cell}
        await self._db("finish_game", self.game_id, value)
        self.emit.state()
        robot = self.robot_player()
        if winner is None:
            await self._robot("eyes", color="#ffffff")
            await self._robot("say", text="It is a draw. There are no legal moves left.")
        elif robot is None:
            side = "left" if winner == MAX else "right"
            await self._robot("eyes", color=COLORS[winner])
            await self._robot("say", text=f"{winner} wins. Well played, {side} side of the hall!")
            await self._robot("animate", name="win")
        elif winner == robot:
            await self._robot("eyes", color=COLORS[robot])
            await self._robot("say", text="I win. You should have looked one move further ahead.")
            await self._robot("animate", name="win")
        else:
            await self._robot("eyes", color=COLORS[winner])
            await self._robot("say", text="You win. Well played!")
            await self._robot("animate", name="lose")

    # voting ------------------------------------------------------------------

    async def _open_round(self) -> None:
        s = self.state
        player = s.to_move
        duration = float(self.settings.vote_seconds)
        rnd = Round(
            id=new_id(), ply=s.ply, player=player, team=self.owner(player), options=s.legal_moves(),
            ends_at=time.time() + duration if self.settings.auto_timer else None,
            remaining=None if self.settings.auto_timer else duration,
        )
        self.round = rnd
        self.phase = VOTING
        await self._db("open_round", rnd.id, self.game_id, rnd.ply, rnd.options)
        self.emit.state()
        await self._robot("eyes", color=COLORS[player])
        self._restart_timer()

    def _restart_timer(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        rnd = self.round
        if rnd and not rnd.closed and rnd.ends_at is not None:
            self._timer = asyncio.create_task(self._run_timer(rnd))
            self._timer.add_done_callback(_log_task_error)

    async def _run_timer(self, rnd: Round) -> None:
        await asyncio.sleep(max(0.0, rnd.ends_at - time.time()))
        if self.round is rnd and not rnd.closed:
            self._timer = None
            await self.close_round()

    async def vote(self, device_hash: str, round_id: int, choice: str, team: str | None = None) -> tuple[bool, str | None]:
        rnd = self.round
        if self.phase != VOTING or rnd is None or rnd.closed:
            return False, "voting is closed"
        if rnd.id != round_id:
            return False, "this round is over"
        if choice not in rnd.options:
            return False, "not a legal move"
        if rnd.team in ("left", "right") and team != rnd.team:
            return False, f"it is the {rnd.team} side's turn"
        rnd.votes[device_hash] = choice
        await self._db("upsert_vote", rnd.id, device_hash, choice)
        self.emit.state()
        return True, None

    def my_vote(self, device_hash: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.round:
            out["round_id"] = self.round.id
            out["choice"] = self.round.votes.get(device_hash)
        if self.quiz:
            out["quiz_id"] = self.quiz.id
            out["quiz_choice"] = self.quiz.votes.get(device_hash)
        return out

    async def close_round(self, override: str | None = None) -> None:
        """Close the open round. `override` is a move chosen by the admin (also used for a show of hands)."""
        rnd = self.round
        if self.phase != VOTING or rnd is None:
            raise ValueError("no voting round in progress")
        if rnd.closed and rnd.tie is None:
            return
        if override is not None and override not in rnd.options:
            raise ValueError(f"{override} is not a legal move")
        if self._timer and self._timer is not asyncio.current_task() and not self._timer.done():
            self._timer.cancel()
        self._timer = None
        counts = rnd.counts()
        if rnd.ends_at is not None:
            rnd.remaining = None
        rnd.ends_at = None
        rnd.closed = True
        source = "vote"
        if override is not None:
            winner = override
            source = "admin"
        else:
            top = max(counts.values()) if counts else 0
            leaders = sorted(o for o, c in counts.items() if c == top)
            if len(leaders) == 1:
                winner = leaders[0]
            elif self.settings.tie_break == "first":
                winner = leaders[0]
            elif self.settings.tie_break == "random":
                winner = random.choice(leaders)
            else:
                rnd.tie = leaders
                log.info("round %s tied between %s, waiting for admin", rnd.id, leaders)
                self.emit.state()
                return
        rnd.tie = None
        rnd.winner = winner
        await self._db("close_round", rnd.id, winner, counts)
        self.emit.state()
        self._spawn(self._execute(winner, rnd.player, source))

    async def extend(self, seconds: float = 10) -> None:
        rnd = self.round
        if self.phase != VOTING or rnd is None or rnd.closed:
            raise ValueError("no open voting round")
        if rnd.ends_at is None:
            rnd.remaining = (rnd.remaining or 0) + seconds
        else:
            rnd.ends_at += seconds
        self._restart_timer()
        self.emit.state()

    async def pause_timer(self) -> None:
        rnd = self.round
        if self.phase != VOTING or rnd is None or rnd.closed or rnd.ends_at is None:
            return
        rnd.remaining = max(0.0, rnd.ends_at - time.time())
        rnd.ends_at = None
        self._restart_timer()
        self.emit.state()

    async def resume_timer(self) -> None:
        rnd = self.round
        if self.phase != VOTING or rnd is None or rnd.closed or rnd.ends_at is not None:
            return
        rnd.ends_at = time.time() + (rnd.remaining if rnd.remaining is not None else self.settings.vote_seconds)
        rnd.remaining = None
        self._restart_timer()
        self.emit.state()

    # moves -------------------------------------------------------------------

    async def _execute(self, cell: str, player: str, source: str, speech: str | None = None) -> None:
        """Commit a move to the game, then have the robot (or the board, in no-robot mode) walk it."""
        prev = self.state
        self.state = prev.apply(cell)
        move = {"ply": prev.ply, "player": player, "from": prev.cell, "to": cell, "source": source}
        self.history.append(move)
        await self._db("add_move", self.game_id, prev.ply, player, prev.cell, cell, source)
        owner = self.owner(player)
        if speech is None:
            speech = {
                "class": f"You chose {cell}. Let me walk there.",
                "left": f"The left side chose {cell}. Walking there.",
                "right": f"The right side chose {cell}. Walking there.",
                "robot": f"I go to {cell}.",
            }[owner]
        await self._walk(prev.cell, cell, player, speech)

    async def _walk(self, frm: str, to: str, player: str, speech: str | None) -> None:
        self.phase = ROBOT_MOVING
        self.robot_error = None
        self.moving = {"from": frm, "to": to, "player": player, "owner": self.owner(player),
                       "mode": self.robot.mode, "started": int(time.time() * 1000),
                       "duration_ms": int(self.settings.sim_move_s * 1000), "failed": False}
        self._move_done = asyncio.get_running_loop().create_future()
        self.emit.state()
        if self.robot.mode == "robot":
            await self._robot("eyes", color=COLORS[player])
            if speech:
                await self._robot("say", text=speech)
            asyncio.create_task(self._watch_robot_move(self.robot_cell, to, self._move_done)) \
                .add_done_callback(_log_task_error)
        else:
            asyncio.create_task(self._sim_move(self._move_done)).add_done_callback(_log_task_error)
        done = self._move_done
        await done
        self._move_done = None
        self.robot_cell = to
        self.moving = None
        await self._advance()

    async def _sim_move(self, done: asyncio.Future) -> None:
        await asyncio.sleep(self.settings.sim_move_s)
        if not done.done():
            done.set_result("sim")

    async def _watch_robot_move(self, frm: str, to: str, done: asyncio.Future) -> None:
        cmd_id = await self.robot.send("move", **{"from": frm, "to": to})
        if self.moving is not None:
            self.moving["cmd_id"] = cmd_id
        result = await self.robot.wait(cmd_id)
        if done.done():
            return
        if result["status"] == "done":
            done.set_result("robot")
            return
        # The move stays pending. The admin can retry, resync ("Robot is at X") or switch to no-robot mode.
        self.robot_error = f"move {frm}->{to} failed: {result.get('error') or 'unknown error'}"
        if self.moving is not None:
            self.moving["failed"] = True
        log.warning(self.robot_error)
        self.emit.state()

    async def retry_move(self) -> None:
        if self.phase != ROBOT_MOVING or not self.moving or not self._move_done:
            raise ValueError("no robot move in progress")
        self.robot_error = None
        self.moving["failed"] = False
        self.emit.state()
        if self.robot.mode == "robot":
            asyncio.create_task(self._watch_robot_move(self.robot_cell, self.moving["to"], self._move_done)) \
                .add_done_callback(_log_task_error)
        else:
            self._move_done.set_result("sim")

    async def robot_at(self, cell: str) -> None:
        """Admin: the robot is physically at `cell`. Completes a pending move if that was its target."""
        cell = cell.upper()
        if cell not in "ABCDEFGHI" or len(cell) != 1:
            raise ValueError("not a cell")
        self.robot_cell = cell
        await self._robot("sync", cell=cell)
        if self.phase == ROBOT_MOVING and self.moving and self.moving["to"] == cell and self._move_done \
                and not self._move_done.done():
            self._move_done.set_result("admin")
        self.emit.state()

    # robot's turn ------------------------------------------------------------

    async def _robot_turn(self) -> None:
        s = self.state
        player = s.to_move
        self.phase = THINKING
        self._skip.clear()
        result = search(s, self.settings.algorithm)
        self.search = self._search_payload(result, replay=False)
        self.emit.search(self.search)
        self.emit.state()
        await self._robot("eyes", color=COLORS[player])
        await self._robot("say", text="My turn. Let me think.")
        await self._robot("animate", name="think")
        try:
            await asyncio.wait_for(self._skip.wait(), self.search["duration_ms"] / 1000)
        except asyncio.TimeoutError:
            pass
        best = result["best"]
        speech = self._decision_line(result, player)
        await self._execute(best, player, self.settings.algorithm, speech)

    def _decision_line(self, result: dict[str, Any], player: str) -> str:
        n = result["stats"]["nodes_visited"]
        v = result["value"]
        mine = v if player == MAX else -v  # value from the robot's point of view
        line = f"I evaluated {n} positions. I go to {result['best']}. Value: {SPOKEN[v]}."
        if mine < 0:
            line += " I already know I will lose, if you play perfectly. But maybe you will make a mistake."
        elif mine > 0:
            line += " Bad news for you."
        else:
            line += " A draw, if we both play well."
        return line

    def _search_payload(self, result: dict[str, Any], replay: bool) -> dict[str, Any]:
        # The animation plus a short pause at the end must fit in think_max_s.
        n = max(1, len(result["trace"]))
        budget_ms = max(0.0, self.settings.think_max_s * 1000 - 800)
        step = min(self.settings.step_ms, max(20, int(budget_ms / n)))
        duration = n * step / 1000 + 0.8
        duration = min(max(duration, self.settings.think_min_s), self.settings.think_max_s)
        return {**result, "replay": replay, "step_ms": step, "duration_ms": int(duration * 1000),
                "started_at": int(time.time() * 1000)}

    async def skip_thinking(self) -> None:
        self._skip.set()

    async def override(self, cell: str) -> None:
        """Admin picks the move: the winning vote during VOTING, or the robot's move during THINKING."""
        cell = cell.upper()
        if self.phase == VOTING:
            await self.close_round(override=cell)
        elif self.phase == THINKING:
            if cell not in self.state.legal_moves():
                raise ValueError(f"{cell} is not a legal move")
            self._cancel_task()
            player = self.state.to_move
            self._spawn(self._execute(cell, player, "admin", f"I go to {cell}."))
        else:
            raise ValueError(f"cannot override a move during {self.phase}")

    # board ---------------------------------------------------------------------

    async def replay_search(self, algorithm: str | None = None, from_root: bool = True) -> None:
        """Animate a search on the board only (lecture explanation). Does not change the game."""
        state = State(self.setup) if from_root else self.state
        if state.is_terminal():
            raise ValueError("no search from a finished position")
        result = search(state, algorithm or self.settings.algorithm)
        self.search = self._search_payload(result, replay=True)
        self.emit.search(self.search)
        self.emit.state()

    async def clear_search(self) -> None:
        self.search = None
        self.emit.search({"cleared": True})
        self.emit.state()

    async def set_mode(self, mode: str) -> None:
        if mode not in ("robot", "sim"):
            raise ValueError("mode must be 'robot' or 'sim'")
        self.robot.mode = mode
        self._eyes = None
        if mode == "sim":
            await self.robot.cancel_pending("switched to no-robot mode")
            if self._move_done and not self._move_done.done():
                self._move_done.set_result("sim")
        self.robot_error = None
        self.emit.state()

    # quiz ----------------------------------------------------------------------

    async def quiz_open(self, question: str | None = None, options: list[str] | None = None,
                        answer: str | None = None, preset: str | None = None) -> None:
        if preset == "root_value":
            question = "What is the minimax value of the root?"
            options = ["-1", "0", "+1"]
            answer = {1: "+1", 0: "0", -1: "-1"}[self.tree()["value"]]
        elif preset == "best_move":
            s = self.state
            if s.is_terminal():
                raise ValueError("game is over")
            question = f"Which move is best for {s.to_move} from {s.cell}?"
            options = s.legal_moves()
            answer = search(s, "minimax", trace=False)["best"]
        if not question or not options or len(options) < 2:
            raise ValueError("a quiz needs a question and at least two options")
        options = [str(o)[:40] for o in options][:6]
        self.quiz = Quiz(id=new_id(), question=str(question)[:200], options=options, answer=answer)
        await self._db("open_round", self.quiz.id, self.game_id, None, options, "quiz", self.quiz.question)
        self.emit.state()

    async def quiz_vote(self, device_hash: str, quiz_id: int, choice: str) -> tuple[bool, str | None]:
        q = self.quiz
        if not q or not q.open or q.id != quiz_id:
            return False, "quiz is closed"
        if choice not in q.options:
            return False, "not an option"
        q.votes[device_hash] = choice
        await self._db("upsert_vote", q.id, device_hash, choice)
        self.emit.state()
        return True, None

    async def quiz_close(self) -> None:
        if self.quiz and self.quiz.open:
            self.quiz.open = False
            await self._db("close_round", self.quiz.id, self.quiz.answer, self.quiz.counts())
            self.emit.state()

    async def quiz_reveal(self) -> None:
        if self.quiz:
            await self.quiz_close()
            self.quiz.revealed = True
            self.emit.state()

    async def quiz_clear(self) -> None:
        await self.quiz_close()
        self.quiz = None
        self.emit.state()

    def shutdown(self) -> None:
        self._cancel_task()


def _log_task_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("background task failed", exc_info=task.exception())
