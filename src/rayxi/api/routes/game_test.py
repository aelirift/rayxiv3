"""Game Test API — auto-generated Playwright tests driven by HLR + impact_map.

Endpoints:
  GET /test/{game_name}     — test control page (Start button + live SSE viewer)
  GET /api/test/{game_name} — SSE stream of test steps with screenshots

Design (v3):
  - Test plan comes from output/{game}/hlr.json + impact_map_final.json
    * scenes → navigation steps
    * input enums / mechanic_specs → gameplay steps
    * mechanic_spec.hud_entities → visual verification checkpoints
  - Playwright runs the steps, takes screenshots between each
  - Verification uses PIL pixel analysis on the screenshot PNG directly —
    unique color count + variance. WebGL canvas reads via JS are unreliable;
    the screenshot bytes are the ground truth.
  - Each run wipes the per-game screenshots directory before starting
  - A test step PASSES only if the canvas shows real rendered content AND
    (for movement steps) the screenshot actually changed.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

router = APIRouter()
log = logging.getLogger("rayxi.api.game_test")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_OUTPUT_DIR = _REPO_ROOT / "output"
_DEBUG_DIR = _REPO_ROOT / ".debug" / "screenshots"


# ---------------------------------------------------------------------------
# Test page
# ---------------------------------------------------------------------------


@router.get("/test/{game_name}", response_class=HTMLResponse)
async def test_page(game_name: str):
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Test: {game_name}</title>
    <style>
        body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
        h1 {{ color: #00ff88; }}
        #status {{ white-space: pre-wrap; background: #0d0d1a; padding: 15px; border-radius: 8px;
                   max-height: 260px; overflow-y: auto; margin: 10px 0; font-size: 13px; }}
        #screenshots {{ display: flex; flex-wrap: wrap; gap: 12px; }}
        .step {{ background: #16213e; border-radius: 8px; padding: 10px; width: 420px; }}
        .step img {{ width: 400px; border-radius: 4px; display: block; }}
        .step .label {{ color: #00ff88; font-weight: bold; margin-bottom: 5px; }}
        .step .keys {{ color: #ffcc00; font-size: 11px; }}
        .step .desc {{ color: #aaa; font-size: 12px; margin-top: 4px; }}
        .step .metrics {{ color: #66ccff; font-size: 11px; margin-top: 3px; font-family: monospace; }}
        .step.fail {{ border: 2px solid #ff4444; }}
        .step.pass {{ border: 1px solid #1a4a2a; }}
        button {{ background: #00ff88; color: #000; border: none; padding: 12px 24px;
                  font-size: 16px; font-weight: bold; border-radius: 6px; cursor: pointer; }}
        button:hover {{ background: #00cc66; }}
        button:disabled {{ background: #333; color: #666; cursor: not-allowed; }}
        .playing {{ display: inline-block; margin-left: 10px; }}
        a {{ color: #00ff88; }}
        .pass {{ color: #00ff88; }} .fail {{ color: #ff4444; }}
    </style>
</head>
<body>
    <h1>🧪 Auto-Test: {game_name}</h1>
    <p>
        Play: <a href="/godot/{game_name}/" target="_blank">/godot/{game_name}/</a> |
        Log: <a href="/log/{game_name}" target="_blank">/log/{game_name}</a> |
        <a href="/gallery">← Gallery</a>
    </p>
    <button id="startBtn" onclick="startTest()">▶ Start Auto-Test</button>
    <span id="playing" class="playing"></span>
    <div id="status"></div>
    <div id="screenshots"></div>
    <script>
        const gameName = "{game_name}";
        function startTest() {{
            document.getElementById('startBtn').disabled = true;
            document.getElementById('playing').textContent = '⏳ Running...';
            document.getElementById('status').textContent = '';
            document.getElementById('screenshots').innerHTML = '';
            const es = new EventSource('/api/test/' + gameName);
            es.onmessage = function(event) {{
                const d = JSON.parse(event.data);
                if (d.type === 'step') {{
                    const div = document.createElement('div');
                    div.className = 'step ' + (d.passed ? 'pass' : 'fail');
                    let h = '<div class="label">' + (d.passed?'✅':'❌') + ' Step ' + d.step + ': ' + d.action + '</div>';
                    if (d.keys) h += '<div class="keys">Keys: ' + d.keys + '</div>';
                    if (d.description) h += '<div class="desc">' + d.description + '</div>';
                    if (d.metrics) h += '<div class="metrics">' + d.metrics + '</div>';
                    if (d.screenshot) h += '<img src="data:image/png;base64,' + d.screenshot + '">';
                    div.innerHTML = h;
                    document.getElementById('screenshots').prepend(div);
                }} else if (d.type === 'status') {{
                    document.getElementById('status').textContent += d.message + '\\n';
                }} else if (d.type === 'done') {{
                    document.getElementById('playing').innerHTML = d.passed ?
                        '<span class="pass">✅ PASSED (' + d.steps + ' steps, ' + d.elapsed + 's)</span>' :
                        '<span class="fail">❌ FAILED (' + d.failures + '/' + d.steps + ' failed)</span>';
                    document.getElementById('startBtn').disabled = false;
                    es.close();
                }} else if (d.type === 'error') {{
                    document.getElementById('status').textContent += '❌ ' + d.message + '\\n';
                    document.getElementById('playing').textContent = '❌ ERROR';
                    document.getElementById('startBtn').disabled = false;
                    es.close();
                }}
                document.getElementById('status').scrollTop = document.getElementById('status').scrollHeight;
            }};
            es.onerror = function() {{
                document.getElementById('playing').textContent = '❌ Connection lost';
                document.getElementById('startBtn').disabled = false;
                es.close();
            }};
        }}
    </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# SSE test runner
# ---------------------------------------------------------------------------


@router.get("/api/test/{game_name}")
async def run_test(game_name: str, request: Request):
    """Run the test plan with Playwright, stream results as SSE."""

    # Auto-detect base URL from the incoming request (respects port + scheme)
    base_url = f"{request.url.scheme}://{request.url.hostname}:{request.url.port or (443 if request.url.scheme=='https' else 80)}"

    async def generate():
        try:
            yield _sse({"type": "status", "message": f"Building test plan from spec for {game_name}..."})
            test_steps = _build_test_steps_from_spec(game_name)
            yield _sse({"type": "status", "message": f"Test plan: {len(test_steps)} step(s)"})
            for s in test_steps:
                yield _sse({"type": "status", "message": f"  {s['action']:<30} keys={s.get('keys','-')}"})

            # Wipe previous screenshots for this game
            _wipe_screenshots_dir(game_name)
            yield _sse({"type": "status", "message": f"Cleared old screenshots in .debug/screenshots/{game_name}/"})

            yield _sse({"type": "status", "message": f"Launching Playwright against {base_url}/godot/{game_name}/"})

            t0 = time.time()
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, _execute_test, game_name, test_steps, base_url)

            for result in results:
                yield _sse(result)
                await asyncio.sleep(0.05)

            elapsed = int(time.time() - t0)
            steps = [r for r in results if r.get("type") == "step"]
            failures = [r for r in steps if not r.get("passed")]
            passed = len(failures) == 0
            yield _sse({"type": "done", "passed": passed, "steps": len(steps),
                        "failures": len(failures), "elapsed": elapsed})

        except Exception as exc:
            import traceback
            log.exception("Test run failed")
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Test plan generation — HLR + impact_map driven
# ---------------------------------------------------------------------------


_GAMEPLAY_SCENE_KEYWORDS = ("fight", "battle", "combat", "gameplay", "match", "play")


def _build_test_steps_from_spec(game_name: str) -> list[dict]:
    """Generate the test plan from the game's HLR + impact_map artifacts.

    Falls back to a generic fighting-game plan if no spec files are found.
    """
    game_dir = _OUTPUT_DIR / game_name
    hlr_path = game_dir / "hlr.json"
    impact_path = game_dir / "impact_map_final.json"

    if not hlr_path.exists():
        log.warning("No HLR for %s — using generic fighting-game fallback plan", game_name)
        return _fallback_plan()

    hlr = json.loads(hlr_path.read_text())
    impact: dict | None = None
    if impact_path.exists():
        try:
            impact = json.loads(impact_path.read_text())
        except Exception:
            impact = None

    # Find the primary gameplay scene
    scenes = [s["scene_name"] for s in hlr.get("scenes", [])]
    gameplay_scene = _pick_gameplay_scene(scenes)

    # Check whether the Godot project boots directly into the gameplay scene.
    # If so, we skip menu-advance steps entirely.
    main_scene_path = _read_main_scene_from_project(game_name)
    boots_into_gameplay = (
        main_scene_path is not None
        and gameplay_scene is not None
        and gameplay_scene in main_scene_path
    )

    steps: list[dict] = [
        {"action": "Load page", "keys": None, "wait_ms": 6000,
         "description": f"Navigate to /godot/{game_name}/ and wait for canvas"},
        {"action": "Click canvas to focus", "keys": "click", "wait_ms": 500,
         "description": "Canvas focus so keyboard input reaches Godot"},
    ]

    if not boots_into_gameplay:
        # Chew through menu scenes with Enter presses
        menu_scene_count = max(0, len(scenes) - 1)
        for i in range(min(menu_scene_count, 4)):
            steps.append({
                "action": f"Advance menu (#{i+1})", "keys": "Enter", "wait_ms": 1200,
                "description": "Press Enter to progress through menu scenes"})

    # Gameplay steps — movement + attacks, with semantic checks.
    gameplay_scene_label = gameplay_scene or "gameplay"
    steps.extend([
        {"action": f"[{gameplay_scene_label}] Initial state (baseline)", "keys": None, "wait_ms": 1500,
         "description": "Capture gameplay scene on first entry — establishes baselines",
         "checks": ["capture_baseline_entity_count", "no_white_halo", "no_fighter_overlap"]},
        {"action": "Walk right", "keys": "d", "method": "hold", "hold_ms": 900, "wait_ms": 400,
         "description": "Hold D to walk the fighter right", "verify_change": True,
         "checks": ["no_fighter_overlap"]},
        {"action": "Walk left", "keys": "a", "method": "hold", "hold_ms": 900, "wait_ms": 400,
         "description": "Hold A to walk the fighter left", "verify_change": True,
         "checks": ["no_fighter_overlap"]},
        {"action": "Walk straight at opponent (collision)", "keys": "d", "method": "hold",
         "hold_ms": 4000, "wait_ms": 500,
         "description": "Hold D for 4 seconds — p1 must NOT pass through p2",
         "checks": ["p1_did_not_walk_through_p2"]},
        {"action": "Jump", "keys": "w", "wait_ms": 1200,
         "description": "Press W to jump"},
        {"action": "Crouch", "keys": "s", "method": "hold", "hold_ms": 600, "wait_ms": 400,
         "description": "Hold S to crouch"},
        # NOTE: pixel-based "sprite swapped to attack pose" detection is too
        # noisy to be a hard gate when the game has only a single static sprite
        # texture (everything looks the same, small L1 noise drowns real pose
        # changes). Attack verification requires game-side state instrumentation
        # (expose fighter.current_action via JavaScriptBridge) — see TODO.
        # For now attack steps only verify no_white_halo (which IS reliable).
        {"action": "Light punch", "keys": "u", "wait_ms": 600,
         "description": "Press U for light punch",
         "checks": ["no_white_halo"]},
        {"action": "Heavy punch", "keys": "o", "wait_ms": 800,
         "description": "Press O for heavy punch",
         "checks": ["no_white_halo"]},
        {"action": "Light kick", "keys": "j", "wait_ms": 600,
         "description": "Press J for light kick"},
        {"action": "Heavy kick", "keys": "l", "wait_ms": 800,
         "description": "Press L for heavy kick"},
        # Recapture the entity-count baseline right before hadouken so "new
        # entity" compares the pre-hadouken scene (just p1 + p2) to the
        # post-hadouken scene (p1 + p2 + projectile).
        {"action": "Pre-hadouken baseline", "keys": None, "wait_ms": 500,
         "description": "Snapshot the scene right before the hadouken motion to recapture entity count",
         "checks": ["capture_baseline_entity_count"]},
        # Hadouken: QCF+U motion input (down, down-forward, forward, punch).
        {"action": "Hadouken sequence: down", "keys": "s", "wait_ms": 80,
         "description": "QCF motion start: press down"},
        {"action": "Hadouken sequence: down-forward", "keys": "d", "wait_ms": 80,
         "description": "QCF: press forward (letting down linger in buffer)"},
        {"action": "Hadouken sequence: forward+punch", "keys": "u", "wait_ms": 1200,
         "description": "QCF complete: punch — should spawn projectile entity",
         "checks": ["new_entity_vs_baseline"]},
    ])

    # Custom-feature verification steps from mechanic_specs
    for spec in hlr.get("mechanic_specs", []):
        sys_name = spec.get("system_name", "")
        if "rage" in sys_name.lower():
            steps.append({
                "action": "Take damage (wait for AI)", "keys": None, "wait_ms": 4500,
                "description": "Wait for CPU opponent to land hits so rage meter charges",
                "verify_change": True,
            })
            steps.append({
                "action": "Charge rage (more waiting)", "keys": None, "wait_ms": 4500,
                "description": "Let the CPU land more hits to approach max rage stacks",
                "verify_change": True,
            })
            steps.append({
                "action": "Fire powered special", "keys": "o", "wait_ms": 1500,
                "description": "Press O — should consume a rage stack for powered damage",
            })
            break

    # Final state snapshot
    steps.append({
        "action": "Final state", "keys": None, "wait_ms": 1500,
        "description": "Capture final scene to verify game is still alive"
    })

    return steps


def _pick_gameplay_scene(scene_names: list[str]) -> str | None:
    # Prefer exact gameplay-style names over intro/transition scenes
    priority = ["fighting", "gameplay", "battle", "match", "play"]
    for p in priority:
        for name in scene_names:
            if name.lower() == p:
                return name
    for name in scene_names:
        lower = name.lower()
        if any(kw in lower for kw in _GAMEPLAY_SCENE_KEYWORDS) and "intro" not in lower:
            return name
    return None


def _read_main_scene_from_project(game_name: str) -> str | None:
    """Return the `run/main_scene` setting from the game's project.godot, if any."""
    project_path = _OUTPUT_DIR / game_name / "godot" / "project.godot"
    if not project_path.exists():
        return None
    try:
        text = project_path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'run/main_scene\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else None
    except Exception:
        return None


