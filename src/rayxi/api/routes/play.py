"""In-browser game player.

Architecture:
  GET  /play/{name}       — serve the game player HTML page
  WS   /ws/games/{name}   — WebSocket: streams JPEG frames to browser,
                            receives keyboard/mouse events from browser

Game runs in a background thread with SDL_VIDEODRIVER=offscreen.
pygame.display.flip is monkey-patched to capture each frame as JPEG
and push it into a per-session asyncio queue.
pygame.event.get is monkey-patched to inject keyboard events received
from the browser via the WebSocket.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import threading
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

log = logging.getLogger("rayxi.api.play")
router = APIRouter()

_GAMES_ROOT = Path("games")


# ---------------------------------------------------------------------------
# Browser key code → pygame key constant (shared by session & event builder)
# ---------------------------------------------------------------------------


def _build_key_map() -> dict:
    import pygame

    return {
        "ArrowLeft": pygame.K_LEFT,
        "ArrowRight": pygame.K_RIGHT,
        "ArrowUp": pygame.K_UP,
        "ArrowDown": pygame.K_DOWN,
        "Space": pygame.K_SPACE,
        "Enter": pygame.K_RETURN,
        "Escape": pygame.K_ESCAPE,
        "ShiftLeft": pygame.K_LSHIFT,
        "ShiftRight": pygame.K_RSHIFT,
        "ControlLeft": pygame.K_LCTRL,
        "ControlRight": pygame.K_RCTRL,
        "AltLeft": pygame.K_LALT,
        "AltRight": pygame.K_RALT,
        "KeyZ": pygame.K_z,
        "KeyX": pygame.K_x,
        "KeyC": pygame.K_c,
        "KeyA": pygame.K_a,
        "KeyS": pygame.K_s,
        "KeyD": pygame.K_d,
        "KeyW": pygame.K_w,
        "KeyQ": pygame.K_q,
        "KeyE": pygame.K_e,
        "KeyR": pygame.K_r,
        "KeyF": pygame.K_f,
        "KeyG": pygame.K_g,
        "KeyH": pygame.K_h,
        "KeyJ": pygame.K_j,
        "KeyK": pygame.K_k,
        "KeyL": pygame.K_l,
        "KeyP": pygame.K_p,
        "Digit1": pygame.K_1,
        "Digit2": pygame.K_2,
        "Digit3": pygame.K_3,
        "Digit4": pygame.K_4,
        "Digit5": pygame.K_5,
        "Backspace": pygame.K_BACKSPACE,
        "Tab": pygame.K_TAB,
        "Comma": pygame.K_COMMA,
        "Period": pygame.K_PERIOD,
        "Slash": pygame.K_SLASH,
    }


# Lazy-initialised once pygame is available
_KEY_CONST_MAP: dict = {}


def _get_key_map() -> dict:
    global _KEY_CONST_MAP
    if not _KEY_CONST_MAP:
        try:
            _KEY_CONST_MAP = _build_key_map()
        except Exception:
            pass
    return _KEY_CONST_MAP


# ---------------------------------------------------------------------------
# Session — one per connected browser tab
# ---------------------------------------------------------------------------


class GameSession:
    """Runs a game in a background thread with offscreen SDL."""

    def __init__(self, name: str, game_path: Path) -> None:
        self.name = name
        self.game_path = game_path
        self.running = False
        self._frame_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        self._pending_events: list[dict] = []
        self._pressed_keys: set[int] = set()  # for pygame.key.get_pressed() emulation
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._error: str | None = None
        self._crash_error: tuple[str, str] | None = None  # (msg, traceback)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"game-{self.name}")
        self._thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        try:
            import pygame

            pygame.event.post(pygame.event.Event(pygame.QUIT))
        except Exception:
            pass

    # ── Input injection ───────────────────────────────────────────────────

    def push_event(self, ev: dict) -> None:
        """Called from the WebSocket coroutine to inject a browser event."""
        with self._lock:
            self._pending_events.append(ev)
            # Track key state for pygame.key.get_pressed() emulation
            key_name = ev.get("key", "")
            key_const = _get_key_map().get(key_name)
            if key_const is not None:
                if ev.get("action") == "down":
                    self._pressed_keys.add(key_const)
                elif ev.get("action") == "up":
                    self._pressed_keys.discard(key_const)

    def _pop_events(self) -> list[dict]:
        with self._lock:
            evs = list(self._pending_events)
            self._pending_events.clear()
        return evs

    # ── Frame stream ──────────────────────────────────────────────────────

    async def next_frame(self, timeout: float = 0.1) -> bytes | None:
        """Wait for the next JPEG frame (returns None on timeout)."""
        try:
            return await asyncio.wait_for(self._frame_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    def _push_frame(self, jpeg: bytes) -> None:
        """Called from game thread — non-blocking put to async queue."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._put_frame_async(jpeg), self._loop)
        except Exception:
            pass

    async def _put_frame_async(self, jpeg: bytes) -> None:
        try:
            self._frame_queue.put_nowait(jpeg)
        except asyncio.QueueFull:
            # Drop the oldest frame and push the new one
            try:
                self._frame_queue.get_nowait()
                self._frame_queue.put_nowait(jpeg)
            except Exception:
                pass

    # ── Game thread ───────────────────────────────────────────────────────

    def _run(self) -> None:
        # Must set env BEFORE any pygame import
        os.environ["SDL_VIDEODRIVER"] = "offscreen"
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        os.environ["DISPLAY"] = ""

        # Initialise so finally block can safely reference them
        _patched_flip = None
        _patched_update = None
        _patched_event_get = None
        _patched_key_get = None
        _orig_flip = None
        _orig_update = None
        _orig_event_get = None
        _orig_key_get = None

        try:
            import pygame

            # Re-init display with offscreen driver
            if pygame.get_init():
                pygame.display.quit()
            pygame.init()

            session = self

            # ── Patch display.flip / display.update ──────────────────────
            _orig_flip = pygame.display.flip
            _orig_update = pygame.display.update

            def _capture_frame() -> None:
                if not session.running:
                    return
                try:
                    surf = pygame.display.get_surface()
                    if surf is None:
                        return
                    w, h = surf.get_size()
                    raw = pygame.image.tobytes(surf, "RGB")
                    from PIL import Image

                    img = Image.frombytes("RGB", (w, h), raw)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=65)
                    session._push_frame(buf.getvalue())
                except Exception as _e:
                    log.debug("Frame capture error: %s", _e)

            def _patched_flip() -> None:
                _orig_flip()
                _capture_frame()

            def _patched_update(*args, **kwargs) -> None:
                _orig_update(*args, **kwargs)
                _capture_frame()

            pygame.display.flip = _patched_flip
            pygame.display.update = _patched_update

            # ── Patch event.get to inject browser keypresses ─────────────
            _orig_event_get = pygame.event.get

            def _patched_event_get(*args, **kwargs):
                events = list(_orig_event_get(*args, **kwargs))
                for ev_dict in session._pop_events():
                    try:
                        ev = _dict_to_pygame_event(ev_dict)
                        if ev is not None:
                            events.append(ev)
                    except Exception:
                        pass
                # Inject QUIT if session is stopping
                if not session.running:
                    events.append(pygame.event.Event(pygame.QUIT))
                return events

            pygame.event.get = _patched_event_get

            # ── Patch key.get_pressed for gameplay input ──────────────────
            _orig_key_get = pygame.key.get_pressed

            _NUM_KEYS = pygame.K_LAST if hasattr(pygame, "K_LAST") else 512

            class _KeyState:
                """Subscriptable like pygame's ScancodeWrapper."""

                def __init__(self, pressed: set) -> None:
                    self._p = pressed

                def __getitem__(self, key: int) -> bool:
                    return key in self._p

                def __len__(self) -> int:
                    return _NUM_KEYS

                def __iter__(self):
                    return iter(self._p)

            def copy(self):
                return _KeyState(frozenset(self._p))

            def _patched_key_get():
                with session._lock:
                    return _KeyState(frozenset(session._pressed_keys))

            pygame.key.get_pressed = _patched_key_get

            # ── Load and exec the game ────────────────────────────────────
            path = self.game_path.resolve()
            game_dir = str(path.parent)

            # Prepend game dir so multi-file games can do `from constants import *`
            import sys as _sys

            if game_dir not in _sys.path:
                _sys.path.insert(0, game_dir)

            src = path.read_text(encoding="utf-8")
            code = compile(src, str(path), "exec")
            globs = {
                "__name__": "__main__",
                "__file__": str(path),
                "__spec__": None,
            }
            exec(code, globs)

        except Exception as exc:
            self._error = str(exc)
            log.warning("Game session %s crashed: %s", self.name, exc)
            if self._loop:
                import traceback

                tb = traceback.format_exc()
                asyncio.run_coroutine_threadsafe(self._put_error_async(str(exc), tb), self._loop)
        finally:
            self.running = False
            # Restore patches so the next session starts from a known-clean state
            try:
                import pygame

                if _orig_flip is not None and _patched_flip is not None:
                    if pygame.display.flip is _patched_flip:
                        pygame.display.flip = _orig_flip  # type: ignore[assignment]
                if _orig_update is not None and _patched_update is not None:
                    if pygame.display.update is _patched_update:
                        pygame.display.update = _orig_update  # type: ignore[assignment]
                if _orig_event_get is not None and _patched_event_get is not None:
                    if pygame.event.get is _patched_event_get:
                        pygame.event.get = _orig_event_get  # type: ignore[assignment]
                if _orig_key_get is not None and _patched_key_get is not None:
                    if pygame.key.get_pressed is _patched_key_get:
                        pygame.key.get_pressed = _orig_key_get  # type: ignore[assignment]
                pygame.quit()
            except Exception:
                pass

    async def _put_error_async(self, msg: str, tb: str) -> None:
        # Store error so send_loop can forward it before closing
        self._crash_error = (msg, tb)


