"""Fair scheduling and durable restart behavior without slow self-play runs."""
import threading

import pytest

from megachess import __version__
from megachess.runtime import Runtime


def batch_payload(**changes):
    return {"config": {"n": 1}, "games": 2, "nodes": 10, "max_plies": 8,
            "workers": 2, "white_profile": "explorer", "black_profile": "aggressive",
            "seed": 31, "learning": False, **changes}


def no_threads(monkeypatch, runtime):
    monkeypatch.setattr(runtime, "_loop", lambda: None)
    monkeypatch.setattr(runtime, "_batch_loop", lambda: None)
    monkeypatch.setattr(runtime, "_learn_loop", lambda: None)


def scheduled_games(runtime, batch):
    saved = runtime.storage.get_batch(batch["id"])
    return [runtime.storage.get_game(item["game_id"]) for item in saved["schedule"]]


def test_benchmarks_have_frozen_profiles_zero_exploration_and_equal_openings(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        batch = runtime.create_batch(batch_payload())
        original = runtime.storage.get_profile("explorer")
        changed = {**original, "weights": {**original["weights"], "pawn": 1.2}}
        runtime.storage.save_profile(changed)
        runtime._batch_tick()
        first, second = scheduled_games(runtime, batch)
        assert first["initial_position"] == second["initial_position"]
        assert first["seed"] == second["seed"]
        assert first["node_budget"] == second["node_budget"] == 10
        assert first["players"]["white"]["profile"]["id"] == second["players"]["black"]["profile"]["id"] == "explorer"
        for game in (first, second):
            assert game["engine_version"] == __version__
            assert game["learning_disabled"]
            assert all(player["profile"]["exploration"] == 0 for player in game["players"].values())
        assert first["players"]["white"]["profile"]["weights"]["pawn"] == 1
        assert runtime.storage.get_profile("explorer")["exploration"] > 0
    finally:
        runtime.close()


def test_odd_game_count_is_rejected(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="even"):
            runtime.create_batch(batch_payload(games=3))
    finally:
        runtime.close()


def test_training_and_evaluation_use_separate_seed_domains(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        training = runtime.create_batch(batch_payload(learning=True))
        benchmark = runtime.create_batch(batch_payload(learning=False))
        runtime._batch_tick()
        trained_pair = scheduled_games(runtime, training)
        held_out_pair = scheduled_games(runtime, benchmark)
        assert trained_pair[0]["seed"] == trained_pair[1]["seed"]
        assert held_out_pair[0]["seed"] == held_out_pair[1]["seed"]
        assert trained_pair[0]["seed"] != held_out_pair[0]["seed"]
    finally:
        runtime.close()


def test_opening_preparation_does_not_hold_supervisor_lock(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = runtime._opening_position
    def slow(config, seed):
        entered.set()
        assert release.wait(2)
        return original(config, seed)
    monkeypatch.setattr(runtime, "_opening_position", slow)
    thread = None
    try:
        runtime.create_batch(batch_payload())
        thread = threading.Thread(target=runtime._batch_tick)
        thread.start()
        assert entered.wait(1)
        assert runtime.lock.acquire(timeout=0.1), "Opening generation blocked deadline supervision"
        runtime.lock.release()
    finally:
        release.set()
        if thread:
            thread.join(timeout=3)
        runtime.close()


def test_completed_learning_cohort_requeues_in_schedule_order_on_restart(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    batch = runtime.create_batch(batch_payload(learning=True))
    runtime._batch_tick()
    games = scheduled_games(runtime, batch)
    for game in games:
        game.update(status="draw", winner=None, reason="repetition")
        runtime.storage.update_game(game, game["revision"])
    saved = runtime.storage.get_batch(batch["id"])
    saved.update(status="completed", learning_queued=True)
    runtime.storage.update_batch(saved)
    runtime.close()
    restarted = Runtime(tmp_path)
    no_threads(monkeypatch, restarted)
    try:
        restarted.start()
        assert [restarted.learning_queue.get_nowait() for _ in games] == [g["id"] for g in games]
    finally:
        restarted.close()


def test_cancel_intent_recovers_partial_per_game_update(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    batch = runtime.create_batch(batch_payload())
    runtime._batch_tick()
    games = scheduled_games(runtime, batch)
    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(runtime.storage, "update_game", crash)
    with pytest.raises(RuntimeError, match="simulated"):
        runtime.control_batch(batch["id"], "cancel")
    assert runtime.storage.get_batch(batch["id"])["status"] == "cancelled"
    runtime.close()
    restarted = Runtime(tmp_path)
    no_threads(monkeypatch, restarted)
    try:
        restarted.start()
        for game in games:
            assert restarted.storage.get_game(game["id"])["status"] == "aborted"
            assert game["id"] not in restarted.tracked
    finally:
        restarted.close()


def test_startup_pages_beyond_storage_limit(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    pages = []
    def fake_games(limit=100, offset=0):
        pages.append((limit, offset))
        return [{"id": str(i), "status": "playing"} for i in range(offset, min(offset + limit, 1001))]
    monkeypatch.setattr(runtime.storage, "list_games", fake_games)
    no_threads(monkeypatch, runtime)
    try:
        runtime.start()
        assert pages == [(1000, 0), (1000, 1000)]
        assert len(runtime.tracked) == 1001
    finally:
        runtime.close()


def test_manual_draw_is_permanently_excluded_from_learning(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        game = runtime.create_game({"config": {"n": 1}, "players": {
            "white": {"kind": "human"}, "black": {"kind": "ai"}}, "learning": True})
        result = runtime.control(game["id"], "draw")
        assert result["learning_disabled"]
        assert runtime.storage.get_game(game["id"])["learning_disabled"]
    finally:
        runtime.close()


def test_per_player_limits_and_viewing_delay(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        game = runtime.create_game({"config": {"n": 1}, "players": {
            "white": {"kind": "human"}, "black": {"kind": "ai", "max_nodes": 100, "max_depth": 3}}})
        assert game["players"]["black"]["max_nodes"] == 100
        assert game["players"]["black"]["max_depth"] == 3
        updated = runtime.control(game["id"], "set_delay", 2.5)
        assert updated["view_delay"] == 2.5
        with pytest.raises(ValueError):
            runtime.control(game["id"], "set_delay", 11)
    finally:
        runtime.close()


def test_metrics_score_pairs_and_exclude_cutoffs(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        batch = runtime.create_batch(batch_payload(games=4, workers=4))
        runtime._batch_tick()
        games = scheduled_games(runtime, batch)
        for i, game in enumerate(games):
            game.update(status="checkmate" if i < 2 else "cutoff", winner=1 if i == 0 else -1 if i == 1 else None)
            runtime.storage.update_game(game, game["revision"])
        runtime._batch_tick()
        done = runtime.storage.get_batch(batch["id"])
        assert done["results"] == {"a_wins": 2, "b_wins": 0, "draws": 0, "cutoffs": 2}
        assert done["metrics"]["completed_pairs"] == 2
        assert done["metrics"]["scored_pairs"] == 1
        assert done["metrics"]["a_pair_score"] == 1
        assert done["metrics"]["cutoff_rate"] == 0.5
        assert done["metrics"]["score_ci95"][1] == 1
        assert done["status"] == "completed"
    finally:
        runtime.close()
