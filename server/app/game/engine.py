"""Grid game from the Adversarial Search lecture (slides 19-38).

Rules (slide 19):
  - Two players take turns moving the robot, horizontally or vertically.
  - The robot cannot move back to a cell it has already visited.
  - The first player to reach a cup wins.
  - If there is no legal move left, the game is a draw.

    A B C
    D E F
    G H I

MAX always moves first. Children are generated in alphabetical order, which
matches the tree layout on the slides.

This module has no dependencies, so the robot bridge can import it to
validate moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CELLS = "ABCDEFGHI"
MAX = "MAX"
MIN = "MIN"


def coords(cell: str) -> tuple[int, int]:
    """(row, col) of a cell, with A = (0, 0) and I = (2, 2)."""
    i = CELLS.index(cell)
    return divmod(i, 3)


def cell_at(row: int, col: int) -> str:
    return CELLS[row * 3 + col]


def _neighbours(cell: str) -> tuple[str, ...]:
    r, c = coords(cell)
    out = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        rr, cc = r + dr, c + dc
        if 0 <= rr < 3 and 0 <= cc < 3:
            out.append(cell_at(rr, cc))
    return tuple(sorted(out))


NEIGHBOURS: dict[str, tuple[str, ...]] = {c: _neighbours(c) for c in CELLS}


def is_adjacent(a: str, b: str) -> bool:
    return a in NEIGHBOURS and b in NEIGHBOURS[a]


def other(player: str) -> str:
    return MIN if player == MAX else MAX


@dataclass(frozen=True)
class Setup:
    cups: frozenset[str]
    start: str = "A"
    name: str = "custom"

    def __post_init__(self) -> None:
        if self.start not in CELLS:
            raise ValueError(f"start cell {self.start!r} is not on the grid")
        if not self.cups:
            raise ValueError("at least one cup is required")
        bad = [c for c in self.cups if c not in CELLS]
        if bad:
            raise ValueError(f"cup cells not on the grid: {bad}")
        if self.start in self.cups:
            raise ValueError("the start cell cannot hold a cup")

    def to_dict(self) -> dict:
        return {"name": self.name, "cups": sorted(self.cups), "start": self.start}


VARIANTS: dict[str, Setup] = {
    "1": Setup(cups=frozenset({"C", "H"}), start="A", name="1"),
    "2": Setup(cups=frozenset({"I"}), start="A", name="2"),
}


def make_setup(variant: str | None = None, cups=None, start: str | None = None) -> Setup:
    """A named variant, or a custom setup when cups/start are given."""
    if variant and variant in VARIANTS and cups is None and start is None:
        return VARIANTS[variant]
    return Setup(cups=frozenset(c.upper() for c in (cups or [])), start=(start or "A").upper(), name="custom")


@dataclass(frozen=True)
class State:
    setup: Setup
    path: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.path:
            object.__setattr__(self, "path", (self.setup.start,))

    @property
    def cell(self) -> str:
        return self.path[-1]

    @property
    def ply(self) -> int:
        """Number of moves made so far."""
        return len(self.path) - 1

    @property
    def to_move(self) -> str:
        return MAX if self.ply % 2 == 0 else MIN

    @property
    def last_mover(self) -> str | None:
        return None if self.ply == 0 else other(self.to_move)

    @property
    def node_id(self) -> str:
        """Stable id of this position in the game tree: the path as a string."""
        return "".join(self.path)

    def on_cup(self) -> bool:
        return self.ply > 0 and self.cell in self.setup.cups

    def legal_moves(self) -> list[str]:
        if self.on_cup():
            return []
        visited = set(self.path)
        return [n for n in NEIGHBOURS[self.cell] if n not in visited]

    def is_terminal(self) -> bool:
        return self.on_cup() or not self.legal_moves()

    def terminal_kind(self) -> str | None:
        """'cup_max' / 'cup_min' when a player reached a cup, 'draw' when stuck, else None."""
        if self.on_cup():
            return "cup_max" if self.last_mover == MAX else "cup_min"
        if not self.legal_moves():
            return "draw"
        return None

    def utility(self) -> int:
        """Outcome for MAX at a terminal state: +1 win, -1 loss, 0 draw."""
        kind = self.terminal_kind()
        if kind == "cup_max":
            return 1
        if kind == "cup_min":
            return -1
        if kind == "draw":
            return 0
        raise ValueError("utility() called on a non-terminal state")

    def apply(self, move: str) -> "State":
        if move not in self.legal_moves():
            raise IllegalMove(f"{move!r} is not a legal move from {self.cell} (path {self.node_id})")
        return State(self.setup, self.path + (move,))

    def to_dict(self) -> dict:
        return {
            "path": list(self.path),
            "cell": self.cell,
            "ply": self.ply,
            "to_move": self.to_move,
            "legal_moves": self.legal_moves(),
            "terminal": self.terminal_kind(),
            "utility": self.utility() if self.is_terminal() else None,
        }


class IllegalMove(ValueError):
    pass


def replay(setup: Setup, moves) -> State:
    """Rebuild a state from its moves. Raises IllegalMove on a bad sequence."""
    s = State(setup)
    for m in moves:
        s = s.apply(m)
    return s