# ── Key name → pygame key constant ────────────────────────────────────────


def _dict_to_pygame_event(ev: dict):
    import pygame

    action = ev.get("action")  # "down" | "up"
    key_name = ev.get("key", "")  # e.g. "ArrowLeft", "Space", "KeyA"

    key_const = _get_key_map().get(key_name)
    if key_const is None:
        return None

    evt_type = pygame.KEYDOWN if action == "down" else pygame.KEYUP
    return pygame.event.Event(
        evt_type,
        {
            "key": key_const,
            "mod": 0,
            "unicode": "",
            "scancode": 0,
        },
    )


# ---------------------------------------------------------------------------
# Active sessions registry — only ONE session runs at a time
# (pygame shares a single display surface; concurrent sessions corrupt state)
# ---------------------------------------------------------------------------

_sessions: dict[str, GameSession] = {}
_active_session: GameSession | None = None


def _get_or_create_session(name: str) -> GameSession | None:
    global _active_session

    game_py = _GAMES_ROOT / name / "game.py"
    if not game_py.exists():
        return None

    # Reuse same game if still running
    existing = _sessions.get(name)
    if existing and existing.running:
        return existing

    # Stop any OTHER running session first (pygame has one display)
    if _active_session and _active_session.running and _active_session.name != name:
        log.info("Stopping previous session %r to start %r", _active_session.name, name)
        _active_session.stop()
        # Give the thread a moment to release pygame
        if _active_session._thread:
            _active_session._thread.join(timeout=3.0)

    session = GameSession(name=name, game_path=game_py)
    _sessions[name] = session
    _active_session = session
    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/play/{name}", response_class=HTMLResponse)
