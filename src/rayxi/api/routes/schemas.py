"""GET /api/schemas — list available FSM schema IDs."""

from __future__ import annotations

from fastapi import APIRouter

from rayxi.api.config import schemas_dir

router = APIRouter()


@router.get("/api/schemas")
async def list_schemas():
    """Return the IDs of all schema JSON files in the schemas directory."""
    d = schemas_dir()
    if not d.is_dir():
        return {"schemas": []}
    return {"schemas": sorted(p.stem for p in d.glob("*.json"))}