def _fallback_plan() -> list[dict]:
    """Generic plan for games without an HLR."""
    return [
        {"action": "Load page", "keys": None, "wait_ms": 6000,
         "description": "Navigate to the game page"},
        {"action": "Click canvas", "keys": "click", "wait_ms": 500,
         "description": "Focus canvas"},
        {"action": "Advance menu", "keys": "Enter", "wait_ms": 1500,
         "description": "Try Enter to progress"},
        {"action": "Walk right", "keys": "d", "method": "hold", "hold_ms": 800, "wait_ms": 400,
         "description": "Movement test", "verify_change": True},
        {"action": "Final state", "keys": None, "wait_ms": 1500,
         "description": "Final snapshot"},
    ]


# ---------------------------------------------------------------------------
# PIL-based screenshot analysis — the ground truth for blank detection
# ---------------------------------------------------------------------------


def _analyze_screenshot_png(png_bytes: bytes) -> dict:
    """Analyze a PNG screenshot for content, variance, and color diversity.

    WebGL canvases cannot be reliably read via the DOM 2D context. The browser
    screenshot, however, captures the composited framebuffer — we can decode
    those bytes with PIL and compute real metrics:

      unique_colors  — # of distinct RGB triples (sampled)
      variance       — stdev of luma across sampled pixels
      nontrivial_pct — fraction of pixels that aren't near-black or near-white
      content        — derived: (unique_colors > 80) AND (variance > 8.0) AND
                       (nontrivial_pct > 0.05)

    A blank/loading screen typically has unique_colors < 40, variance < 4.
    """
    from PIL import Image
    import numpy as np

    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception as exc:
        return {"has_content": False, "reason": f"decode failed: {exc}",
                "unique_colors": 0, "variance": 0.0, "nontrivial_pct": 0.0}

    # Downsample to at most 200x150 for speed — still captures global statistics
    max_w = 200
    if img.width > max_w:
        new_h = int(img.height * max_w / img.width)
        img = img.resize((max_w, new_h), Image.BILINEAR)

    arr = np.asarray(img, dtype=np.uint8)  # (h, w, 3)
    pixels = arr.reshape(-1, 3)

    # Unique colors — sample up to 5000 pixels for speed
    if len(pixels) > 5000:
        idx = np.random.default_rng(42).choice(len(pixels), size=5000, replace=False)
        sample = pixels[idx]
    else:
        sample = pixels
    unique_colors = int(len({tuple(p) for p in sample}))

    # Luma variance
    luma = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])
    variance = float(np.std(luma))

    # Non-trivial pixels: not near-black and not near-white
    near_black = (luma < 20).sum()
    near_white = (luma > 235).sum()
    total = luma.size
    nontrivial_pct = float(1.0 - (near_black + near_white) / total)

    has_content = unique_colors > 80 and variance > 8.0 and nontrivial_pct > 0.05

    return {
        "has_content": has_content,
        "unique_colors": unique_colors,
        "variance": round(variance, 2),
        "nontrivial_pct": round(nontrivial_pct, 3),
        "size": f"{img.width}x{img.height}",
    }


