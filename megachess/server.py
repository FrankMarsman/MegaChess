"""Same-origin HTTP API and the single-page Mega Chess application."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .engine import Position, extract_features, iter_legal_moves, new_position
from .runtime import Runtime
from .statistics import StatisticsCache


def default_data_dir():
    if os.environ.get("MEGACHESS_DATA_DIR"):
        return Path(os.environ["MEGACHESS_DATA_DIR"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MegaChess"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "MegaChess"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "megachess"


def create_app(data_dir=None, start_workers=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = Runtime(Path(data_dir) if data_dir else default_data_dir())
        if start_workers:
            app.state.runtime.start()
        yield
        app.state.runtime.close()

    app = FastAPI(title="Mega Chess", version=__version__, lifespan=lifespan)
    web = Path(__file__).parent / "web"
    statistics_cache = StatisticsCache()

    def runtime(request):
        return request.app.state.runtime

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409 if "changed" in str(exc).lower() or "revision" in str(exc).lower() else 400)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/bootstrap")
    def bootstrap(request: Request):
        store = runtime(request).storage
        return {"profiles": store.list_profiles(), "games": store.list_games(), "batches": store.list_batches(),
                "capabilities": {"version": __version__, "sizes": [1, 3, 5, 7, 9, 11, 13],
                                 "seconds": [2, 5, 10, 30, 60], "data_dir": str(runtime(request).data_dir)}}

    @app.post("/api/v1/preview")
    def preview(payload: dict = Body(...)):
        position = new_position(payload.get("config", {}))
        return {"position": position.to_dict(), "counts": {
            "white": sum(p > 0 for p in position.board), "black": sum(p < 0 for p in position.board)}}

    @app.post("/api/v1/games")
    def create_game(request: Request, payload: dict = Body(...)):
        return {"game": runtime(request).create_game(payload)}

    @app.get("/api/v1/games")
    def games(request: Request, limit: int = 100, offset: int = 0):
        return {"games": runtime(request).storage.list_games(limit=max(1, min(1000, limit)), offset=max(0, offset))}

    @app.get("/api/v1/games/{game_id}")
    def get_game(game_id: str, request: Request):
        store = runtime(request).storage
        return {"game": store.get_game(game_id), "moves": store.get_moves(game_id)}

    @app.get("/api/v1/games/{game_id}/updates")
    def game_updates(game_id: str, request: Request, revision: int = -1):
        rt = runtime(request)
        with rt.lock:
            game = rt.storage.get_game(game_id)
            return {"game": game, "search": rt.search_status(game_id),
                    "moves": rt.storage.get_moves(game_id, after_revision=revision)}

    @app.get("/api/v1/games/{game_id}/legal")
    def legal(game_id: str, from_square: int, request: Request):
        game = runtime(request).storage.get_game(game_id)
        if game["status"] != "playing":
            return {"moves": [], "revision": game["revision"]}
        position = Position.from_dict(game["position"])
        return {"moves": list(iter_legal_moves(position, from_square)), "revision": game["revision"]}

    @app.get("/api/v1/games/{game_id}/statistics")
    def game_statistics(game_id: str, request: Request, revision: int | None = None,
                        position_index: int | None = None):
        store = runtime(request).storage
        game = store.get_game(game_id)
        if revision is not None and revision != game["revision"]:
            raise ValueError("Game changed; refresh its current position")
        position = (game["position"] if position_index is None
                    else store.get_recorded_position(game_id, position_index))
        stats = statistics_cache.get((game_id, game["revision"], position_index), position)
        return {"game_id": game_id, "revision": game["revision"],
                "position_index": position_index, **stats}

    @app.post("/api/v1/games/{game_id}/move")
    def move(game_id: str, request: Request, payload: dict = Body(...)):
        return {"game": runtime(request).play_move(game_id, payload["move"], payload["revision"])}

    @app.post("/api/v1/games/{game_id}/control")
    def game_control(game_id: str, request: Request, payload: dict = Body(...)):
        return {"game": runtime(request).control(game_id, payload["action"], payload.get("value"))}

    @app.get("/api/v1/games/{game_id}/export")
    def export_game(game_id: str, request: Request):
        store = runtime(request).storage
        return JSONResponse({"format": "megachess-game-v1", "engine_version": __version__,
            "game": store.get_game(game_id), "moves": store.get_moves(game_id)},
            headers={"Content-Disposition": f'attachment; filename="megachess-{game_id}.json"'})

    @app.get("/api/v1/games/{game_id}/evaluation")
    def evaluation(game_id: str, request: Request):
        game = runtime(request).storage.get_game(game_id)
        features = extract_features(Position.from_dict(game["position"]))
        contributions = {}
        for side in ("white", "black"):
            profile = game["players"][side].get("profile")
            if profile:
                contributions[side] = {key: value * profile["weights"].get(key, 0) for key, value in features.items()}
        return {"features": features, "contributions": contributions}

    @app.get("/api/v1/profiles")
    def profiles(request: Request):
        return {"profiles": runtime(request).storage.list_profiles()}

    @app.get("/api/v1/profiles/{profile_id}/versions")
    def profile_versions(profile_id: str, request: Request):
        return {"versions": runtime(request).storage.profile_versions(profile_id)}

    @app.post("/api/v1/profiles")
    def save_profile(request: Request, payload: dict = Body(...)):
        profile = payload["profile"]
        if payload.get("clone"):
            profile = {**profile, "id": uuid.uuid4().hex, "name": profile["name"] + " copy"}
        profile["anchor_weights"] = dict(profile["weights"])
        return {"profile": runtime(request).storage.save_profile(profile)}

    @app.post("/api/v1/profiles/{profile_id}/reset")
    def reset_profile(profile_id: str, request: Request):
        return {"profile": runtime(request).storage.reset_profile(profile_id)}

    @app.get("/api/v1/batches")
    def batches(request: Request):
        return {"batches": runtime(request).storage.list_batches()}

    @app.post("/api/v1/batches")
    def create_batch(request: Request, payload: dict = Body(...)):
        return {"batch": runtime(request).create_batch(payload)}

    @app.post("/api/v1/batches/{batch_id}/control")
    def batch_control(batch_id: str, request: Request, payload: dict = Body(...)):
        return {"batch": runtime(request).control_batch(batch_id, payload["action"])}

    @app.get("/api/v1/training/export")
    def export_training(request: Request):
        def records():
            for record in runtime(request).storage.export_training():
                yield json.dumps(record, separators=(",", ":")) + "\n"
        return StreamingResponse(records(), media_type="application/x-ndjson", headers={
            "Content-Disposition": 'attachment; filename="megachess-training.jsonl"'})

    @app.post("/api/v1/backup")
    def backup(request: Request):
        rt = runtime(request)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        target = rt.data_dir / "backups" / f"megachess-{stamp}.sqlite3"
        target.parent.mkdir(exist_ok=True)
        return {"path": str(rt.storage.backup(target))}

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    app.mount("/static", StaticFiles(directory=str(web), check_dir=False), name="static")
    return app


app = create_app()
