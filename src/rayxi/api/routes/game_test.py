"""Game Test API — auto-generate Playwright tests from scene-based game data.

Endpoints:
  GET /test/{game_name}     — test page with Start button
  GET /api/test/{game_name} — SSE stream of test steps with screenshots

Standalone:
  run_screenshot_suite(game_name) — batch-callable, returns list of step results
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, StreamingResponse

router = APIRouter()
log = logging.getLogger("rayxi.api.game_test")

_GAMES_DIR = Path(__file__).resolve().parents[4] / "games"
_DEBUG_DIR = Path(__file__).resolve().parents[4] / ".debug" / "screenshots"
_BASE_URL = "https://localhost:8000"


@router.get("/test/{game_name}", response_class=HTMLResponse)
async def test_page(game_name: str):
    """Render the test control page."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Test: {game_name}</title>
    <style>
        body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
        h1 {{ color: #00ff88; }}
        #status {{ white-space: pre-wrap; background: #0d0d1a; padding: 15px; border-radius: 8px;
                   max-height: 300px; overflow-y: auto; margin: 10px 0; font-size: 13px; }}
        #screenshots {{ display: flex; flex-wrap: wrap; gap: 10px; }}
        .step {{ background: #16213e; border-radius: 8px; padding: 10px; max-width: 420px; }}
        .step img {{ max-width: 400px; border-radius: 4px; }}
        .step .label {{ color: #00ff88; font-weight: bold; margin-bottom: 5px; }}
        .step .desc {{ color: #aaa; font-size: 12px; margin-top: 4px; }}
        .step .keys {{ color: #ffcc00; font-size: 11px; }}
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
    <p>Play: <a href="/godot/{game_name}/" target="_blank">/godot/{game_name}/</a></p>
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
                    div.className = 'step';
                    let h = '<div class="label">' + (d.passed?'✅':'❌') + ' Step ' + d.step + ': ' + d.action + '</div>';
                    if (d.keys) h += '<div class="keys">Keys: ' + d.keys + '</div>';
                    if (d.description) h += '<div class="desc">' + d.description + '</div>';
                    if (d.scene_objects) {{
                        h += '<div class="desc" style="color:#88aaff">Scene objects:</div>';
                        for (const [layer, objs] of Object.entries(d.scene_objects)) {{
                            h += '<div class="desc" style="color:#aaa">&nbsp;&nbsp;' + layer + ': ';
                            h += objs.map(o => o.name).join(', ');
                            h += '</div>';
                        }}
                    }}
                    if (d.screenshot) h += '<img src="data:image/png;base64,' + d.screenshot + '">';
                    div.innerHTML = h;
                    document.getElementById('screenshots').prepend(div);
                }} else if (d.type === 'status') {{
                    document.getElementById('status').textContent += d.message + '\\n';
                }} else if (d.type === 'done') {{
                    document.getElementById('playing').innerHTML = d.passed ?
                        '<span class="pass">✅ PASSED (' + d.steps + ' steps)</span>' :
                        '<span class="fail">❌ FAILED</span>';
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


@router.get("/api/test/{game_name}")
async def run_test(game_name: str):
    """Run auto-generated Playwright test and stream results as SSE."""

    async def generate():
        try:
            yield _sse({"type": "status", "message": f"Generating test plan for {game_name}..."})

            # Build test steps from scene-based game data
            test_steps = _build_test_steps(game_name)
            yield _sse({"type": "status", "message": f"Test plan: {len(test_steps)} steps"})
            for step in test_steps:
                yield _sse({"type": "status", "message": f"  {step['action']}: {step.get('keys', 'N/A')}"})

            yield _sse({"type": "status", "message": "Launching Playwright..."})

            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, _execute_test, game_name, test_steps)

            for result in results:
                yield _sse(result)
                await asyncio.sleep(0.1)

            passed = all(r.get("passed", True) for r in results if r.get("type") == "step")
            yield _sse({"type": "done", "passed": passed, "steps": len([r for r in results if r.get("type") == "step"])})

        except Exception as exc:
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Test plan generation — scene-based architecture
# ---------------------------------------------------------------------------
#
# New format:
#   game.gd        — thin scene switcher (SCENE_* constants, _load_scene)
#   scripts/*_scene.gd — per-scene orchestrator with local FSM states
#
# This parser reads game.gd to discover scenes and their order, then reads
# each scene orchestrator to discover FSM states, input actions, and
# transitions between scenes.
# ---------------------------------------------------------------------------


def _build_test_steps(game_name: str) -> list[dict]:
    """Build test steps from scene-based game architecture.

    Reads game.gd for scene declarations and order, then reads each
    scene orchestrator to find input actions and scene transitions.
    """
    game_dir = _GAMES_DIR / game_name
    steps: list[dict] = []

    # ── Step 0: Load ──
    steps.append({
        "action": "Load Game",
        "keys": None,
        "wait_ms": 6000,
        "description": "Load page and wait for Godot WASM",
    })

    # ── Step 1: Focus canvas ──
    steps.append({
        "action": "Focus Canvas",
        "keys": "click",
        "wait_ms": 1000,
        "description": "Click canvas to capture keyboard input",
    })

    # ── Parse game.gd for scene graph ──
    game_gd = game_dir / "scripts" / "game.gd"
    if not game_gd.exists():
        steps.extend(_fallback_steps())
        return steps

    game_source = game_gd.read_text(encoding="utf-8")
    scene_names = _parse_scene_names_from_game(game_source)
    initial_scene = _parse_initial_scene(game_source)

    if not scene_names:
        steps.extend(_fallback_steps())
        return steps

    # ── Parse each scene orchestrator ──
    scene_data: dict[str, dict[str, Any]] = {}
    for scene_name in scene_names:
        orchestrator_path = game_dir / "scripts" / f"{scene_name}_scene.gd"
        if not orchestrator_path.exists():
            continue
        source = orchestrator_path.read_text(encoding="utf-8")
        scene_data[scene_name] = {
            "states": _parse_scene_states(source),
            "initial_state": _parse_scene_initial_state(source),
            "input_actions": _parse_scene_input_actions(source),
            "transitions_out": _parse_scene_transitions_out(source),
        }

    # ── Build scene flow using BFS across scenes ──
    scene_flow = _build_scene_flow(initial_scene, scene_data, scene_names)

    # ── Generate steps for each scene in the flow ──
    for scene_name in scene_flow:
        data = scene_data.get(scene_name)
        if not data:
            continue

        # Identify gameplay-type states (where the player actively plays)
        gameplay_keywords = ("playing", "fighting", "racing", "running", "battle", "duel", "serve")
        is_gameplay_scene = any(
            any(kw in s.lower() for kw in gameplay_keywords)
            for s in data["states"]
        )

        if is_gameplay_scene:
            # Gameplay scene — exercise gameplay inputs
            _add_gameplay_steps(steps, scene_name, data)
        else:
            # Menu/transition scene — find the key to advance to next scene
            _add_navigation_steps(steps, scene_name, data)

    # ── Layer/object verification ──
    scene_objects = _parse_scene_objects(game_dir)
    if scene_objects:
        steps.append({
            "action": "Verify Scene Objects",
            "keys": None,
            "wait_ms": 1000,
            "description": "Checking visible objects per layer",
            "verify_objects": scene_objects,
        })

    # ── Final screenshot ──
    steps.append({
        "action": "Final State",
        "keys": None,
        "wait_ms": 2000,
        "description": "Final game state after all inputs",
    })

    return steps


# ---------------------------------------------------------------------------
# game.gd parsers — scene declarations and initial scene
# ---------------------------------------------------------------------------


def _parse_scene_names_from_game(source: str) -> list[str]:
    """Extract scene names from SCENE_* constants in game.gd.

    Reads constants like:
        const SCENE_TITLE_SCREEN: String = "res://scenes/title_screen.tscn"
    And also reads the _load_scene match branches like:
        if scene_name == "title_screen":

    Returns scene names in declaration order, e.g. ["title_screen", "gameplay", "game_over"].
    """
    # Primary: extract from SCENE_* constants
    scene_names: list[str] = []
    for m in re.finditer(
        r'const SCENE_\w+:\s*String\s*=\s*"res://scenes/(\w+)\.tscn"', source
    ):
        name = m.group(1)
        if name not in scene_names:
            scene_names.append(name)

    # Fallback: extract from _load_scene branch conditions
    if not scene_names:
        for m in re.finditer(r'if scene_name == "(\w+)":', source):
            name = m.group(1)
            if name not in scene_names:
                scene_names.append(name)
        for m in re.finditer(r'elif scene_name == "(\w+)":', source):
            name = m.group(1)
            if name not in scene_names:
                scene_names.append(name)

    return scene_names


def _parse_initial_scene(source: str) -> str:
    """Find the initial scene loaded in _ready().

    Looks for:  _load_scene("title_screen")
    Falls back to current_scene_name default.
    """
    m = re.search(r'_load_scene\("(\w+)"\)', source)
    if m:
        return m.group(1)
    m = re.search(r'var current_scene_name:\s*String\s*=\s*"(\w+)"', source)
    if m:
        return m.group(1)
    return "title_screen"


# ---------------------------------------------------------------------------
# Scene orchestrator parsers — states, inputs, transitions
# ---------------------------------------------------------------------------


def _parse_scene_states(source: str) -> list[str]:
    """Extract FSM state constants from a scene orchestrator.

    Matches: const PLAYING: String = "PLAYING"
    (No S_ prefix in the new architecture.)
    """
    return re.findall(r'const (\w+):\s*String\s*=\s*"\1"', source)


def _parse_scene_initial_state(source: str) -> str | None:
    """Find the initial scene_state value.

    Matches: var scene_state: String = SERVE
    """
    m = re.search(r'var scene_state:\s*String\s*=\s*(\w+)', source)
    return m.group(1) if m else None


def _parse_scene_input_actions(source: str) -> list[dict]:
    """Extract input actions from _handle_input() in a scene orchestrator.

    Parses Input.is_action_just_pressed / is_action_pressed calls and
    associates them with the interaction function they call.

    The typical pattern is two lines:
        if Input.is_action_just_pressed("ui_accept"):
            _interaction_player_confirm_start_prompt()
    """
    actions: list[dict] = []
    in_handle_input = False
    lines = source.splitlines()

    for idx, line in enumerate(lines):
        stripped = line.strip()

        # Detect _handle_input function boundary
        if "func _handle_input()" in line:
            in_handle_input = True
            continue
        if in_handle_input and line.startswith("func "):
            break  # Left _handle_input
        if not in_handle_input:
            continue

        # Parse input check: if Input.is_action_just_pressed("ui_accept"):
        m = re.search(r'Input\.is_action_(just_)?pressed\("(\w+)"\)', stripped)
        if not m:
            continue

        is_held = not m.group(1)  # is_action_pressed = hold, just_pressed = tap
        action_name = m.group(2)
        key = _action_to_key(action_name)
        method = "hold" if is_held else "press"

        # Find the interaction function called — same line or next line
        interaction_name = ""
        interaction_m = re.search(r'_interaction_(\w+)\(\)', stripped)
        if not interaction_m and idx + 1 < len(lines):
            # Check the next indented line
            interaction_m = re.search(r'_interaction_(\w+)\(\)', lines[idx + 1].strip())
        if interaction_m:
            interaction_name = interaction_m.group(1)
        else:
            interaction_name = action_name

        # Try to determine what this interaction does from the stub comments
        effect = _find_interaction_effect(source, interaction_name)

        actions.append({
            "action_name": action_name,
            "interaction": interaction_name,
            "key": key,
            "method": method,
            "effect": effect,
            "is_transition": _is_transition_interaction(source, interaction_name),
        })

    return actions


def _find_interaction_effect(source: str, interaction_name: str) -> str:
    """Read the comment block in an interaction stub to find its effect description."""
    pattern = rf'func _interaction_{re.escape(interaction_name)}\(\).*?(?=\nfunc |\Z)'
    m = re.search(pattern, source, re.DOTALL)
    if not m:
        return ""
    block = m.group(0)
    # Look for Effect lines
    effects = re.findall(r'## Effect:\s*(.+)', block)
    if effects:
        return "; ".join(effects[:3])
    # Look for the first comment line
    first_comment = re.search(r'##\s*(.+)', block)
    return first_comment.group(1) if first_comment else ""


def _is_transition_interaction(source: str, interaction_name: str) -> bool:
    """Check if an interaction function calls _transition_to or scene_transition.emit."""
    pattern = rf'func _interaction_{re.escape(interaction_name)}\(\).*?(?=\nfunc |\Z)'
    m = re.search(pattern, source, re.DOTALL)
    if not m:
        return False
    block = m.group(0)
    return bool(
        re.search(r"transition_to\(", block)
        or re.search(r"scene_transition\.emit\(", block)
        or re.search(r"transition_to_scene\(", block)
        or re.search(r"## Effect:.*transition", block, re.IGNORECASE)
        or re.search(r"## Effect:.*change_scene", block, re.IGNORECASE)
    )


def _parse_scene_transitions_out(source: str) -> list[str]:
    """Find all target scenes this scene can transition to.

    Looks for patterns like:
        _transition_to("gameplay")
        scene_transition.emit("game_over")
    And in interaction comments:
        ## Effect: transition_to('gameplay')
        ## Effect: change_scene('gameplay')
    """
    targets: list[str] = []

    # Direct calls
    for m in re.finditer(r'_transition_to\(["\'](\w+)["\']\)', source):
        t = m.group(1)
        if t not in targets:
            targets.append(t)

    # Signal emission
    for m in re.finditer(r'scene_transition\.emit\(["\'](\w+)["\']\)', source):
        t = m.group(1)
        if t not in targets:
            targets.append(t)

    # Comment-described transitions
    for m in re.finditer(r"## Effect:.*(?:transition_to|change_scene)\(['\"](\w+)['\"]\)", source):
        t = m.group(1)
        if t not in targets:
            targets.append(t)

    return targets


# ---------------------------------------------------------------------------
# Scene flow — determine the order to walk through scenes
# ---------------------------------------------------------------------------


def _build_scene_flow(
    initial_scene: str,
    scene_data: dict[str, dict[str, Any]],
    all_scene_names: list[str],
) -> list[str]:
    """Build the test flow through scenes.

    Uses BFS from the initial scene through known transitions. When the
    transition graph has gaps (e.g., stubs not yet built, or comment-described
    targets that don't match actual scene names), falls back to scene
    declaration order to ensure we still visit menu scenes and at least one
    gameplay scene.

    Returns an ordered list of scenes to visit, stopping once we reach a
    gameplay scene (we exercise gameplay inputs there).
    """
    gameplay_keywords = ("playing", "fighting", "racing", "running", "battle", "duel", "serve")

    def _is_gameplay(scene_name: str) -> bool:
        data = scene_data.get(scene_name)
        if not data:
            return False
        return any(
            any(kw in s.lower() for kw in gameplay_keywords)
            for s in data["states"]
        )

    # Try BFS first
    visited: list[str] = []
    queue = [initial_scene]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        if current not in scene_data:
            continue
        visited.append(current)

        if _is_gameplay(current):
            return visited

        # Queue transition targets that exist as actual scenes
        for target in scene_data[current]["transitions_out"]:
            if target in scene_data and target not in visited:
                queue.append(target)

    # BFS didn't reach a gameplay scene — fall back to declaration order.
    # Walk scenes in the order they were declared in game.gd, starting from
    # the initial scene, until we reach a gameplay scene.
    flow: list[str] = []
    started = False
    for scene_name in all_scene_names:
        if scene_name == initial_scene:
            started = True
        if not started:
            continue
        if scene_name not in scene_data:
            continue
        flow.append(scene_name)
        if _is_gameplay(scene_name):
            return flow

    # If still nothing, return whatever we have
    return flow if flow else visited


# ---------------------------------------------------------------------------
# Step generators — navigation and gameplay
# ---------------------------------------------------------------------------


def _add_navigation_steps(
    steps: list[dict],
    scene_name: str,
    data: dict[str, Any],
) -> None:
    """Add steps to navigate through a menu/transition scene.

    Finds the input action that triggers a scene transition and presses it.
    """
    # Screenshot the scene first
    steps.append({
        "action": f"Scene: {scene_name}",
        "keys": None,
        "wait_ms": 2000,
        "description": f"Screenshot of {scene_name} scene",
    })

    # Find the input action that triggers a scene transition
    transition_action = None
    for action in data["input_actions"]:
        if action["is_transition"]:
            transition_action = action
            break

    # Fallback: any ui_accept or return action
    if not transition_action:
        for action in data["input_actions"]:
            if action["action_name"] in ("ui_accept", "return"):
                transition_action = action
                break

    # Fallback: first available action
    if not transition_action and data["input_actions"]:
        transition_action = data["input_actions"][0]

    if transition_action:
        key = transition_action["key"]
        method = transition_action["method"]
        targets = data["transitions_out"]
        target_desc = f" -> {targets[0]}" if targets else ""

        steps.append({
            "action": f"Navigate: {scene_name}{target_desc}",
            "keys": key,
            "method": method,
            "wait_ms": 2000,
            "description": f"Press {key} to advance from {scene_name}{target_desc}",
        })
    else:
        # No input actions found — try Enter as universal fallback
        steps.append({
            "action": f"Navigate: {scene_name} (fallback)",
            "keys": "Enter",
            "method": "press",
            "wait_ms": 2000,
            "description": f"Press Enter to try advancing from {scene_name}",
        })


def _add_gameplay_steps(
    steps: list[dict],
    scene_name: str,
    data: dict[str, Any],
) -> None:
    """Add steps to exercise gameplay inputs in a gameplay scene.

    Takes a screenshot, then presses each unique gameplay input action.
    """
    # Screenshot the gameplay scene on entry
    steps.append({
        "action": f"Scene: {scene_name}",
        "keys": None,
        "wait_ms": 2000,
        "description": f"Screenshot of {scene_name} scene on entry",
    })

    # Deduplicate actions by key to avoid pressing the same key twice
    seen_keys: set[str] = set()
    gameplay_actions: list[dict] = []
    for action in data["input_actions"]:
        if action["key"] not in seen_keys:
            seen_keys.add(action["key"])
            gameplay_actions.append(action)

    # Exercise up to 8 unique gameplay actions
    for action in gameplay_actions[:8]:
        key = action["key"]
        method = action["method"]
        hold_ms = 500 if method == "hold" else 0
        effect_desc = action["effect"] or f"Press {key}"

        steps.append({
            "action": f"Gameplay: {action['action_name']}",
            "keys": key,
            "method": method,
            "hold_ms": hold_ms,
            "wait_ms": 800,
            "description": f"[{scene_name}] {effect_desc}",
        })


# ---------------------------------------------------------------------------
# Scene object parser — unchanged, reads .tscn files
# ---------------------------------------------------------------------------


def _parse_scene_objects(game_dir: Path) -> dict[str, list[str]]:
    """Parse .tscn files to find objects per layer.

    Returns {"Background": ["stage_bg", "ground"], "Gameplay": ["p1_fighter"], ...}
    """
    result: dict[str, list[str]] = {}

    for tscn_file in game_dir.glob("scenes/*.tscn"):
        scene_name = tscn_file.stem
        content = tscn_file.read_text(encoding="utf-8")

        # Find all nodes with parent paths
        for m in re.finditer(r'\[node name="(\w+)" type="(\w+)" parent="([^"]+)"\]', content):
            node_name = m.group(1)
            node_type = m.group(2)
            parent = m.group(3)

            # Determine layer from parent path
            layer = "root"
            if "Background" in parent:
                layer = "Background"
            elif "Gameplay" in parent:
                layer = "Gameplay"
            elif "HUD" in parent:
                layer = "HUD"
            elif "Effects" in parent:
                layer = "Effects"
            elif "Hitboxes" in parent:
                layer = "Hitboxes"

            key = f"{scene_name}/{layer}"
            if key not in result:
                result[key] = []
            result[key].append(node_name)

    return result


def _fallback_steps() -> list[dict]:
    """Default test steps when we can't parse game data."""
    return [
        {"action": "Press ENTER", "keys": "Enter", "wait_ms": 2000, "description": "Try to start/advance"},
        {"action": "Press ENTER", "keys": "Enter", "wait_ms": 2000, "description": "Confirm / next screen"},
        {"action": "Press ENTER", "keys": "Enter", "wait_ms": 2000, "description": "Start gameplay"},
        {"action": "Move Right", "keys": "ArrowRight", "method": "hold", "hold_ms": 500, "wait_ms": 500, "description": "Move right"},
        {"action": "Move Left", "keys": "ArrowLeft", "method": "hold", "hold_ms": 500, "wait_ms": 500, "description": "Move left"},
        {"action": "Jump", "keys": "ArrowUp", "wait_ms": 1000, "description": "Jump"},
        {"action": "Attack", "keys": "z", "wait_ms": 500, "description": "Attack"},
        {"action": "Final State", "keys": None, "wait_ms": 2000, "description": "Final state"},
    ]


def _check_canvas(page) -> dict:
    """Check if the Godot canvas has non-black content."""
    result = page.evaluate("""() => {
        const canvas = document.querySelector('canvas');
        if (!canvas) return {has_canvas: false, has_content: false, reason: 'no canvas'};
        if (canvas.width === 0 || canvas.height === 0)
            return {has_canvas: true, has_content: false, reason: 'zero-size canvas'};

        // Try 2D context for pixel sampling
        try {
            const ctx = canvas.getContext('2d', {willReadFrequently: true});
            if (ctx) {
                const w = Math.min(canvas.width, 200);
                const h = Math.min(canvas.height, 200);
                const data = ctx.getImageData(0, 0, w, h).data;
                let nonBlack = 0;
                for (let i = 0; i < data.length; i += 16) {
                    if (data[i] > 15 || data[i+1] > 15 || data[i+2] > 15) nonBlack++;
                }
                const pct = (nonBlack / (data.length / 16) * 100).toFixed(1);
                return {has_canvas: true, has_content: nonBlack > 5, reason: pct + '% non-black', pixels: nonBlack};
            }
        } catch(e) {}

        // WebGL — can't read pixels, assume content if canvas has dimensions
        return {has_canvas: true, has_content: true, reason: 'webgl (assumed ok)'};
    }""")
    return result or {"has_canvas": False, "has_content": False, "reason": "evaluate failed"}


def _verify_scene_objects(page, expected_objects: dict[str, list[str]]) -> dict:
    """Verify that expected scene objects are present/visible.

    Uses JavaScript to query the Godot scene tree via the debug console.
    Since we can't directly query Godot nodes from the browser, we check
    what's visually rendered by sampling specific screen regions.

    Returns {layer: [{name, expected, visible, region}]}
    """
    results = {}

    # We can't directly query Godot's scene tree from Playwright.
    # Instead, we sample the canvas at specific regions to detect if
    # objects are rendered there (non-black pixels in their expected area).
    #
    # For now, return the expected structure so the UI can show what
    # SHOULD be visible. The actual visibility check comes from the
    # canvas content analysis.

    for layer_key, objects in expected_objects.items():
        scene_name, layer = layer_key.rsplit("/", 1) if "/" in layer_key else ("", layer_key)
        layer_results = []
        for obj_name in objects:
            layer_results.append({
                "name": obj_name,
                "layer": layer,
                "scene": scene_name,
                "expected": True,
                "visible": None,  # Would need Godot debug API to check
            })
        results[layer_key] = layer_results

    return results


def _action_to_key(action_name: str) -> str:
    """Map Godot input action name to Playwright key name."""
    _MAP = {
        "move_up": "w", "move_down": "s", "move_left": "a", "move_right": "d",
        "ui_accept": "Enter", "confirm": "Enter", "ui_cancel": "Escape", "pause": "Escape",
        "return": "Enter", "ui_select": "Enter",
        "action_1": "z", "action_2": "x", "action_3": "c",
        "action_4": "Shift", "action_5": "Control", "action_6": "f",
    }
    return _MAP.get(action_name, action_name)


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------


def _save_screenshot_to_disk(
    game_name: str, step_index: int, action: str, png_bytes: bytes
) -> Path:
    """Save a screenshot PNG to .debug/screenshots/{game_name}/{step_name}.png.

    Returns the absolute path to the saved file.
    """
    screenshot_dir = _DEBUG_DIR / game_name
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    # Build a filesystem-safe name from the action
    safe_name = re.sub(r"[^a-z0-9_]+", "_", action.lower()).strip("_")
    filename = f"{game_name}_{step_index + 1:02d}_{safe_name}.png"
    filepath = screenshot_dir / filename

    filepath.write_bytes(png_bytes)
    return filepath


def _execute_test(game_name: str, test_steps: list[dict]) -> list[dict]:
    """Execute test steps with Playwright, saving screenshots to disk."""
    from playwright.sync_api import sync_playwright

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1024, "height": 768},
        )
        page = ctx.new_page()

        for i, step in enumerate(test_steps):
            action = step["action"]
            keys = step.get("keys")
            method = step.get("method", "press")
            wait_ms = step.get("wait_ms", 1000)
            hold_ms = step.get("hold_ms", 0)

            try:
                if keys is None and i == 0:
                    # Load page
                    page.goto(f"{_BASE_URL}/godot/{game_name}/", timeout=30000, wait_until="networkidle")
                    page.wait_for_timeout(wait_ms)
                elif keys == "click":
                    canvas = page.query_selector("canvas")
                    if canvas:
                        canvas.click()
                    page.wait_for_timeout(wait_ms)
                elif keys and method == "hold":
                    page.keyboard.down(keys)
                    page.wait_for_timeout(hold_ms or 500)
                    page.keyboard.up(keys)
                    page.wait_for_timeout(wait_ms)
                elif keys:
                    # Click canvas first to ensure focus (Godot web exports can lose it)
                    canvas = page.query_selector("canvas")
                    if canvas:
                        canvas.click()
                        page.wait_for_timeout(100)
                    page.keyboard.press(keys)
                    # For navigation steps, press again to be robust
                    if "Navigate" in step.get("action", ""):
                        page.wait_for_timeout(500)
                        page.keyboard.press(keys)
                    page.wait_for_timeout(wait_ms)
                else:
                    page.wait_for_timeout(wait_ms)

                png_bytes = page.screenshot()
                screenshot = base64.b64encode(png_bytes).decode("utf-8")

                # Save screenshot to disk
                saved_path = _save_screenshot_to_disk(game_name, i, action, png_bytes)

                # Check if canvas has non-black content
                canvas_check = _check_canvas(page)
                is_blank = not canvas_check["has_content"]

                # Compare with previous screenshot to detect state change
                changed = True
                if i > 0 and results:
                    prev = next((r for r in reversed(results) if r.get("type") == "step" and r.get("screenshot")), None)
                    if prev and prev.get("screenshot") == screenshot:
                        changed = False

                # Verify scene objects if this step has verification data
                verify_results = {}
                if step.get("verify_objects"):
                    verify_results = _verify_scene_objects(page, step["verify_objects"])

                status_parts = []
                if is_blank:
                    status_parts.append("BLANK SCREEN")
                if not changed and keys:
                    status_parts.append("NO CHANGE after input")
                if verify_results:
                    total = sum(len(v) for v in verify_results.values())
                    visible = sum(1 for v in verify_results.values() for o in v if o.get("visible"))
                    status_parts.append(f"Objects: {visible}/{total} visible")
                status_note = " — " + ", ".join(status_parts) if status_parts else ""

                # Pass = has content AND (changed or acceptable no-change)
                passed = not is_blank
                if keys and not changed and i > 2:
                    # Movement steps must show change; attacks/abilities may not
                    _is_movement = any(k in action.lower() for k in
                                       ("move", "walk", "steer", "navigate", "jump", "crouch"))
                    if _is_movement:
                        passed = False
                    # Non-movement gameplay (attacks, drift, items) — accept if not blank

                results.append({
                    "type": "step",
                    "step": i + 1,
                    "action": action,
                    "keys": str(keys),
                    "description": step.get("description", "") + status_note,
                    "screenshot": screenshot,
                    "screenshot_path": str(saved_path),
                    "passed": passed,
                    "canvas": canvas_check,
                    "scene_objects": verify_results if verify_results else None,
                })

            except Exception as exc:
                results.append({
                    "type": "step",
                    "step": i + 1,
                    "action": action,
                    "keys": str(keys),
                    "description": f"Error: {exc}",
                    "screenshot": None,
                    "screenshot_path": None,
                    "passed": False,
                })

        page.close()
        ctx.close()
        browser.close()

    return results


# ---------------------------------------------------------------------------
# Standalone batch-callable function
# ---------------------------------------------------------------------------


def run_screenshot_suite(game_name: str) -> list[dict]:
    """Run the full screenshot test suite for a game — batch-callable.

    Does not require an SSE/HTTP context. Generates test steps from the
    scene-based architecture, executes them with Playwright, saves
    screenshots to disk, and returns a list of step results.

    Returns:
        List of dicts, each with keys:
            step        — 1-based step number
            action      — human-readable action name
            screenshot_path — absolute path to saved PNG (or None)
            passed      — bool
            notes       — status notes (blank screen, no change, etc.)
    """
    log.info("run_screenshot_suite: starting for %s", game_name)

    test_steps = _build_test_steps(game_name)
    log.info("run_screenshot_suite: %d steps generated for %s", len(test_steps), game_name)

    raw_results = _execute_test(game_name, test_steps)

    # Flatten to batch-friendly format (no base64 screenshots in memory)
    suite_results: list[dict] = []
    for r in raw_results:
        if r.get("type") != "step":
            continue
        suite_results.append({
            "step": r["step"],
            "action": r["action"],
            "screenshot_path": r.get("screenshot_path"),
            "passed": r.get("passed", False),
            "notes": r.get("description", ""),
        })

    passed = sum(1 for r in suite_results if r["passed"])
    total = len(suite_results)
    log.info(
        "run_screenshot_suite: %s — %d/%d passed",
        game_name, passed, total,
    )

    return suite_results
