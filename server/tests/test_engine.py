import pytest

from app.game.engine import (
    MAX, MIN, NEIGHBOURS, VARIANTS, IllegalMove, Setup, State, is_adjacent, make_setup, replay,
)


def test_neighbours():
    assert NEIGHBOURS["A"] == ("B", "D")
    assert NEIGHBOURS["E"] == ("B", "D", "F", "H")
    assert NEIGHBOURS["I"] == ("F", "H")
    assert is_adjacent("A", "B") and not is_adjacent("A", "E") and not is_adjacent("C", "D")


def test_start_state():
    s = State(VARIANTS["1"])
    assert s.path == ("A",)
    assert s.to_move == MAX
    assert s.legal_moves() == ["B", "D"]
    assert not s.is_terminal()


def test_turns_alternate_and_no_revisits():
    s = State(VARIANTS["2"]).apply("B")
    assert s.to_move == MIN
    assert s.legal_moves() == ["C", "E"]  # A is visited
    s = s.apply("E")
    assert s.to_move == MAX
    assert s.legal_moves() == ["D", "F", "H"]


def test_cup_win_values():
    s = replay(VARIANTS["1"], ["B", "C"])  # MIN steps onto C
    assert s.terminal_kind() == "cup_min" and s.utility() == -1
    s = replay(VARIANTS["1"], ["D", "G", "H"])  # MAX steps onto H
    assert s.terminal_kind() == "cup_max" and s.utility() == 1
    assert s.legal_moves() == []


def test_draw_when_stuck():
    s = replay(VARIANTS["2"], ["B", "E", "F", "C"])  # C: neighbours B and F both visited
    assert s.terminal_kind() == "draw" and s.utility() == 0


def test_illegal_moves():
    s = State(VARIANTS["1"])
    with pytest.raises(IllegalMove):
        s.apply("E")  # not adjacent
    with pytest.raises(IllegalMove):
        s.apply("B").apply("A")  # revisit
    with pytest.raises(IllegalMove):
        replay(VARIANTS["1"], ["B", "C", "F"])  # game is over


def test_replay_restores_same_state():
    moves = ["D", "E", "F", "I", "H"]
    s = State(VARIANTS["1"])
    for m in moves:
        s = s.apply(m)
    assert replay(VARIANTS["1"], moves) == s
    assert s.node_id == "ADEFIH"


def test_custom_setup_validation():
    assert make_setup("1") is VARIANTS["1"]
    s = make_setup(cups=["e"], start="a")
    assert s.cups == frozenset({"E"}) and s.start == "A"
    with pytest.raises(ValueError):
        Setup(cups=frozenset({"A"}), start="A")
    with pytest.raises(ValueError):
        Setup(cups=frozenset(), start="A")
    with pytest.raises(ValueError):
        Setup(cups=frozenset({"Z"}), start="A")


def test_custom_start_cell():
    s = State(make_setup(cups=["A"], start="E"))
    assert s.legal_moves() == ["B", "D", "F", "H"]