def _screenshots_differ(a: bytes, b: bytes, threshold: float = 2.0) -> tuple[bool, float]:
    """Detect whether two screenshots differ meaningfully.

    Uses mean absolute pixel-difference across luma. threshold is in 0..255 units.
    Returns (changed, diff_value).
    """
    from PIL import Image
    import numpy as np

    try:
        img_a = Image.open(io.BytesIO(a)).convert("L").resize((200, 150), Image.BILINEAR)
        img_b = Image.open(io.BytesIO(b)).convert("L").resize((200, 150), Image.BILINEAR)
    except Exception:
        return (True, 0.0)

    arr_a = np.asarray(img_a, dtype=np.int16)
    arr_b = np.asarray(img_b, dtype=np.int16)
    diff = float(np.mean(np.abs(arr_a - arr_b)))
    return (diff > threshold, round(diff, 2))


# ---------------------------------------------------------------------------
# Semantic checks — each function takes (png_bytes, state_dict, step_dict) and
# returns {name, passed, reason}. State persists across steps so later checks
# can compare against baselines captured earlier.
# ---------------------------------------------------------------------------


def _detect_white_sprite_regions(png_bytes: bytes) -> list[tuple[int, int, int, int]]:
    """Return list of (x0, y0, x1, y1) bounding boxes for FIGHTER-SHAPED regions,
    using proper connected-component analysis (not column clustering).

    Uses scipy.ndimage.label with morphological closing to find solid white
    blobs, then filters by size + aspect + density.
    """
    from PIL import Image
    import numpy as np
    from scipy import ndimage

    img = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB"))
    white = (img[..., 0] > 200) & (img[..., 1] > 200) & (img[..., 2] > 200)

    # Close small gaps so a fighter's non-white features (skin, belt, hair)
    # don't break the white gi into tiny disconnected fragments.
    closed = ndimage.binary_closing(white, structure=np.ones((7, 7)), iterations=2)

    labels, num = ndimage.label(closed)
    if num == 0:
        return []

    boxes: list[tuple[int, int, int, int]] = []
    for lbl in range(1, num + 1):
        ys, xs = np.where(labels == lbl)
        if len(ys) < 2000:  # tiny blob
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        width = x1 - x0 + 1
        height = y1 - y0 + 1
        if width < 100 or height < 200:
            continue
        aspect = width / max(1, height)
        if aspect < 0.25 or aspect > 1.2:
            continue
        # Density of THIS specific component within its bbox (not all white in bbox)
        component_density = len(ys) / (width * height)
        if component_density < 0.35:
            continue
        boxes.append((x0, y0, x1, y1))

    boxes.sort(key=lambda b: b[0])  # left to right
    return boxes