async def play_page(name: str) -> HTMLResponse:
    game_py = _GAMES_ROOT / name / "game.py"
    display_name = name.replace("_", " ").title()
    if not game_py.exists():
        return HTMLResponse(f"<h2>Game not found: {name}</h2>", status_code=404)
    return HTMLResponse(_PLAYER_HTML.replace("{{NAME}}", name).replace("{{DISPLAY_NAME}}", display_name))


@router.websocket("/ws/games/{name}")
async def game_ws(websocket: WebSocket, name: str) -> None:
    await websocket.accept()
    loop = asyncio.get_event_loop()

    session = _get_or_create_session(name)
    if session is None:
        await websocket.send_json({"type": "error", "message": f"Game not found: {name}"})
        await websocket.close()
        return

    if not session.running:
        session.start(loop)

    await websocket.send_json({"type": "started", "game": name})

    # Receive input events and stream frames concurrently
    async def recv_loop() -> None:
        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "key":
                    session.push_event(msg)
                elif msg.get("type") == "stop":
                    session.stop()
                    break
        except Exception:
            pass

    async def send_loop() -> None:
        try:
            while session.running or not session._frame_queue.empty():
                frame = await session.next_frame(timeout=0.2)
                if frame is not None:
                    b64 = base64.b64encode(frame).decode()
                    await websocket.send_json({"type": "frame", "data": b64})
            # Game ended — report crash if one occurred
            if session._crash_error:
                msg, tb = session._crash_error
                # Extract just the last meaningful line of the traceback
                lines = [l for l in tb.splitlines() if l.strip()]
                short = lines[-1] if lines else msg
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": f"Game crashed: {short}",
                        "detail": tb[-800:],
                    }
                )
        except Exception:
            pass

    recv_task = asyncio.create_task(recv_loop())
    send_task = asyncio.create_task(send_loop())

    try:
        await asyncio.wait(
            [recv_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
    except WebSocketDisconnect:
        pass
    finally:
        recv_task.cancel()
        send_task.cancel()
        session.stop()
        await websocket.close()


# ---------------------------------------------------------------------------
# Embedded player HTML (no extra template file needed)
# ---------------------------------------------------------------------------

_PLAYER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{DISPLAY_NAME}} — RayXI</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0f1117; --surface: #1a1d27; --border: #2d3148;
      --text: #e2e8f0; --muted: #8892a4; --accent: #7c6ee8; --l1: #4ade80;
    }
    html, body { height: 100%; background: var(--bg); color: var(--text);
                 font-family: "Inter", system-ui, sans-serif; overflow: hidden; }
    #shell { display: flex; flex-direction: column; height: 100vh; }
    #bar {
      flex: 0 0 40px; background: var(--surface); border-bottom: 1px solid var(--border);
      display: flex; align-items: center; padding: 0 14px; gap: 10px;
    }
    #bar h1 { color: var(--accent); font-size: 13px; font-weight: 700; margin-right: 8px; }
    #bar a { color: var(--muted); text-decoration: none; font-size: 12px; }
    #bar a:hover { color: var(--text); }
    #game-name { font-size: 13px; font-weight: 600; flex: 1; }
    #status { font-size: 11px; color: var(--muted); }
    #status.ok  { color: var(--l1); }
    #status.err { color: #f87171; }
    #viewport {
      flex: 1 1 0; min-height: 0; display: flex;
      align-items: center; justify-content: center; background: #000;
      position: relative;
    }
    #game-canvas { display: block; image-rendering: pixelated; max-width: 100%; max-height: 100%; }
    #overlay {
      position: absolute; inset: 0; display: flex;
      align-items: center; justify-content: center;
      flex-direction: column; gap: 14px; color: var(--muted); font-size: 14px;
    }
    #start-btn {
      background: var(--accent); color: #fff; border: none; border-radius: 8px;
      padding: 12px 32px; font-size: 16px; font-weight: 700; cursor: pointer;
    }
    #start-btn:hover { opacity: .85; }
    #keys-hint {
      position: absolute; bottom: 8px; left: 50%; transform: translateX(-50%);
      font-size: 10px; color: var(--muted); white-space: nowrap;
    }
  </style>
