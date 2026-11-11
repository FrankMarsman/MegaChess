from fastapi.testclient import TestClient

from megachess.engine import Position, apply_move, new_position
from megachess.server import create_app
from megachess.statistics import StatisticsCache, position_statistics


def sparse_position(pieces, turn=1, rights=()):
    board = [0] * 64
    for square, piece in pieces.items():
        board[square] = piece
    return Position({"n": 1}, board, turn=turn, pawn_rights=set(rights))


def test_reference_counts_legal_moves_and_no_state_mutation():
    position = new_position({"n": 1})
    original = position.to_dict()
    stats = position_statistics(original)
    assert original == position.to_dict()
    assert stats["legal_moves"] == 20
    assert stats["legal_captures"] == 0
    assert stats["occupied"] == 32 and stats["empty"] == 32
    assert stats["material_balance"] == 0
    for side in ("white", "black"):
        assert stats["sides"][side] == {
            "pieces": 16,
            "counts": {"pawn": 8, "knight": 2, "bishop": 2, "rook": 2, "queen": 1, "king": 1},
            "material": 40.0,
        }


def test_actual_capture_changes_remaining_material():
    position = new_position({"n": 1})
    for move in ({"from": 52, "to": 36}, {"from": 11, "to": 27}, {"from": 36, "to": 27}):
        position = apply_move(position, move)
    stats = position_statistics(position.to_dict())
    assert stats["sides"]["white"]["pieces"] == 16
    assert stats["sides"]["black"]["counts"]["pawn"] == 7
    assert stats["sides"]["black"]["pieces"] == 15
    assert stats["material_balance"] == 1
    assert stats["occupied"] == 31 and stats["empty"] == 33
    assert stats["ply"] == 3 and stats["turn"] == -1 and stats["halfmove"] == 0


def test_en_passant_is_counted_as_a_legal_capture():
    position = sparse_position({60: 6, 4: -6, 28: 1, 11: -1}, turn=-1, rights=[11])
    position = apply_move(position, {"from": 11, "to": 27})
    assert position.board[position.ep_target] == 0
    stats = position_statistics(position.to_dict())
    assert stats["legal_captures"] == 1
    captured = apply_move(position, {"from": 28, "to": 19})
    assert position_statistics(captured.to_dict())["material_balance"] == 1


def test_capture_promotion_choices_are_distinct_legal_moves():
    position = sparse_position({60: 6, 7: -6, 8: 1, 1: -4})
    stats = position_statistics(position.to_dict())
    assert stats["legal_captures"] == 4
    promoted = apply_move(position, {"from": 8, "to": 1, "promotion": 5})
    after = position_statistics(promoted.to_dict())
    assert after["sides"]["white"]["counts"]["pawn"] == 0
    assert after["sides"]["white"]["counts"]["queen"] == 1
    assert after["sides"]["black"]["counts"]["rook"] == 0
    assert after["material_balance"] == 9


def test_piece_counts_include_multiple_promoted_queens():
    stats = position_statistics(sparse_position({60: 6, 4: -6, 27: 5, 35: 5, 10: -1}).to_dict())
    assert stats["sides"]["white"]["counts"]["queen"] == 2
    assert stats["material_balance"] == 17


def test_cache_is_revision_specific_bounded_and_returns_independent_data(monkeypatch):
    import megachess.statistics as module
    calls = []
    actual = module.position_statistics

    def counted(data):
        calls.append(True)
        return actual(data)

    monkeypatch.setattr(module, "position_statistics", counted)
    cache = StatisticsCache(capacity=2)
    position = new_position({"n": 1}).to_dict()
    result = cache.get(("game", 0, None), position)
    result["sides"]["white"]["pieces"] = 0
    assert cache.get(("game", 0, None), position)["sides"]["white"]["pieces"] == 16
    assert len(calls) == 1
    cache.get(("game", 1, None), position)
    cache.get(("game", 1, 0), position)
    cache.get(("game", 0, None), position)
    assert len(calls) == 4


def test_statistics_endpoint_replay_uses_recorded_move_index_not_revision(tmp_path):
    with TestClient(create_app(tmp_path, start_workers=False)) as client:
        game = client.post("/api/v1/games", json={"config": {"n": 1}, "learning": False}).json()["game"]
        ident = game["id"]
        initial = client.get(f"/api/v1/games/{ident}/statistics", params={"revision": 0}).json()
        assert initial["position_index"] is None and initial["legal_moves"] == 20
        # Control revisions must not become replay-position indices.
        game = client.post(f"/api/v1/games/{ident}/control", json={"action": "pause"}).json()["game"]
        for move in ({"from": 52, "to": 36}, {"from": 11, "to": 27}, {"from": 36, "to": 27}):
            game = client.post(f"/api/v1/games/{ident}/move", json={"revision": game["revision"], "move": move}).json()["game"]
        response = client.get(f"/api/v1/games/{ident}/statistics", params={"revision": game["revision"]})
        assert response.status_code == 200
        assert response.json()["material_balance"] == 1
        assert response.json()["revision"] == game["revision"]
        before_capture = client.get(f"/api/v1/games/{ident}/statistics", params={"revision": game["revision"], "position_index": 2}).json()
        assert before_capture["position_index"] == 2 and before_capture["ply"] == 2
        assert before_capture["occupied"] == 32 and before_capture["material_balance"] == 0
        initial_replay = client.get(f"/api/v1/games/{ident}/statistics", params={"position_index": 0}).json()
        assert initial_replay["ply"] == 0 and initial_replay["legal_moves"] == 20
        assert client.get(f"/api/v1/games/{ident}/statistics", params={"revision": 0}).status_code == 409
        assert client.get(f"/api/v1/games/{ident}/statistics", params={"position_index": -1}).status_code == 400
        assert client.get(f"/api/v1/games/{ident}/statistics", params={"position_index": 4}).status_code == 400


def test_large_formation_counts(tmp_path):
    position = new_position({"n": 13})
    stats = position_statistics(position.to_dict())
    assert stats["sides"]["white"]["pieces"] == 2536
    assert stats["sides"]["black"]["pieces"] == 2536
    assert stats["sides"]["white"]["counts"]["pawn"] == 1352
    assert stats["board_squares"] == 10816
    assert stats["legal_moves"] > 0
