"""RayXI FastAPI application.

Run with:
    uvicorn rayxi.api.app:app --host 0.0.0.0 --port 8000 --reload
or via the CLI:
    rayxi serve
"""

from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, Request

from rayxi.logging_setup import configure as _configure_logging

_configure_logging()
import pathlib as _pathlib

from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rayxi.api.config import web_dir
from rayxi.api.routes.chat import router as chat_router
from rayxi.api.routes.graph import router as graph_router
from rayxi.api.routes.play import router as play_router
from rayxi.api.routes.schemas import router as schemas_router
from rayxi.api.routes.studio import router as studio_router
from rayxi.api.routes.game_test import router as game_test_router
from rayxi.api.routes.game_log import router as game_log_router

# ---------------------------------------------------------------------------
# Logging — full stack traces to stderr (captured by uvicorn)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("rayxi.api")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="RayXI", version="1.0.0")

_web = web_dir()
app.mount("/static", StaticFiles(directory=str(_web / "static")), name="static")
_templates = Jinja2Templates(directory=str(_web / "templates"))

app.include_router(graph_router)
app.include_router(schemas_router)
app.include_router(chat_router)
app.include_router(play_router)
app.include_router(studio_router)
app.include_router(game_test_router)
app.include_router(game_log_router)

# Serve Godot web exports dynamically — new games are available immediately
# without server restart. Uses a catch-all route instead of static mounts.
_godot_games = _pathlib.Path(__file__).resolve().parents[3] / "games"


@app.get("/godot/{game_name}/{file_path:path}")
async def serve_godot_game(game_name: str, file_path: str):
    """Serve Godot web export files dynamically.

    Games created by the pipeline are immediately available without restart.
    """
    from fastapi.responses import FileResponse

    export_dir = _godot_games / game_name / "export"
    if not export_dir.is_dir():
        return JSONResponse(status_code=404, content={"detail": f"Game '{game_name}' not found or not exported"})

    # Default to index.html
    if not file_path or file_path == "/":
        file_path = "index.html"

    target = export_dir / file_path
    if not target.is_file():
        return JSONResponse(status_code=404, content={"detail": f"File not found: {file_path}"})

    # Set correct MIME types for Godot web exports
    import mimetypes

    content_type, _ = mimetypes.guess_type(str(target))
    if target.suffix == ".wasm":
        content_type = "application/wasm"
    elif target.suffix == ".pck":
        content_type = "application/octet-stream"

    # Godot web exports need specific headers for SharedArrayBuffer
    headers = {
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Embedder-Policy": "require-corp",
    }

    return FileResponse(str(target), media_type=content_type, headers=headers)


# ---------------------------------------------------------------------------
# Global exception handler — log full traceback, return structured JSON
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    tb = traceback.format_exc()
    log.error(
        "Unhandled exception on %s %s\n%s",
        request.method,
        request.url,
        tb,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": type(exc).__name__,
            "detail": str(exc),
            "path": str(request.url),
            "traceback": tb,
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse(request, "index.html")


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse(request, "chat.html")


@app.get("/gallery", response_class=HTMLResponse)
async def gallery_page(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse(request, "gallery.html")


@app.get("/api/gallery/games")
async def gallery_games() -> JSONResponse:
    """List all playable games — Godot web exports + pygame games."""
    games = []

    # Godot web exports (GPU-accelerated, runs in browser)
    for gdir in sorted(_godot_games.iterdir()):
        export_dir = gdir / "export"
        if export_dir.is_dir() and (export_dir / "index.html").exists():
            games.append(
                {
                    "name": gdir.name,
                    "engine": "godot",
                    "url": f"/godot/{gdir.name}/",
                    "test_url": f"/test/{gdir.name}",
                    "log_url": f"/log/{gdir.name}",
                    "thumb": None,
                }
            )

    # Pygame games (streamed via WebSocket — heavier on server)
    for gdir in sorted(_godot_games.iterdir()):
        game_py = gdir / "game.py"
        if game_py.exists() and not (gdir / "export" / "index.html").exists():
            games.append(
                {
                    "name": gdir.name,
                    "engine": "pygame",
                    "url": f"/play/{gdir.name}",
                    "thumb": None,
                }
            )

    return JSONResponse({"games": games})


@app.get("/studio", response_class=HTMLResponse)
async def studio_page(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse(request, "studio.html")
