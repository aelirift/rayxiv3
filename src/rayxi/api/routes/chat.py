"""Chat API routes.

GET  /api/projects                    — list existing game directories
GET  /api/projects/{name}/source      — current game.py content
GET  /api/projects/{name}/history     — chat history for a project
POST /api/games/{name}/launch         — launch game as subprocess (returns pid)
POST /api/chat                        — stream agent progress (SSE over POST)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from rayxi.agent import AgentTask, GameAgent
from rayxi.agent.web_searcher import WebSearcher
from rayxi.knowledge.knowledge_base import KnowledgeBase
from rayxi.llm.callers import build_callers

log = logging.getLogger("rayxi.api.chat")
router = APIRouter()

_GAMES_ROOT = Path("games")

# Keywords that route an existing-project message to FIX rather than IMPROVE
_FIX_KEYWORDS = {
    "fix",
    "bug",
    "error",
    "broken",
    "issue",
    "crash",
    "fail",
    "wrong",
    "security",
    "vulnerability",
    "inject",
    "leak",
    "problem",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _games_root() -> Path:
    _GAMES_ROOT.mkdir(exist_ok=True)
    return _GAMES_ROOT


def _project_dir(name: str) -> Path:
    return _games_root() / _sanitize(name)


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower().strip()).strip("_") or "game"


def _detect_mode(message: str, project_exists: bool) -> str:
    if not project_exists:
        return "create"
    words = set(message.lower().split())
    if words & _FIX_KEYWORDS:
        return "fix"
    return "improve"


def _history_path(name: str) -> Path:
    return _project_dir(name) / "chat.json"


def _load_history(name: str) -> list[dict]:
    p = _history_path(name)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _append_history(name: str, role: str, content: str) -> None:
    history = _load_history(name)
    history.append(
        {
            "role": role,
            "content": content,
            "ts": datetime.now(UTC).isoformat(),
        }
    )
    p = _history_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(history, indent=2), encoding="utf-8")


def _build_agent() -> GameAgent:
    try:
        callers = build_callers()
        llm = callers.get("primary") or callers.get("glm") or callers.get("minimax") or next(iter(callers.values()))
        kb = KnowledgeBase(knowledge_dir=Path("knowledge"))
        searcher = WebSearcher(cache_dir=Path(".cache/web_search"))
        return GameAgent(llm, searcher=searcher, knowledge_base=kb)
    except Exception as exc:
        raise RuntimeError(f"No LLM callers available: {exc}") from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/projects")
async def list_projects() -> dict:
    root = _games_root()
    projects = sorted(d.name for d in root.iterdir() if d.is_dir() and (d / "game.py").exists())
    return {"projects": projects}


@router.get("/api/projects/{name}/source")
async def get_source(name: str) -> dict:
    game_py = _project_dir(name) / "game.py"
    if not game_py.exists():
        return {"source": "", "exists": False}
    return {"source": game_py.read_text(encoding="utf-8"), "exists": True}


@router.get("/api/projects/{name}/history")
async def get_history(name: str) -> dict:
    return {"history": _load_history(name)}


@router.get("/api/games/{name}/verify")
async def verify_game(name: str) -> JSONResponse:
    """Quick verify check for a game — syntax-only (no subprocess smoke test).

    The full GameVerifier.verify() spawns subprocesses that can trigger heavy
    imports (rembg, torch) and hang. Gallery badges only need a syntax check.
    """
    import ast

    game_dir = _project_dir(_sanitize(name))
    game_py = game_dir / "game.py"
    if not game_py.exists():
        return JSONResponse({"passed": False, "error_type": "missing", "error": "game.py not found"})
    try:
        source = game_py.read_text(encoding="utf-8")
        ast.parse(source, filename=str(game_py))
        return JSONResponse({"passed": True, "error_type": "", "error": ""})
    except SyntaxError as exc:
        return JSONResponse(
            {
                "passed": False,
                "error_type": "syntax",
                "error": f"Line {exc.lineno}: {exc.msg}",
            }
        )


@router.post("/api/games/{name}/launch")
async def launch_game(name: str) -> JSONResponse:
    """Launch the game as a subprocess. The pygame window opens on the user's display."""
    game_py = _project_dir(_sanitize(name)) / "game.py"
    if not game_py.exists():
        raise HTTPException(status_code=404, detail=f"game.py not found for {name!r}")

    env = os.environ.copy()
    # Ensure a display is available; fall back to :0 if DISPLAY not set
    if not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"

    try:
        proc = subprocess.Popen(
            [sys.executable, str(game_py.resolve())],
            cwd=str(game_py.parent.resolve()),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # detach from server process group
        )
        return JSONResponse({"launched": True, "pid": proc.pid, "game": name})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class ChatRequest(BaseModel):
    project: str  # project name (new or existing)
    message: str  # user's message / concept
    max_iterations: int = 3


@router.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    project_name = _sanitize(req.project) if req.project.strip() else "untitled"
    project_dir = _project_dir(project_name)
    project_exists = (project_dir / "game.py").exists()
    mode = _detect_mode(req.message, project_exists)

    log.info("Chat: project=%s  mode=%s  exists=%s", project_name, mode, project_exists)

    # Save user message to history
    _append_history(project_name, "user", req.message)

    async def generate():
        agent_summary_parts: list[str] = []

        try:
            agent = _build_agent()
        except RuntimeError as exc:
            event = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(event)}\n\n"
            return

        task = AgentTask(
            mode=mode,
            concept=req.message,
            games_root=_games_root(),
            game_dir=project_dir if mode != "create" else None,
            max_iterations=req.max_iterations,
        )

        try:
            async for event in agent.stream(
                task,
                game_name_override=project_name if mode == "create" else None,
            ):
                yield f"data: {json.dumps(event)}\n\n"

                # Accumulate summary for history
                t = event.get("type")
                if t == "created":
                    agent_summary_parts.append(f"Game '{event.get('game_name')}' created.")
                elif t == "scan":
                    h = event.get("high", 0)
                    agent_summary_parts.append(f"Scan {event.get('iteration')}: {h} HIGH findings.")
                elif t == "fixed":
                    if event.get("applied"):
                        fn = event.get("function", "")
                        agent_summary_parts.append(f"Fixed {fn}.")
                elif t == "done":
                    agent_summary_parts.append(
                        f"Done ({event.get('stopped_reason')}, {event.get('total_fixes', 0)} fixes)."
                    )
                elif t == "error":
                    agent_summary_parts.append(f"Error: {event.get('message')}")

        except Exception as exc:
            tb_event = {
                "type": "error",
                "message": str(exc),
            }
            yield f"data: {json.dumps(tb_event)}\n\n"
            log.exception("Unhandled error in chat stream")
        finally:
            # Save agent summary to history
            if agent_summary_parts:
                _append_history(project_name, "agent", " ".join(agent_summary_parts))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind proxy
        },
    )
