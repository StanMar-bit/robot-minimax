"""SQLite persistence: restore a game after a restart, and vote statistics afterwards.

The game itself runs from memory. Every write here is best-effort: the caller
logs a failure and carries on, so a disk problem never stops the demo.

Privacy: votes are stored per device as an HMAC of the random device id with a
server secret. No IP addresses, user agents or names are stored. Raw votes can
be deleted after the lecture; the per-round counts in `rounds.counts` remain.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  id INTEGER PRIMARY KEY, variant TEXT, cups TEXT, start_cell TEXT,
  class_side TEXT,              -- 'MAX' | 'MIN' | 'BOTH'
  status TEXT, result INTEGER,  -- +1 / 0 / -1 for MAX
  created_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS moves (
  id INTEGER PRIMARY KEY, game_id INTEGER REFERENCES games(id),
  ply INTEGER, player TEXT, from_cell TEXT, to_cell TEXT,
  source TEXT,                  -- 'vote' | 'minimax' | 'alphabeta' | 'admin'
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY, game_id INTEGER REFERENCES games(id), ply INTEGER,
  kind TEXT DEFAULT 'move',     -- 'move' | 'quiz'
  question TEXT,
  options TEXT, opened_at TEXT, closed_at TEXT, winner TEXT,
  counts TEXT                   -- JSON {choice: count}, kept after votes are deleted
);
CREATE TABLE IF NOT EXISTS votes (
  round_id INTEGER REFERENCES rounds(id), device_hash TEXT, choice TEXT, created_at TEXT,
  PRIMARY KEY (round_id, device_hash)
);
CREATE INDEX IF NOT EXISTS moves_game ON moves(game_id, ply);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str, secret: str) -> None:
        self.path = path
        self._secret = secret.encode()
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    def device_hash(self, device_id: str) -> str:
        return hmac.new(self._secret, device_id.encode(), hashlib.sha256).hexdigest()[:24]

    async def _exec(self, sql: str, args: tuple = ()) -> aiosqlite.Cursor:
        assert self.conn is not None, "database not open"
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur

    # games -----------------------------------------------------------------

    async def create_game(self, game_id: int, setup: dict, class_side: str) -> None:
        await self._exec("UPDATE games SET status='abandoned', finished_at=? WHERE status='active'", (now_iso(),))
        await self._exec(
            "INSERT INTO games (id, variant, cups, start_cell, class_side, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (game_id, setup["name"], ",".join(setup["cups"]), setup["start"], class_side, now_iso()),
        )

    async def finish_game(self, game_id: int, result: int | None, status: str = "finished") -> None:
        await self._exec(
            "UPDATE games SET status=?, result=?, finished_at=? WHERE id=?",
            (status, result, now_iso(), game_id),
        )

    async def add_move(self, game_id: int, ply: int, player: str, from_cell: str, to_cell: str, source: str) -> None:
        await self._exec(
            "INSERT INTO moves (game_id, ply, player, from_cell, to_cell, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (game_id, ply, player, from_cell, to_cell, source, now_iso()),
        )

    async def active_game(self) -> dict[str, Any] | None:
        assert self.conn is not None
        cur = await self.conn.execute("SELECT * FROM games WHERE status='active' ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        if not row:
            return None
        game = dict(row)
        cur = await self.conn.execute("SELECT * FROM moves WHERE game_id=? ORDER BY ply", (game["id"],))
        game["moves"] = [dict(r) for r in await cur.fetchall()]
        return game

    # rounds and votes ------------------------------------------------------

    async def open_round(self, round_id: int, game_id: int | None, ply: int | None, options: list[str],
                         kind: str = "move", question: str | None = None) -> None:
        await self._exec(
            "INSERT INTO rounds (id, game_id, ply, kind, question, options, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (round_id, game_id, ply, kind, question, json.dumps(options), now_iso()),
        )

    async def close_round(self, round_id: int, winner: str | None, counts: dict[str, int]) -> None:
        await self._exec(
            "UPDATE rounds SET closed_at=?, winner=?, counts=? WHERE id=?",
            (now_iso(), winner, json.dumps(counts), round_id),
        )

    async def upsert_vote(self, round_id: int, device_hash: str, choice: str) -> None:
        await self._exec(
            "INSERT INTO votes (round_id, device_hash, choice, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(round_id, device_hash) DO UPDATE SET choice=excluded.choice, created_at=excluded.created_at",
            (round_id, device_hash, choice, now_iso()),
        )

    async def delete_votes(self) -> int:
        cur = await self._exec("DELETE FROM votes")
        return cur.rowcount

    # export ----------------------------------------------------------------

    async def export(self) -> dict[str, Any]:
        """Aggregates only: games, moves and per-round counts. No per-device data."""
        assert self.conn is not None
        out: dict[str, Any] = {}
        for table in ("games", "moves"):
            cur = await self.conn.execute(f"SELECT * FROM {table} ORDER BY id")
            out[table] = [dict(r) for r in await cur.fetchall()]
        cur = await self.conn.execute("SELECT * FROM rounds ORDER BY id")
        rounds = []
        for r in await cur.fetchall():
            d = dict(r)
            d["options"] = json.loads(d["options"]) if d["options"] else []
            d["counts"] = json.loads(d["counts"]) if d["counts"] else None
            if d["counts"]:
                total = sum(d["counts"].values())
                d["percent"] = {k: round(100 * v / total) for k, v in d["counts"].items()} if total else {}
            rounds.append(d)
        out["rounds"] = rounds
        cur = await self.conn.execute("SELECT COUNT(*) FROM votes")
        out["raw_votes_stored"] = (await cur.fetchone())[0]
        return out
