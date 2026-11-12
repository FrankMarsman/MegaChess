"""Pure, size-independent Mega Chess rules. Coordinates start at Black's home edge.

The public API returns new positions; search uses reversible internal moves so it
never copies the entire board at each node.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from functools import lru_cache
import math
import random
from typing import Callable, Iterator

PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING = range(1, 7)
MAX_N = 65
SUPPORTED_N = tuple(range(1, MAX_N + 1, 2))
FEATURE_NAMES = ("pawn", "knight", "bishop", "rook", "queen", "advancement", "activity", "approach", "safety")
MATERIAL_NAMES = {1: "pawn", 2: "knight", 3: "bishop", 4: "rook", 5: "queen"}
ORTHOGONAL = ((-1, 0), (1, 0), (0, -1), (0, 1))
DIAGONAL = ((-1, -1), (-1, 1), (1, -1), (1, 1))
KNIGHT_STEPS = ((-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1))
Move = dict[str, int]
_MASK64 = (1 << 64) - 1


def _repetition_token(key: int, count: int) -> int:
    """Stable count-sensitive token for the draw-relevant history multiset."""
    value = (key + count * 0x9E3779B97F4A7C15) & _MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return value ^ (value >> 31)


def validate_config(config: dict) -> dict:
    n = config.get("n", 3)
    if isinstance(n, bool) or not isinstance(n, int) or n not in SUPPORTED_N:
        raise ValueError(f"N must be 1 or an odd number from 3 to {MAX_N}")
    setup = config.get("setup", "blocks")
    placement = config.get("king_placement", "original")
    limit = config.get("slider_limit")
    progress = config.get("no_progress_limit")
    if setup not in ("blocks", "rows"):
        raise ValueError("Unknown starting formation")
    if placement not in ("original", "center"):
        raise ValueError("Unknown king placement")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("Sliding range must be a positive square count")
    if progress is not None and (isinstance(progress, bool) or not isinstance(progress, int) or progress < 1):
        raise ValueError("No-progress limit must be a positive full-move count")
    return {"n": n, "setup": setup, "king_placement": placement, "slider_limit": limit, "no_progress_limit": progress}


@lru_cache(maxsize=7)
def _tokens(size: int):
    rng = random.Random(0x4D4547414348455353 + size)
    # Preserve the token sequence used by saved games without a Python integer
    # object per entry; the largest board has more than four million tokens.
    return (array("Q", (rng.getrandbits(64) for _ in range(size * size * 13))),
            array("Q", (rng.getrandbits(64) for _ in range(size * size))),
            array("Q", (rng.getrandbits(64) for _ in range(size * size))), rng.getrandbits(64))


@dataclass
class Position:
    config: dict
    board: list[int]
    turn: int = 1
    pawn_rights: set[int] = field(default_factory=set)
    ep_target: int | None = None
    ep_pawn: int | None = None
    halfmove: int = 0
    ply: int = 0
    history: list[int] = field(default_factory=list)
    size: int = field(init=False)
    pieces: dict[int, set[int]] = field(init=False, repr=False)
    kings: dict[int, int] = field(init=False, repr=False)
    totals: dict[str, float] = field(init=False, repr=False)
    hash: int = field(init=False, repr=False)
    counts: dict[int, int] = field(init=False, repr=False)
    repetition_signature: int = field(init=False, repr=False)
    army_factor: float = field(init=False, repr=False)

    def __post_init__(self):
        self.config = validate_config(self.config)
        self.size = self.config["n"] * 8
        if len(self.board) != self.size ** 2 or self.turn not in (1, -1):
            raise ValueError("Invalid board size or player")
        if any(isinstance(p, bool) or not isinstance(p, int) or abs(p) > KING for p in self.board):
            raise ValueError("Invalid piece")
        self.pieces = {p: set() for p in range(-6, 7) if p}
        for square, piece in enumerate(self.board):
            if piece:
                self.pieces[piece].add(square)
        if len(self.pieces[KING]) != 1 or len(self.pieces[-KING]) != 1:
            raise ValueError("A position must have exactly one king per side")
        self.kings = {1: next(iter(self.pieces[KING])), -1: next(iter(self.pieces[-KING]))}
        if any(not isinstance(i, int) or not 0 <= i < len(self.board) or abs(self.board[i]) != PAWN for i in self.pawn_rights):
            raise ValueError("Invalid pawn first-move rights")
        if (self.ep_target is None) != (self.ep_pawn is None):
            raise ValueError("Incomplete en passant state")
        if self.ep_target is not None and (not 0 <= self.ep_target < len(self.board) or not 0 <= self.ep_pawn < len(self.board) or self.board[self.ep_target] or self.board[self.ep_pawn] != -self.turn):
            raise ValueError("Invalid en passant state")
        self.army_factor = math.sqrt((15 * (self.config["n"] ** (2 if self.config["setup"] == "blocks" else 1)) + 1) / 16)
        self.totals = {name: 0.0 for name in FEATURE_NAMES}
        for square, piece in enumerate(self.board):
            if piece:
                self._adjust_features(piece, square, 1)
        self.hash = self._calculate_hash()
        if not self.history or self.history[-1] != self.hash:
            self.history.append(self.hash)
        self.counts = {}
        for key in self.history:
            self.counts[key] = self.counts.get(key, 0) + 1
        self.repetition_signature = 0
        for key, count in self.counts.items():
            self.repetition_signature ^= _repetition_token(key, count)

    def to_dict(self) -> dict:
        return {"config": dict(self.config), "size": self.size, "board": list(self.board), "turn": self.turn,
                "pawn_rights": sorted(self.pawn_rights), "ep_target": self.ep_target, "ep_pawn": self.ep_pawn,
                "halfmove": self.halfmove, "ply": self.ply, "history": [f"{h:016x}" for h in self.history]}

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        return cls(config=dict(data["config"]), board=list(data["board"]), turn=int(data.get("turn", 1)),
                   pawn_rights=set(data.get("pawn_rights", [])), ep_target=data.get("ep_target"), ep_pawn=data.get("ep_pawn"),
                   halfmove=int(data.get("halfmove", 0)), ply=int(data.get("ply", 0)),
                   history=[int(h, 16) if isinstance(h, str) else int(h) for h in data.get("history", [])])

    def _adjust_features(self, piece: int, square: int, multiplier: int):
        color = 1 if piece > 0 else -1
        kind = abs(piece)
        sign = color * multiplier
        if kind in MATERIAL_NAMES:
            self.totals[MATERIAL_NAMES[kind]] += sign
        row, col = divmod(square, self.size)
        span = self.size - 1
        if kind == PAWN:
            self.totals["advancement"] += sign * ((span - row) if color > 0 else row) / span
        if kind != KING:
            central = 1 - (abs(2 * row - span) + abs(2 * col - span)) / (2 * span)
            self.totals["activity"] += sign * central
            er, ec = divmod(self.kings[-color], self.size)
            self.totals["approach"] += sign * (1 - (abs(er - row) + abs(ec - col)) / (2 * span))

    def _calculate_hash(self) -> int:
        piece_tokens, rights_tokens, ep_tokens, turn_token = _tokens(self.size)
        value = turn_token if self.turn < 0 else 0
        for square, piece in enumerate(self.board):
            if piece:
                value ^= piece_tokens[square * 13 + piece + 6]
        for square in self.pawn_rights:
            value ^= rights_tokens[square]
        if _effective_ep(self):
            value ^= ep_tokens[self.ep_target]
        return value


def new_position(config: dict) -> Position:
    config = validate_config(config)
    n = config["n"]
    size = 8 * n
    board = [0] * (size * size)
    back = (ROOK, KNIGHT, BISHOP, QUEEN, KING, BISHOP, KNIGHT, ROOK)
    rights = set()
    for color in (1, -1):
        def index(home_row, column):
            row = size - 1 - home_row if color > 0 else home_row
            return row * size + column
        if config["setup"] == "rows":
            for column in range(size):
                kind = back[column % 8]
                if kind != KING:
                    board[index(0, column)] = color * kind
                pawn = index(1, column)
                board[pawn] = color * PAWN
                rights.add(pawn)
            board[index(0, 4 * n)] = color * KING
        else:
            for column in range(size):
                kind = back[column // n]
                for home_row in range(n):
                    if kind != KING:
                        board[index(home_row, column)] = color * kind
                for home_row in range(n, 2 * n):
                    pawn = index(home_row, column)
                    board[pawn] = color * PAWN
                    rights.add(pawn)
            king_col = 4 * n + ((n - 1) // 2 if config["king_placement"] == "original" else 0)
            board[index((n - 1) // 2, king_col)] = color * KING
    return Position(config=config, board=board, pawn_rights=rights)


def position_key(position: Position) -> str:
    return f"{position.hash:016x}"


def is_attacked(position: Position, square: int, by_color: int) -> bool:
    size, board = position.size, position.board
    row, col = divmod(square, size)
    source_row = row + by_color
    if 0 <= source_row < size:
        for source_col in (col - 1, col + 1):
            if 0 <= source_col < size and board[source_row * size + source_col] == by_color * PAWN:
                return True
    for dr, dc in KNIGHT_STEPS:
        r, c = row + dr, col + dc
        if 0 <= r < size and 0 <= c < size and board[r * size + c] == by_color * KNIGHT:
            return True
    limit = position.config["slider_limit"] or size
    for dr, dc in ORTHOGONAL + DIAGONAL:
        r, c = row + dr, col + dc
        distance = 1
        while 0 <= r < size and 0 <= c < size and distance <= max(1, limit):
            piece = board[r * size + c]
            if piece:
                if piece * by_color > 0:
                    kind = abs(piece)
                    if distance == 1 and kind == KING:
                        return True
                    if distance <= limit and (kind == QUEEN or kind == (ROOK if dr == 0 or dc == 0 else BISHOP)):
                        return True
                break
            r, c = r + dr, c + dc
            distance += 1
    return False


def in_check(position: Position, color: int | None = None) -> bool:
    color = position.turn if color is None else color
    return is_attacked(position, position.kings[color], -color)


def _effective_ep(position: Position) -> bool:
    """Only an actually legal en passant right changes repetition identity."""
    if position.ep_target is None or position.ep_pawn is None:
        return False
    size, color = position.size, position.turn
    target_row, target_col = divmod(position.ep_target, size)
    source_row = target_row + color
    if not 0 <= source_row < size:
        return False
    for col in (target_col - 1, target_col + 1):
        if not 0 <= col < size:
            continue
        source = source_row * size + col
        if position.board[source] != color * PAWN:
            continue
        captured = position.board[position.ep_pawn]
        position.board[source] = position.board[position.ep_pawn] = 0
        position.board[position.ep_target] = color * PAWN
        legal = not in_check(position)
        position.board[source], position.board[position.ep_target], position.board[position.ep_pawn] = color * PAWN, 0, captured
        if legal:
            return True
    return False


def _pseudo_moves(position: Position, from_square: int | None = None, check_budget: Callable | None = None) -> Iterator[Move]:
    size, board, color = position.size, position.board, position.turn
    if from_square is not None:
        if isinstance(from_square, bool) or not isinstance(from_square, int) or not 0 <= from_square < len(board) or board[from_square] * color <= 0:
            return
        squares = (from_square,)
    else:
        squares = (square for kind in (PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING) for square in sorted(position.pieces[color * kind]))
    for source in squares:
        if check_budget:
            check_budget()
        piece = board[source]
        kind = abs(piece)
        row, col = divmod(source, size)
        if kind == PAWN:
            next_row = row - color
            if not 0 <= next_row < size:
                continue
            target = next_row * size + col
            if not board[target]:
                if next_row in (0, size - 1):
                    for promotion in (QUEEN, ROOK, BISHOP, KNIGHT):
                        yield {"from": source, "to": target, "promotion": promotion}
                else:
                    yield {"from": source, "to": target}
                    double_row = row - 2 * color
                    if source in position.pawn_rights and 0 <= double_row < size and not board[double_row * size + col]:
                        yield {"from": source, "to": double_row * size + col}
            for dc in (-1, 1):
                c = col + dc
                if not 0 <= c < size:
                    continue
                target = next_row * size + c
                if (board[target] * color < 0 and abs(board[target]) != KING) or (target == position.ep_target and position.ep_pawn is not None and board[position.ep_pawn] == -color * PAWN):
                    if next_row in (0, size - 1):
                        for promotion in (QUEEN, ROOK, BISHOP, KNIGHT):
                            yield {"from": source, "to": target, "promotion": promotion}
                    else:
                        yield {"from": source, "to": target}
        elif kind in (KING, KNIGHT):
            for dr, dc in (ORTHOGONAL + DIAGONAL if kind == KING else KNIGHT_STEPS):
                r, c = row + dr, col + dc
                if 0 <= r < size and 0 <= c < size:
                    target = r * size + c
                    if board[target] * color <= 0 and abs(board[target]) != KING:
                        yield {"from": source, "to": target}
        else:
            directions = ORTHOGONAL if kind == ROOK else DIAGONAL if kind == BISHOP else ORTHOGONAL + DIAGONAL
            limit = position.config["slider_limit"] or size
            for dr, dc in directions:
                r, c, distance = row + dr, col + dc, 1
                while 0 <= r < size and 0 <= c < size and distance <= limit:
                    if check_budget and distance % 16 == 0:
                        check_budget()
                    target = r * size + c
                    occupant = board[target]
                    if occupant * color > 0:
                        break
                    if abs(occupant) != KING:
                        yield {"from": source, "to": target}
                    if occupant:
                        break
                    r, c, distance = r + dr, c + dc, distance + 1


def _make(position: Position, move: Move):
    source, target = move["from"], move["to"]
    piece = position.board[source]
    color = position.turn
    capture_square = target
    if abs(piece) == PAWN and target == position.ep_target and not position.board[target]:
        capture_square = position.ep_pawn
    captured = position.board[capture_square]
    old = (piece, captured, capture_square, position.ep_target, position.ep_pawn, position.halfmove,
           position.hash, dict(position.totals), dict(position.kings), [])
    piece_tokens, rights_tokens, ep_tokens, turn_token = _tokens(position.size)
    if _effective_ep(position):
        position.hash ^= ep_tokens[position.ep_target]
    for square in (source, capture_square):
        if square in position.pawn_rights:
            position.pawn_rights.remove(square)
            position.hash ^= rights_tokens[square]
            old[-1].append(square)
    position._adjust_features(piece, source, -1)
    position.pieces[piece].remove(source)
    position.board[source] = 0
    position.hash ^= piece_tokens[source * 13 + piece + 6]
    if captured:
        position._adjust_features(captured, capture_square, -1)
        position.pieces[captured].remove(capture_square)
        position.board[capture_square] = 0
        position.hash ^= piece_tokens[capture_square * 13 + captured + 6]
    promoted = color * move.get("promotion", abs(piece))
    position.board[target] = promoted
    position.pieces[promoted].add(target)
    position._adjust_features(promoted, target, 1)
    position.hash ^= piece_tokens[target * 13 + promoted + 6]
    if abs(piece) == KING:
        position.kings[color] = target
        position.totals["approach"] = 0.0
        for p, squares in position.pieces.items():
            if abs(p) != KING:
                c = 1 if p > 0 else -1
                er, ec = divmod(position.kings[-c], position.size)
                for square in squares:
                    r, col = divmod(square, position.size)
                    position.totals["approach"] += c * (1 - (abs(er - r) + abs(ec - col)) / (2 * (position.size - 1)))
    position.ep_target = position.ep_pawn = None
    if abs(piece) == PAWN and abs(target - source) == 2 * position.size:
        position.ep_target, position.ep_pawn = (source + target) // 2, target
    position.halfmove = 0 if abs(piece) == PAWN or captured else position.halfmove + 1
    position.turn = -color
    position.ply += 1
    position.hash ^= turn_token
    if _effective_ep(position):
        position.hash ^= ep_tokens[position.ep_target]
    position.history.append(position.hash)
    previous_count = position.counts.get(position.hash, 0)
    if previous_count:
        position.repetition_signature ^= _repetition_token(position.hash, previous_count)
    position.counts[position.hash] = previous_count + 1
    position.repetition_signature ^= _repetition_token(position.hash, previous_count + 1)
    return old


def _unmake(position: Position, move: Move, old):
    key = position.history.pop()
    position.repetition_signature ^= _repetition_token(key, position.counts[key])
    position.counts[key] -= 1
    if not position.counts[key]:
        del position.counts[key]
    else:
        position.repetition_signature ^= _repetition_token(key, position.counts[key])
    piece, captured, capture_square, ep_target, ep_pawn, halfmove, old_hash, totals, kings, rights = old
    source, target = move["from"], move["to"]
    promoted = position.board[target]
    position.pieces[promoted].remove(target)
    position.board[target] = 0
    position.board[source] = piece
    position.pieces[piece].add(source)
    if captured:
        position.board[capture_square] = captured
        position.pieces[captured].add(capture_square)
    position.pawn_rights.update(rights)
    position.ep_target, position.ep_pawn, position.halfmove = ep_target, ep_pawn, halfmove
    position.hash, position.totals, position.kings = old_hash, totals, kings
    position.turn *= -1
    position.ply -= 1


def iter_legal_moves(position: Position, from_square: int | None = None, check_budget: Callable | None = None) -> Iterator[Move]:
    color = position.turn
    for move in _pseudo_moves(position, from_square, check_budget):
        if check_budget:
            check_budget()
        old = _make(position, move)
        try:
            legal = not in_check(position, color)
        finally:
            _unmake(position, move, old)
        if legal:
            yield move


def apply_move(position: Position, move: dict) -> Position:
    if not isinstance(move, dict) or any(isinstance(move.get(k), bool) or not isinstance(move.get(k), int) for k in ("from", "to")):
        raise ValueError("A move requires integer from/to squares")
    wanted = {key: move[key] for key in ("from", "to", "promotion") if key in move and move[key] is not None}
    if wanted not in iter_legal_moves(position, wanted["from"]):
        raise ValueError("Illegal move")
    result = Position.from_dict(position.to_dict())
    _make(result, wanted)
    return result


def draw_reason(position: Position) -> str | None:
    if position.counts.get(position.hash, 0) >= 3:
        return "threefold_repetition"
    if sum(len(squares) for kind, squares in position.pieces.items() if abs(kind) != KING) == 0:
        return "bare_kings"
    limit = position.config["no_progress_limit"]
    if limit and position.halfmove >= 2 * limit:
        return "no_progress"
    return None


def game_status(position: Position) -> dict:
    check = in_check(position)
    # Mate takes precedence over a move-count draw on the final move.
    if next(iter_legal_moves(position), None) is None:
        return {"status": "checkmate" if check else "draw", "winner": -position.turn if check else None,
                "reason": "checkmate" if check else "stalemate", "check": check}
    reason = draw_reason(position)
    return {"status": "draw" if reason else "playing", "winner": None, "reason": reason, "check": check}


def extract_features(position: Position) -> dict[str, float]:
    result = {key: value / position.army_factor for key, value in position.totals.items()}
    shelter = 0.0
    for color in (1, -1):
        row, col = divmod(position.kings[color], position.size)
        local = 0.0
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                r, c = row + dr, col + dc
                if (dr == dc == 0) or not (0 <= r < position.size and 0 <= c < position.size):
                    continue
                piece = position.board[r * position.size + c]
                distance = max(abs(dr), abs(dc))
                if piece * color > 0:
                    local += (0.2 if abs(piece) == PAWN else 0.05) / distance
                elif piece:
                    local -= 0.3 / distance
        if in_check(position, color):
            local -= 0.5
        shelter += color * local
    result["safety"] = shelter
    return result