</head>
<body>
<div id="shell">
  <div id="bar">
    <h1>RayXI</h1>
    <a href="/gallery">← Gallery</a>
    <span id="game-name">{{DISPLAY_NAME}}</span>
    <span id="status">Waiting…</span>
  </div>
  <div id="viewport">
    <canvas id="game-canvas"></canvas>
    <div id="overlay">
      <div>Click to start — then use keyboard to play</div>
      <button id="start-btn">▶ Start {{DISPLAY_NAME}}</button>
    </div>
    <div id="keys-hint">Arrow keys · Space · Z/X/C · A/S/D/W · Enter/Esc</div>
  </div>
</div>
<script>
'use strict';
const canvas   = document.getElementById('game-canvas');
const ctx      = canvas.getContext('2d');
const overlay  = document.getElementById('overlay');
const startBtn = document.getElementById('start-btn');
const statusEl = document.getElementById('status');
const gameName = '{{NAME}}';
let ws = null;

startBtn.addEventListener('click', connect);

function connect() {
  // Remove focus from button so Space/Enter don't re-trigger it
  startBtn.blur();
  startBtn.disabled = true;
  overlay.style.display = 'none';
  statusEl.textContent = 'Connecting…';
  statusEl.className = '';

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/games/${encodeURIComponent(gameName)}`);

  ws.onopen  = () => { statusEl.textContent = 'Starting game…'; };
  ws.onclose = () => { statusEl.textContent = 'Disconnected'; statusEl.className = 'err'; };
  ws.onerror = () => { statusEl.textContent = 'Connection error'; statusEl.className = 'err'; };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'started') {
      statusEl.textContent = 'Running ▶';
      statusEl.className = 'ok';
    } else if (msg.type === 'frame') {
      drawFrame(msg.data);
    } else if (msg.type === 'error') {
      statusEl.textContent = 'Error: ' + msg.message;
      statusEl.className = 'err';
    }
  };
}

function drawFrame(b64) {
  const img = new Image();
  img.onload = () => {
    if (canvas.width !== img.width || canvas.height !== img.height) {
      canvas.width  = img.width;
      canvas.height = img.height;
    }
    ctx.drawImage(img, 0, 0);
  };
  img.src = 'data:image/jpeg;base64,' + b64;
}

// ── Keyboard forwarding ──────────────────────────────────────────────────
const FORWARD_KEYS = new Set([
  'ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Space','Enter','Escape',
  'ShiftLeft','ShiftRight','ControlLeft','ControlRight','AltLeft','AltRight',
  'KeyZ','KeyX','KeyC','KeyA','KeyS','KeyD','KeyW','KeyQ','KeyE','KeyR',
  'KeyF','KeyG','KeyH','KeyJ','KeyK','KeyL','KeyP',
  'Digit1','Digit2','Digit3','Digit4','Digit5',
  'Backspace','Tab','Comma','Period','Slash',
]);

document.addEventListener('keydown', e => {
  if (FORWARD_KEYS.has(e.code)) {
    e.preventDefault();
    sendKey(e.code, 'down');
  }
});
document.addEventListener('keyup', e => {
  if (FORWARD_KEYS.has(e.code)) {
    e.preventDefault();
    sendKey(e.code, 'up');
  }
});

function sendKey(code, action) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'key', key: code, action }));
  }
}
</script>
</body>
</html>"""
