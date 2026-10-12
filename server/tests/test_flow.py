import asyncio
import time

import pytest

from app.db import Database
from app.game.flow import GAME_OVER, LOBBY, ROBOT_MOVING, THINKING, VOTING, Flow, Settings
from app.robot_link import RobotLink


class FakeEmitter:
    def __init__(self):
        self.states = 0
        self.trees = 0
        self.searches = []

    def state(self):
        self.states += 1

    def tree(self):
        self.trees += 1

    def search(self, payload):
        self.searches.append(payload)


async def until(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def fast_settings(**kw):
    s = Settings(think_min_s=0, think_max_s=0.05, step_ms=20, sim_move_s=0)
    s.vote_seconds = 60
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"), "secret")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
async def flow(db):
    f = Flow(db, RobotLink(), FakeEmitter(), fast_settings())
    await f.new_game("1", "MAX")
    yield f
    f.shutdown()


async def vote_all(flow, votes):
    for i, choice in enumerate(votes):
        ok, err = await flow.vote(f"dev{i}", flow.round.id, choice)
        assert ok, err


async def test_full_game_variant1_class_wins(flow):
    await flow.start()
    await until(lambda: flow.phase == VOTING)
    assert flow.round.options == ["B", "D"] and flow.round.team == "class"
    await vote_all(flow, ["D", "D", "D", "B"])
    await flow.close_round()
    # Robot (MIN) at AD: both E and G lose for MIN, so it takes the first one alphabetically.
    await until(lambda: flow.phase == VOTING and flow.state.ply == 2)
    assert flow.state.path == ("A", "D", "E")
    assert flow.history[1]["source"] == "minimax"
    assert flow.emit.searches[-1]["best"] == "E"
    await vote_all(flow, ["H"])
    await flow.close_round()
    await until(lambda: flow.phase == GAME_OVER)
    assert flow.result["value"] == 1 and flow.result["owner"] == "class"
    export = await flow.db.export()
    assert export["games"][-1]["result"] == 1
    assert [m["to_cell"] for m in export["moves"]] == ["D", "E", "H"]
    assert export["rounds"][0]["counts"] == {"B": 1, "D": 3}


async def test_class_picks_b_and_robot_wins_at_once(flow):
    await flow.start()
    await vote_all(flow, ["B"])
    await flow.close_round()
    await until(lambda: flow.phase == GAME_OVER)
    assert flow.state.path == ("A", "B", "C")
    assert flow.result["owner"] == "robot" and flow.result["value"] == -1


async def test_vote_validation_and_revote(flow):
    await flow.start()
    rid = flow.round.id
    assert await flow.vote("x", rid + 1, "D") == (False, "this round is over")
    assert (await flow.vote("x", rid, "E"))[0] is False
    assert await flow.vote("x", rid, "B") == (True, None)
    assert await flow.vote("x", rid, "D") == (True, None)  # change of mind
    assert flow.round.counts() == {"B": 0, "D": 1}
    assert flow.my_vote("x") == {"round_id": rid, "choice": "D"}


async def test_timer_closes_round(flow):
    flow.settings.vote_seconds = 0.2
    await flow.start()
    rid = flow.round.id
    await vote_all(flow, ["D"])
    await until(lambda: flow.history and flow.history[0]["to"] == "D")
    assert flow.history[0]["source"] == "vote"
    assert (await flow.vote("y", rid, "B"))[0] is False


async def test_extend_pause_resume(flow):
    await flow.start()
    ends = flow.round.ends_at
    await flow.extend(10)
    assert flow.round.ends_at == pytest.approx(ends + 10)
    await flow.pause_timer()
    assert flow.round.ends_at is None and flow.round.remaining > 60
    snap = flow.snapshot()
    assert snap["round"]["paused"] is True
    await flow.resume_timer()
    assert flow.round.ends_at > time.time() + 60


async def test_tie_waits_for_admin(flow):
    flow.settings.tie_break = "admin"
    await flow.start()
    await vote_all(flow, ["B", "D"])
    await flow.close_round()
    assert flow.phase == VOTING and flow.round.tie == ["B", "D"]
    await flow.override("D")
    await until(lambda: flow.state.ply >= 1)
    assert flow.history[0] == {"ply": 0, "player": "MAX", "from": "A", "to": "D", "source": "admin"}


async def test_tie_break_first(flow):
    flow.settings.tie_break = "first"
    await flow.start()
    await flow.close_round()  # no votes at all: every option ties
    await until(lambda: flow.state.ply >= 1)
    assert flow.state.path[1] == "B"


async def test_blind_votes_hidden_until_close(flow):
    await flow.start()
    await vote_all(flow, ["D", "B", "D"])
    snap = flow.snapshot()["round"]
    assert snap["counts"] is None and snap["total"] == 3
    flow.settings.live_votes = True
    assert flow.snapshot()["round"]["counts"] == {"B": 1, "D": 2}


async def test_robot_opens_when_class_is_min(db):
    f = Flow(db, RobotLink(), FakeEmitter(), fast_settings())
    await f.new_game("2", "MIN")
    await f.start()
    await until(lambda: f.phase == VOTING)
    assert f.state.path == ("A", "B")  # robot (MAX) picks B, first among equally losing moves
    assert f.round.team == "class" and f.round.player == "MIN"
    assert "I already know I will lose" in f._decision_line(f.emit.searches[0], "MAX")
    f.shutdown()


async def test_hall_vs_hall(db):
    f = Flow(db, RobotLink(), FakeEmitter(), fast_settings())
    await f.new_game("1", "BOTH")
    await f.start()
    assert f.round.team == "left"
    assert (await f.vote("a", f.round.id, "D", "right"))[0] is False
    assert (await f.vote("a", f.round.id, "D", "left"))[0] is True
    await f.close_round()
    await until(lambda: f.phase == VOTING and f.state.ply == 1)
    assert f.round.team == "right"
    assert f.emit.searches == []  # the robot never thinks in hall vs hall
    f.shutdown()


async def test_override_robot_move_during_thinking(db):
    s = fast_settings(think_min_s=30, think_max_s=60)
    f = Flow(db, RobotLink(), FakeEmitter(), s)
    await f.new_game("1", "MAX")
    await f.start()
    await vote_all(f, ["D"])
    await f.close_round()
    await until(lambda: f.phase == THINKING)
    await f.override("G")
    await until(lambda: f.phase == VOTING)
    assert f.state.path == ("A", "D", "G") and f.history[-1]["source"] == "admin"
    f.shutdown()


async def test_skip_thinking(db):
    s = fast_settings(think_min_s=30, think_max_s=60)
    f = Flow(db, RobotLink(), FakeEmitter(), s)
    await f.new_game("1", "MAX")
    await f.start()
    await vote_all(f, ["D"])
    await f.close_round()
    await until(lambda: f.phase == THINKING)
    await f.skip_thinking()
    await until(lambda: f.phase == VOTING, timeout=2)
    f.shutdown()


class FakeBridge:
    """Pulls commands from a RobotLink like bridge.py does and answers them."""

    def __init__(self, link, fail_moves=0):
        self.link = link
        self.fail_moves = fail_moves
        self.log = []
        self.last = 0
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            cmd = await self.link.next_command(self.last, "main", timeout=0.2)
            if cmd is None:
                continue
            self.last = cmd["cmd_id"]
            self.log.append((cmd["cmd"], cmd["args"]))
            status = "done"
            if cmd["cmd"] == "move" and self.fail_moves:
                self.fail_moves -= 1
                status = "failed"
            await self.link.post_result({"cmd_id": cmd["cmd_id"], "status": status, "battery": 77,
                                         "error": "fell over" if status == "failed" else None})


async def test_robot_mode_sends_commands(db):
    link = RobotLink()
    link.mode = "robot"
    f = Flow(db, link, FakeEmitter(), fast_settings())
    bridge = FakeBridge(link)
    await f.new_game("1", "MAX")
    await f.start()
    await vote_all(f, ["D"])
    await f.close_round()
    await until(lambda: f.phase == VOTING and f.state.ply == 2)
    cmds = [c for c, _ in bridge.log]
    assert cmds[0] == "sync"
    moves = [a for c, a in bridge.log if c == "move"]
    assert moves == [{"from": "A", "to": "D"}, {"from": "D", "to": "E"}]
    says = [a["text"] for c, a in bridge.log if c == "say"]
    assert says[0] == "You chose D. Let me walk there."
    assert says[1] == "My turn. Let me think."
    assert says[2].startswith("I evaluated") and "I go to E" in says[2]
    assert ("animate", {"name": "think"}) in bridge.log
    assert link.battery == 77
    bridge.task.cancel()
    f.shutdown()


async def test_failed_move_then_resync(db):
    link = RobotLink()
    link.mode = "robot"
    f = Flow(db, link, FakeEmitter(), fast_settings())
    bridge = FakeBridge(link, fail_moves=1)
    await f.new_game("1", "MAX")
    await f.start()
    await vote_all(f, ["D"])
    await f.close_round()
    await until(lambda: f.robot_error is not None)
    assert f.phase == ROBOT_MOVING and f.moving["failed"]
    await f.robot_at("D")  # TA carries the robot to D
    await until(lambda: f.phase in (THINKING, VOTING) and f.state.ply >= 1)
    assert f.robot_cell in ("D", "E")
    bridge.task.cancel()
    f.shutdown()


async def test_failed_move_then_retry(db):
    link = RobotLink()
    link.mode = "robot"
    f = Flow(db, link, FakeEmitter(), fast_settings())
    bridge = FakeBridge(link, fail_moves=1)
    await f.new_game("1", "MAX")
    await f.start()
    await vote_all(f, ["D"])
    await f.close_round()
    await until(lambda: f.robot_error is not None)
    await f.retry_move()
    await until(lambda: f.state.ply == 2 and f.phase == VOTING)
    assert [a for c, a in bridge.log if c == "move"][:2] == [{"from": "A", "to": "D"}, {"from": "A", "to": "D"}]
    bridge.task.cancel()
    f.shutdown()


async def test_switch_to_sim_completes_stuck_move(db):
    link = RobotLink()
    link.mode = "robot"  # no bridge running: the move never completes
    f = Flow(db, link, FakeEmitter(), fast_settings())
    await f.new_game("1", "MAX")
    await f.start()
    await vote_all(f, ["D"])
    await f.close_round()
    await until(lambda: f.phase == ROBOT_MOVING)
    await asyncio.sleep(0.05)
    assert f.phase == ROBOT_MOVING
    await f.set_mode("sim")
    await until(lambda: f.phase == VOTING and f.state.ply == 2)
    assert link.queues["main"] == []
    f.shutdown()


async def test_restore_after_restart(tmp_path):
    path = str(tmp_path / "r.db")
    db1 = Database(path, "secret")
    await db1.open()
    f1 = Flow(db1, RobotLink(), FakeEmitter(), fast_settings())
    await f1.new_game("2", "MAX")
    await f1.start()
    await vote_all(f1, ["D"])
    await f1.close_round()
    await until(lambda: f1.phase == VOTING and f1.state.ply == 2)
    path_before = f1.state.path
    f1.shutdown()
    await db1.close()

    db2 = Database(path, "secret")
    await db2.open()
    f2 = Flow(db2, RobotLink(), FakeEmitter(), fast_settings())
    assert await f2.restore()
    assert f2.state.path == path_before and f2.phase == LOBBY and f2.restored
    assert f2.setup.name == "2" and f2.class_side == "MAX"
    assert [m["to"] for m in f2.history] == list(path_before[1:])
    await f2.start()
    await until(lambda: f2.phase == VOTING)
    assert f2.round.options == f2.state.legal_moves()
    f2.shutdown()
    await db2.close()


async def test_new_game_abandons_previous(flow):
    await flow.start()
    first = flow.game_id
    await flow.new_game("2", "MIN")
    export = await flow.db.export()
    statuses = {g["id"]: g["status"] for g in export["games"]}
    assert statuses[first] == "abandoned" and statuses[flow.game_id] == "active"
    assert flow.phase == LOBBY and flow.round is None


async def test_custom_setup(db):
    f = Flow(db, RobotLink(), FakeEmitter(), fast_settings())
    await f.new_game(None, "MAX", cups=["E"], start="I")
    assert f.state.path == ("I",) and f.setup.cups == frozenset({"E"})
    assert f.tree()["root"] == "I"
    with pytest.raises(ValueError):
        await f.new_game(None, "MAX", cups=["I"], start="I")
    f.shutdown()


async def test_replay_search_on_board(flow):
    await flow.replay_search("alphabeta", from_root=True)
    payload = flow.emit.searches[-1]
    assert payload["replay"] and payload["algorithm"] == "alphabeta" and payload["root"] == "A"
    assert payload["stats"]["nodes_pruned"] > 0
    assert payload["duration_ms"] <= flow.settings.think_max_s * 1000
    assert flow.phase == LOBBY


async def test_quiz(flow):
    await flow.quiz_open(preset="root_value")
    q = flow.quiz
    assert q.answer == "+1" and q.options == ["-1", "0", "+1"]
    assert (await flow.quiz_vote("a", q.id, "+1"))[0]
    assert (await flow.quiz_vote("b", q.id, "-1"))[0]
    assert (await flow.quiz_vote("c", q.id, "2"))[0] is False
    pub = flow.snapshot()["quiz"]
    assert pub["counts"] is None and pub["answer"] is None and pub["total"] == 2
    await flow.quiz_reveal()
    pub = flow.snapshot()["quiz"]
    assert pub["counts"] == {"-1": 1, "0": 0, "+1": 1} and pub["answer"] == "+1"
    assert (await flow.quiz_vote("d", q.id, "0"))[0] is False


async def test_settings_validation():
    s = Settings()
    s.update({"vote_seconds": "30", "algorithm": "alphabeta", "live_votes": 1})
    assert s.vote_seconds == 30 and s.algorithm == "alphabeta" and s.live_votes is True
    with pytest.raises(ValueError):
        s.update({"algorithm": "expectimax"})
    with pytest.raises(ValueError):
        s.update({"vote_seconds": 1})
    with pytest.raises(ValueError):
        s.update({"nope": 1})


async def test_db_survives_failure(flow):
    await flow.db.close()  # every database call now fails
    await flow.start()
    await vote_all(flow, ["D"])
    await flow.close_round()
    await until(lambda: flow.state.ply == 2)
