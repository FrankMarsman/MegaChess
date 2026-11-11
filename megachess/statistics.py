"""Compact, authoritative statistics for a displayed game position."""
from collections import OrderedDict
from copy import deepcopy
import threading

from .engine import PAWN, Position, in_check, iter_legal_moves


PIECE_NAMES = ("pawn", "knight", "bishop", "rook", "queen", "king")
MATERIAL_VALUES = (0, 1, 3.2, 3.3, 5, 9, 0)


def position_statistics(data: dict) -> dict:
    """Count the actual board, including promoted pieces, without changing it.

    Legal moves refer to the side to move. Distinct promotion choices count as
    distinct moves, including when a pawn promotes while capturing.
    """
    position = Position.from_dict(data)
    sides = {}
    for label, color in (("white", 1), ("black", -1)):
        counts = {name: len(position.pieces[color * kind])
                  for kind, name in enumerate(PIECE_NAMES, 1)}
        sides[label] = {
            "pieces": sum(counts.values()),
            "counts": counts,
            "material": round(sum(counts[name] * MATERIAL_VALUES[kind]
                                  for kind, name in enumerate(PIECE_NAMES, 1)), 1),
        }
    moves = captures = 0
    for move in iter_legal_moves(position):
        moves += 1
        en_passant = (abs(position.board[move["from"]]) == PAWN
                      and move["to"] == position.ep_target
                      and position.ep_pawn is not None)
        if position.board[move["to"]] or en_passant:
            captures += 1
    occupied = sides["white"]["pieces"] + sides["black"]["pieces"]
    squares = position.size ** 2
    return {
        "ply": position.ply,
        "turn": position.turn,
        "legal_moves": moves,
        "legal_captures": captures,
        "in_check": in_check(position),
        "sides": sides,
        "material_balance": round(sides["white"]["material"] - sides["black"]["material"], 1),
        "occupied": occupied,
        "empty": squares - occupied,
        "board_squares": squares,
        "halfmove": position.halfmove,
    }


class StatisticsCache:
    """Bound memory by revisions, not by game length or total saved games."""

    def __init__(self, capacity=128):
        self.capacity = max(1, capacity)
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple, data: dict) -> dict:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return deepcopy(self._items[key])
        # Move generation must not hold the game coordinator or cache lock.
        result = position_statistics(data)
        with self._lock:
            self._items[key] = result
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
        return deepcopy(result)
