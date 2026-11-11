"""Interruptible, instrumented game-tree search shared by every AI personality.

The supervisor supplies an absolute deadline and can terminate the process. This
module also checks the same deadline during move generation, not just at leaves.
"""
from __future__ import annotations

import math
import random
import time
from typing import Callable

from .engine import (PAWN, KING, Position, _make, _unmake, draw_reason,
                     extract_features, in_check, iter_legal_moves)

DEFAULT_WEIGHTS = {"pawn": 1.0, "knight": 3.2, "bishop": 3.3, "rook": 5.0, "queen": 9.0,
                   "advancement": 0.15, "activity": 0.10, "approach": 0.05, "safety": 0.5}
PIECE_VALUES = (0, 1, 3.2, 3.3, 5, 9, 10000)
MATE = 1_000_000_000.0
INF = float("inf")


class SearchStopped(Exception):
    pass


class Search:
    def __init__(self, position: Position, profile: dict, limits: dict, emit: Callable[[dict], None]):
        self.position, self.emit = position, emit
        self.seconds = max(0.001, float(limits.get("seconds", 2)))
        self.deadline = float(limits.get("deadline") or time.monotonic() + self.seconds)
        self.started = self.deadline - self.seconds
        self.node_limit = limits.get("nodes")
        self.max_depth = min(64, max(1, int(limits.get("depth") or 64)))
        settings = profile.get("search", {})
        self.selective = settings.get("mode", "selective") != "full"
        self.root_width = max(1, int(settings.get("root_width", 48)))
        self.width = max(1, int(settings.get("width", 16)))
        self.qdepth = min(8, max(0, int(settings.get("qdepth", 2))))
        self.qwidth = max(1, int(settings.get("qwidth", 8)))
        self.weights = {**DEFAULT_WEIGHTS, **profile.get("weights", {})}
        self.exploration = max(0, min(1, float(profile.get("exploration", 0))))
        self.rng = random.Random(int(limits.get("seed", 0)))
        self.nodes = self.qnodes = self.evaluated = self.cutoffs = self.tt_hits = self.generated = 0
        self.depth = self.completed_depth = self.root_done = 0
        self.root_total = None
        self.root_legal_total = None
        self.candidates_skipped = 0
        self.move = None
        self.score = None
        self.pv = []
        self.reason = "searching"
        self.last_report = time.monotonic()
        self.tt: dict[tuple[int, int, int], tuple] = {}
        self.history: dict[tuple[int, int, int], int] = {}
        self.killers: dict[int, list[dict]] = {}

    def report(self, event: str | None = None) -> dict:
        now = time.monotonic()
        self.last_report = now
        elapsed = max(0.0, now - self.started)
        data = {"move": self.move, "nodes": self.nodes, "qnodes": self.qnodes, "evaluated": self.evaluated,
                "generated": self.generated, "cutoffs": self.cutoffs, "tt_hits": self.tt_hits,
                "depth": self.depth, "completed_depth": self.completed_depth, "root_done": self.root_done,
                "root_total": self.root_total, "root_legal_total": self.root_legal_total,
                "candidates_skipped": self.candidates_skipped, "score": self.score, "pv": list(self.pv),
                "elapsed": elapsed, "remaining": max(0.0, self.deadline - now),
                "nps": round(self.nodes / max(elapsed, 0.001)), "selective": self.selective,
                "reason": self.reason}
        if event:
            data["event"] = event
        self.emit(data)
        return data

    def check(self):
        now = time.monotonic()
        if now >= self.deadline:
            self.reason = "deadline"
            raise SearchStopped()
        if self.node_limit is not None and self.nodes >= int(self.node_limit):
            self.reason = "node_budget"
            raise SearchStopped()
        if now - self.last_report >= 0.25:
            self.report()

    def evaluate(self) -> float:
        self.check()
        self.evaluated += 1
        features = extract_features(self.position)
        value = sum(float(self.weights.get(key, 0)) * val for key, val in features.items())
        # A terminal result always outranks user-edited heuristic weights.
        return max(-MATE / 4, min(MATE / 4, value)) * self.position.turn

    def tt_key(self) -> tuple[int, int, int]:
        # Repetition can change a descendant's value even when the current
        # board itself has occurred only once. Cache bounds only when the
        # complete occurrence-count context agrees; a board-only key is unsafe.
        return self.position.hash, self.position.halfmove, self.position.repetition_signature

    def order_score(self, move: dict, tt_move: dict | None, ply: int) -> float:
        if move == tt_move:
            return 1e9
        pos = self.position
        piece = abs(pos.board[move["from"]])
        victim = abs(pos.board[move["to"]])
        if piece == PAWN and move["to"] == pos.ep_target:
            victim = PAWN
        value = 10000 + PIECE_VALUES[victim] * 100 - PIECE_VALUES[piece] if victim else 0
        if "promotion" in move:
            value += 20000 + PIECE_VALUES[move["promotion"]] * 100
        if not victim:
            value += 1000 if move in self.killers.get(ply, ()) else 0
            value += min(500, self.history.get((pos.turn, move["from"], move["to"]), 0))
            source_row, source_col = divmod(move["from"], pos.size)
            target_row, target_col = divmod(move["to"], pos.size)
            value += (source_row - target_row) * pos.turn * (3 if piece == PAWN else 0.2)
            span = pos.size - 1
            value += (abs(2 * source_row - span) + abs(2 * source_col - span)
                      - abs(2 * target_row - span) - abs(2 * target_col - span)) / span
        return value

    def moves(self, ply: int, tt_move: dict | None = None, root=False, tactical=False) -> list[dict]:
        checked = in_check(self.position)
        moves = []
        for move in iter_legal_moves(self.position, check_budget=self.check):
            self.generated += 1
            if root and self.move is None:
                self.move = dict(move)
                self.pv = [dict(move)]
                self.report("Legal fallback ready")
            if tactical and not checked and not self.position.board[move["to"]] and "promotion" not in move and not (abs(self.position.board[move["from"]]) == PAWN and move["to"] == self.position.ep_target):
                continue
            moves.append(move)
        self.check()
        # Sorting is cheap compared with board copies/legality; deadlines are
        # checked on either side and before every scored child.
        moves.sort(key=lambda move: self.order_score(move, tt_move, ply)
                   + (self.rng.uniform(-self.exploration, self.exploration) if root else 0), reverse=True)
        self.check()
        if root:
            self.root_legal_total = len(moves)
        limit = self.qwidth if tactical else self.root_width if root else self.width
        if self.selective and not checked and len(moves) > limit:
            self.candidates_skipped += len(moves) - limit
            moves = moves[:limit]
        if root:
            self.root_total = len(moves)
        return moves

    def quiescence(self, alpha: float, beta: float, depth: int, ply: int) -> tuple[float, list]:
        self.check()
        self.nodes += 1
        self.qnodes += 1
        checked = in_check(self.position)
        reason = draw_reason(self.position)
        if reason:
            if next(iter_legal_moves(self.position, check_budget=self.check), None) is None and checked:
                return -MATE + ply, []
            return 0.0, []
        if ply >= 64:
            return self.evaluate(), []
        stand_pat = self.evaluate()
        if not checked:
            # Even at a quiet horizon, stalemate is a draw, not stand-pat.
            if next(iter_legal_moves(self.position, check_budget=self.check), None) is None:
                return 0.0, []
            if stand_pat >= beta:
                self.cutoffs += 1
                return stand_pat, []
            alpha = max(alpha, stand_pat)
            if depth <= 0:
                return stand_pat, []
        moves = self.moves(ply, tactical=True)
        if checked and not moves:
            return -MATE + ply, []
        best = -INF if checked else stand_pat
        best_pv = []
        for move in moves:
            self.check()
            old = _make(self.position, move)
            try:
                value, pv = self.quiescence(-beta, -alpha, depth - 1, ply + 1)
                value = -value
            finally:
                _unmake(self.position, move, old)
            if value > best:
                best, best_pv = value, [move] + pv
            alpha = max(alpha, value)
            if alpha >= beta:
                self.cutoffs += 1
                break
        return best, best_pv

    def negamax(self, depth: int, alpha: float, beta: float, ply: int) -> tuple[float, list]:
        self.check()
        if depth <= 0:
            return self.quiescence(alpha, beta, self.qdepth, ply)
        self.nodes += 1
        reason = draw_reason(self.position)
        if reason:
            checked = in_check(self.position)
            if next(iter_legal_moves(self.position, check_budget=self.check), None) is None and checked:
                return -MATE + ply, []
            return 0.0, []
        key = self.tt_key()
        entry = self.tt.get(key) if self.position.counts.get(self.position.hash, 0) == 1 else None
        original_alpha = alpha
        tt_move = entry[3] if entry else None
        if entry and entry[0] >= depth:
            self.tt_hits += 1
            value = entry[1]
            # Mate scores are relative to the root ply; normalize on storage.
            if value > MATE / 2:
                value -= ply
            elif value < -MATE / 2:
                value += ply
            if entry[2] == "exact":
                return value, [tt_move] if tt_move else []
            if entry[2] == "lower":
                alpha = max(alpha, value)
            else:
                beta = min(beta, value)
            if alpha >= beta:
                return value, [tt_move] if tt_move else []
        moves = self.moves(ply, tt_move)
        if not moves:
            return (-MATE + ply if in_check(self.position) else 0.0), []
        best, best_move, best_pv = -INF, None, []
        for move in moves:
            self.check()
            quiet = not self.position.board[move["to"]] and "promotion" not in move
            old = _make(self.position, move)
            try:
                value, pv = self.negamax(depth - 1, -beta, -alpha, ply + 1)
                value = -value
            finally:
                _unmake(self.position, move, old)
            if value > best:
                best, best_move, best_pv = value, move, [move] + pv
            alpha = max(alpha, value)
            if alpha >= beta:
                self.cutoffs += 1
                if quiet:
                    killers = self.killers.setdefault(ply, [])
                    if move not in killers:
                        killers.insert(0, move)
                        del killers[2:]
                    hkey = self.position.turn, move["from"], move["to"]
                    self.history[hkey] = self.history.get(hkey, 0) + depth * depth
                break
        if self.position.counts.get(self.position.hash, 0) == 1:
            flag = "upper" if best <= original_alpha else "lower" if best >= beta else "exact"
            stored = best + ply if best > MATE / 2 else best - ply if best < -MATE / 2 else best
            if len(self.tt) >= 65536 and key not in self.tt:
                self.tt.pop(next(iter(self.tt)))
            self.tt[key] = (depth, stored, flag, best_move)
        return best, best_pv

    def run(self) -> dict:
        try:
            roots = self.moves(0, root=True)
            if not roots:
                self.score = -MATE if in_check(self.position) else 0.0
                self.reason = "terminal"
                return self.report("No legal moves")
            reason = draw_reason(self.position)
            if reason:
                self.move, self.pv, self.score, self.reason = None, [], 0.0, "terminal"
                return self.report(reason)
            for depth in range(1, self.max_depth + 1):
                self.check()
                self.depth, self.root_done = depth, 0
                best, best_move, best_pv = -INF, roots[0], []
                scores = {}
                for move in roots:
                    self.check()
                    old = _make(self.position, move)
                    try:
                        # The previous root best supplies a bound, allowing a
                        # single opposing refutation to reject weaker moves.
                        value, pv = self.negamax(depth - 1, -INF, -best, 1)
                        value = -value
                    finally:
                        _unmake(self.position, move, old)
                    self.root_done += 1
                    scores[(move["from"], move["to"], move.get("promotion"))] = value
                    if value > best:
                        best, best_move, best_pv = value, move, [move] + pv
                self.move, self.score, self.pv = dict(best_move), best, best_pv
                self.completed_depth = depth
                self.report(f"Depth {depth} completed ({'selective' if self.selective else 'full-width'})")
                roots.sort(key=lambda move: scores.get((move["from"], move["to"], move.get("promotion")), -INF), reverse=True)
                if abs(best) > MATE / 2:
                    self.reason = "mate_found"
                    break
            else:
                self.reason = "maximum_depth"
        except SearchStopped:
            pass
        return self.report("Search finished: " + self.reason)


def search_position(position_data: dict, profile: dict, limits: dict, emit: Callable[[dict], None]) -> dict:
    return Search(Position.from_dict(position_data), profile, limits, emit).run()
