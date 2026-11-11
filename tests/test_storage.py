import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from megachess.storage import Storage, rules_key


@pytest.fixture
def db(tmp_path):
    return Storage(tmp_path / "games.sqlite3")


def game_data():
    return {"config": {"n": 3, "setup": "rows"}, "position": {"board": [0, 1, -6], "ply": 0}, "players": {"white": {"kind": "human"}, "black": {"kind": "human"}}}


def test_immutable_profiles_and_manual_edits_invalidate_learned_head(db):
    original = db.get_profile("balanced")
    assert len(db.list_profiles()) == 4
    changed = dict(original, weights={**original["weights"], "pawn": 1.1})
    trained = db.save_learning_head("balanced", rules_key({"n": 3}), changed, "game-a")
    assert db.profile_for_rules("balanced", {"n": 3})["weights"]["pawn"] == 1.1
    assert db.profile_for_rules("balanced", {"n": 5})["version"] == 1
    assert db.get_profile("balanced", 1) == original
    assert trained["version"] > original["version"]
    assert db.list_profiles()[0]["trained_versions"][0]["version"] == trained["version"]
    manual = db.save_profile(original)
    assert manual["version"] > trained["version"]
    assert db.profile_for_rules("balanced", {"n": 3})["version"] == manual["version"]
    assert db.list_profiles()[0]["trained_versions"] == []


def test_learning_receipt_and_head_commit_are_idempotent(db):
    p = db.get_profile("balanced")
    first = db.save_learning_head(p["id"], "rules", p, "finished-game")
    second = db.save_learning_head(p["id"], "rules", p, "finished-game")
    assert first == second
    assert len(db.profile_versions(p["id"])) == 2


def test_atomic_move_commit_and_stale_rejection(db):
    game = db.create_game(game_data())
    game["position"]["ply"] = 1
    saved = db.commit_move(game, {"from": 1, "to": 0}, {"pawn": 1}, {"nodes": 40}, 0)
    assert saved["revision"] == 1
    assert db.get_game(game["id"])["position"]["ply"] == 1
    assert db.get_moves(game["id"])[0]["stats"]["nodes"] == 40
    assert db.get_feature_history(game["id"]) == [{"pawn": 1}]
    assert "position" not in db.get_move_summaries(game["id"])[0]
    with pytest.raises(ValueError):
        db.commit_move(game, {"from": 1, "to": 0}, {}, None, 0)
    assert len(db.get_moves(game["id"])) == 1
    assert db.get_moves(game["id"], after_revision=1) == []
    assert len(db.get_moves(game["id"], after_revision=0)) == 1
    assert "position" not in db.list_games()[0]


def test_revision_conflict_has_one_winner_across_threads(db):
    game = db.create_game(game_data())
    def update(_):
        try:
            return db.update_game(game, expected_revision=0)["revision"]
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(update, range(4)))
    assert results.count(1) == 1
    assert results.count(None) == 3


def test_restart_backup_and_training_export(db, tmp_path):
    game = db.create_game(game_data())
    game.update(status="checkmate", winner=1, reason="checkmate")
    saved = db.commit_move(game, {"from": 1, "to": 0}, {"pawn": 1}, expected_revision=0)
    reopened = Storage(db.path)
    assert reopened.get_game(saved["id"]) == saved
    records = reopened.export_training()
    assert len(records) == 1
    assert records[0]["winner"] == 1
    path = db.backup(tmp_path / "backup.sqlite3")
    copy = Storage(path)
    assert copy.get_game(saved["id"]) == saved
    assert copy.get_moves(saved["id"]) == db.get_moves(saved["id"])
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_batches_resume_and_incomplete_games_excluded(db):
    b = db.create_batch({"status": "paused", "schedule": ["a", "b"], "completed": 1})
    b["status"] = "running"
    db.update_batch(b)
    assert Storage(db.path).get_batch(b["id"])["schedule"] == ["a", "b"]
    game = db.create_game(game_data())
    game["status"] = "cutoff"
    db.commit_move(game, {"from": 1, "to": 0}, {"pawn": 1}, expected_revision=0)
    assert db.export_training() == []


def test_bad_profile_input_does_not_create_a_version(db):
    profile = db.get_profile("balanced")
    profile["weights"]["pawn"] = float("nan")
    with pytest.raises(ValueError):
        db.save_profile(profile)
    assert len(db.profile_versions("balanced")) == 1


def test_rules_key_is_stable():
    assert rules_key({"n": 3, "setup": "blocks"}) == rules_key({"setup": "blocks", "n": 3})
    assert rules_key({"n": 3}) != rules_key({"n": 5})