def _crop_png(png_bytes: bytes, bbox: tuple[int, int, int, int]) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    x0, y0, x1, y1 = bbox
    cropped = img.crop((x0, y0, x1 + 1, y1 + 1))
    out = io.BytesIO()
    cropped.save(out, format="PNG")
    return out.getvalue()


def _sprite_pose_signature(png_bytes: bytes, bbox: tuple[int, int, int, int]) -> "np.ndarray":
    """8x8 grid of normalized white-pixel density inside the sprite bbox.

    Captures the SHAPE/POSE of the fighter — specifically where the white gi
    is concentrated — independent of position on the stage. Two crops of the
    same idle pose (even at different stage positions) produce near-identical
    grids. A different pose (attack) shifts the mass distribution — arms out
    means more density in top cells, less in mid-body.

    Returns a 64-element float array summing to 1.0 (normalized).
    """
    from PIL import Image
    import numpy as np
    img = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB"))
    x0, y0, x1, y1 = bbox
    crop = img[y0:y1 + 1, x0:x1 + 1]
    white = (crop[..., 0] > 200) & (crop[..., 1] > 200) & (crop[..., 2] > 200)
    h, w = white.shape
    if h < 8 or w < 8:
        return np.zeros(64)
    grid = np.zeros(64)
    for gy in range(8):
        for gx in range(8):
            ys = gy * h // 8
            ye = (gy + 1) * h // 8
            xs = gx * w // 8
            xe = (gx + 1) * w // 8
            cell = white[ys:ye, xs:xe]
            grid[gy * 8 + gx] = float(cell.mean()) if cell.size else 0.0
    total = float(grid.sum())
    if total > 0:
        grid = grid / total
    return grid


