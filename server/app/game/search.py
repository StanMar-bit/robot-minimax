"""Minimax and alpha-beta search with a step-by-step trace.

Every node is identified by its path from the start cell ("A", "AB", "ABE", ...),
so a search started from any position refers to the same node ids as the full
tree that the board draws.

Trace events (replayed by the board):
  {"t": "enter",  "node": id, "alpha": a, "beta": b}
  {"t": "leaf",   "node": id, "value": v}
  {"t": "update", "node": id, "value": v, "child": child_id, "alpha": a, "beta": b}
  {"t": "prune",  "node": child_id, "parent": id, "size": n, "reason": "alpha>=beta"}
  {"t": "return", "node": id, "value": v}

alpha/beta are only set for alpha-beta. None means -inf (alpha) or +inf (beta).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .engine import MAX, State

INF = float("inf")


@lru_cache(maxsize=4096)
def subtree_size(state: State) -> int:
    if state.is_terminal():
        return 1
    return 1 + sum(subtree_size(state.apply(m)) for m in state.legal_moves())


def build_tree(state: State) -> dict[str, Any]:
    """The full game tree below `state`, with exact minimax values on every node."""
    nodes: list[dict[str, Any]] = []
    max_depth = state.ply

    def rec(s: State, parent: str | None) -> int:
        nonlocal max_depth
        max_depth = max(max_depth, s.ply)
        node = {
            "id": s.node_id,
            "cell": s.cell,
            "depth": s.ply,
            "player": s.to_move,
            "terminal": s.terminal_kind(),
            "value": None,
            "parent": parent,
            "children": [],
        }
        nodes.append(node)
        if s.is_terminal():
            node["value"] = s.utility()
        else:
            values = []
            for m in s.legal_moves():
                child = s.apply(m)
                node["children"].append(child.node_id)
                values.append(rec(child, s.node_id))
            node["value"] = max(values) if s.to_move == MAX else min(values)
        return node["value"]

    value = rec(state, None)
    ab = alphabeta(state, trace=False)
    return {
        "setup": state.setup.to_dict(),
        "root": state.node_id,
        "value": value,
        "nodes": nodes,
        "max_depth": max_depth,
        "stats": {
            "nodes_total": len(nodes),
            "minimax_visited": len(nodes),
            "alphabeta_visited": ab["stats"]["nodes_visited"],
            "alphabeta_pruned": ab["stats"]["nodes_pruned"],
        },
    }


class _Ctx:
    def __init__(self, record: bool) -> None:
        self.record = record
        self.trace: list[dict[str, Any]] = []
        self.visited = 0
        self.pruned = 0

    def emit(self, ev: dict[str, Any]) -> None:
        if self.record:
            self.trace.append(ev)


def _bound(x: float) -> float | None:
    return None if x in (INF, -INF) else x


def _result(algorithm: str, state: State, value: int, best: str | None,
            child_values: dict[str, int | None], ctx: _Ctx) -> dict[str, Any]:
    return {
        "algorithm": algorithm,
        "root": state.node_id,
        "player": state.to_move,
        "value": value,
        "best": best,
        "best_node": state.node_id + best if best else None,
        "child_values": child_values,
        "trace": ctx.trace,
        "stats": {
            "nodes_visited": ctx.visited,
            "nodes_pruned": ctx.pruned,
            "nodes_total": subtree_size(state),
        },
    }


def minimax(state: State, trace: bool = True) -> dict[str, Any]:
    ctx = _Ctx(trace)
    child_values: dict[str, int | None] = {}

    def rec(s: State) -> int:
        nid = s.node_id
        ctx.visited += 1
        ctx.emit({"t": "enter", "node": nid})
        if s.is_terminal():
            v = s.utility()
            ctx.emit({"t": "leaf", "node": nid, "value": v})
            return v
        maximizing = s.to_move == MAX
        best: int | None = None
        for m in s.legal_moves():
            child = s.apply(m)
            cv = rec(child)
            if s is state:
                child_values[m] = cv
            if best is None or (cv > best if maximizing else cv < best):
                best = cv
            ctx.emit({"t": "update", "node": nid, "value": best, "child": child.node_id})
        ctx.emit({"t": "return", "node": nid, "value": best})
        return best  # type: ignore[return-value]

    value = rec(state)
    best_move = _pick(state, child_values)
    return _result("minimax", state, value, best_move, child_values, ctx)


def alphabeta(state: State, trace: bool = True) -> dict[str, Any]:
    ctx = _Ctx(trace)
    child_values: dict[str, int | None] = {}

    def rec(s: State, alpha: float, beta: float) -> float:
        nid = s.node_id
        ctx.visited += 1
        ctx.emit({"t": "enter", "node": nid, "alpha": _bound(alpha), "beta": _bound(beta)})
        if s.is_terminal():
            v = s.utility()
            ctx.emit({"t": "leaf", "node": nid, "value": v})
            return v
        maximizing = s.to_move == MAX
        v = -INF if maximizing else INF
        moves = s.legal_moves()
        for i, m in enumerate(moves):
            child = s.apply(m)
            cv = rec(child, alpha, beta)
            if s is state:
                child_values[m] = int(cv)
            if maximizing:
                v = max(v, cv)
                alpha = max(alpha, v)
            else:
                v = min(v, cv)
                beta = min(beta, v)
            ctx.emit({"t": "update", "node": nid, "value": int(v), "child": child.node_id,
                      "alpha": _bound(alpha), "beta": _bound(beta)})
            if alpha >= beta:
                for rest in moves[i + 1:]:
                    pruned = s.apply(rest)
                    size = subtree_size(pruned)
                    ctx.pruned += size
                    if s is state:
                        child_values[rest] = None
                    ctx.emit({"t": "prune", "node": pruned.node_id, "parent": nid,
                              "size": size, "reason": "alpha>=beta"})
                break
        ctx.emit({"t": "return", "node": nid, "value": int(v)})
        return v

    value = int(rec(state, -INF, INF))
    best_move = _pick(state, child_values)
    return _result("alphabeta", state, value, best_move, child_values, ctx)


def _pick(state: State, child_values: dict[str, int | None]) -> str | None:
    """Best move for the player to move; ties go to the first move in alphabetical order.

    With alpha-beta, a later child may return a bound equal to the best value
    rather than its exact value. Only a strictly better value replaces the
    current choice, so both algorithms pick the same move.
    """
    best: str | None = None
    best_v: int | None = None
    maximizing = state.to_move == MAX
    for m in state.legal_moves():
        v = child_values.get(m)
        if v is None:
            continue
        if best_v is None or (v > best_v if maximizing else v < best_v):
            best, best_v = m, v
    return best


ALGORITHMS = {"minimax": minimax, "alphabeta": alphabeta}


def search(state: State, algorithm: str = "minimax", trace: bool = True) -> dict[str, Any]:
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown algorithm {algorithm!r}")
    return ALGORITHMS[algorithm](state, trace=trace)
