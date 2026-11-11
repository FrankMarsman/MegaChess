"""Real-process integration checks for the deadline supervisor and durable jobs."""
import time

import pytest

from megachess.engine import Position, apply_move
from megachess.runtime import Runtime


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for runtime condition")


@pytest.fixture
def runtime(tmp_path):
    rt = Runtime(tmp_path)
    rt.start()
    try:
        yield rt
    finally:
        rt.close()


def ai_game(rt, seconds=0.7):
    return rt.create_game({"config": {"n": 3}, "players": {
        "white": {"kind": "ai", "profile_id": "balanced", "seconds": seconds},
        "black": {"kind": "human"}}, "learning": False, "view_delay": 0})


def working(rt, game_id):
    stats = rt.search_status(game_id)
    return stats if stats and stats.get("thinking") and stats.get("nodes", 0) > 0 else None


def moved(rt, game_id):
    game = rt.storage.get_game(game_id)
    return game if game["position"]["ply"] >= 1 else None


def test_real_deadline_returns_legal_move_with_live_statistics(runtime):
    game = ai_game(runtime, seconds=0.7)
    live = wait_for(lambda: working(runtime, game["id"]))
    assert live["remaining"] > 0
    assert live["nodes"] > 0
    assert live["elapsed"] < 0.9
    done = wait_for(lambda: moved(runtime, game["id"]))
    records = runtime.storage.get_moves(game["id"])
    assert len(records) == 1
    record = records[0]
    expected = apply_move(Position.from_dict(game["position"]), record["move"])
    assert done["position"] == expected.to_dict()
    assert record["stats"]["elapsed"] <= 1.1
    assert record["stats"]["reason"] == "deadline"
    assert record["stats"]["nodes"] >= live["nodes"]
    assert record["stats"]["events"]


def test_move_now_uses_a_legal_result_without_waiting_for_full_budget(runtime):
    game = ai_game(runtime, seconds=5)
    live = wait_for(lambda: working(runtime, game["id"]))
    started = time.monotonic()
    runtime.control(game["id"], "move_now")
    wait_for(lambda: moved(runtime, game["id"]))
    assert time.monotonic() - started < 0.6
    record = runtime.storage.get_moves(game["id"])[0]
    apply_move(Position.from_dict(game["position"]), record["move"])
    assert record["stats"]["reason"] == "move_now"
    assert record["stats"]["nodes"] >= live["nodes"]


def test_pause_discards_search_and_prevents_late_moves(runtime):
    game = ai_game(runtime, seconds=0.6)
    wait_for(lambda: working(runtime, game["id"]))
    paused = runtime.control(game["id"], "pause")
    assert paused["paused"]
    time.sleep(0.7)
    saved = runtime.storage.get_game(game["id"])
    assert saved["revision"] == paused["revision"]
    assert saved["position"] == game["position"]
    assert runtime.search_status(game["id"])["reason"] == "cancelled"


def test_one_search_deadline_does_not_stop_another(runtime):
    first = ai_game(runtime, seconds=0.6)
    second = ai_game(runtime, seconds=1.6)
    runtime.control(first["id"], "resume")
    wait_for(lambda: working(runtime, first["id"]))
    wait_for(lambda: working(runtime, second["id"]))
    wait_for(lambda: moved(runtime, first["id"]))
    assert runtime.storage.get_game(second["id"])["revision"] == 0
    assert runtime.search_status(second["id"])["thinking"]
    wait_for(lambda: moved(runtime, second["id"]))
    assert runtime.storage.get_moves(first["id"])[0]["stats"]["reason"] == "deadline"
    assert runtime.storage.get_moves(second["id"])[0]["stats"]["reason"] == "deadline"


def test_small_batch_finishes_as_cutoffs_and_survives_restart(tmp_path):
    rt = Runtime(tmp_path)
    rt.start()
    try:
        batch = rt.create_batch({"config": {"n": 1}, "games": 2, "nodes": 10,
                                 "max_plies": 8, "workers": 2, "learning": False})
        def any_move():
            current = rt.storage.get_batch(batch["id"])
            return any(item.get("game_id") and rt.storage.get_game(item["game_id"])["revision"]
                       for item in current["schedule"])
        wait_for(any_move)
        paused = rt.control_batch(batch["id"], "pause")
        assert paused["status"] == "paused"
        scheduled = rt.storage.get_batch(batch["id"])["schedule"]
    finally:
        rt.close()
    rt = Runtime(tmp_path)
    rt.start()
    try:
        restored = rt.storage.get_batch(batch["id"])
        assert restored["schedule"] == scheduled
        assert restored["status"] == "paused"
        rt.control_batch(batch["id"], "resume")
        done = wait_for(lambda: (b if (b := rt.storage.get_batch(batch["id"]))["status"] == "completed" else None), timeout=12)
        assert done["completed"] == 2
        assert done["results"] == {"a_wins": 0, "b_wins": 0, "draws": 0, "cutoffs": 2}
        games = [rt.storage.get_game(item["game_id"]) for item in done["schedule"]]
        assert all(g["status"] == "cutoff" and g["position"]["ply"] == 8 for g in games)
        assert games[0]["initial_position"] == games[1]["initial_position"]
        assert games[0]["players"]["white"]["profile"]["id"] == games[1]["players"]["black"]["profile"]["id"]
        assert all(len(rt.storage.get_moves(g["id"])) == 4 for g in games)
    finally:
        rt.close()


def test_actual_checkmate_automatically_learns_once_and_keeps_match_frozen(tmp_path):
    board = [0] * 64
    board[0], board[18], board[17] = -6, 6, 5
    initial = Position({"n": 1}, board)
    rt = Runtime(tmp_path)
    rt.start()
    try:
        game = rt.create_game({"players": {
            "white": {"kind": "ai", "profile_id": "balanced", "seconds": 0.5},
            "black": {"kind": "human"}}, "learning": True, "view_delay": 0}, initial=initial)
        frozen = game["players"]["white"]["profile"]
        finished = wait_for(lambda: (g if (g := rt.storage.get_game(game["id"]))["status"] == "checkmate" else None))
        assert finished["winner"] == 1
        assert finished["position"]["ply"] == 1
        def learned_head():
            head = rt.storage.profile_for_rules("balanced", initial.config)
            return head if head["version"] > frozen["version"] else None
        head = wait_for(learned_head)
        assert head["games_learned"] == 1
        assert head["last_training_game"] == game["id"]
        assert head["weights"] != frozen["weights"]
        assert rt.storage.get_game(game["id"])["players"]["white"]["profile"] == frozen
        versions = len(rt.storage.profile_versions("balanced"))
        learned_version = head["version"]
    finally:
        rt.close()
    rt = Runtime(tmp_path)
    rt.start()
    try:
        # Restart re-enqueues completed games. Both that notification and a
        # direct retry must be harmless because the learning receipt is durable.
        from megachess.learning import learn_from_game
        retry = learn_from_game(rt.storage, rt.storage.get_game(game["id"]))
        assert retry["status"] == "already_processed"
        time.sleep(0.12)
        restored = rt.storage.profile_for_rules("balanced", initial.config)
        assert restored["version"] == learned_version
        assert restored["games_learned"] == 1
        assert len(rt.storage.profile_versions("balanced")) == versions
        assert rt.storage.get_game(game["id"])["players"]["white"]["profile"] == frozen
    finally:
        rt.close()
