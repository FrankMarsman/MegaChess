from copy import deepcopy
import math

from megachess.learning import learn_from_game, td_update
from megachess.storage import FEATURES, Storage


def feature(**changes):
    return {**dict.fromkeys(FEATURES, 0.0), **changes}


def test_td_uses_outcomes_and_does_not_mutate_frozen_profile(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")
    original = db.get_profile("balanced")
    frozen = deepcopy(original)
    positions = [feature(approach=0.2), feature(approach=0.5)]
    win = td_update(original, [(positions, 1)])
    loss = td_update(original, [(positions, -1)])
    assert win["weights"]["approach"] > original["weights"]["approach"]
    assert loss["weights"]["approach"] < original["weights"]["approach"]
    assert original == frozen


def test_td_bounded_update_and_finite_weights(tmp_path):
    profile = Storage(tmp_path / "test.sqlite3").get_profile("explorer")
    result = td_update(profile, [([feature(pawn=1e6, safety=-1e6)], -1)])
    for key in FEATURES:
        assert math.isfinite(result["weights"][key])
        assert abs(result["weights"][key] - profile["weights"][key]) <= 0.0500001


def finished_game(db, same_ai=False):
    p = db.get_profile("balanced")
    game = db.create_game({"config": {"n": 3, "setup": "rows"}, "position": {}, "players": {
        "white": {"kind": "ai", "profile": p},
        "black": {"kind": "ai", "profile": p} if same_ai else {"kind": "human"},
    }})
    for i in range(3):
        game = db.commit_move(game, {"from": i, "to": i + 1}, feature(approach=(i + 1) / 10), expected_revision=game["revision"])
    game.update(status="checkmate", winner=1, reason="checkmate")
    return db.update_game(game, game["revision"])


def test_exactly_once_learning_and_rules_isolation(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")
    game = finished_game(db)
    result = learn_from_game(db, game)
    assert result["status"] == "learned"
    trained = db.profile_for_rules("balanced", game["config"])
    assert trained["version"] == 2
    assert trained["games_learned"] == 1
    assert db.profile_for_rules("balanced", {"n": 5})["version"] == 1
    assert db.get_profile("aggressive")["version"] == 1
    assert learn_from_game(Storage(db.path), game)["status"] == "already_processed"
    assert len(db.profile_versions("balanced")) == 2


def test_same_ai_both_colors_creates_one_version(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")
    game = finished_game(db, same_ai=True)
    assert len(learn_from_game(db, game)["profiles"]) == 1
    assert db.profile_for_rules("balanced", game["config"])["games_learned"] == 1


def test_learning_disabled_benchmark_and_incomplete_results(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")
    game = finished_game(db)
    for status in ("playing", "cutoff", "aborted"):
        assert learn_from_game(db, {**game, "status": status})["status"] == "ineligible"
    assert learn_from_game(db, {**game, "benchmark": True})["status"] == "disabled"
    assert len(db.profile_versions("balanced")) == 1


def test_serial_updates_recompute_against_latest_head(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")
    first = finished_game(db)
    second = finished_game(db)
    learn_from_game(db, first)
    learn_from_game(db, second)
    head = db.profile_for_rules("balanced", first["config"])
    assert head["version"] == 3
    assert head["games_learned"] == 2
