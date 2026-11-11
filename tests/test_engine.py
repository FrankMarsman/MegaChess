import random

import pytest

from megachess.engine import (Position, _make, _unmake, apply_move, extract_features,
                              game_status, in_check, iter_legal_moves, new_position, position_key)


def fixture(pieces, turn=1, rights=(), ep_target=None, ep_pawn=None, **config):
    config = {"n": 1, **config}
    board = [0] * (8 * config["n"]) ** 2
    for square, piece in pieces.items():
        board[square] = piece
    return Position(config, board, turn, set(rights), ep_target, ep_pawn)


@pytest.mark.parametrize("n", [1, 3, 5, 7, 9, 11, 13])
@pytest.mark.parametrize("setup", ["blocks", "rows"])
@pytest.mark.parametrize("placement", ["original", "center"])
def test_setup_counts_mirroring_and_king_anchors(n, setup, placement):
    pos = new_position({"n": n, "setup": setup, "king_placement": placement})
    expected = 15 * n ** (2 if setup == "blocks" else 1) + 1
    assert sum(piece > 0 for piece in pos.board) == expected
    assert sum(piece < 0 for piece in pos.board) == expected
    for row in range(pos.size):
        for col in range(pos.size):
            assert pos.board[row * pos.size + col] == -pos.board[(pos.size - 1 - row) * pos.size + col]
    home = (n - 1) // 2 if setup == "blocks" else 0
    col = 4 * n + ((n - 1) // 2 if setup == "blocks" and placement == "original" else 0)
    assert pos.kings[1] == (pos.size - 1 - home) * pos.size + col
    assert pos.kings[-1] == home * pos.size + col
    assert sum(abs(piece) == 6 for piece in pos.board) == 2


@pytest.mark.parametrize("bad", [0, 2, 4, -1, 15, 3.0, True])
def test_reject_bad_size(bad):
    with pytest.raises(ValueError):
        new_position({"n": bad})


def perft(position, depth):
    if depth == 0:
        return 1
    result = 0
    for move in iter_legal_moves(position):
        old = _make(position, move)
        result += perft(position, depth - 1)
        _unmake(position, move, old)
    return result


def test_reference_move_generation_and_reversal():
    pos = new_position({"n": 1})
    before = pos.to_dict()
    assert [perft(pos, depth) for depth in (1, 2, 3)] == [20, 400, 8902]
    assert pos.to_dict() == before
    assert pos.hash == pos._calculate_hash()


@pytest.mark.parametrize("piece,delta", [(4, (0, 1)), (3, (-1, 1)), (5, (0, 1)), (5, (-1, 1))])
def test_slider_limit_and_blockers(piece, delta):
    size = 24
    origin = 12 * size + 12
    dr, dc = delta
    target8 = (12 + 8 * dr) * size + 12 + 8 * dc
    target9 = (12 + 9 * dr) * size + 12 + 9 * dc
    pos = fixture({23 * size: 6, 23: -6, origin: piece}, n=3, slider_limit=8)
    destinations = {m["to"] for m in iter_legal_moves(pos, origin)}
    assert target8 in destinations and target9 not in destinations
    unlimited = fixture({23 * size: 6, 23: -6, origin: piece}, n=3)
    assert target9 in {m["to"] for m in iter_legal_moves(unlimited, origin)}
    blocker = (12 + 2 * dr) * size + 12 + 2 * dc
    blocked = fixture({23 * size: 6, 23: -6, origin: piece, blocker: 1}, n=3)
    assert target8 not in {m["to"] for m in iter_legal_moves(blocked, origin)}


def test_pin_and_king_adjacency():
    pos = fixture({60: 6, 52: 4, 4: -4, 0: -6})
    assert not in_check(pos)
    assert all(move["to"] % 8 == 4 for move in iter_legal_moves(pos, 52))
    with pytest.raises(ValueError):
        apply_move(pos, {"from": 52, "to": 51})
    adjacent = fixture({60: 6, 44: -6, 56: 4})
    assert 52 not in {m["to"] for m in iter_legal_moves(adjacent, 60)}
    assert {"from": 60, "to": 62} not in list(iter_legal_moves(adjacent))


def test_block_pawns_have_first_move_rights_at_each_original_square():
    pos = new_position({"n": 3})
    source = 18 * 24
    assert {"from": source, "to": 16 * 24} in list(iter_legal_moves(pos, source))
    assert not list(iter_legal_moves(pos, 19 * 24))


def test_en_passant_and_round_trip():
    pos = fixture({60: 6, 4: -6, 28: 1, 11: -1}, turn=-1, rights=[11])
    pos = apply_move(pos, {"from": 11, "to": 27})
    assert pos.ep_target == 19 and pos.ep_pawn == 27
    assert {"from": 28, "to": 19} in list(iter_legal_moves(pos))
    pos = apply_move(pos, {"from": 28, "to": 19})
    assert pos.board[19] == 1 and pos.board[27] == 0
    assert Position.from_dict(pos.to_dict()).to_dict() == pos.to_dict()
    assert pos.hash == pos._calculate_hash()


def test_illegal_en_passant_does_not_change_repetition_identity():
    pieces = {31: 6, 30: 1, 24: -4, 29: -1, 0: -6}
    pos = fixture(pieces, ep_target=21, ep_pawn=29)
    assert {"from": 30, "to": 21} not in list(iter_legal_moves(pos))
    assert position_key(pos) == position_key(fixture(pieces))


def test_promotion_choices():
    pos = fixture({63: 6, 7: -6, 8: 1})
    moves = [m for m in iter_legal_moves(pos, 8) if m["to"] == 0]
    assert {m["promotion"] for m in moves} == {2, 3, 4, 5}
    assert apply_move(pos, {"from": 8, "to": 0, "promotion": 2}).board[0] == 2
    with pytest.raises(ValueError):
        apply_move(pos, {"from": 8, "to": 0})


def test_mate_stalemate_and_bare_kings():
    assert game_status(fixture({0: -6, 9: 5, 18: 6}, turn=-1))["reason"] == "checkmate"
    assert game_status(fixture({0: -6, 17: 5, 18: 6}, turn=-1))["reason"] == "stalemate"
    assert game_status(fixture({0: -6, 63: 6}))["reason"] == "bare_kings"


def test_repetition_and_no_progress():
    pos = fixture({60: 6, 4: -6, 62: 2, 6: -2})
    cycle = [{"from": 62, "to": 45}, {"from": 6, "to": 21}, {"from": 45, "to": 62}, {"from": 21, "to": 6}]
    for move in cycle * 2:
        pos = apply_move(pos, move)
    assert game_status(pos)["reason"] == "threefold_repetition"
    pos.config["no_progress_limit"] = 2
    pos.history = [pos.hash]
    pos.counts = {pos.hash: 1}
    assert game_status(pos)["reason"] == "no_progress"


def test_random_make_unmake_hash_and_feature_consistency():
    rng = random.Random(12)
    pos = new_position({"n": 3})
    for _ in range(35):
        moves = list(iter_legal_moves(pos))
        if not moves:
            break
        before = pos.to_dict()
        before_features = extract_features(pos)
        before_signature = pos.repetition_signature
        move = rng.choice(moves)
        old = _make(pos, move)
        assert pos.hash == pos._calculate_hash()
        rebuilt = Position.from_dict(pos.to_dict())
        assert pos.repetition_signature == rebuilt.repetition_signature
        assert extract_features(pos) == pytest.approx(extract_features(rebuilt), abs=1e-10)
        _unmake(pos, move, old)
        assert pos.to_dict() == before
        assert pos.repetition_signature == before_signature
        assert extract_features(pos) == before_features
        _make(pos, move)


def test_budget_exception_leaves_position_unchanged():
    pos = new_position({"n": 13})
    before = pos.to_dict()
    calls = 0
    def budget():
        nonlocal calls
        calls += 1
        if calls > 25:
            raise TimeoutError()
    with pytest.raises(TimeoutError):
        list(iter_legal_moves(pos, check_budget=budget))
    assert pos.to_dict() == before
