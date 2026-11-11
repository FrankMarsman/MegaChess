"""Durable game records and immutable AI profiles.

SQLite writes are short transactions. Connections are never shared between
threads, and live database files should stay outside a synchronised checkout.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid


FEATURES = ("pawn", "knight", "bishop", "rook", "queen", "advancement", "activity", "approach", "safety")
BASE_WEIGHTS = dict(zip(FEATURES, (1.0, 3.2, 3.3, 5.0, 9.0, 0.15, 0.10, 0.05, 0.5)))
SEARCH_DEFAULTS = {"mode": "selective", "root_width": 48, "width": 16, "qdepth": 2, "qwidth": 8}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rules_key(config: dict) -> str:
    """A stable training lineage, independent of JSON dictionary ordering."""
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def default_profiles() -> list[dict]:
    profiles = []
    styles = (
        ("balanced", "Balanced", {}, 0.02, 0.0),
        ("aggressive", "Aggressive", {"approach": 0.22, "activity": 0.16, "safety": 0.3}, 0.025, 0.02),
        ("defensive", "Defensive", {"safety": 1.0, "approach": 0.02, "activity": 0.08}, 0.01, 0.0),
        ("explorer", "Explorer", {"activity": 0.35, "advancement": 0.25, "approach": 0.12}, 0.03, 0.08),
    )
    for ident, name, changes, rate, exploration in styles:
        weights = {**BASE_WEIGHTS, **changes}
        profiles.append({
            "id": ident, "name": name, "version": 1,
            "weights": weights, "anchor_weights": deepcopy(weights),
            "search": deepcopy(SEARCH_DEFAULTS),
            "learning": {"enabled": True, "rate": rate, "lambda": 0.7},
            "exploration": exploration, "games_learned": 0,
            "origin": "preset",
        })
    return profiles


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.training_lock = threading.RLock()
        with self._connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS profile_versions (
                    id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(id, version)
                );
                CREATE TABLE IF NOT EXISTS profile_heads (
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learning_heads (
                    profile_id TEXT NOT NULL, rules_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, rules_key)
                );
                CREATE TABLE IF NOT EXISTS learning_receipts (
                    game_id TEXT NOT NULL, profile_id TEXT NOT NULL,
                    rules_key TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(game_id, profile_id, rules_key)
                );
                CREATE TABLE IF NOT EXISTS games (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, batch_id TEXT, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS games_updated ON games(updated_at DESC);
                CREATE TABLE IF NOT EXISTS moves (
                    game_id TEXT NOT NULL REFERENCES games(id),
                    revision INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(game_id, revision)
                );
                CREATE TABLE IF NOT EXISTS move_telemetry (
                    game_id TEXT NOT NULL REFERENCES games(id),
                    revision INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(game_id, revision)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, data TEXT NOT NULL
                );
                PRAGMA user_version = 1;
            """)
            for profile in default_profiles():
                if not con.execute("SELECT 1 FROM profile_heads WHERE id=?", (profile["id"],)).fetchone():
                    self._insert_profile(con, profile, update_base=True)

    @contextmanager
    def _connection(self):
        con = sqlite3.connect(str(self.path), timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA journal_mode=WAL")
        try:
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def _dump(data):
        return json.dumps(data, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _load(row, missing: str):
        if row is None:
            raise KeyError(missing)
        return json.loads(row["data"])

    def _insert_profile(self, con, profile, update_base=False):
        p = deepcopy(profile)
        p["id"] = str(p.get("id") or uuid.uuid4().hex)
        p["name"] = str(p.get("name") or "Custom AI")[:100]
        latest = con.execute("SELECT MAX(version) FROM profile_versions WHERE id=?", (p["id"],)).fetchone()[0]
        p["version"] = (latest or 0) + 1
        p["created_at"] = now()
        p.setdefault("anchor_weights", deepcopy(p["weights"]))
        p.setdefault("search", deepcopy(SEARCH_DEFAULTS))
        p.setdefault("learning", {"enabled": True, "rate": 0.02, "lambda": 0.7})
        p.setdefault("exploration", 0.0)
        p.setdefault("games_learned", 0)
        if update_base:
            p["base_version"] = p["version"]
            p.pop("trained_rules", None)
        con.execute("INSERT INTO profile_versions VALUES(?,?,?)", (p["id"], p["version"], self._dump(p)))
        if update_base:
            con.execute("INSERT INTO profile_heads VALUES(?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version", (p["id"], p["version"]))
        return p

    def list_profiles(self):
        with self._connection() as con:
            rows = con.execute("SELECT p.data FROM profile_heads h JOIN profile_versions p ON p.id=h.id AND p.version=h.version ORDER BY CASE h.id WHEN 'balanced' THEN 0 WHEN 'aggressive' THEN 1 WHEN 'defensive' THEN 2 WHEN 'explorer' THEN 3 ELSE 4 END, h.id").fetchall()
            trained = con.execute("SELECT p.data FROM learning_heads h JOIN profile_versions p ON p.id=h.profile_id AND p.version=h.version ORDER BY p.version DESC").fetchall()
        profiles = [json.loads(r["data"]) for r in rows]
        for profile in profiles:
            profile["trained_versions"] = [
                {"version": p["version"], "rules_key": p["trained_rules"], "games_learned": p.get("games_learned", 0), "created_at": p["created_at"]}
                for row in trained
                if (p := json.loads(row["data"]))["id"] == profile["id"] and p.get("base_version") == profile["version"]
            ]
        return profiles

    def get_profile(self, ident, version=None):
        with self._connection() as con:
            if version is None:
                row = con.execute("SELECT p.data FROM profile_heads h JOIN profile_versions p ON p.id=h.id AND p.version=h.version WHERE h.id=?", (ident,)).fetchone()
            else:
                row = con.execute("SELECT data FROM profile_versions WHERE id=? AND version=?", (ident, version)).fetchone()
        return self._load(row, f"Unknown AI profile: {ident}")

    def profile_versions(self, ident):
        with self._connection() as con:
            rows = con.execute("SELECT data FROM profile_versions WHERE id=? ORDER BY version DESC", (ident,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def save_profile(self, profile):
        p = deepcopy(profile)
        weights = p.get("weights", {})
        if set(weights) != set(FEATURES):
            raise ValueError("AI weights must contain each evaluation feature")
        import math
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1000 for v in weights.values()):
            raise ValueError("AI weights must be finite numbers between -1000 and 1000")
        search = {**SEARCH_DEFAULTS, **p.get("search", {})}
        if search["mode"] not in ("selective", "full"):
            raise ValueError("Search mode must be selective or full")
        for k in ("root_width", "width", "qwidth"):
            if not isinstance(search[k], int) or not 1 <= search[k] <= 10000:
                raise ValueError(f"{k} must be an integer from 1 to 10000")
        if not isinstance(search["qdepth"], int) or not 0 <= search["qdepth"] <= 8:
            raise ValueError("Quiescence depth must be from 0 to 8")
        p["search"] = search
        learning = {"enabled": True, "rate": 0.02, "lambda": 0.7, **p.get("learning", {})}
        for key, high in (("rate", 1), ("lambda", 1)):
            val = learning[key]
            if not isinstance(val, (int, float)) or not math.isfinite(val) or not 0 <= val <= high:
                raise ValueError(f"Learning {key} must be from 0 to {high}")
        p["learning"] = learning
        exploration = p.get("exploration", 0)
        if not isinstance(exploration, (int, float)) or not math.isfinite(exploration) or not 0 <= exploration <= 1:
            raise ValueError("Exploration must be from 0 to 1")
        p["origin"] = "manual"
        with self._write_lock, self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            return self._insert_profile(con, p, update_base=True)

    def reset_profile(self, ident):
        initial = next((p for p in default_profiles() if p["id"] == ident), None)
        if initial is None:
            initial = self.get_profile(ident)
            initial["weights"] = deepcopy(initial["anchor_weights"])
            initial["search"] = deepcopy(SEARCH_DEFAULTS)
        return self.save_profile(initial)

    def get_learning_head(self, profile_id, key):
        with self._connection() as con:
            row = con.execute("SELECT p.data FROM learning_heads h JOIN profile_versions p ON p.id=h.profile_id AND p.version=h.version WHERE h.profile_id=? AND h.rules_key=?", (profile_id, key)).fetchone()
        return json.loads(row["data"]) if row else None

    def profile_for_rules(self, ident, config):
        base = self.get_profile(ident)
        learned = self.get_learning_head(ident, rules_key(config))
        if learned and learned.get("base_version") == base["version"]:
            return learned
        return base

    def has_learned(self, game_id, profile_id, key):
        with self._connection() as con:
            return bool(con.execute("SELECT 1 FROM learning_receipts WHERE game_id=? AND profile_id=? AND rules_key=?", (game_id, profile_id, key)).fetchone())

    def save_learning_head(self, profile_id, key, profile, gameid):
        """Atomically create a learned version and receipt, or return its prior version."""
        with self._write_lock, self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            receipt = con.execute("SELECT version FROM learning_receipts WHERE game_id=? AND profile_id=? AND rules_key=?", (gameid, profile_id, key)).fetchone()
            if receipt:
                return self._load(con.execute("SELECT data FROM profile_versions WHERE id=? AND version=?", (profile_id, receipt["version"])).fetchone(), profile_id)
            p = deepcopy(profile)
            p["id"] = profile_id
            p["trained_rules"] = key
            p["origin"] = "learning"
            p = self._insert_profile(con, p)
            con.execute("INSERT INTO learning_heads VALUES(?,?,?) ON CONFLICT(profile_id,rules_key) DO UPDATE SET version=excluded.version", (profile_id, key, p["version"]))
            con.execute("INSERT INTO learning_receipts VALUES(?,?,?,?,?)", (gameid, profile_id, key, p["version"], now()))
            return p

    def create_game(self, game):
        g = deepcopy(game)
        g["id"] = g.get("id") or uuid.uuid4().hex
        g.setdefault("revision", 0)
        g.setdefault("status", "playing")
        g.setdefault("winner", None)
        g.setdefault("reason", None)
        g.setdefault("paused", False)
        g.setdefault("seed", 0)
        g.setdefault("batch_id", None)
        g.setdefault("initial_position", deepcopy(g.get("position")))
        g["created_at"] = g.get("created_at") or now()
        g["updated_at"] = now()
        with self._write_lock, self._connection() as con:
            con.execute("INSERT INTO games VALUES(?,?,?,?,?,?,?)", (g["id"], g["revision"], g["created_at"], g["updated_at"], g["status"], g["batch_id"], self._dump(g)))
        return g

    def get_game(self, ident):
        with self._connection() as con:
            row = con.execute("SELECT data FROM games WHERE id=?", (ident,)).fetchone()
        return self._load(row, f"Unknown game: {ident}")

    def list_games(self, limit=100, offset=0):
        with self._connection() as con:
            rows = con.execute("SELECT data FROM games ORDER BY updated_at DESC LIMIT ? OFFSET ?", (max(1, min(int(limit), 1000)), max(0, int(offset)))).fetchall()
        games = []
        for row in rows:
            g = json.loads(row["data"])
            g["ply"] = g.get("position", {}).get("ply", 0)
            g.pop("position", None)
            g.pop("initial_position", None)
            games.append(g)
        return games

    def _update_game(self, con, game, expected_revision):
        g = deepcopy(game)
        current = con.execute("SELECT revision FROM games WHERE id=?", (g["id"],)).fetchone()
        if current is None:
            raise KeyError(f"Unknown game: {g['id']}")
        if expected_revision is not None and current["revision"] != expected_revision:
            raise ValueError("Game changed; refresh its current position")
        g["revision"] = current["revision"] + 1
        g["updated_at"] = now()
        con.execute("UPDATE games SET revision=?,updated_at=?,status=?,batch_id=?,data=? WHERE id=?", (g["revision"], g["updated_at"], g["status"], g.get("batch_id"), self._dump(g), g["id"]))
        return g

    def update_game(self, game, expected_revision=None):
        with self._write_lock, self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            return self._update_game(con, game, expected_revision)

    def commit_move(self, game, move, features, stats=None, expected_revision=None):
        with self._write_lock, self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            g = self._update_game(con, game, expected_revision)
            data = {"revision": g["revision"], "move": move, "position": g["position"], "features": features, "stats": stats, "created_at": g["updated_at"]}
            con.execute("INSERT INTO moves VALUES(?,?,?)", (g["id"], g["revision"], self._dump(data)))
            brief = {k: v for k, v in data.items() if k != "position"}
            con.execute("INSERT INTO move_telemetry VALUES(?,?,?)", (g["id"], g["revision"], self._dump(brief)))
            return g

    def record_move(self, game_id, revision, move, position, features, stats=None):
        data = {"revision": revision, "move": move, "position": position, "features": features, "stats": stats, "created_at": now()}
        with self._write_lock, self._connection() as con:
            con.execute("INSERT INTO moves VALUES(?,?,?)", (game_id, revision, self._dump(data)))
            brief = {k: v for k, v in data.items() if k != "position"}
            con.execute("INSERT INTO move_telemetry VALUES(?,?,?)", (game_id, revision, self._dump(brief)))

    def get_moves(self, gameid, after_revision=None):
        with self._connection() as con:
            if after_revision is None:
                rows = con.execute("SELECT data FROM moves WHERE game_id=? ORDER BY revision", (gameid,)).fetchall()
            else:
                rows = con.execute("SELECT data FROM moves WHERE game_id=? AND revision>? ORDER BY revision", (gameid, int(after_revision))).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def get_recorded_position(self, gameid, position_index):
        """Read one replay snapshot without loading the entire move history."""
        if position_index < 0:
            raise ValueError("Replay position index must be zero or greater")
        if position_index == 0:
            game = self.get_game(gameid)
            if not game.get("initial_position"):
                raise ValueError("The initial position is unavailable")
            return game["initial_position"]
        with self._connection() as con:
            row = con.execute(
                "SELECT data FROM moves WHERE game_id=? ORDER BY revision LIMIT 1 OFFSET ?",
                (gameid, position_index - 1),
            ).fetchone()
        if row is None:
            raise ValueError("Replay position index is outside the recorded game")
        return json.loads(row["data"])["position"]

    def get_move_summaries(self, gameid):
        """Move diagnostics without materializing thousands of large boards."""
        with self._connection() as con:
            rows = con.execute("SELECT data FROM move_telemetry WHERE game_id=? ORDER BY revision", (gameid,)).fetchall()
        if rows:
            return [json.loads(r["data"]) for r in rows]
        # Compatibility with early development databases created before the
        # compact telemetry table was introduced.
        return [{k: v for k, v in m.items() if k != "position"} for m in self.get_moves(gameid)]

    def get_feature_history(self, gameid):
        return [m["features"] for m in self.get_move_summaries(gameid) if m.get("features")]

    def create_batch(self, batch):
        b = deepcopy(batch)
        b["id"] = b.get("id") or uuid.uuid4().hex
        b["created_at"] = b.get("created_at") or now()
        b["updated_at"] = now()
        with self._write_lock, self._connection() as con:
            con.execute("INSERT INTO batches VALUES(?,?,?,?)", (b["id"], b["created_at"], b["updated_at"], self._dump(b)))
        return b

    def get_batch(self, ident):
        with self._connection() as con:
            row = con.execute("SELECT data FROM batches WHERE id=?", (ident,)).fetchone()
        return self._load(row, f"Unknown batch: {ident}")

    def list_batches(self):
        with self._connection() as con:
            rows = con.execute("SELECT data FROM batches ORDER BY created_at DESC").fetchall()
        return [json.loads(r["data"]) for r in rows]

    def update_batch(self, batch):
        b = deepcopy(batch)
        b["updated_at"] = now()
        with self._write_lock, self._connection() as con:
            cursor = con.execute("UPDATE batches SET updated_at=?,data=? WHERE id=?", (b["updated_at"], self._dump(b), b["id"]))
            if not cursor.rowcount:
                raise KeyError(f"Unknown batch: {b['id']}")
        return b

    def backup(self, path):
        target = Path(path).expanduser().resolve()
        if target == self.path:
            raise ValueError("Backup destination must differ from the live database")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._connection() as source:
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        return str(target)

    def export_training(self):
        with self._connection() as con:
            rows = con.execute("SELECT data FROM games WHERE status IN ('checkmate','draw','resigned') ORDER BY created_at").fetchall()
        records = []
        for row in rows:
            g = json.loads(row["data"])
            for move in self.get_move_summaries(g["id"]):
                records.append({"game_id": g["id"], "revision": move["revision"], "config": g["config"], "features": move["features"], "winner": g.get("winner"), "result": g["status"], "reason": g.get("reason"), "players": g.get("players"), "seed": g.get("seed"), "move": move["move"], "stats": move.get("stats"), "engine_version": g.get("engine_version", "1")})
        return records