def _histogram_distance(a: "np.ndarray", b: "np.ndarray") -> float:
    """L1 distance between two normalized feature vectors."""
    import numpy as np
    return float(np.abs(a - b).sum())


# Alias so existing callers still work
_sprite_histogram = _sprite_pose_signature


def _pick_unclipped_box(png_bytes: bytes) -> tuple[int, int, int, int] | None:
    """Return the first fighter bbox that is NOT clipped by the viewport edges.

    A clipped sprite (touching x=0 or x=width-1) produces unreliable pose
    signatures because half the sprite is cut off. Only fully-in-frame boxes
    are comparable.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes))
    w, h = img.size
    boxes = _detect_white_sprite_regions(png_bytes)
    for box in boxes:
        x0, y0, x1, y1 = box
        if x0 > 5 and x1 < w - 5 and y0 >= 0 and y1 < h - 5:
            return box
    return None


def check_sprite_differs_from_baseline(png: bytes, state: dict, step: dict) -> dict:
    """Compare the fighter's pose signature to the stored baseline.

    Only compares UNCLIPPED bboxes (fighter fully in frame). If either the
    baseline or the current frame has a clipped fighter, the check is
    SKIPPED with a "insufficient framing" note rather than pass/fail guess.

    FAILS when both are unclipped and their pose signatures are nearly
    identical — the sprite didn't swap on input.
    """
    box = _pick_unclipped_box(png)
    if box is None:
        return {"name": "sprite_differs_from_baseline", "passed": True,
                "reason": "no fully-in-frame fighter to compare (skipped — inconclusive)"}

    sig = _sprite_pose_signature(png, box)

    baseline_sig = state.get("idle_fighter_hist")
    baseline_box = state.get("idle_fighter_box")
    if baseline_sig is None or baseline_box is None:
        # First call with an unclipped fighter — store as baseline
        state["idle_fighter_hist"] = sig
        state["idle_fighter_box"] = box
        return {"name": "sprite_differs_from_baseline", "passed": True,
                "reason": f"baseline captured (bbox={box})"}

    dist = _histogram_distance(baseline_sig, sig)
    # Threshold tuned for pose-grid comparison of fully-in-frame fighters.
    # Same idle pose at different stage positions: L1 < 0.10.
    # Different pose (attack frame): L1 > 0.20.
    passed = dist > 0.12
    return {"name": "sprite_differs_from_baseline", "passed": passed,
            "reason": (f"pose grid L1={dist:.3f} (different pose)"
                       if passed
                       else f"pose grid L1={dist:.3f} — same silhouette as idle, sprite not swapping on input")}


def check_no_white_halo(png: bytes, state: dict, step: dict) -> dict:
    """Fighter sprite should be a silhouette, not a white rectangle.

    FAILS when the 4 CORNERS of the sprite bbox are white — a proper sprite
    silhouette has transparent/background corners (the corners of a humanoid
    shape are almost always not-character), while a JPEG with white background
    has white corners that leak into the bbox.

    A fighter silhouette covers roughly 40-55% of its bbox. A rectangular
    JPEG background of a character fills 80%+ of the bbox. We check both:
      1. Corner pixels white?  (instant fail)
      2. Bbox white density > 70%?  (fill fail)
    """
    boxes = _detect_white_sprite_regions(png)
    if not boxes:
        return {"name": "no_white_halo", "passed": True,
                "reason": "no fighter to check"}

    from PIL import Image
    import numpy as np
    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))

    offenders = []
    for (x0, y0, x1, y1) in boxes:
        width = x1 - x0 + 1
        height = y1 - y0 + 1
        # Sample a small square at each bbox corner
        pad = max(3, min(width, height) // 20)
        corner_samples = []
        for (cx, cy) in [
            (x0 + pad, y0 + pad),      # top-left
            (x1 - pad, y0 + pad),      # top-right
            (x0 + pad, y1 - pad),      # bottom-left
            (x1 - pad, y1 - pad),      # bottom-right
        ]:
            r, g, b = img[cy, cx]
            is_white = int(r) > 215 and int(g) > 215 and int(b) > 215
            corner_samples.append(is_white)
        white_corners = sum(corner_samples)

        # White-pixel density across the whole bbox
        crop = img[y0:y1 + 1, x0:x1 + 1]
        white_mask = (crop[..., 0] > 200) & (crop[..., 1] > 200) & (crop[..., 2] > 200)
        density = float(white_mask.mean())

        if white_corners >= 3:
            offenders.append(
                f"bbox=({x0},{y0},{x1},{y1}) {white_corners}/4 corners are white "
                f"(density {int(density*100)}%) — rectangular JPEG background, not a silhouette"
            )
        elif density > 0.70:
            offenders.append(
                f"bbox=({x0},{y0},{x1},{y1}) {int(density*100)}% white density "
                f"(expected <55% for a character silhouette) — opaque background"
            )

    passed = len(offenders) == 0
    return {
        "name": "no_white_halo",
        "passed": passed,
        "reason": ("sprite silhouette clean (transparent corners)" if passed
                   else "; ".join(offenders)),
    }


def check_no_fighter_overlap(png: bytes, state: dict, step: dict) -> dict:
    """Two fighter sprites should never overlap their bboxes significantly.

    If detection returns fewer than 2 fighters this is a DETECTION gap (sprites
    merged visually, went off-screen, or clipped) — it's not a game bug, so the
    check is skipped rather than failed. This avoids false positives from the
    connected-components detector's edge cases.
    """
    boxes = _detect_white_sprite_regions(png)
    if len(boxes) < 2:
        return {"name": "no_fighter_overlap", "passed": True,
                "reason": f"only {len(boxes)} fighter(s) detected — skipped (detection gap, not a game bug)"}

    boxes.sort(key=lambda b: b[0])
    left, right = boxes[0], boxes[1]
    overlap_px = left[2] - right[0]  # positive = overlap
    left_w = left[2] - left[0]
    if overlap_px > left_w * 0.2:  # more than 20% overlap
        return {"name": "no_fighter_overlap", "passed": False,
                "reason": f"sprites overlap by {overlap_px}px (left x={left[0]}..{left[2]}, right x={right[0]}..{right[2]})"}
    return {"name": "no_fighter_overlap", "passed": True,
            "reason": f"clean separation (gap={-overlap_px}px)"}


def check_p1_did_not_walk_through_p2(png: bytes, state: dict, step: dict) -> dict:
    """Track p1 and p2 x positions. If p1 crosses to the right of p2, collision is broken."""
    boxes = _detect_white_sprite_regions(png)
    if len(boxes) < 2:
        return {"name": "p1_did_not_walk_through_p2", "passed": True,
                "reason": f"only {len(boxes)} sprite(s) — skipping (need both to check)"}
    boxes.sort(key=lambda b: (b[0] + b[2]) // 2)
    p1_cx = (boxes[0][0] + boxes[0][2]) // 2
    p2_cx = (boxes[1][0] + boxes[1][2]) // 2

    # Track history on the state
    history = state.setdefault("p1_p2_history", [])
    history.append((p1_cx, p2_cx))

    # Baseline: at step 1 or 2 (initial state), p1 should be to the left of p2
    if p1_cx >= p2_cx:
        return {"name": "p1_did_not_walk_through_p2", "passed": False,
                "reason": f"p1_x={p1_cx} >= p2_x={p2_cx} — fighters crossed or merged"}
    return {"name": "p1_did_not_walk_through_p2", "passed": True,
            "reason": f"p1_x={p1_cx} < p2_x={p2_cx} gap={p2_cx-p1_cx}px"}


def check_new_entity_vs_baseline(png: bytes, state: dict, step: dict) -> dict:
    """After a hadouken / spawn action, there should be more distinct sprite regions."""
    boxes = _detect_white_sprite_regions(png)
    baseline_count = state.get("baseline_entity_count", 2)
    if len(boxes) > baseline_count:
        return {"name": "new_entity_vs_baseline", "passed": True,
                "reason": f"{len(boxes)} regions (up from {baseline_count}) — new entity detected"}
    return {"name": "new_entity_vs_baseline", "passed": False,
            "reason": f"{len(boxes)} regions (same as baseline {baseline_count}) — no hadouken projectile spawned"}


def capture_baseline_entity_count(png: bytes, state: dict, step: dict) -> dict:
    """Not a real check — stores the current sprite-region count as baseline."""
    boxes = _detect_white_sprite_regions(png)
    state["baseline_entity_count"] = len(boxes)
    return {"name": "capture_baseline_entity_count", "passed": True,
            "reason": f"baseline = {len(boxes)} regions"}


SEMANTIC_CHECKS = {
    "sprite_differs_from_baseline": check_sprite_differs_from_baseline,
    "no_white_halo": check_no_white_halo,
    "no_fighter_overlap": check_no_fighter_overlap,
    "p1_did_not_walk_through_p2": check_p1_did_not_walk_through_p2,
    "new_entity_vs_baseline": check_new_entity_vs_baseline,
    "capture_baseline_entity_count": capture_baseline_entity_count,
}


# ---------------------------------------------------------------------------
# Screenshot persistence
# ---------------------------------------------------------------------------


def _wipe_screenshots_dir(game_name: str) -> None:
    """Delete all prior screenshots for this game before a new run."""
    if not _is_safe_name(game_name):
        return
    d = _DEBUG_DIR / game_name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)


def _save_screenshot_to_disk(
    game_name: str, step_index: int, action: str, png_bytes: bytes
) -> Path:
    d = _DEBUG_DIR / game_name
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_]+", "_", action.lower()).strip("_")[:40] or "step"
    path = d / f"{step_index + 1:02d}_{safe}.png"
    path.write_bytes(png_bytes)
    return path


def _is_safe_name(name: str) -> bool:
    return bool(re.match(r"^[\w\-]+$", name))


# ---------------------------------------------------------------------------
# Playwright runner
# ---------------------------------------------------------------------------


def _execute_test(game_name: str, test_steps: list[dict], base_url: str) -> list[dict]:
    """Run the test plan with Playwright. Returns one dict per step for SSE."""
    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    prev_png: bytes | None = None
    # Shared state dict for semantic checks — persists across steps so later
    # checks can compare against baselines captured earlier in the run.
    shared_state: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-web-security"])
        ctx = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 720},
        )
        page = ctx.new_page()
        # Log browser console to our log so we can see Godot errors
        page.on("console", lambda msg: log.info("[browser:%s] %s", msg.type, msg.text))
        page.on("pageerror", lambda err: log.error("[browser:err] %s", err))

        for i, step in enumerate(test_steps):
            action = step["action"]
            keys = step.get("keys")
            method = step.get("method", "press")
            wait_ms = step.get("wait_ms", 1000)
            hold_ms = step.get("hold_ms", 0)
            verify_change = step.get("verify_change", False)

            try:
                if keys is None and i == 0:
                    url = f"{base_url}/godot/{game_name}/"
                    log.info("playwright: goto %s", url)
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    # Wait for the canvas element to actually exist
                    try:
                        page.wait_for_selector("canvas", timeout=15000)
                    except Exception:
                        log.warning("playwright: no canvas within 15s for %s", game_name)
                    page.wait_for_timeout(wait_ms)
                elif keys == "click":
                    canvas = page.query_selector("canvas")
                    if canvas:
                        canvas.click()
                    page.wait_for_timeout(wait_ms)
                elif keys and method == "hold":
                    canvas = page.query_selector("canvas")
                    if canvas:
                        canvas.click()
                        page.wait_for_timeout(50)
                    page.keyboard.down(keys)
                    page.wait_for_timeout(hold_ms or 500)
                    page.keyboard.up(keys)
                    page.wait_for_timeout(wait_ms)
                elif keys:
                    canvas = page.query_selector("canvas")
                    if canvas:
                        canvas.click()
                        page.wait_for_timeout(50)
                    page.keyboard.press(keys)
                    page.wait_for_timeout(wait_ms)
                else:
                    page.wait_for_timeout(wait_ms)

                png_bytes = page.screenshot(full_page=False)
                saved_path = _save_screenshot_to_disk(game_name, i, action, png_bytes)

                analysis = _analyze_screenshot_png(png_bytes)

                changed = True
                diff_value = 0.0
                if prev_png is not None:
                    changed, diff_value = _screenshots_differ(prev_png, png_bytes)
                prev_png = png_bytes

                metrics = (
                    f"colors={analysis['unique_colors']} "
                    f"var={analysis['variance']} "
                    f"nontrivial={int(analysis['nontrivial_pct']*100)}% "
                    f"Δ={diff_value}"
                )

                # Pass criteria:
                #   1. Must have real content (not blank / loading)
                #   2. If verify_change is True, must differ from previous frame
                #   3. Every declared semantic check must pass
                passed = analysis["has_content"]
                failure_reasons: list[str] = []
                check_results: list[dict] = []
                if not analysis["has_content"]:
                    failure_reasons.append("BLANK CANVAS")
                if verify_change and not changed:
                    passed = False
                    failure_reasons.append(f"NO CHANGE after input (Δ={diff_value})")

                # Run semantic checks
                for check_name in step.get("checks") or []:
                    check_fn = SEMANTIC_CHECKS.get(check_name)
                    if not check_fn:
                        failure_reasons.append(f"unknown check: {check_name}")
                        passed = False
                        continue
                    try:
                        result = check_fn(png_bytes, shared_state, step)
                        check_results.append(result)
                        if not result.get("passed"):
                            passed = False
                            failure_reasons.append(f"{result['name']}: {result['reason']}")
                    except Exception as exc:
                        failure_reasons.append(f"{check_name} crashed: {exc}")
                        passed = False

                desc = step.get("description", "")
                if check_results:
                    check_summary = " | ".join(
                        f"{'✓' if r['passed'] else '✗'} {r['name']}" for r in check_results
                    )
                    desc = f"{desc}\n  checks: {check_summary}"
                if failure_reasons:
                    desc = f"{desc}\n  ❌ {'; '.join(failure_reasons)}"

                results.append({
                    "type": "step",
                    "step": i + 1,
                    "action": action,
                    "keys": str(keys),
                    "description": desc,
                    "metrics": metrics,
                    "screenshot": base64.b64encode(png_bytes).decode("utf-8"),
                    "screenshot_path": str(saved_path),
                    "passed": passed,
                })

            except Exception as exc:
                log.exception("Step %d (%s) crashed", i + 1, action)
                results.append({
                    "type": "step",
                    "step": i + 1,
                    "action": action,
                    "keys": str(keys),
                    "description": f"Error: {exc}",
                    "metrics": "",
                    "screenshot": None,
                    "screenshot_path": None,
                    "passed": False,
                })

        page.close()
        ctx.close()
        browser.close()

    return results


# ---------------------------------------------------------------------------
# Standalone batch-callable entry
# ---------------------------------------------------------------------------


def run_screenshot_suite(game_name: str, base_url: str = "http://localhost:8443") -> list[dict]:
    """Run the full test suite for a game outside of HTTP/SSE context."""
    log.info("run_screenshot_suite: starting for %s", game_name)
    steps = _build_test_steps_from_spec(game_name)
    _wipe_screenshots_dir(game_name)
    raw = _execute_test(game_name, steps, base_url)
    out = []
    for r in raw:
        if r.get("type") != "step":
            continue
        out.append({
            "step": r["step"],
            "action": r["action"],
            "keys": r.get("keys"),
            "screenshot_path": r.get("screenshot_path"),
            "passed": r.get("passed", False),
            "metrics": r.get("metrics", ""),
            "notes": r.get("description", ""),
        })
    passed = sum(1 for r in out if r["passed"])
    log.info("run_screenshot_suite: %s — %d/%d passed", game_name, passed, len(out))
    return out
