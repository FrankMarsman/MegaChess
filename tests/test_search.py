import time

from megachess.engine import Position, apply_move, iter_legal_moves, new_position
from megachess.search import Search, search_position


def test_legal_fallback_precedes_completed_iterations():
    pos = new_position({"n": 3})
    reports = []
    result = search_position(pos.to_dict(), {}, {"seconds": 1, "depth": 2}, reports.append)
    assert reports[0]["event"] == "Legal fallback ready"
    assert reports[0]["completed_depth"] == 0
    assert reports[0]["move"] in list(iter_legal_moves(pos))
    assert result["completed_depth"] == 2
    assert result["nodes"] > 0 and result["cutoffs"] > 0
    assert result["move"] in list(iter_legal_moves(pos))


def test_node_budget_reproducibility_and_input_unchanged():
    data = new_position({"n": 3}).to_dict()
    profile = {"exploration": 0.15}
    limits = {"seconds": 10, "nodes": 180, "seed": 7}
    first = search_position(data, profile, limits, lambda _: None)
    second = search_position(data, profile, limits, lambda _: None)
    assert first["move"] == second["move"]
    assert first["pv"] == second["pv"]
    assert first["nodes"] == second["nodes"] == 180
    assert first["completed_depth"] == second["completed_depth"]
    assert first["reason"] == "node_budget"
    assert data == new_position({"n": 3}).to_dict()


def test_large_board_deadline_and_progress():
    pos = new_position({"n": 13})
    reports = []
    started = time.monotonic()
    result = search_position(pos.to_dict(), {}, {"seconds": 0.35}, reports.append)
    assert time.monotonic() - started < 0.65
    assert result["reason"] == "deadline"
    assert result["move"] is not None
    apply_move(pos, result["move"])
    assert len(reports) >= 3


def test_expired_deadline_does_not_invent_a_move_or_terminal_status():
    result = search_position(new_position({"n": 1}).to_dict(), {},
                             {"seconds": 30, "deadline": time.monotonic() - 1}, lambda _: None)
    assert result["move"] is None
    assert result["reason"] == "deadline"


def test_search_finds_forced_mate_and_avoids_king_capture():
    board = [0] * 64
    board[0], board[18], board[17] = -6, 6, 5
    pos = Position({"n": 1}, board)
    result = search_position(pos.to_dict(), {"search": {"mode": "full"}},
                             {"seconds": 1, "depth": 2}, lambda _: None)
    next_pos = apply_move(pos, result["move"])
    from megachess.engine import game_status
    assert game_status(next_pos)["reason"] == "checkmate"
    assert result["reason"] == "mate_found"


def test_full_width_does_not_apply_candidate_limits():
    result = search_position(new_position({"n": 1}).to_dict(),
                             {"search": {"mode": "full", "root_width": 1, "width": 1}},
                             {"seconds": 1, "depth": 1}, lambda _: None)
    assert result["root_total"] == result["root_legal_total"] == 20
    assert result["candidates_skipped"] == 0


def test_transposition_bounds_do_not_cross_repetition_histories():
    original = new_position({"n": 1})
    data = original.to_dict()
    # Same board, same side and halfmove count, and only one occurrence of the
    # current board. Other positions in the past can nevertheless be repeated
    # by a descendant and change a search value to a draw.
    earlier = apply_move(original, {"from": 62, "to": 45})
    data["history"] = [f"{earlier.hash:016x}", f"{earlier.hash:016x}", f"{original.hash:016x}"]
    contextual = Position.from_dict(data)
    assert contextual.hash == original.hash
    assert contextual.counts[contextual.hash] == original.counts[original.hash] == 1
    a = Search(original, {}, {"seconds": 1}, lambda _: None)
    b = Search(contextual, {}, {"seconds": 1}, lambda _: None)
    assert a.tt_key() != b.tt_key()
    assert contextual.repetition_signature == Position.from_dict(contextual.to_dict()).repetition_signature
