"""Durable game coordinator and isolated, deadline-supervised AI searches."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import random
import threading
import time
import traceback

from .engine import Position, apply_move, extract_features, game_status, iter_legal_moves, new_position
from . import __version__
from .learning import learn_from_game
from .storage import Storage


def _search_child(connection, position, profile, limits):
    """A child has no database access; its pipe belongs to this search alone."""
    try:
        from .search import search_position
        result = search_position(position, profile, limits,
                                 lambda data: connection.send({"type": "progress", "data": data}))
        connection.send({"type": "done", "data": result})
    except (BrokenPipeError, EOFError):
        pass
    except BaseException as exc:
        try:
            connection.send({"type": "error", "error": str(exc)})
        except (BrokenPipeError, EOFError):
            pass
    finally:
        connection.close()


@dataclass
class SearchTask:
    game_id: str
    revision: int
    process: object
    connection: object
    started: float
    deadline: float
    profile_version: int
    step: bool = False
    profile_id: str | None = None
    fallback: dict | None = None
    completed: dict | None = None
    latest: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    done: dict | None = None
    error: str | None = None
    force: bool = False


class Runtime:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(self.data_dir / "megachess.sqlite3")
        self.lock = threading.RLock()
        self.context = mp.get_context("spawn")
        self.active: dict[str, SearchTask] = {}
        self.progress: dict[str, dict] = {}
        self.tracked: set[str] = set()
        self.eligible: dict[str, float] = {}
        self.stop_event = threading.Event()
        self.learning_queue = queue.Queue()
        self.thread = None
        self.learner = None
        self.batcher = None

    def start(self):
        # A queue item is only a notification. The saved result and persistent
        # learning receipts are authoritative, so replaying notifications after
        # a crash is safe even when the prior process died after queueing them.
        offset = 0
        batch_games = {}
        while True:
            page = self.storage.list_games(limit=1000, offset=offset)
            for game in page:
                if game.get("batch_id") and game.get("schedule_index") is not None:
                    batch_games.setdefault(game["batch_id"], {})[game["schedule_index"]] = game["id"]
                if game["status"] == "playing" and not game.get("paused"):
                    self.tracked.add(game["id"])
                elif game.get("learning") and not game.get("batch_id"):
                    self.learning_queue.put(game["id"])
            if len(page) < 1000:
                break
            offset += len(page)
        for batch in self.storage.list_batches():
            repaired = False
            for item in batch["schedule"]:
                found = batch_games.get(batch["id"], {}).get(item["index"])
                if not item.get("game_id") and found:
                    item["game_id"] = found
                    repaired = True
            if repaired:
                batch = self.storage.update_batch(batch)
            if batch["status"] in ("paused", "cancelled"):
                # Recover a crash partway through applying a batch control to
                # its individual games; the batch's saved intent wins.
                for item in batch["schedule"]:
                    if not item.get("game_id"):
                        continue
                    game = self.storage.get_game(item["game_id"])
                    if game["status"] != "playing":
                        continue
                    if batch["status"] == "cancelled":
                        game.update(status="aborted", reason="batch_cancelled", winner=None)
                        self.tracked.discard(game["id"])
                    else:
                        game["paused"] = True
                    self.storage.update_game(game, game["revision"])
            if batch.get("status") == "completed" and batch.get("learning"):
                for item in batch["schedule"]:
                    if item.get("game_id"):
                        self.learning_queue.put(item["game_id"])
        self.thread = threading.Thread(target=self._loop, name="megachess-coordinator", daemon=True)
        self.learner = threading.Thread(target=self._learn_loop, name="megachess-learner", daemon=True)
        self.batcher = threading.Thread(target=self._batch_loop, name="megachess-batches", daemon=True)
        self.thread.start()
        self.learner.start()
        self.batcher.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.batcher:
            self.batcher.join(timeout=3)
        with self.lock:
            for game_id in list(self.active):
                self._cancel(game_id)
        self.learning_queue.put(None)
        if self.learner:
            self.learner.join(timeout=3)

    def _learn_loop(self):
        while True:
            game_id = self.learning_queue.get()
            if game_id is None:
                return
            try:
                game = self.storage.get_game(game_id)
                if game.get("learning"):
                    learn_from_game(self.storage, game)
            except Exception:
                traceback.print_exc()

    def create_game(self, payload, batch_id=None, initial=None):
        position = initial or new_position(payload.get("config", {}))
        config = position.config
        players = {}
        for side in ("white", "black"):
            player = payload.get("players", {}).get(side, {"kind": "human"})
            kind = player.get("kind", "human")
            if kind not in ("human", "ai"):
                raise ValueError("Choose Human or AI for each player")
            seconds = float(player.get("seconds", 2))
            if not 0.05 <= seconds <= 3600:
                raise ValueError("Thinking time must be between 0.05 and 3600 seconds")
            profile = None
            if kind == "ai":
                profile = player.get("profile") or self.storage.profile_for_rules(player.get("profile_id", "balanced"), config)
                profile = copy.deepcopy(profile)
            limits = {}
            for key, maximum in (("max_nodes", 100_000_000), ("max_depth", 64)):
                value = player.get(key)
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                        raise ValueError(f"{key} must be an integer from 1 to {maximum}")
                limits[key] = value
            players[side] = {"kind": kind, "seconds": seconds, "profile": profile, **limits}
        with self.lock:
            if not batch_id:
                # A new board preserves the old match, paused in Saved Games.
                for old_id in list(self.tracked):
                    old = self.storage.get_game(old_id)
                    if not old.get("batch_id") and old["status"] == "playing" and not old.get("paused"):
                        self._cancel(old_id)
                        old["paused"] = True
                        self.storage.update_game(old)
                        self.tracked.discard(old_id)
            status = game_status(position)
            if payload.get("max_plies") and position.ply >= payload["max_plies"] and status["status"] == "playing":
                status.update(status="cutoff", winner=None, reason="ply_limit")
            game = self.storage.create_game({
                "config": config, "initial_position": position.to_dict(), "position": position.to_dict(),
                "players": players, "revision": 0, "status": status["status"], "winner": status["winner"],
                "reason": status["reason"], "check": status["check"], "paused": bool(payload.get("paused", False)),
                "learning": bool(payload.get("learning", True)), "view_delay": max(0, min(10, float(payload.get("view_delay", 0.4)))),
                "seed": int(payload.get("seed", 1)), "batch_id": batch_id,
                "node_budget": payload.get("node_budget"), "max_plies": payload.get("max_plies"),
                "engine_version": __version__, "learning_disabled": bool(payload.get("learning_disabled", False)),
                "schedule_index": payload.get("schedule_index"),
            })
            if game["status"] == "playing" and not game.get("paused"):
                self.tracked.add(game["id"])
            return game

    def play_move(self, game_id, move, revision):
        with self.lock:
            game = self.storage.get_game(game_id)
            if game["revision"] != revision:
                raise ValueError("The position changed. Refresh before moving again.")
            if game["status"] != "playing":
                raise ValueError("This game has finished")
            player = game["players"]["white" if game["position"]["turn"] == 1 else "black"]
            if player["kind"] != "human":
                raise ValueError("It is the AI's turn")
            return self._commit(game, move, None)

    def _commit(self, game, move, stats):
        position = apply_move(Position.from_dict(game["position"]), move)
        status = game_status(position)
        game.update(status)
        game["position"] = position.to_dict()
        game.pop("search_error", None)
        game["compute_seconds"] = game.get("compute_seconds", 0.0) + float((stats or {}).get("elapsed", 0.0))
        if game.get("max_plies") and position.ply >= game["max_plies"] and game["status"] == "playing":
            game.update(status="cutoff", winner=None, reason="ply_limit")
        result = self.storage.commit_move(game, move, extract_features(position), stats, game["revision"])
        self.eligible[game["id"]] = time.monotonic() + game.get("view_delay", 0)
        if result["status"] != "playing":
            self.tracked.discard(game["id"])
            if result.get("learning") and not result.get("batch_id"):
                self.learning_queue.put(result["id"])
        return result

    def control(self, game_id, action, value=None):
        with self.lock:
            game = self.storage.get_game(game_id)
            if action == "set_delay":
                delay = float(value)
                if not math.isfinite(delay) or not 0 <= delay <= 10:
                    raise ValueError("Viewing delay must be between 0 and 10 seconds")
                self._cancel(game_id)
                game["view_delay"] = delay
                return self.storage.update_game(game, game["revision"])
            if action == "move_now":
                if game_id in self.active:
                    self.active[game_id].force = True
                return game
            if action in ("pause", "resume", "step"):
                if game["status"] != "playing":
                    return game
                if action == "pause":
                    self._cancel(game_id)
                    game["paused"] = True
                    self.tracked.discard(game_id)
                elif action == "resume":
                    game["paused"] = False
                    game.pop("search_error", None)
                    self.tracked.add(game_id)
                else:
                    if game_id in self.active:
                        raise ValueError("A move is already being searched")
                    game["paused"] = True
                    game.pop("search_error", None)
                    self.tracked.discard(game_id)
                game = self.storage.update_game(game)
                if action == "step":
                    side = "white" if game["position"]["turn"] == 1 else "black"
                    if game["players"][side]["kind"] == "ai":
                        self._launch(game, step=True)
                return game
            if action not in ("resign", "draw"):
                raise ValueError("Unknown game control")
            if game["status"] != "playing":
                return game
            self._cancel(game_id)
            game.update(status="resigned" if action == "resign" else "draw",
                        winner=-game["position"]["turn"] if action == "resign" else None,
                        reason="resignation" if action == "resign" else "agreement")
            if action == "draw":
                game["learning_disabled"] = True
            game = self.storage.update_game(game)
            self.tracked.discard(game_id)
            # An AI cannot agree to an outcome via the human control button.
            # Manual draw adjudications are preserved but not training labels.
            if action == "resign" and game.get("learning") and not game.get("batch_id"):
                self.learning_queue.put(game_id)
            return game

    def _launch(self, game, step=False):
        side = "white" if game["position"]["turn"] == 1 else "black"
        player = game["players"][side]
        started = time.monotonic()
        deadline = started + player["seconds"]
        receive, send = self.context.Pipe(duplex=False)
        limits = {"seconds": player["seconds"], "deadline": deadline,
                  "nodes": game.get("node_budget") or player.get("max_nodes"),
                  "depth": player.get("max_depth"),
                  "seed": game["seed"] + game["position"]["ply"] * 1009}
        process = self.context.Process(target=_search_child,
                                       args=(send, game["position"], player["profile"], limits), daemon=True)
        process.start()
        send.close()
        task = SearchTask(game["id"], game["revision"], process, receive, started, deadline,
                          player["profile"]["version"], step=step, profile_id=player["profile"]["id"])
        task.latest = {"search_id": f"{game['id']}:{game['revision']}", "profile_name": player["profile"]["name"],
                       "seconds": player["seconds"], "ply": game["position"]["ply"]}
        task.events.append({"time": 0, "message": f"{player['profile']['name']} started · {player['seconds']:g}s maximum"})
        self.active[game["id"]] = task

    def _cancel(self, game_id):
        task = self.active.pop(game_id, None)
        if task:
            self._stop_process(task)
            self.progress[game_id] = {**task.latest, "thinking": False, "reason": "cancelled", "events": task.events}

    @staticmethod
    def _stop_process(task):
        if task.process.is_alive():
            task.process.terminate()
        task.process.join(timeout=0.06)
        if task.process.is_alive():
            task.process.kill()
            task.process.join(timeout=0.06)
        task.connection.close()

    def _poll_task(self, task):
        try:
            while task.connection.poll():
                message = task.connection.recv()
                if message["type"] == "error":
                    task.error = message["error"]
                    return
                data = message["data"]
                previous_depth = task.latest.get("completed_depth", 0)
                task.latest = {**task.latest, **data}
                if data.get("move") and task.fallback is None:
                    task.fallback = copy.deepcopy(data)
                if data.get("move") and data.get("completed_depth", 0) > 0:
                    if task.completed is None or data["completed_depth"] >= task.completed.get("completed_depth", 0):
                        task.completed = copy.deepcopy(data)
                depth = data.get("completed_depth", 0)
                if depth > previous_depth:
                    score = data.get("score")
                    score_text = f"{score:+.2f}" if isinstance(score, (int, float)) else "—"
                    task.events.append({"time": round(time.monotonic() - task.started, 2),
                                        "message": f"Depth {depth} completed · {data.get('nodes', 0):,} nodes · score {score_text}"})
                elif data.get("event"):
                    task.events.append({"time": round(time.monotonic() - task.started, 2), "message": str(data["event"])[:250]})
                task.events[:] = task.events[-200:]
                if message["type"] == "done":
                    task.done = data
                    return
        except (EOFError, OSError):
            if not task.done:
                task.error = task.error or "Search worker stopped"

    def _finish_task(self, task, reason):
        self.active.pop(task.game_id, None)
        chosen = task.completed or task.fallback
        if task.done and task.done.get("move") and (not chosen or task.done.get("completed_depth", 0) >= chosen.get("completed_depth", 0)):
            chosen = task.done
        self._stop_process(task)
        elapsed = time.monotonic() - task.started
        selection = {key: chosen[key] for key in ("move", "score", "pv", "completed_depth") if chosen and key in chosen}
        stats = {**task.latest, **selection, "elapsed": round(elapsed, 3), "remaining": 0,
                 "reason": reason, "thinking": False, "profile_version": task.profile_version}
        task.events.append({"time": round(elapsed, 2), "message": f"Stopped: {reason} · completed depth {stats.get('completed_depth', 0)}"})
        stats["events"] = task.events[-200:]
        self.progress[task.game_id] = stats
        game = self.storage.get_game(task.game_id)
        if game["revision"] != task.revision or game["status"] != "playing":
            return
        side = "white" if game["position"]["turn"] == 1 else "black"
        player_profile = game["players"][side].get("profile")
        if not player_profile or player_profile["version"] != task.profile_version or (task.profile_id and player_profile["id"] != task.profile_id):
            return
        if chosen and chosen.get("move"):
            try:
                self._commit(game, chosen["move"], stats)
                return
            except ValueError as exc:
                task.error = str(exc)
        # A timeout is not evidence of mate. Only the rules engine may end play.
        status = game_status(Position.from_dict(game["position"]))
        if status["status"] != "playing":
            game.update(status)
        else:
            game["paused"] = True
            self.tracked.discard(game["id"])
            game["search_error"] = task.error or "No legal fallback was produced within the budget. Increase thinking time and resume."
            stats["error"] = game["search_error"]
        self.storage.update_game(game)

    def search_status(self, game_id):
        with self.lock:
            task = self.active.get(game_id)
            if not task:
                return self.progress.get(game_id)
            return {**task.latest, "thinking": True, "elapsed": round(time.monotonic() - task.started, 2),
                    "remaining": round(max(0, task.deadline - time.monotonic()), 2),
                    "events": task.events[-200:], "profile_version": task.profile_version}

    def create_batch(self, payload):
        count = int(payload.get("games", 100))
        nodes = int(payload.get("nodes", 5000))
        max_plies = int(payload.get("max_plies", 1000))
        workers = int(payload.get("workers", 2))
        if not 2 <= count <= 10000 or count % 2 or not 10 <= nodes <= 10000000 or not 2 <= max_plies <= 100000 or not 1 <= workers <= 4:
            raise ValueError("Batch needs an even number of 2–10000 games, 10–10000000 nodes, 2–100000 plies, and 1–4 workers")
        config = new_position(payload.get("config", {})).config
        profiles = [self.storage.profile_for_rules(payload.get("white_profile", "balanced"), config),
                    self.storage.profile_for_rules(payload.get("black_profile", "aggressive"), config)]
        learning = bool(payload.get("learning", True))
        profiles = copy.deepcopy(profiles)
        if not learning:
            for profile in profiles:
                profile["exploration"] = 0.0
        return self.storage.create_batch({"config": config, "total": count, "nodes": nodes,
            "max_plies": max_plies, "workers": workers, "learning": learning,
            "seed": int(payload.get("seed", 1)), "profiles": profiles, "status": "running",
            "schedule": [{"index": i, "game_id": None} for i in range(count)], "completed": 0,
            "results": {"a_wins": 0, "b_wins": 0, "draws": 0, "cutoffs": 0}, "learning_queued": False,
            "engine_version": __version__, "seed_set": "training" if learning else "held-out evaluation",
            "protocol": "Frozen profiles; paired color reversal; shared legal openings; equal node budgets",
            "metrics": {}})

    def control_batch(self, batch_id, action):
        with self.lock:
            batch = self.storage.get_batch(batch_id)
            if action not in ("pause", "resume", "cancel"):
                raise ValueError("Unknown batch control")
            if batch["status"] in ("completed", "cancelled"):
                return batch
            batch["status"] = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
            # Persist intent before per-game changes, allowing startup recovery
            # to finish a partially applied pause/cancellation after a crash.
            batch = self.storage.update_batch(batch)
            for item in batch["schedule"]:
                if not item.get("game_id"):
                    continue
                game = self.storage.get_game(item["game_id"])
                if game["status"] != "playing":
                    continue
                self._cancel(game["id"])
                if action == "cancel":
                    game.update(status="aborted", reason="batch_cancelled", winner=None)
                    self.tracked.discard(game["id"])
                else:
                    game["paused"] = action == "pause"
                    if action == "resume":
                        self.tracked.add(game["id"])
                self.storage.update_game(game)
            return batch

    @staticmethod
    def _batch_metrics(batch, games, completed):
        valid = {i: g for i, g in games.items() if g["status"] in ("checkmate", "draw", "resigned")}
        def score(index):
            game = valid[index]
            if game.get("winner") is None:
                return 0.5
            return float(game["winner"] == (1 if index % 2 == 0 else -1))
        paired = [(score(i) + score(i + 1)) / 2 for i in range(0, batch["total"], 2) if i in valid and i + 1 in valid]
        score_mean = sum(paired) / len(paired) if paired else None
        # Each color pair contributes one bounded observation. This conservative
        # finite-sample interval avoids treating correlated color games as
        # independent or implying precision from a single pair.
        radius = math.sqrt(math.log(40) / (2 * len(paired))) if paired else 1
        cutoff_count = sum(g["status"] in ("cutoff", "aborted") for g in completed)
        end = batch.get("completed_at") or datetime.now(timezone.utc).isoformat()
        elapsed = max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(batch["created_at"])).total_seconds())
        return {
            "average_plies": round(sum(g["position"]["ply"] for g in completed) / len(completed), 2) if completed else 0,
            "elapsed_seconds": round(elapsed, 2),
            "compute_seconds": round(sum(g.get("compute_seconds", 0) for g in games.values()), 3),
            "cutoff_rate": cutoff_count / len(completed) if completed else 0,
            "completed_pairs": sum(i in games and i + 1 in games and games[i]["status"] != "playing" and games[i + 1]["status"] != "playing" for i in range(0, batch["total"], 2)),
            "scored_pairs": len(paired), "a_pair_score": score_mean,
            "score_ci95": [max(0, score_mean - radius), min(1, score_mean + radius)] if paired else [0, 1],
            "uncertainty_method": "95% Hoeffding interval over complete color pairs",
        }

    @staticmethod
    def _opening_position(config, seed):
        position = new_position(config)
        rng = random.Random(seed)
        for _ in range(4):
            moves = list(iter_legal_moves(position))
            if not moves:
                break
            position = apply_move(position, rng.choice(moves))
        return position

    def _batch_tick(self):
        # This runs on a separate scheduler thread. In particular, opening
        # generation and scanning completed games never hold the supervisor's
        # global lock, so a large batch cannot delay an active AI's deadline.
        for original in self.storage.list_batches():
            if self.stop_event.is_set():
                return
            if original["status"] != "running":
                continue
            games = {}
            for item in original["schedule"]:
                if item.get("game_id"):
                    games[item["index"]] = self.storage.get_game(item["game_id"])
            running = [g for g in games.values() if g["status"] == "playing"]
            completed = [g for g in games.values() if g["status"] != "playing"]
            results = {"a_wins": 0, "b_wins": 0, "draws": 0, "cutoffs": 0}
            for index, game in games.items():
                if game["status"] == "playing":
                    continue
                if game["status"] in ("cutoff", "aborted"):
                    results["cutoffs"] += 1
                elif game.get("winner") is None:
                    results["draws"] += 1
                else:
                    results["a_wins" if game["winner"] == (1 if index % 2 == 0 else -1) else "b_wins"] += 1
            with self.lock:
                batch = self.storage.get_batch(original["id"])
                if batch["status"] != "running":
                    continue
                self.tracked.update(g["id"] for g in running if not g.get("paused"))
                batch["completed"], batch["results"] = len(completed), results
                if len(completed) == batch["total"]:
                    batch["status"] = "completed"
                    batch["completed_at"] = datetime.now(timezone.utc).isoformat()
                batch["metrics"] = self._batch_metrics(batch, games, completed)
                batch = self.storage.update_batch(batch)
                if batch["status"] == "completed":
                    if batch["learning"] and not batch.get("learning_queued"):
                        for item in batch["schedule"]:
                            self.learning_queue.put(item["game_id"])
                        batch["learning_queued"] = True
                        self.storage.update_batch(batch)
                    continue
            capacity = max(0, original["workers"] - len(running))
            for item in original["schedule"]:
                if not capacity or self.stop_event.is_set():
                    break
                if item.get("game_id"):
                    continue
                # Separate deterministic seed domains prevent the same UI seed
                # from reusing training openings in a held-out benchmark.
                domain = "training" if original["learning"] else "evaluation"
                pair_seed = random.Random(f"{domain}:{original['seed']}:{item['index'] // 2}").randrange(2**31)
                # Expensive preparation deliberately occurs outside the lock.
                position = self._opening_position(original["config"], pair_seed)
                with self.lock:
                    batch = self.storage.get_batch(original["id"])
                    scheduled = batch["schedule"][item["index"]]
                    if batch["status"] != "running" or self.stop_event.is_set():
                        break
                    if scheduled.get("game_id"):
                        continue
                    a, b = batch["profiles"]
                    white, black = (a, b) if item["index"] % 2 == 0 else (b, a)
                    game = self.create_game({"players": {
                        "white": {"kind": "ai", "profile": white, "seconds": 60},
                        "black": {"kind": "ai", "profile": black, "seconds": 60}},
                        "seed": pair_seed, "learning": batch["learning"], "view_delay": 0,
                        "learning_disabled": not batch["learning"], "schedule_index": item["index"],
                        "node_budget": batch["nodes"], "max_plies": batch["max_plies"]}, batch["id"], position)
                    scheduled["game_id"] = game["id"]
                    self.storage.update_batch(batch)
                    capacity -= 1

    def _batch_loop(self):
        while not self.stop_event.wait(0.25):
            try:
                self._batch_tick()
            except Exception:
                traceback.print_exc()
            self.stop_event.wait(0.75)

    def _loop(self):
        while not self.stop_event.wait(0.025):
            try:
                with self.lock:
                    for task in list(self.active.values()):
                        self._poll_task(task)
                        if task.done is not None or task.error or task.force or time.monotonic() >= task.deadline:
                            reason = ("move_now" if task.force else "deadline" if time.monotonic() >= task.deadline
                                      else "error" if task.error else task.done.get("reason", "completed"))
                            self._finish_task(task, reason)
                    batch_active = sum(bool(self.storage.get_game(gid).get("batch_id")) for gid in self.active)
                    interactive_waiting = []
                    batch_waiting = []
                    for game_id in list(self.tracked):
                        if game_id in self.active or time.monotonic() < self.eligible.get(game_id, 0):
                            continue
                        game = self.storage.get_game(game_id)
                        if game["status"] != "playing":
                            self.tracked.discard(game_id)
                            continue
                        if game.get("paused"):
                            continue
                        side = "white" if game["position"]["turn"] == 1 else "black"
                        if game["players"][side]["kind"] == "ai":
                            (batch_waiting if game.get("batch_id") else interactive_waiting).append(game)
                    for game in interactive_waiting[:2]:
                        self._launch(game)
                    capacity = max(1, min(4, (os.cpu_count() or 2) - 1))
                    for game in batch_waiting[:max(0, capacity - batch_active)]:
                        self._launch(game)
            except Exception:
                traceback.print_exc()
                self.stop_event.wait(0.1)
