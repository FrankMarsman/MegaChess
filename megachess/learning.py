"""Small, inspectable post-game TD(lambda) updates for linear evaluators."""

from __future__ import annotations

from copy import deepcopy
import math

from .storage import FEATURES, rules_key


def _prediction(weights, features):
    score = sum(weights.get(k, 0.0) * features.get(k, 0.0) for k in FEATURES)
    return math.tanh(score / 4.0)


def td_update(profile: dict, trajectories: list[tuple[list[dict], float]]) -> dict:
    """Return a new bounded weight set; never mutate the match's profile.

    Trajectories use one fixed player's perspective throughout. Targets are
    computed with the frozen input weights before a single averaged update.
    """
    result = deepcopy(profile)
    weights = result["weights"]
    anchor = result.get("anchor_weights", weights)
    rate = float(result.get("learning", {}).get("rate", 0.02))
    lam = float(result.get("learning", {}).get("lambda", 0.7))
    gradients = {k: 0.0 for k in FEATURES}
    count = 0
    for positions, outcome in trajectories:
        if not positions:
            continue
        values = [_prediction(weights, f) for f in positions]
        target = max(-1.0, min(1.0, float(outcome)))
        for i in range(len(positions) - 1, -1, -1):
            if i < len(positions) - 1:
                target = (1 - lam) * values[i + 1] + lam * target
            error = target - values[i]
            derivative = (1.0 - values[i] ** 2) / 4.0
            for k in FEATURES:
                value = float(positions[i].get(k, 0.0))
                gradients[k] += error * derivative * value
            count += 1
    if not count:
        return result
    for k in FEATURES:
        original = float(anchor.get(k, weights[k]))
        delta = rate * gradients[k] / count - rate * 0.01 * (weights[k] - original)
        delta = max(-0.05, min(0.05, delta))
        # Material values remain positive; positional weights retain their
        # personality with a generous but finite range around the anchor.
        lower = max(0.05, original * 0.5) if k in FEATURES[:5] else min(0.0, original - 1)
        upper = max(original * 1.5, original + 0.5) if k in FEATURES[:5] else max(0.5, original + 1)
        weights[k] = max(lower, min(upper, float(weights[k]) + delta))
    result["games_learned"] = int(result.get("games_learned", 0)) + 1
    return result


def learn_from_game(storage, game: dict) -> dict:
    """Learn each participating AI exactly once, including same-AI self-play."""
    if game.get("status") not in ("checkmate", "draw", "resigned"):
        return {"status": "ineligible", "profiles": []}
    if game.get("learning_disabled") or game.get("benchmark"):
        return {"status": "disabled", "profiles": []}
    positions = storage.get_feature_history(game["id"])
    if not positions:
        return {"status": "no_positions", "profiles": []}
    key = rules_key(game["config"])
    groups = {}
    for color, sign in (("white", 1), ("black", -1)):
        player = game.get("players", {}).get(color, {})
        frozen = player.get("profile")
        if player.get("kind") != "ai" or not frozen or not frozen.get("learning", {}).get("enabled", True):
            continue
        ident = frozen["id"]
        outcome = 0.0 if game.get("winner") is None else (1.0 if game["winner"] == sign else -1.0)
        perspective = [{k: sign * float(f.get(k, 0.0)) for k in FEATURES} for f in positions]
        groups.setdefault(ident, {"profile": frozen, "trajectories": []})["trajectories"].append((perspective, outcome))
    summaries = []
    with storage.training_lock:
        for ident, group in groups.items():
            if storage.has_learned(game["id"], ident, key):
                continue
            current = storage.profile_for_rules(ident, game["config"])
            if not current.get("learning", {}).get("enabled", True):
                continue
            # Recompute against the current head: concurrently completed games
            # never apply deltas calculated for an obsolete profile version.
            updated = td_update(current, group["trajectories"])
            updated["last_training_game"] = game["id"]
            saved = storage.save_learning_head(ident, key, updated, game["id"])
            summaries.append({"id": ident, "name": saved["name"], "version": saved["version"], "games_learned": saved["games_learned"]})
    return {"status": "learned" if summaries else "already_processed", "profiles": summaries}
