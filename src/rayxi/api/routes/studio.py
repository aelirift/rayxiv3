"""Studio API routes — sprite/asset viewer and image upload for any game."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

GAMES_DIR = Path(__file__).resolve().parents[4] / "games"


def _game_assets(game: str) -> Path:
    safe = Path(game).name
    return GAMES_DIR / safe / "assets"


@router.get("/api/studio/games")
async def list_games() -> JSONResponse:
    """List games that have an assets directory."""
    games = sorted(d.name for d in GAMES_DIR.iterdir() if d.is_dir() and (d / "assets").is_dir())
    return JSONResponse({"games": games})


@router.get("/api/studio/frames")
async def list_frames(game: str = "chun_li_demo") -> JSONResponse:
    """List all image files in a game's assets directory."""
    assets = _game_assets(game)
    if not assets.exists():
        return JSONResponse({"frames": [], "error": "no assets dir"})
    frames = sorted(f.name for f in assets.iterdir() if f.suffix.lower() in (".png", ".jpg", ".gif"))
    return JSONResponse({"frames": frames, "game": game})


@router.get("/api/studio/frame/{name}")
async def get_frame(name: str, game: str = "chun_li_demo") -> FileResponse:
    """Serve a single asset image."""
    safe = Path(name).name
    path = _game_assets(game) / safe
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/png")


@router.post("/api/studio/upload")
async def upload_image(
    file: UploadFile = File(...),
    name: str = Form("uploaded"),
    game: str = Form("chun_li_demo"),
) -> JSONResponse:
    """Save an uploaded image to a game's assets directory."""
    safe = "".join(c for c in name if c.isalnum() or c in "_-").strip() or "uploaded"
    assets = _game_assets(game)
    assets.mkdir(parents=True, exist_ok=True)
    out = assets / f"{safe}.png"
    content = await file.read()
    out.write_bytes(content)
    return JSONResponse({"message": f"Saved as {out.name} ({len(content)} bytes)", "path": str(out)})
