import json
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from megachess.engine import Position, apply_move, iter_legal_moves
from megachess.server import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, start_workers=False)) as test_client:
        yield test_client


def create_game(client, **payload):
    response = client.post("/api/v1/games", json={"config": {"n": 1}, "learning": False, **payload})
    assert response.status_code == 200, response.text
    return response.json()["game"]


def test_bootstrap_preview_and_invalid_configuration(client):
    bootstrap = client.get("/api/v1/bootstrap").json()
    assert len(bootstrap["profiles"]) == 4
    assert {30, 60} <= set(bootstrap["capabilities"]["seconds"])
    assert bootstrap["capabilities"]["sizes"] == list(range(1, 66, 2))
    default = client.post("/api/v1/preview", json={}).json()
    assert default["counts"] == {"white": 136, "black": 136}
    assert default["position"]["size"] == 24
    rows = client.post("/api/v1/preview", json={"config": {"n": 5, "setup": "rows"}}).json()
    assert rows["counts"] == {"white": 76, "black": 76}
    assert client.post("/api/v1/preview", json={"config": {"n": 4}}).status_code == 400


def test_human_move_autosave_stale_rejection_and_incremental_updates(client):
    game = create_game(client)
    legal = client.get(f"/api/v1/games/{game['id']}/legal", params={"from_square": 52}).json()
    assert {"from": 52, "to": 36} in legal["moves"]
    payload = {"move": {"from": 52, "to": 36}, "revision": 0}
    response = client.post(f"/api/v1/games/{game['id']}/move", json=payload)
    assert response.status_code == 200
    moved = response.json()["game"]
    assert moved["revision"] == 1
    assert moved["position"] == apply_move(Position.from_dict(game["position"]), payload["move"]).to_dict()
    assert client.post(f"/api/v1/games/{game['id']}/move", json=payload).status_code == 409
    updates = client.get(f"/api/v1/games/{game['id']}/updates", params={"revision": 0}).json()
    assert updates["game"]["revision"] == 1 and len(updates["moves"]) == 1
    fresh = client.get(f"/api/v1/games/{game['id']}/updates", params={"revision": 1}).json()
    assert fresh["moves"] == []
    records = client.get(f"/api/v1/games/{game['id']}").json()["moves"]
    assert records[0]["features"]


def test_maximum_board_preview_play_and_reload(client):
    config = {"n": 65, "setup": "rows", "slider_limit": 520}
    preview = client.post("/api/v1/preview", json={"config": config})
    assert preview.status_code == 200
    assert preview.json()["counts"] == {"white": 976, "black": 976}
    game = create_game(client, config=config, players={
        "white": {"kind": "human"}, "black": {"kind": "human"}})
    assert game["position"]["size"] == 520
    assert len(game["position"]["board"]) == 270400
    source = 518 * 520
    move = {"from": source, "to": source - 1040}
    legal = client.get(f"/api/v1/games/{game['id']}/legal", params={"from_square": source})
    assert move in legal.json()["moves"]
    played = client.post(f"/api/v1/games/{game['id']}/move", json={"move": move, "revision": 0})
    assert played.status_code == 200
    saved = client.get(f"/api/v1/games/{game['id']}").json()
    assert saved["game"]["config"]["n"] == 65
    assert saved["game"]["position"]["ply"] == 1
    assert saved["game"]["position"]["board"][move["to"]] == 1
    assert saved["moves"][0]["move"] == move
    assert saved["moves"][0]["position"] == saved["game"]["position"]
    assert client.post("/api/v1/preview", json={"config": {"n": 67}}).status_code == 400


def test_thirty_and_sixty_second_player_limits_are_saved(client):
    game = create_game(client, players={
        "white": {"kind": "ai", "profile_id": "balanced", "seconds": 30},
        "black": {"kind": "ai", "profile_id": "explorer", "seconds": 60}})
    assert game["players"]["white"]["seconds"] == 30
    assert game["players"]["black"]["seconds"] == 60
    assert game["players"]["white"]["profile"]["id"] != game["players"]["black"]["profile"]["id"]
    assert client.post(f"/api/v1/games/{game['id']}/move", json={"move": {"from": 52, "to": 36}, "revision": 0}).status_code == 400


def test_profiles_clone_edit_reset_and_frozen_game_versions(client):
    profiles = client.get("/api/v1/profiles").json()["profiles"]
    original = next(p for p in profiles if p["id"] == "balanced")
    game = create_game(client, players={"black": {"kind": "ai", "profile_id": "balanced"}})
    edited = {**original, "weights": {**original["weights"], "pawn": 1.4}}
    saved = client.post("/api/v1/profiles", json={"profile": edited}).json()["profile"]
    assert saved["version"] > original["version"]
    assert client.get(f"/api/v1/games/{game['id']}").json()["game"]["players"]["black"]["profile"]["weights"]["pawn"] == 1
    clone = client.post("/api/v1/profiles", json={"profile": saved, "clone": True}).json()["profile"]
    assert clone["id"] != saved["id"] and clone["weights"] == saved["weights"]
    reset = client.post("/api/v1/profiles/balanced/reset").json()["profile"]
    assert reset["weights"]["pawn"] == 1
    versions = client.get("/api/v1/profiles/balanced/versions").json()["versions"]
    assert len(versions) >= 3


def test_json_training_export_and_sqlite_backup(client):
    game = create_game(client)
    response = client.post(f"/api/v1/games/{game['id']}/move", json={"move": {"from": 52, "to": 36}, "revision": 0})
    assert response.status_code == 200
    client.post(f"/api/v1/games/{game['id']}/control", json={"action": "resign"})
    exported = client.get(f"/api/v1/games/{game['id']}/export")
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.json()["format"] == "megachess-game-v1"
    assert len(exported.json()["moves"]) == 1
    training = client.get("/api/v1/training/export")
    rows = [json.loads(line) for line in training.text.splitlines()]
    assert rows and rows[0]["game_id"] == game["id"]
    assert rows[0]["features"] and rows[0]["move"] == {"from": 52, "to": 36}
    backup = Path(client.post("/api/v1/backup").json()["path"])
    assert backup.is_file()
    with sqlite3.connect(backup) as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.execute("SELECT count(*) FROM games").fetchone()[0] == 1


def test_new_game_preserves_previous_match_and_resume_control(client):
    first = create_game(client)
    second = create_game(client)
    assert first["id"] != second["id"]
    saved = client.get(f"/api/v1/games/{first['id']}").json()["game"]
    assert saved["paused"] and saved["status"] == "playing"
    resumed = client.post(f"/api/v1/games/{first['id']}/control", json={"action": "resume"}).json()["game"]
    assert not resumed["paused"]
    assert client.get("/api/v1/games/missing").status_code == 404
