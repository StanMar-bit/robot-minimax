import itertools

import pytest

from app.game.engine import CELLS, VARIANTS, Setup, State, replay
from app.game.search import alphabeta, build_tree, minimax, search, subtree_size

# Node values as printed on slide 27 (variant 1, cups on C and H).
SLIDE_27 = {
    "A": 1,
    "AB": -1, "AD": 1,
    "ABC": -1, "ABE": 1, "ADE": 1, "ADG": 1,
    "ABED": 1, "ABEF": -1, "ABEH": 1, "ADEB": -1, "ADEF": -1, "ADEH": 1, "ADGH": 1,
    "ABEDG": 1, "ABEFC": -1, "ABEFI": 1, "ADEBC": -1, "ADEFC": -1, "ADEFI": 1,
    "ABEDGH": 1, "ABEFIH": 1, "ADEFIH": 1,
}

# Node values as printed on slide 38 (variant 2, cup on I).
SLIDE_38 = {
    "A": -1,
    "AB": -1, "AD": -1,
    "ABC": -1, "ABE": -1, "ADE": -1, "ADG": -1,
    "ABCF": -1, "ABED": -1, "ABEF": -1, "ABEH": -1, "ADEB": -1, "ADEF": -1, "ADEH": -1, "ADGH": -1,
    "ABCFE": -1, "ABCFI": -1, "ABEDG": -1, "ABEFC": 0, "ABEFI": -1, "ABEHI": -1, "ADEBC": -1,
    "ADEFC": 0, "ADEFI": -1, "ADEHG": 0, "ADEHI": -1, "ADGHE": -1, "ADGHI": -1,
    "ABCFED": -1, "ABCFEH": -1, "ABEDGH": -1, "ADEBCF": -1, "ADEFCB": 0, "ADGHEB": -1, "ADGHEF": -1,
    "ABCFEDG": -1, "ABCFEHG": 0, "ABCFEHI": -1, "ABEDGHI": -1, "ADEBCFI": -1, "ADGHEBC": -1, "ADGHEFI": -1,
    "ABCFEDGH": -1, "ABCFEHGD": 0, "ADGHEBCF": -1,
    "ABCFEDGHI": -1, "ADGHEBCFI": -1,
}

# Slides 29-38 leave out two branches. Both end in a draw and change no value above them.
SLIDE_38_OMITTED = {"ABEHG": 0, "ABEHGD": 0, "ADGHEFC": 0, "ADGHEFCB": 0}


def values(variant):
    tree = build_tree(State(VARIANTS[variant]))
    return {n["id"]: n["value"] for n in tree["nodes"]}, tree


def test_variant1_root_value_and_tree_match_slide_27():
    vals, tree = values("1")
    assert tree["value"] == 1
    assert vals == SLIDE_27


def test_variant2_root_value_and_tree_match_slide_38():
    vals, tree = values("2")
    assert tree["value"] == -1
    for node, v in SLIDE_38.items():
        assert vals[node] == v, node
    assert {k: v for k, v in vals.items() if k not in SLIDE_38} == SLIDE_38_OMITTED


def test_tree_structure():
    _, tree = values("1")
    by_id = {n["id"]: n for n in tree["nodes"]}
    assert by_id["A"]["children"] == ["AB", "AD"]
    assert by_id["ABE"]["children"] == ["ABED", "ABEF", "ABEH"]
    assert by_id["ABC"]["terminal"] == "cup_min"
    assert by_id["ADGH"]["terminal"] == "cup_max"
    assert by_id["AB"]["player"] == "MIN" and by_id["ABE"]["player"] == "MAX"
    _, tree2 = values("2")
    by_id2 = {n["id"]: n for n in tree2["nodes"]}
    assert by_id2["ABEFC"]["terminal"] == "draw"


@pytest.mark.parametrize("variant,best,value", [("1", "D", 1), ("2", "B", -1)])
def test_best_move_from_root(variant, best, value):
    for algo in ("minimax", "alphabeta"):
        r = search(State(VARIANTS[variant]), algo)
        assert r["best"] == best and r["value"] == value


def test_robot_as_min_wins_after_class_picks_b():
    s = State(VARIANTS["1"]).apply("B")
    r = minimax(s)
    assert r["best"] == "C" and r["value"] == -1


def all_reachable_states():
    """Every non-terminal position for every single-cup setup plus both variants."""
    setups = list(VARIANTS.values())
    for start, cup in itertools.product(CELLS, CELLS):
        if start != cup:
            setups.append(Setup(cups=frozenset({cup}), start=start))
    setups.append(Setup(cups=frozenset({"C", "G"}), start="E"))
    for setup in setups:
        stack = [State(setup)]
        while stack:
            s = stack.pop()
            yield s
            if not s.is_terminal():
                stack.extend(s.apply(m) for m in s.legal_moves())


def test_alphabeta_matches_minimax_everywhere():
    count = 0
    for s in all_reachable_states():
        if s.is_terminal():
            continue
        mm, ab = minimax(s, trace=False), alphabeta(s, trace=False)
        assert mm["value"] == ab["value"], s.node_id
        assert mm["best"] == ab["best"], s.node_id
        assert ab["stats"]["nodes_visited"] <= mm["stats"]["nodes_visited"]
        assert ab["stats"]["nodes_visited"] + ab["stats"]["nodes_pruned"] == mm["stats"]["nodes_visited"]
        assert mm["stats"]["nodes_visited"] == subtree_size(s)
        count += 1
    assert count > 100


def test_tie_break_is_alphabetical():
    # Variant 2: from A both moves lose for MAX; the first in alphabetical order is chosen.
    r = minimax(State(VARIANTS["2"]))
    assert r["child_values"] == {"B": -1, "D": -1}
    assert r["best"] == "B"


def test_alphabeta_prunes_variant2():
    ab = alphabeta(State(VARIANTS["2"]))
    assert ab["stats"]["nodes_pruned"] > 0
    assert ab["stats"]["nodes_visited"] < 51
    prunes = [e for e in ab["trace"] if e["t"] == "prune"]
    assert prunes and all(e["reason"] == "alpha>=beta" for e in prunes)


def test_trace_is_well_formed():
    for algo in ("minimax", "alphabeta"):
        r = search(State(VARIANTS["2"]), algo)
        tr = r["trace"]
        assert tr[0] == {"t": "enter", "node": "A", **({"alpha": None, "beta": None} if algo == "alphabeta" else {})}
        assert tr[-1]["t"] == "return" and tr[-1]["node"] == "A" and tr[-1]["value"] == -1
        enters = sum(1 for e in tr if e["t"] == "enter")
        assert enters == r["stats"]["nodes_visited"]
        # every non-leaf that is entered also returns
        leaves = sum(1 for e in tr if e["t"] == "leaf")
        returns = sum(1 for e in tr if e["t"] == "return")
        assert enters == leaves + returns


def test_search_from_mid_game_uses_same_node_ids():
    s = replay(VARIANTS["2"], ["D", "E"])
    r = alphabeta(s)
    assert r["root"] == "ADE"
    assert all(e["node"].startswith("ADE") for e in r["trace"])
    full = {n["id"] for n in build_tree(State(VARIANTS["2"]))["nodes"]}
    assert {e["node"] for e in r["trace"]} <= full


def test_unknown_algorithm():
    with pytest.raises(ValueError):
        search(State(VARIANTS["1"]), "expectimax")
