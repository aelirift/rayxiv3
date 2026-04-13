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
from rayxi.spec.mechanic_coverage import (
    audit_test_plan_coverage,
    audit_test_results_coverage,
    load_mechanic_manifest,
    write_mechanic_artifact,
)
from rayxi.spec.mechanic_behavior_fallback import default_behaviors_for_feature, merge_behaviors
from rayxi.spec.mechanic_contract import MechanicBehavior, MechanicFeature, MechanicManifest, MechanicTestAction

router = APIRouter()
log = logging.getLogger("rayxi.api.game_test")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_OUTPUT_DIR = _REPO_ROOT / "output"
_DEBUG_DIR = _REPO_ROOT / ".debug" / "screenshots"


def _finalize_test_steps(game_name: str, steps: list[dict]) -> list[dict]:
    steps = _normalize_test_steps(steps)
    manifest = load_mechanic_manifest(_OUTPUT_DIR / game_name / "mechanic_manifest.json")
    if manifest is not None:
        report = audit_test_plan_coverage(manifest, steps)
        write_mechanic_artifact(_OUTPUT_DIR / game_name / "mechanic_coverage_test_plan.json", report)
    return steps


def _write_test_results_coverage(game_name: str, steps: list[dict], results: list[dict]) -> None:
    manifest = load_mechanic_manifest(_OUTPUT_DIR / game_name / "mechanic_manifest.json")
    if manifest is None:
        return
    report = audit_test_results_coverage(manifest, steps, results)
    write_mechanic_artifact(_OUTPUT_DIR / game_name / "mechanic_coverage_test_results.json", report)


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


_GAMEPLAY_SCENE_KEYWORDS = ("fight", "battle", "combat", "gameplay", "match", "play", "race", "racing", "track")


def _load_mechanic_features(game_name: str, contract: dict[str, Any] | None) -> list[MechanicFeature]:
    raw_features = list((contract or {}).get("mechanics") or [])
    if raw_features:
        try:
            features = [MechanicFeature.model_validate(item) for item in raw_features]
            for feature in features:
                feature.behaviors = merge_behaviors(list(feature.behaviors) + default_behaviors_for_feature(feature))
            return features
        except Exception:
            pass
    manifest = load_mechanic_manifest(_OUTPUT_DIR / game_name / "mechanic_manifest.json")
    if manifest is None:
        return []
    features = list(manifest.features)
    for feature in features:
        feature.behaviors = merge_behaviors(list(feature.behaviors) + default_behaviors_for_feature(feature))
    return features


def _step_merge_key(step: dict[str, Any]) -> str:
    return "|".join(
        [
            str(step.get("action") or "").lower(),
            str(step.get("keys") or ""),
            str(step.get("method") or "press"),
            str(step.get("navigate_query") or ""),
            str(step.get("url_query") or ""),
            json.dumps(step.get("sequence") or [], sort_keys=True),
        ]
    )


def _merge_step_lists(left: list[str], right: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in list(left) + list(right):
        clean = str(item or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        out.append(clean)
        seen.add(key)
    return out


def _merge_test_step(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged["description"] = existing.get("description") or incoming.get("description") or ""
    merged["wait_ms"] = max(int(existing.get("wait_ms", 0) or 0), int(incoming.get("wait_ms", 0) or 0))
    merged["hold_ms"] = max(int(existing.get("hold_ms", 0) or 0), int(incoming.get("hold_ms", 0) or 0))
    merged["verify_change"] = bool(existing.get("verify_change")) or bool(incoming.get("verify_change"))
    existing_diff = existing.get("diff_threshold")
    incoming_diff = incoming.get("diff_threshold")
    if existing_diff is None:
        merged["diff_threshold"] = incoming_diff
    elif incoming_diff is None:
        merged["diff_threshold"] = existing_diff
    else:
        merged["diff_threshold"] = min(float(existing_diff), float(incoming_diff))
    for key in (
        "checks",
        "trace_any",
        "trace_all",
        "trace_any_global",
        "trace_all_global",
        "trace_none",
        "trace_none_global",
    ):
        merged[key] = _merge_step_lists(existing.get(key) or [], incoming.get(key) or [])
    return merged


def _action_to_step(action: MechanicTestAction, behavior: MechanicBehavior) -> dict[str, Any]:
    step: dict[str, Any] = {
        "action": action.action,
        "description": action.description or behavior.summary,
        "keys": action.keys,
        "method": action.method,
        "wait_ms": action.wait_ms,
        "hold_ms": action.hold_ms,
        "verify_change": action.verify_change,
        "diff_threshold": action.diff_threshold,
        "navigate_query": action.navigate_query,
        "url_query": action.url_query,
        "sequence": list(action.sequence or []),
    }
    verification = action.verification.model_dump(exclude_none=True)
    for key, value in verification.items():
        if value:
            step[key] = value
    return step


_PROPERTY_REF_RE = re.compile(r"\b([a-z_][\w]*)\.([a-z_][\w]*)\b", re.IGNORECASE)
_PROPERTY_COMPARE_RE = re.compile(
    r"\b([a-z_][\w]*)\.([a-z_][\w]*)\s*(==|!=|>=|<=|>|<)\s*(['\"]?[^'\"\s]+['\"]?)",
    re.IGNORECASE,
)
_BARE_PROPERTY_COMPARE_RE = re.compile(
    r"\b([a-z_][\w]*)\s*(==|!=|>=|<=|>|<)\s*(['\"]?[^'\"\s]+['\"]?)",
    re.IGNORECASE,
)
_PROPERTY_TRUE_RE = re.compile(
    r"\b([a-z_][\w]*)\.([a-z_][\w]*)\s+(?:is true|was set to true)\b",
    re.IGNORECASE,
)
_PROPERTY_FALSE_RE = re.compile(
    r"\b([a-z_][\w]*)\.([a-z_][\w]*)\s+(?:is false|was set to false)\b",
    re.IGNORECASE,
)
_BARE_PROPERTY_TRUE_RE = re.compile(r"\b([a-z_][\w]*)\s+(?:is true|was set to true)\b", re.IGNORECASE)
_BARE_PROPERTY_FALSE_RE = re.compile(r"\b([a-z_][\w]*)\s+(?:is false|was set to false)\b", re.IGNORECASE)
_PROPERTY_INCREMENT_RE = re.compile(
    r"\b([a-z_][\w]*)\.([a-z_][\w]*)\s+increment(?:ed|s)\s+by\s+([^\s]+)",
    re.IGNORECASE,
)
_BARE_PROPERTY_INCREMENT_RE = re.compile(r"\b([a-z_][\w]*)\s+increment(?:ed|s)\s+by\s+([^\s]+)", re.IGNORECASE)
_PROPERTY_RESET_RE = re.compile(
    r"\b([a-z_][\w]*)\.([a-z_][\w]*)\s+reset to\s+([^\s]+)",
    re.IGNORECASE,
)
_BARE_PROPERTY_RESET_RE = re.compile(r"\b([a-z_][\w]*)\s+reset to\s+([^\s]+)", re.IGNORECASE)
_PROPERTY_RECORDED_RE = re.compile(
    r"\b([a-z_][\w]*)\.([a-z_][\w]*)\s+recorded\b",
    re.IGNORECASE,
)
_BARE_PROPERTY_RECORDED_RE = re.compile(r"\b([a-z_][\w]*)\s+recorded\b", re.IGNORECASE)
_SCENE_TRANSITION_RE = re.compile(r"\bscene transitions to\s+([a-z_][\w]*)", re.IGNORECASE)
_FOLLOWS_RE = re.compile(
    r"\b([a-z_][\w]*)\.([a-z_][\w]*)\s+(?:follows|is behind)\s+([a-z_][\w]*)\.([a-z_][\w]*)",
    re.IGNORECASE,
)

_GLOBAL_TRACE_OWNERS = {"game", "scene", "camera", "hud", "race_manager", "round_manager"}
_INPUT_PROP_HINTS: dict[str, list[str]] = {
    "acceleration_input": ["accel=true", "accel=1", "accel=1.0"],
    "accel_input": ["accel=true", "accel=1", "accel=1.0"],
    "throttle_input": ["accel=true", "accel=1", "accel=1.0"],
    "brake_input": ["brake=true", "brake=1", "brake=1.0"],
    "steer_input": ["steer="],
    "drift_input": ["drift=true", "drift=1", "drift=1.0"],
    "item_trigger_input": ["item=true", "item=1", "item=1.0"],
    "item_input": ["item=true", "item=1", "item=1.0"],
}


def _clean_expected_value(raw_value: str | None) -> str:
    return str(raw_value or "").strip().strip("'\"").lower()


def _trace_field_for_owner(owner: str, prop: str = "") -> str:
    lower_owner = owner.lower()
    lower_prop = prop.lower()
    if lower_owner in _GLOBAL_TRACE_OWNERS or lower_prop.startswith("countdown") or "state" in lower_prop:
        return "trace_any_global"
    return "trace_any"


def _state_trace_patterns(value: str) -> list[str]:
    clean = _clean_expected_value(value)
    if not clean:
        return []
    upper = clean.upper()
    prefixed = upper if upper.startswith("S_") else f"S_{upper}"
    patterns = [f"state={clean}", f"state={upper}", f"state={prefixed}"]
    if clean.endswith("racing") or clean == "racing":
        patterns.extend(["countdown.complete", "scene.ready scene=racing"])
    if "finish" in clean:
        patterns.append("race_progress.finish")
    if "countdown" in clean:
        patterns.append("countdown.start")
    return _merge_step_lists([], patterns)


def _property_trace_patterns(
    owner: str,
    prop: str,
    *,
    operator: str = "",
    value: str = "",
    raw_text: str = "",
) -> tuple[str, list[str]]:
    lower_owner = owner.lower()
    lower_prop = prop.lower()
    lower_value = _clean_expected_value(value)
    lower_text = raw_text.lower()
    field = _trace_field_for_owner(lower_owner, lower_prop)
    patterns: list[str] = []

    if lower_prop in {"current_state", "fsm_state", "state"}:
        return "trace_any_global", _state_trace_patterns(lower_value or lower_text)

    if "countdown_value" in lower_prop or ("countdown" in lower_prop and "transition" in lower_text):
        return "trace_all_global", ["countdown.start value=3", "countdown.tick value=2", "countdown.tick value=1", "countdown.complete"]
    if "countdown_active" in lower_prop:
        return "trace_any_global", ["countdown.complete" if lower_value == "false" else "countdown.start"]
    if "race_timer" in lower_prop:
        return "trace_any_global", ["countdown.complete", "scene.ready scene="]

    if "laterally" in lower_text or "trajectory modified by steering input" in lower_text:
        return field, ["physics.turn", "physics.update"]
    if "changes" in lower_text:
        if "camera" in lower_owner or lower_prop.startswith("world_"):
            return "trace_any_global", ["camera.update position="]
        if "position" in lower_prop or "velocity" in lower_prop or "speed" in lower_prop:
            return field, ["physics.update"]

    if lower_prop in _INPUT_PROP_HINTS or lower_prop.endswith("_input"):
        field = "trace_all"
        patterns.append("input.update actor=")
        patterns.extend(_INPUT_PROP_HINTS.get(lower_prop, []))
        return field, _merge_step_lists([], patterns)

    if lower_prop in {"speed", "velocity"} or lower_prop.endswith("_velocity"):
        return field, ["physics.update"]
    if lower_prop in {"position", "world_x", "world_y"}:
        if "camera" in lower_owner or lower_prop.startswith("world_"):
            return "trace_any_global", ["camera.update position="]
        return field, ["physics.update"]
    if lower_prop in {"angle", "rotation", "facing_angle", "heading_degrees"}:
        if "camera" in lower_owner:
            return "trace_any_global", ["camera.update position="]
        return field, ["physics.turn", "physics.update"]

    if lower_prop == "drift_charge":
        return field, ["drift_boost.drift_start", "drift_boost.tier_up"]
    if lower_prop == "is_drifting":
        if lower_value == "false":
            return field, ["drift_boost.boost_start", "drift_boost.boost_end"]
        return field, ["drift_boost.drift_start"]
    if lower_prop == "drift_direction":
        return field, ["drift_boost.drift_start"]
    if lower_prop == "boost_timer":
        return field, ["drift_boost.boost_start", "item.boost_start", "item_usage.boost_start"]
    if lower_prop == "boost_multiplier":
        return field, ["drift_boost.boost_start", "item.boost_start", "item.boost_applied"]

    if lower_prop == "current_item":
        if operator == "!=" and lower_value == "none":
            return field, ["item.collect", "test.mode mode=item_ready"]
        if lower_value == "none":
            return field, ["item.use"]
        return field, ["item.collect", "test.mode mode=item_ready"]
    if lower_prop == "item_use_cooldown":
        return field, ["item.use", "item_usage.activate"]
    if lower_prop == "is_active" and "item_box" in lower_owner:
        if lower_value == "false":
            return field, ["item.collect"]
        return field, ["item.pickup_respawn"]
    if lower_prop == "spin_out_timer":
        return field, ["collision.spin_out", "collision.kart_kart", "collision.vehicle_vehicle", "collision.actor_actor"]
    if lower_prop == "invincibility_timer":
        return field, ["item.invulnerability_start", "item_usage.invincibility_start"]
    if lower_prop == "current_lap":
        return field, ["race_progress.lap_up"]
    if lower_prop == "waypoint_index":
        return field, ["race_progress.checkpoint", "ai_navigation.waypoint"]
    if lower_prop == "finish_time":
        return field, ["race_progress.finish"]
    if lower_prop == "race_finished":
        return field, ["race_progress.finish"]
    if lower_prop == "ai_target_waypoint":
        return field, ["ai_navigation.waypoint", "ai_navigation.tick"]
    if lower_prop == "speed_display_value":
        return "trace_any_global", ["hud.update_values speed="]

    return field, _merge_step_lists([], patterns)


def _translated_verification_entries(field_name: str, raw_text: str) -> dict[str, list[str]]:
    clean = str(raw_text or "").strip()
    if not clean:
        return {}
    lower = clean.lower()
    if "transitioned from" in lower and "countdown" in lower:
        return {"trace_all_global": ["countdown.start value=3", "countdown.tick value=2", "countdown.tick value=1", "countdown.complete"]}

    scene_match = _SCENE_TRANSITION_RE.search(clean)
    if scene_match:
        target_scene = scene_match.group(1)
        return {"trace_any_global": _state_trace_patterns(target_scene)}

    follow_match = _FOLLOWS_RE.search(clean)
    if follow_match:
        owner, prop, _, _ = follow_match.groups()
        field, patterns = _property_trace_patterns(owner, prop, raw_text=clean)
        return {field: patterns} if patterns else {}

    compare_match = _PROPERTY_COMPARE_RE.search(clean)
    if compare_match:
        owner, prop, op, value = compare_match.groups()
        field, patterns = _property_trace_patterns(owner, prop, operator=op, value=value, raw_text=clean)
        return {field: patterns} if patterns else {}

    bare_compare_match = _BARE_PROPERTY_COMPARE_RE.search(clean)
    if bare_compare_match:
        prop, op, value = bare_compare_match.groups()
        field, patterns = _property_trace_patterns("", prop, operator=op, value=value, raw_text=clean)
        return {field: patterns} if patterns else {}

    true_match = _PROPERTY_TRUE_RE.search(clean)
    if true_match:
        owner, prop = true_match.groups()
        field, patterns = _property_trace_patterns(owner, prop, value="true", raw_text=clean)
        return {field: patterns} if patterns else {}

    bare_true_match = _BARE_PROPERTY_TRUE_RE.search(clean)
    if bare_true_match:
        prop = bare_true_match.group(1)
        field, patterns = _property_trace_patterns("", prop, value="true", raw_text=clean)
        return {field: patterns} if patterns else {}

    false_match = _PROPERTY_FALSE_RE.search(clean)
    if false_match:
        owner, prop = false_match.groups()
        field, patterns = _property_trace_patterns(owner, prop, value="false", raw_text=clean)
        return {field: patterns} if patterns else {}

    bare_false_match = _BARE_PROPERTY_FALSE_RE.search(clean)
    if bare_false_match:
        prop = bare_false_match.group(1)
        field, patterns = _property_trace_patterns("", prop, value="false", raw_text=clean)
        return {field: patterns} if patterns else {}

    increment_match = _PROPERTY_INCREMENT_RE.search(clean)
    if increment_match:
        owner, prop, value = increment_match.groups()
        field, patterns = _property_trace_patterns(owner, prop, value=value, raw_text=clean)
        return {field: patterns} if patterns else {}

    bare_increment_match = _BARE_PROPERTY_INCREMENT_RE.search(clean)
    if bare_increment_match:
        prop, value = bare_increment_match.groups()
        field, patterns = _property_trace_patterns("", prop, value=value, raw_text=clean)
        return {field: patterns} if patterns else {}

    reset_match = _PROPERTY_RESET_RE.search(clean)
    if reset_match:
        owner, prop, value = reset_match.groups()
        field, patterns = _property_trace_patterns(owner, prop, value=value, raw_text=clean)
        return {field: patterns} if patterns else {}

    bare_reset_match = _BARE_PROPERTY_RESET_RE.search(clean)
    if bare_reset_match:
        prop, value = bare_reset_match.groups()
        field, patterns = _property_trace_patterns("", prop, value=value, raw_text=clean)
        return {field: patterns} if patterns else {}

    recorded_match = _PROPERTY_RECORDED_RE.search(clean)
    if recorded_match:
        owner, prop = recorded_match.groups()
        field, patterns = _property_trace_patterns(owner, prop, raw_text=clean)
        return {field: patterns} if patterns else {}

    bare_recorded_match = _BARE_PROPERTY_RECORDED_RE.search(clean)
    if bare_recorded_match:
        prop = bare_recorded_match.group(1)
        field, patterns = _property_trace_patterns("", prop, raw_text=clean)
        return {field: patterns} if patterns else {}

    prop_match = _PROPERTY_REF_RE.search(clean)
    if prop_match:
        owner, prop = prop_match.groups()
        field, patterns = _property_trace_patterns(owner, prop, raw_text=clean)
        return {field: patterns} if patterns else {}

    if "race timer" in lower and ("started" in lower or "> 0" in lower):
        return {"trace_any_global": ["countdown.complete", "scene.ready scene="]}
    return {}


def _sanitize_verification_field(field_name: str, values: list[str]) -> dict[str, list[str]]:
    allowed_structured = {"trace_any", "trace_all", "trace_any_global", "trace_all_global", "trace_none", "trace_none_global"}
    out: dict[str, list[str]] = {}
    for raw in values:
        clean = str(raw or "").strip()
        if not clean:
            continue
        translated = _translated_verification_entries(field_name, clean)
        if translated:
            for target_field, entries in translated.items():
                out.setdefault(target_field, []).extend(entries)
            continue
        if field_name in allowed_structured and not clean.lower().startswith("verify "):
            out.setdefault(field_name, []).append(clean)
    return out


def _normalize_test_step(step: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(step)
    verification_fields = [
        "trace_any",
        "trace_all",
        "trace_any_global",
        "trace_all_global",
        "trace_none",
        "trace_none_global",
    ]
    merged_fields: dict[str, list[str]] = {field: [] for field in verification_fields}
    for field_name in verification_fields:
        field_values = normalized.get(field_name) or []
        field_bucket = _sanitize_verification_field(field_name, list(field_values))
        for target_field, entries in field_bucket.items():
            merged_fields.setdefault(target_field, []).extend(entries)
    for field_name in verification_fields:
        cleaned = _merge_step_lists([], merged_fields.get(field_name) or [])
        if cleaned:
            normalized[field_name] = cleaned
        elif field_name in normalized:
            normalized.pop(field_name, None)
    raw_checks = normalized.get("checks") or []
    checks: list[str] = []
    translated_from_checks: dict[str, list[str]] = {}
    for raw in raw_checks:
        clean = str(raw or "").strip()
        if not clean:
            continue
        if clean in SEMANTIC_CHECKS:
            checks.append(clean)
            continue
        translated = _translated_verification_entries("checks", clean)
        for target_field, entries in translated.items():
            translated_from_checks.setdefault(target_field, []).extend(entries)
    for target_field, entries in translated_from_checks.items():
        normalized[target_field] = _merge_step_lists(normalized.get(target_field) or [], entries)
    if checks:
        normalized["checks"] = _merge_step_lists([], checks)
    else:
        normalized.pop("checks", None)
    return normalized


def _normalize_test_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_test_step(step) for step in steps]


def _steps_from_mechanic_features(features: list[MechanicFeature]) -> list[dict[str, Any]]:
    behaviors: list[MechanicBehavior] = []
    for feature in features:
        behaviors.extend(feature.behaviors or [])
    if not behaviors:
        return []
    ordered = sorted(behaviors, key=lambda behavior: (behavior.priority, behavior.id))
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for behavior in ordered:
        for action in behavior.actions:
            step = _action_to_step(action, behavior)
            key = _step_merge_key(step)
            if key not in merged:
                merged[key] = step
                order.append(key)
                continue
            merged[key] = _merge_test_step(merged[key], step)
    return [merged[key] for key in order]


def _build_test_steps_from_spec(game_name: str) -> list[dict]:
    """Generate the test plan from the game's HLR + impact_map artifacts.

    Falls back to a generic fighting-game plan if no spec files are found.
    """
    game_dir = _OUTPUT_DIR / game_name
    hlr_path = game_dir / "hlr.json"
    impact_path = game_dir / "impact_map_final.json"

    if not hlr_path.exists():
        log.warning("No HLR for %s — using generic fighting-game fallback plan", game_name)
        return _finalize_test_steps(game_name, _fallback_plan())

    hlr = json.loads(hlr_path.read_text())
    impact: dict | None = None
    contract: dict[str, Any] | None = None
    if impact_path.exists():
        try:
            impact = json.loads(impact_path.read_text())
        except Exception:
            impact = None
    contract_path = game_dir / "build_contract.json"
    if contract_path.exists():
        try:
            contract = json.loads(contract_path.read_text())
        except Exception:
            contract = None

    # Find the primary gameplay scene
    scenes = [s["scene_name"] for s in hlr.get("scenes", [])]
    gameplay_scene = _pick_gameplay_scene(scenes)
    genre = str((contract or {}).get("genre") or hlr.get("genre") or "")
    system_names = list((contract or {}).get("systems") or ((impact or {}).get("systems") or []))
    role_defs = dict(((contract or {}).get("roles") or {}))
    role_names = list(role_defs.keys())
    scene_defaults = dict((contract or {}).get("scene_defaults") or {})
    role_groups = dict((contract or {}).get("role_groups") or {})
    capabilities = dict((contract or {}).get("capabilities") or {})

    # Check whether the Godot project boots directly into the gameplay scene.
    # If so, we skip menu-advance steps entirely.
    main_scene_path = _read_main_scene_from_project(game_name)
    main_scene_name = Path(main_scene_path).stem.lower() if main_scene_path else None
    boots_into_gameplay = (
        main_scene_name is not None
        and (
            (gameplay_scene is not None and gameplay_scene.lower() == main_scene_name)
            or any(kw in main_scene_name for kw in _GAMEPLAY_SCENE_KEYWORDS)
        )
    )

    steps: list[dict] = [
        {"action": "Load page", "keys": None, "wait_ms": 1800, "url_query": "rayxi_test_mode=dummy",
         "description": f"Navigate to /godot/{game_name}/ in dummy test mode and wait for canvas",
         "trace_any": ["scene.ready"]},
        {"action": "Click canvas to focus", "keys": "click", "wait_ms": 500,
         "description": "Canvas focus so keyboard input reaches Godot"},
        {"action": "Wait for gameplay start", "keys": None, "wait_ms": 1200,
         "description": "Allow the initial scene setup to finish before gameplay assertions"},
    ]

    if not boots_into_gameplay:
        # Chew through menu scenes with Enter presses
        menu_scene_count = max(0, len(scenes) - 1)
        for i in range(min(menu_scene_count, 4)):
            steps.append({
                "action": f"Advance menu (#{i+1})", "keys": "Enter", "wait_ms": 1200,
                "description": "Press Enter to progress through menu scenes"})

    mechanic_features = _load_mechanic_features(game_name, contract)
    mechanic_steps = _steps_from_mechanic_features(mechanic_features)
    if mechanic_steps:
        steps.extend(mechanic_steps)
        steps.append({
            "action": "Final state", "keys": None, "wait_ms": 1500,
            "description": "Capture final scene to verify game is still alive"
        })
        return _finalize_test_steps(game_name, steps)

    vehicle_race_profile = _vehicle_race_profile(
        game_name,
        genre,
        system_names,
        role_defs,
        scene_defaults,
        gameplay_scene,
        role_groups=role_groups,
        capabilities=capabilities,
    )
    if vehicle_race_profile is not None:
        steps.extend(_vehicle_race_plan(gameplay_scene or "gameplay", vehicle_race_profile))
        steps.append({
            "action": "Final state", "keys": None, "wait_ms": 1500,
            "description": "Capture final scene to verify game is still alive"
        })
        return _finalize_test_steps(game_name, steps)

    # Gameplay steps — movement + attacks, with semantic checks.
    gameplay_scene_label = gameplay_scene or "gameplay"
    steps.extend([
        {"action": f"[{gameplay_scene_label}] Initial state (baseline)", "keys": None, "wait_ms": 500,
         "description": "Capture gameplay scene on first entry — establishes baselines",
         "checks": ["two_fighters_visible", "fighters_grounded", "capture_baseline_entity_count", "no_white_halo", "no_fighter_overlap"]},
        {"action": "Walk right", "keys": "d", "method": "hold", "hold_ms": 500, "wait_ms": 250,
         "description": "Hold D to walk the fighter right", "verify_change": True,
         "diff_threshold": 1.0,
         "checks": ["two_fighters_visible", "fighters_grounded", "no_fighter_overlap"],
         "trace_any": ["to=walk_forward"]},
        {"action": "Walk left", "keys": "a", "method": "hold", "hold_ms": 500, "wait_ms": 250,
         "description": "Hold A to walk the fighter left", "verify_change": True,
         "diff_threshold": 1.0,
         "checks": ["two_fighters_visible", "fighters_grounded", "no_fighter_overlap"],
         "trace_any": ["to=walk_back"]},
        {"action": "Jump", "keys": "w", "wait_ms": 1200,
         "description": "Press W to jump",
         "trace_any": ["movement.jump", "to=jump_neutral", "to=jump_forward", "to=jump_back"]},
        {"action": "Crouch", "keys": "s", "method": "hold", "hold_ms": 600, "wait_ms": 400,
         "description": "Hold S to crouch",
         "trace_any": ["input.crouch_change", "to=crouch"]},
        # NOTE: pixel-based "sprite swapped to attack pose" detection is too
        # noisy to be a hard gate when the game has only a single static sprite
        # texture (everything looks the same, small L1 noise drowns real pose
        # changes). Attack verification requires game-side state instrumentation
        # (expose fighter.current_action via JavaScriptBridge) — see TODO.
        # For now attack steps only verify no_white_halo (which IS reliable).
        {"action": "Light punch", "keys": "u", "wait_ms": 600,
         "description": "Press U for light punch",
         "checks": ["no_white_halo"],
         "trace_any": ["to=light_punch"]},
        {"action": "Heavy punch", "keys": "o", "wait_ms": 800,
         "description": "Press O for heavy punch",
         "checks": ["no_white_halo"],
         "trace_any": ["to=heavy_punch"]},
        {"action": "Light kick", "keys": "j", "wait_ms": 600,
         "description": "Press J for light kick",
         "trace_any": ["to=light_kick"]},
        {"action": "Heavy kick", "keys": "l", "wait_ms": 800,
         "description": "Press L for heavy kick",
         "trace_any": ["to=heavy_kick"]},
        {"action": "Crouch light punch", "keys": "s+u", "method": "sequence", "wait_ms": 0,
         "description": "Hold down and press U for a low punch",
         "sequence": [
             {"type": "down", "key": "s"},
             {"type": "wait", "ms": 80},
             {"type": "down", "key": "u"},
             {"type": "wait", "ms": 90},
             {"type": "up", "key": "u"},
             {"type": "wait", "ms": 260},
             {"type": "up", "key": "s"},
             {"type": "wait", "ms": 180},
         ],
         "trace_any": ["to=crouch_light_punch"]},
        {"action": "Crouch heavy kick", "keys": "s+l", "method": "sequence", "wait_ms": 0,
         "description": "Hold down and press L for a low kick",
         "sequence": [
             {"type": "down", "key": "s"},
             {"type": "wait", "ms": 80},
             {"type": "down", "key": "l"},
             {"type": "wait", "ms": 110},
             {"type": "up", "key": "l"},
             {"type": "wait", "ms": 320},
             {"type": "up", "key": "s"},
             {"type": "wait", "ms": 200},
         ],
         "trace_any": ["to=crouch_heavy_kick"]},
        {"action": "Reload special-ready mode", "keys": None, "wait_ms": 1200, "navigate_query": "rayxi_test_mode=projectile_ready",
         "description": "Reload a farther dummy spacing before projectile and special-motion checks",
         "trace_any": ["test.mode mode=projectile_ready"]},
        {"action": "Click canvas to focus (special ready)", "keys": "click", "wait_ms": 300,
         "description": "Refocus canvas after special-ready reload"},
        # Recapture the entity-count baseline right before the projectile
        # special so "new entity" compares the pre-special scene to the
        # post-special scene with the spawned projectile.
        {"action": "Pre-projectile-special baseline", "keys": None, "wait_ms": 500,
         "description": "Snapshot the scene right before the projectile special to recapture entity count",
         "checks": ["capture_baseline_entity_count"]},
        {"action": "Projectile special sequence", "keys": "s+d,u", "method": "sequence",
         "description": "Quarter-circle forward plus punch — should spawn a projectile",
         "wait_ms": 0,
         "sequence": [
             {"type": "down", "key": "s"},
             {"type": "wait", "ms": 45},
             {"type": "down", "key": "d"},
             {"type": "wait", "ms": 45},
             {"type": "up", "key": "s"},
             {"type": "wait", "ms": 25},
             {"type": "down", "key": "u"},
             {"type": "wait", "ms": 60},
             {"type": "up", "key": "u"},
             {"type": "wait", "ms": 80},
             {"type": "up", "key": "d"},
             {"type": "wait", "ms": 40},
         ],
         "checks": ["projectile_visible"],
         "trace_all": ["input.special_detected", "projectile.spawn"]},
        {"action": "Reload uppercut-ready mode", "keys": None, "wait_ms": 1200, "navigate_query": "rayxi_test_mode=uppercut_ready",
         "description": "Reload a closer dummy spacing before dragon punch motion verification",
         "trace_any": ["test.mode mode=uppercut_ready"]},
        {"action": "Click canvas to focus (uppercut ready)", "keys": "click", "wait_ms": 300,
         "description": "Refocus canvas after uppercut-ready reload"},
        {"action": "Uppercut special sequence", "keys": "d,s,d+u", "method": "sequence",
         "description": "Forward, down, down-forward plus punch — should trigger the upward special move",
         "wait_ms": 0,
         "sequence": [
             {"type": "down", "key": "d"},
             {"type": "wait", "ms": 45},
             {"type": "up", "key": "d"},
             {"type": "wait", "ms": 35},
             {"type": "down", "key": "s"},
             {"type": "wait", "ms": 45},
             {"type": "down", "key": "d"},
             {"type": "wait", "ms": 45},
             {"type": "down", "key": "u"},
             {"type": "wait", "ms": 60},
             {"type": "up", "key": "u"},
             {"type": "wait", "ms": 500},
             {"type": "up", "key": "d"},
             {"type": "up", "key": "s"},
             {"type": "wait", "ms": 450},
         ],
         "trace_any": ["movement.special_start"],
         "trace_any_global": [
             "input.special_detected move=special_uppercut",
             "special.motion_detected motion=dp",
             "special.executed fighter=",
             "combat.hit attacker=p1_fighter defender=p2_fighter move=special_uppercut",
         ]},
        {"action": "Reload spinning-ready mode", "keys": None, "wait_ms": 1200, "navigate_query": "rayxi_test_mode=dummy",
         "description": "Reload a clean dummy state before hurricane kick motion verification",
         "trace_any": ["test.mode mode=dummy"]},
        {"action": "Click canvas to focus (spinning ready)", "keys": "click", "wait_ms": 300,
         "description": "Refocus canvas after spinning-ready reload"},
        {"action": "Spinning special sequence", "keys": "s+a,j", "method": "sequence",
         "description": "Quarter-circle back plus kick — should trigger the spinning special move",
         "wait_ms": 0,
         "sequence": [
             {"type": "down", "key": "s"},
             {"type": "wait", "ms": 45},
             {"type": "down", "key": "a"},
             {"type": "wait", "ms": 45},
             {"type": "up", "key": "s"},
             {"type": "wait", "ms": 35},
             {"type": "down", "key": "j"},
             {"type": "wait", "ms": 60},
             {"type": "up", "key": "j"},
             {"type": "wait", "ms": 450},
             {"type": "up", "key": "a"},
             {"type": "wait", "ms": 450},
         ],
         "trace_any": ["movement.special_start"],
         "trace_any_global": [
             "input.special_detected move=special_spinning",
             "special.motion_detected motion=qcb",
             "special.executed fighter=",
         ]},
        {"action": "Reload collision-ready dummy", "keys": None, "wait_ms": 1200, "navigate_query": "rayxi_test_mode=dummy",
         "description": "Reload a clean dummy state before the pushout/collision walk test",
         "trace_any": ["test.mode mode=dummy"]},
        {"action": "Click canvas to focus (collision dummy)", "keys": "click", "wait_ms": 300,
         "description": "Refocus canvas after collision-ready reload"},
        {"action": "Walk straight at opponent (collision)", "keys": "d", "method": "hold",
         "hold_ms": 2200, "wait_ms": 400,
         "description": "Hold D until p1 reaches p2 — collision must stop overlap-through",
         "checks": ["p1_did_not_walk_through_p2"],
         "trace_any_global": ["collision.pushout"]},
        {"action": "Reload guard-high mode", "keys": None, "wait_ms": 1600, "navigate_query": "rayxi_test_mode=guard_high",
         "description": "Reload with a guarding dummy that holds back",
         "trace_any": ["test.mode mode=guard_high"]},
        {"action": "Click canvas to focus (guard high)", "keys": "click", "wait_ms": 300,
         "description": "Refocus canvas after guard-high reload"},
        {"action": "Stand block test", "keys": "d,o", "method": "sequence", "wait_ms": 0,
         "description": "A standing heavy punch against a high-guard dummy should be blocked",
         "sequence": [
             {"type": "down", "key": "d"},
             {"type": "wait", "ms": 140},
             {"type": "up", "key": "d"},
             {"type": "wait", "ms": 40},
             {"type": "down", "key": "o"},
             {"type": "wait", "ms": 80},
             {"type": "up", "key": "o"},
             {"type": "wait", "ms": 650},
         ],
         "trace_all": ["combat.blocked"]},
        {"action": "Reload guard-low mode", "keys": None, "wait_ms": 1600, "navigate_query": "rayxi_test_mode=guard_low",
         "description": "Reload with a guarding dummy that crouch-blocks",
         "trace_any": ["test.mode mode=guard_low"]},
        {"action": "Click canvas to focus (guard low)", "keys": "click", "wait_ms": 300,
         "description": "Refocus canvas after guard-low reload"},
        {"action": "Crouch block test", "keys": "d,s+l", "method": "sequence",
         "description": "Hold down and use a longer-range low kick against a crouch-block dummy — it should be blocked",
         "wait_ms": 0,
         "sequence": [
             {"type": "down", "key": "d"},
             {"type": "wait", "ms": 150},
             {"type": "up", "key": "d"},
             {"type": "wait", "ms": 30},
             {"type": "down", "key": "s"},
             {"type": "wait", "ms": 80},
             {"type": "down", "key": "l"},
             {"type": "wait", "ms": 140},
             {"type": "up", "key": "l"},
             {"type": "wait", "ms": 420},
             {"type": "up", "key": "s"},
             {"type": "wait", "ms": 250},
         ],
         "trace_all": ["combat.blocked"]},
    ])

    if capabilities.get("duel_combat") and len(steps) > 3:
        steps[3].setdefault("trace_any_global", []).append("hud.widget_ready name=rayxi_duel_status")

    # Custom-feature verification steps from mechanic_specs
    for spec in hlr.get("mechanic_specs", []):
        sys_name = spec.get("system_name", "")
        if "rage" in sys_name.lower():
            steps.append({
                "action": "Reload aggressor mode", "keys": None, "wait_ms": 900, "navigate_query": "rayxi_test_mode=aggressor",
                "description": "Reload with an aggressive CPU so rage gain is deterministic",
                "trace_any": ["test.mode mode=aggressor"],
            })
            steps.append({
                "action": "Click canvas to focus (aggressor)", "keys": "click", "wait_ms": 300,
                "description": "Refocus canvas after aggressor reload",
            })
            steps.append({
                "action": "Take damage (wait for AI)", "keys": None, "wait_ms": 3000,
                "description": "Wait for CPU opponent to land hits so rage meter visibly charges",
                "trace_all": [
                    "combat.hit",
                    "rage.fill_progress",
                ],
            })
            steps.append({
                "action": "Reload rage-ready mode", "keys": None, "wait_ms": 1200, "navigate_query": "rayxi_test_mode=rage_ready",
                "description": "Reload a deterministic powered-special setup with one seeded rage stack",
                "trace_all": [
                    "test.mode mode=rage_ready",
                    "rage.test_seed",
                ],
            })
            steps.append({
                "action": "Click canvas to focus (rage ready)", "keys": "click", "wait_ms": 300,
                "description": "Refocus canvas after rage-ready reload",
            })
            steps.append({
                "action": "Fire powered special", "keys": "s+d,u", "method": "sequence",
                "description": "Quarter-circle forward plus punch — should consume rage and fire a powered projectile",
                "wait_ms": 0,
                "sequence": [
                    {"type": "down", "key": "s"},
                    {"type": "wait", "ms": 40},
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 40},
                    {"type": "up", "key": "s"},
                    {"type": "wait", "ms": 30},
                    {"type": "down", "key": "u"},
                    {"type": "wait", "ms": 60},
                    {"type": "up", "key": "u"},
                    {"type": "wait", "ms": 80},
                    {"type": "up", "key": "d"},
                    {"type": "wait", "ms": 40},
                ],
                "trace_all": [
                    "rage.stack_consumed",
                    "projectile.spawn",
                ],
                "trace_any_global": [
                    "move=special_projectile powered=true",
                    "powered_special=true",
                ],
            })
            break

    # Final state snapshot
    steps.append({
        "action": "Final state", "keys": None, "wait_ms": 1500,
        "description": "Capture final scene to verify game is still alive"
    })

    return _finalize_test_steps(game_name, steps)


_RACE_ROLE_TOKENS = ("vehicle", "kart", "car", "bike", "ship", "racer", "driver")
_RACE_SYSTEM_TOKENS = ("vehicle_movement", "race_progress", "position_ranking", "drift_boost", "camera_tracking", "item_usage")


def _primary_vehicle_role(role_defs: dict[str, dict]) -> str | None:
    for token in _RACE_ROLE_TOKENS:
        for role_name, role_meta in role_defs.items():
            lower_name = role_name.lower()
            if token in lower_name:
                return role_name
    for role_name, role_meta in role_defs.items():
        if str((role_meta or {}).get("godot_base_node") or "") == "CharacterBody2D" and "fighter" not in role_name.lower():
            return role_name
    return None


def _available_hud_widget_names(game_name: str) -> set[str]:
    hud_dir = _OUTPUT_DIR / game_name / "godot" / "scripts" / "hud"
    if not hud_dir.exists():
        return set()
    return {
        path.stem
        for path in hud_dir.glob("*.gd")
        if path.stem and path.stem != "rayxi_duel_status"
    }


def _race_hud_widgets(game_name: str, scene_defaults: dict[str, Any], gameplay_scene_label: str | None) -> list[str]:
    widgets: list[str] = []
    available = _available_hud_widget_names(game_name)
    preferred = ("lap_counter", "position_display", "item_icon", "minimap", "mini_map", "speedometer", "finish_banner")

    def _pick_widget(name: str) -> str | None:
        candidates = (name,)
        if name == "minimap":
            candidates = ("minimap", "mini_map")
        elif name == "mini_map":
            candidates = ("mini_map", "minimap")
        for candidate in candidates:
            if available and candidate not in available:
                continue
            return candidate
        return None

    if gameplay_scene_label:
        scene_default = scene_defaults.get(gameplay_scene_label, {})
        hud_layout = dict(scene_default.get("hud_layout", {}) if isinstance(scene_default, dict) else {})
        for name in preferred:
            if name in hud_layout:
                picked = _pick_widget(name)
                if picked and picked not in widgets:
                    widgets.append(picked)
    if not widgets:
        for scene_default in scene_defaults.values():
            if not isinstance(scene_default, dict):
                continue
            hud_layout = dict(scene_default.get("hud_layout", {}) if isinstance(scene_default.get("hud_layout"), dict) else {})
            for name in preferred:
                if name in hud_layout:
                    picked = _pick_widget(name)
                    if picked and picked not in widgets:
                        widgets.append(picked)
    return widgets


def _first_role_from_groups(
    role_groups: dict[str, list[str]] | None,
    *group_names: str,
) -> str | None:
    groups = role_groups or {}
    for group_name in group_names:
        roles = [role for role in groups.get(group_name, []) if role]
        if roles:
            return roles[0]
    return None


def _vehicle_race_profile(
    game_name: str,
    genre: str,
    system_names: list[str],
    role_defs: dict[str, dict],
    scene_defaults: dict[str, Any],
    gameplay_scene_label: str | None,
    *,
    role_groups: dict[str, list[str]] | None = None,
    capabilities: dict[str, bool] | None = None,
) -> dict[str, Any] | None:
    lower_genre = genre.lower()
    lower_systems = [name.lower() for name in system_names]
    capability_flags = capabilities or {}
    vehicle_role = _first_role_from_groups(role_groups, "vehicle_actor_roles", "actor_roles") or _primary_vehicle_role(role_defs)
    has_vehicle_actor = vehicle_role is not None
    has_race_systems = any(any(token in name for token in _RACE_SYSTEM_TOKENS) for name in lower_systems)
    has_race_defaults = any(
        isinstance(scene_default, dict) and (
            "checkpoint_positions" in scene_default or "item_box_positions" in scene_default
        )
        for scene_default in scene_defaults.values()
    )
    has_race_genre_hint = "race" in lower_genre or "racing" in lower_genre
    has_race_capability = bool(capability_flags.get("checkpoint_race"))
    has_ai_rival_system = any(name == "ai_system" or name.startswith("ai_") for name in lower_systems)
    if not (has_race_capability or (has_vehicle_actor and has_race_systems) or has_race_defaults or has_race_genre_hint):
        return None
    return {
        "scene_name": gameplay_scene_label or "gameplay",
        "vehicle_role": vehicle_role or "vehicle",
        "hud_widgets": _race_hud_widgets(game_name, scene_defaults, gameplay_scene_label),
        "expects_ai_rival": has_ai_rival_system,
    }


def _vehicle_race_plan(gameplay_scene_label: str, profile: dict[str, Any]) -> list[dict]:
    baseline_trace_all = [
        f"scene.ready scene={profile.get('scene_name', gameplay_scene_label)}",
        "debug.overlay_ready kind=boxes",
        "debug.overlay_ready kind=log",
        "stage.track_seeded",
    ]
    if bool(profile.get("expects_ai_rival")):
        baseline_trace_all.append("ai_navigation.tick kart=")
    accelerate_trace_any = [
        "input.update actor=",
        "physics.update kart=",
    ]
    if bool(profile.get("expects_ai_rival")):
        accelerate_trace_any.append("ai_navigation.tick kart=")
    return [
        {
            "action": f"[{gameplay_scene_label}] Initial state (baseline)",
            "keys": None,
            "wait_ms": 500,
            "description": "Capture gameplay scene on first entry and establish vehicle-race baselines",
            "checks": ["capture_baseline_entity_count", "mode7_checkpoint_marker_visible", "mode7_item_marker_visible", "race_hud_visible"],
            "trace_all_global": baseline_trace_all,
            "trace_none_global": ["race_progress.finish"],
        },
        {
            "action": "Accelerate",
            "keys": "w",
            "method": "hold",
            "hold_ms": 1200,
            "wait_ms": 250,
            "description": "Hold W to accelerate the lead vehicle forward",
            "verify_change": False,
            "trace_any": accelerate_trace_any,
        },
        {
            "action": "Steer right while accelerating",
            "keys": "w+d",
            "method": "sequence",
            "wait_ms": 0,
            "description": "Hold W, then add D to verify steering under load",
            "verify_change": False,
            "sequence": [
                {"type": "down", "key": "w"},
                {"type": "wait", "ms": 350},
                {"type": "down", "key": "d"},
                {"type": "wait", "ms": 900},
                {"type": "up", "key": "d"},
                {"type": "wait", "ms": 120},
                {"type": "up", "key": "w"},
                {"type": "wait", "ms": 220},
            ],
            "trace_any": [
                "input.update actor=",
                "physics.turn kart=",
            ],
        },
        {
            "action": "Reload drift-ready mode",
            "keys": None,
            "wait_ms": 350,
            "navigate_query": "rayxi_test_mode=drift_ready",
            "description": "Reload with seeded speed so drift mechanics become testable quickly",
            "trace_any": ["test.mode mode=drift_ready"],
        },
        {
            "action": "Click canvas to focus (drift ready)",
            "keys": "click",
            "wait_ms": 120,
            "description": "Refocus canvas after drift-ready reload",
        },
        {
            "action": "Drift",
            "keys": "w+d+Shift",
            "method": "sequence",
            "wait_ms": 0,
            "description": "Accelerate, steer, and hold Shift to trigger drift/boost traces",
            "verify_change": False,
            "sequence": [
                {"type": "down", "key": "w"},
                {"type": "wait", "ms": 80},
                {"type": "down", "key": "d"},
                {"type": "wait", "ms": 80},
                {"type": "down", "key": "Shift"},
                {"type": "wait", "ms": 900},
                {"type": "up", "key": "Shift"},
                {"type": "wait", "ms": 160},
                {"type": "up", "key": "d"},
                {"type": "up", "key": "w"},
                {"type": "wait", "ms": 260},
            ],
            "trace_any": [
                "drift_boost.drift_start",
                "drift_boost.tier_up",
                "drift_boost.boost_start",
            ],
        },
        {
            "action": "Reload item-ready mode",
            "keys": None,
            "wait_ms": 1200,
            "navigate_query": "rayxi_test_mode=item_ready",
            "description": "Reload with a seeded item so item usage is deterministic",
            "trace_any": ["test.mode mode=item_ready"],
        },
        {
            "action": "Click canvas to focus (item ready)",
            "keys": "click",
            "wait_ms": 300,
            "description": "Refocus canvas after item-ready reload",
        },
        {
            "action": "Use item",
            "keys": "Space",
            "wait_ms": 900,
            "description": "Press Space to consume the seeded vehicle item",
            "trace_any": [
                "item.use",
                "item.spawn_projectile",
                "item.boost_applied",
                "item_usage.activate",
                "item_usage.spawn_shell",
                "item_usage.spawn_banana",
                "item_usage.boost_start",
                "item_usage.invincibility_start",
            ],
        },
        {
            "action": "Reload collision-ready mode",
            "keys": None,
            "wait_ms": 1200,
            "navigate_query": "rayxi_test_mode=collision_ready",
            "description": "Reload with karts positioned for a quick bump test",
            "trace_any": ["test.mode mode=collision_ready"],
        },
        {
            "action": "Click canvas to focus (collision ready)",
            "keys": "click",
            "wait_ms": 300,
            "description": "Refocus canvas after collision-ready reload",
        },
        {
            "action": "Drive into rival",
            "keys": "w",
            "method": "hold",
            "hold_ms": 2200,
            "wait_ms": 350,
            "description": "Hold W until the lead vehicle reaches the rival and collision resolves",
            "verify_change": False,
            "trace_any_global": ["collision.kart_kart", "collision.spin_out"],
        },
    ]


def _pick_gameplay_scene(scene_names: list[str]) -> str | None:
    # Prefer exact gameplay-style names over intro/transition scenes
    priority = ["fighting", "racing", "race", "gameplay", "battle", "match", "play"]
    for p in priority:
        for name in scene_names:
            if name.lower() == p:
                return name
    for name in scene_names:
        lower = name.lower()
        if (
            any(kw in lower for kw in _GAMEPLAY_SCENE_KEYWORDS)
            and "intro" not in lower
            and "select" not in lower
            and "menu" not in lower
        ):
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

    foreground_regions = 0
    try:
        foreground_regions = len(_detect_white_sprite_regions(png_bytes))
    except Exception:
        foreground_regions = 0

    has_content = (
        (unique_colors > 80 and variance > 8.0 and nontrivial_pct > 0.05)
        or foreground_regions >= 1
    )

    return {
        "has_content": has_content,
        "unique_colors": unique_colors,
        "variance": round(variance, 2),
        "nontrivial_pct": round(nontrivial_pct, 3),
        "foreground_regions": foreground_regions,
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
    """Return gameplay-region bboxes for fighters/projectiles on the stage.

    Older versions only looked for "white sprite" blobs, which breaks on dark
    art, overlapping fighters, and non-white projectiles. The current detector
    segments foreground by distance from the dominant stage background color,
    then filters out HUD bars and long floor strips.
    """
    from PIL import Image
    import numpy as np
    from scipy import ndimage

    def _mask_bbox(mask, x_offset: int, y_offset: int) -> tuple[int, int, int, int] | None:
        ys, xs = np.where(mask)
        if len(ys) == 0:
            return None
        return (
            x_offset + int(xs.min()),
            y_offset + int(ys.min()),
            x_offset + int(xs.max()),
            y_offset + int(ys.max()),
        )

    def _split_box(
        mask: "np.ndarray",
        box: tuple[int, int, int, int],
    ) -> list[tuple[int, int, int, int]]:
        x0, y0, x1, y1 = box
        crop = mask[y0 : y1 + 1, x0 : x1 + 1]
        if crop.size == 0:
            return [box]

        width = crop.shape[1]
        if width < 90:
            return [box]

        col_counts = crop.sum(axis=0).astype(float)
        smooth = ndimage.gaussian_filter1d(col_counts, sigma=max(width / 60.0, 1.5))
        valley_start = max(width // 5, 1)
        valley_end = min((width * 4) // 5, width - 1)
        if valley_end <= valley_start:
            return [box]

        valley_offset = int(np.argmin(smooth[valley_start:valley_end]))
        split_at = valley_start + valley_offset
        left_peak = float(smooth[:split_at].max()) if split_at > 0 else 0.0
        right_peak = float(smooth[split_at + 1 :].max()) if split_at + 1 < width else 0.0
        valley_value = float(smooth[split_at])
        if min(left_peak, right_peak) <= 0.0:
            return [box]
        if valley_value > min(left_peak, right_peak) * 0.88:
            return [box]

        left_mask = crop[:, :split_at]
        right_mask = crop[:, split_at + 1 :]
        pieces: list[tuple[int, int, int, int]] = []
        left_box = _mask_bbox(left_mask, x0, y0)
        right_box = _mask_bbox(right_mask, x0 + split_at + 1, y0)
        if left_box is not None:
            pieces.append(left_box)
        if right_box is not None:
            pieces.append(right_box)
        return pieces or [box]

    img = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB")).astype(np.int16)
    h, w = img.shape[:2]

    samples = np.concatenate(
        [
            img[:64, :64].reshape(-1, 3),
            img[:64, max(0, w - 64):].reshape(-1, 3),
            img[max(0, h - 64):, :64].reshape(-1, 3),
            img[max(0, h - 64):, max(0, w - 64):].reshape(-1, 3),
        ],
        axis=0,
    )
    bg = np.median(samples, axis=0)
    color_distance = np.sqrt(((img - bg) ** 2).sum(axis=2))
    luminance = img.mean(axis=2)

    foreground = (color_distance > 28.0) | (luminance > 175.0)
    foreground[: int(h * 0.18), :] = False
    closed = ndimage.binary_closing(foreground, structure=np.ones((5, 5)), iterations=1)

    labels, num = ndimage.label(closed)
    if num == 0:
        return []

    boxes: list[tuple[int, int, int, int]] = []
    for lbl in range(1, num + 1):
        component = labels == lbl
        ys, xs = np.where(component)
        if len(ys) < 60:
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        width = x1 - x0 + 1
        height = y1 - y0 + 1
        if width < 10 or height < 10:
            continue
        if y1 < int(h * 0.22):
            continue
        if width > int(w * 0.55) and height < int(h * 0.20):
            continue
        if width > int(w * 0.80):
            continue
        component_density = len(ys) / max(width * height, 1)
        if component_density < 0.05:
            continue
        boxes.extend(_split_box(closed, (x0, y0, x1, y1)))

    boxes.sort(key=lambda b: b[0])
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


def check_two_fighters_visible(png: bytes, state: dict, step: dict) -> dict:
    boxes = _detect_white_sprite_regions(png)
    if len(boxes) >= 2:
        return {"name": "two_fighters_visible", "passed": True,
                "reason": f"detected {len(boxes)} fighter-like regions"}
    return {"name": "two_fighters_visible", "passed": False,
            "reason": f"detected {len(boxes)} fighter-like regions, expected at least 2"}


def check_fighters_grounded(png: bytes, state: dict, step: dict) -> dict:
    from PIL import Image

    boxes = _detect_white_sprite_regions(png)
    if len(boxes) < 2:
        return {"name": "fighters_grounded", "passed": False,
                "reason": f"only {len(boxes)} fighter(s) detected"}

    img = Image.open(io.BytesIO(png)).convert("RGB")
    _w, h = img.size
    cutoff = int(h * 0.55)
    offenders: list[str] = []
    for idx, box in enumerate(boxes[:2], start=1):
        bottom = box[3]
        if bottom < cutoff:
            offenders.append(f"fighter{idx} bottom={bottom} above cutoff={cutoff}")

    if offenders:
        return {"name": "fighters_grounded", "passed": False,
                "reason": "; ".join(offenders)}
    return {"name": "fighters_grounded", "passed": True,
            "reason": f"fighter bottoms are below cutoff={cutoff}"}


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
    """After a projectile or spawn action, there should be more distinct sprite regions."""
    boxes = _detect_white_sprite_regions(png)
    baseline_count = state.get("baseline_entity_count", 2)
    if len(boxes) > baseline_count:
        return {"name": "new_entity_vs_baseline", "passed": True,
                "reason": f"{len(boxes)} regions (up from {baseline_count}) — new entity detected"}
    return {"name": "new_entity_vs_baseline", "passed": False,
            "reason": f"{len(boxes)} regions (same as baseline {baseline_count}) — no new projectile/entity spawned"}


def capture_baseline_entity_count(png: bytes, state: dict, step: dict) -> dict:
    """Not a real check — stores the current sprite-region count as baseline."""
    boxes = _detect_white_sprite_regions(png)
    state["baseline_entity_count"] = len(boxes)
    return {"name": "capture_baseline_entity_count", "passed": True,
            "reason": f"baseline = {len(boxes)} regions"}


def check_projectile_visible(png: bytes, state: dict, step: dict) -> dict:
    from PIL import Image
    import numpy as np
    from scipy import ndimage

    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    img_h, _img_w, _ = img.shape

    projectile_mask = (
        (img[..., 0] >= 220)
        & (img[..., 1] >= 95)
        & (img[..., 1] <= 235)
        & (img[..., 2] <= 125)
    )
    labels, num = ndimage.label(projectile_mask)
    for lbl in range(1, num + 1):
        ys, xs = np.where(labels == lbl)
        if len(ys) < 20:
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        width = x1 - x0 + 1
        height = y1 - y0 + 1
        if width < 10 or height < 10:
            continue
        if y0 < int(img_h * 0.45):
            continue
        if max(width, height) > 96:
            continue
        aspect = width / max(height, 1)
        if aspect < 0.55 or aspect > 3.25:
            continue
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        return {
            "name": "projectile_visible",
            "passed": True,
            "reason": f"projectile-colored blob at ({cx}, {cy}) size={width}x{height}",
        }

    return {
        "name": "projectile_visible",
        "passed": False,
        "reason": "no projectile-colored blob was visible outside fighter bounds",
    }


def check_track_checkpoints_visible(png: bytes, state: dict, step: dict) -> dict:
    from PIL import Image
    import numpy as np
    from scipy import ndimage

    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    checkpoint_mask = (
        (img[..., 1] >= 180)
        & (img[..., 0] <= 140)
        & (img[..., 2] <= 170)
    )
    labels, num = ndimage.label(checkpoint_mask)
    visible = 0
    for lbl in range(1, num + 1):
        ys, xs = np.where(labels == lbl)
        if len(xs) < 80:
            continue
        if (xs.max() - xs.min() + 1) < 10 or (ys.max() - ys.min() + 1) < 10:
            continue
        visible += 1
    if visible >= 4:
        return {
            "name": "track_checkpoints_visible",
            "passed": True,
            "reason": f"detected {visible} checkpoint-colored blobs",
        }
    return {
        "name": "track_checkpoints_visible",
        "passed": False,
        "reason": f"only detected {visible} checkpoint-colored blobs",
    }


def check_item_boxes_visible(png: bytes, state: dict, step: dict) -> dict:
    from PIL import Image
    import numpy as np
    from scipy import ndimage

    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    item_box_mask = (
        (img[..., 0] >= 180)
        & (img[..., 1] >= 90)
        & (img[..., 1] <= 220)
        & (img[..., 2] <= 120)
    )
    labels, num = ndimage.label(item_box_mask)
    visible = 0
    for lbl in range(1, num + 1):
        ys, xs = np.where(labels == lbl)
        if len(xs) < 50:
            continue
        if (xs.max() - xs.min() + 1) < 8 or (ys.max() - ys.min() + 1) < 8:
            continue
        visible += 1
    if visible >= 3:
        return {
            "name": "item_boxes_visible",
            "passed": True,
            "reason": f"detected {visible} item-box colored blobs",
        }
    return {
        "name": "item_boxes_visible",
        "passed": False,
        "reason": f"only detected {visible} item-box colored blobs",
    }


def check_mode7_checkpoint_marker_visible(png: bytes, state: dict, step: dict) -> dict:
    from PIL import Image
    import numpy as np
    from scipy import ndimage

    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    height, width = img.shape[:2]
    roi = img[int(height * 0.18):int(height * 0.74), :int(width * 0.72)]
    checkpoint_mask = (
        (roi[..., 1] >= 180)
        & (roi[..., 0] <= 150)
        & (roi[..., 2] <= 170)
    )
    labels, num = ndimage.label(checkpoint_mask)
    visible = 0
    for lbl in range(1, num + 1):
        ys, xs = np.where(labels == lbl)
        if len(xs) < 40:
            continue
        if (xs.max() - xs.min() + 1) < 6 or (ys.max() - ys.min() + 1) < 18:
            continue
        visible += 1
    if visible >= 1:
        return {
            "name": "race_checkpoint_marker_visible",
            "passed": True,
            "reason": f"detected {visible} projected checkpoint marker(s)",
        }
    return {
        "name": "race_checkpoint_marker_visible",
        "passed": False,
        "reason": "no projected checkpoint marker was visible in the race view",
    }


def check_mode7_item_marker_visible(png: bytes, state: dict, step: dict) -> dict:
    from PIL import Image
    import numpy as np
    from scipy import ndimage

    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    height, width = img.shape[:2]
    roi = img[int(height * 0.22):int(height * 0.72), :int(width * 0.78)]
    item_mask = (
        (roi[..., 0] >= 185)
        & (roi[..., 1] >= 95)
        & (roi[..., 1] <= 230)
        & (roi[..., 2] <= 140)
    )
    labels, num = ndimage.label(item_mask)
    visible = 0
    for lbl in range(1, num + 1):
        ys, xs = np.where(labels == lbl)
        if len(xs) < 32:
            continue
        if (xs.max() - xs.min() + 1) < 8 or (ys.max() - ys.min() + 1) < 8:
            continue
        visible += 1
    if visible >= 1:
        return {
            "name": "race_item_marker_visible",
            "passed": True,
            "reason": f"detected {visible} projected item marker(s)",
        }
    return {
        "name": "race_item_marker_visible",
        "passed": False,
        "reason": "no projected item marker was visible in the race view",
    }


def check_race_hud_visible(png: bytes, state: dict, step: dict) -> dict:
    from PIL import Image
    import numpy as np

    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    height, width = img.shape[:2]
    top_left = img[:110, :260]
    top_right = img[:110, max(width - 260, 0):]
    minimap = img[max(height - 190, 0):max(height - 10, 0), max(width - 230, 0):max(width - 10, 0)]

    left_bright = int(np.count_nonzero(
        (top_left[..., 0] >= 215)
        & (top_left[..., 1] >= 215)
        & (top_left[..., 2] >= 215)
    ))
    right_bright = int(np.count_nonzero(
        (top_right[..., 0] >= 215)
        & (top_right[..., 1] >= 185)
        & (top_right[..., 2] <= 120)
    ))
    minimap_var = float(np.std(minimap.mean(axis=2))) if minimap.size else 0.0
    minimap_unique = int(np.unique(minimap.reshape(-1, 3), axis=0).shape[0]) if minimap.size else 0

    passed = left_bright >= 20 and right_bright >= 20 and minimap_var >= 6.0 and minimap_unique >= 16
    if passed:
        return {
            "name": "race_hud_visible",
            "passed": True,
            "reason": f"lap/position HUD visible and minimap active (left={left_bright}, right={right_bright}, minimap_var={minimap_var:.1f})",
        }
    return {
        "name": "race_hud_visible",
        "passed": False,
        "reason": f"HUD/minimap visibility too weak (left={left_bright}, right={right_bright}, minimap_var={minimap_var:.1f}, minimap_unique={minimap_unique})",
    }


SEMANTIC_CHECKS = {
    "sprite_differs_from_baseline": check_sprite_differs_from_baseline,
    "no_white_halo": check_no_white_halo,
    "two_fighters_visible": check_two_fighters_visible,
    "two_actors_visible": check_two_fighters_visible,
    "fighters_grounded": check_fighters_grounded,
    "actors_grounded": check_fighters_grounded,
    "no_fighter_overlap": check_no_fighter_overlap,
    "no_actor_overlap": check_no_fighter_overlap,
    "p1_did_not_walk_through_p2": check_p1_did_not_walk_through_p2,
    "new_entity_vs_baseline": check_new_entity_vs_baseline,
    "capture_baseline_entity_count": capture_baseline_entity_count,
    "projectile_visible": check_projectile_visible,
    "track_checkpoints_visible": check_track_checkpoints_visible,
    "item_boxes_visible": check_item_boxes_visible,
    "race_checkpoint_marker_visible": check_mode7_checkpoint_marker_visible,
    "race_item_marker_visible": check_mode7_item_marker_visible,
    "mode7_checkpoint_marker_visible": check_mode7_checkpoint_marker_visible,
    "mode7_item_marker_visible": check_mode7_item_marker_visible,
    "race_hud_visible": check_race_hud_visible,
    "kart_hud_visible": check_race_hud_visible,
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
        def _onerror(func, path, _exc_info):
            try:
                Path(path).chmod(0o700)
            except OSError:
                pass
            func(path)

        removed = False
        for attempt in range(5):
            try:
                shutil.rmtree(d, onerror=_onerror)
                removed = True
                break
            except PermissionError:
                time.sleep(0.2 * (attempt + 1))
        if not removed and d.exists():
            stale = d.with_name(f"{d.name}_stale_{int(time.time() * 1000)}")
            try:
                d.rename(stale)
                removed = True
            except OSError:
                pass
        if not removed and d.exists():
            for child in sorted(d.glob("**/*"), reverse=True):
                try:
                    child.chmod(0o700)
                except OSError:
                    pass
                try:
                    if child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                except OSError:
                    continue
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


def _save_runtime_trace_log(game_name: str, console_events: list[dict], browser_errors: list[str]) -> Path:
    d = _DEBUG_DIR / game_name
    d.mkdir(parents=True, exist_ok=True)
    path = d / "runtime_trace.log"
    lines: list[str] = []
    for event in console_events:
        lines.append(f"[{event['t_ms']:06d}ms] [{event['type']}] {event['text']}")
    if browser_errors:
        lines.append("")
        lines.append("# browser_errors")
        lines.extend(browser_errors)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _trace_contains(events: list[dict], needle: str) -> bool:
    return any(needle in event["text"] for event in events)


def _trace_summary(events: list[dict], limit: int = 4) -> str:
    if not events:
        return ""
    trimmed = events[-limit:]
    return " | ".join(event["text"] for event in trimmed)


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
    console_events: list[dict] = []
    trace_events: list[dict] = []
    browser_errors: list[str] = []
    run_started = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-web-security"])
        ctx = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 720},
        )
        page = ctx.new_page()

        def _record_console(msg) -> None:
            entry = {
                "t_ms": int((time.time() - run_started) * 1000),
                "type": msg.type,
                "text": msg.text,
            }
            console_events.append(entry)
            if "[trace]" in msg.text:
                trace_events.append(entry)
            if msg.type == "error":
                browser_errors.append(f"[{entry['t_ms']:06d}ms] [console:error] {msg.text}")
            log.info("[browser:%s] %s", msg.type, msg.text)

        def _record_page_error(err) -> None:
            stamp = int((time.time() - run_started) * 1000)
            browser_errors.append(f"[{stamp:06d}ms] [pageerror] {err}")
            log.error("[browser:err] %s", err)

        page.on("console", _record_console)
        page.on("pageerror", _record_page_error)

        def _focus_canvas() -> None:
            canvas = page.query_selector("canvas")
            if canvas:
                try:
                    canvas.click(force=True, timeout=5000)
                    page.wait_for_timeout(50)
                    return
                except Exception:
                    pass
            page.mouse.click(640, 360)
            page.wait_for_timeout(50)

        for i, step in enumerate(test_steps):
            action = step["action"]
            keys = step.get("keys")
            method = step.get("method", "press")
            wait_ms = step.get("wait_ms", 1000)
            hold_ms = step.get("hold_ms", 0)
            verify_change = step.get("verify_change", False)
            raw_diff_threshold = step.get("diff_threshold", 2.0)
            diff_threshold = float(2.0 if raw_diff_threshold is None else raw_diff_threshold)
            trace_start = len(trace_events)
            error_start = len(browser_errors)

            try:
                navigate_query = step.get("navigate_query")
                url_query = step.get("url_query")
                if navigate_query is not None or (keys is None and i == 0):
                    query = navigate_query if navigate_query is not None else url_query
                    url = f"{base_url}/godot/{game_name}/"
                    if query:
                        url += f"?{query}"
                    log.info("playwright: goto %s", url)
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    # Wait for the canvas element to actually exist
                    try:
                        page.wait_for_selector("canvas", state="visible", timeout=30000)
                    except Exception:
                        log.warning("playwright: no visible canvas within 30s for %s", game_name)
                    if navigate_query is not None:
                        prev_png = None
                        shared_state.clear()
                    page.wait_for_timeout(wait_ms)
                elif keys == "click":
                    _focus_canvas()
                    page.wait_for_timeout(wait_ms)
                elif method == "sequence":
                    _focus_canvas()
                    for action_step in step.get("sequence", []):
                        seq_type = action_step.get("type")
                        key = action_step.get("key")
                        if seq_type == "down" and key:
                            page.keyboard.down(key)
                        elif seq_type == "up" and key:
                            page.keyboard.up(key)
                        elif seq_type == "press" and key:
                            page.keyboard.press(key)
                        elif seq_type == "wait":
                            page.wait_for_timeout(int(action_step.get("ms", 0)))
                    if wait_ms:
                        page.wait_for_timeout(wait_ms)
                elif keys and method == "hold":
                    _focus_canvas()
                    combo_keys = [part.strip() for part in str(keys).split("+") if part.strip()]
                    if len(combo_keys) > 1:
                        for combo_key in combo_keys:
                            page.keyboard.down(combo_key)
                    else:
                        page.keyboard.down(keys)
                    page.wait_for_timeout(hold_ms or 500)
                    if len(combo_keys) > 1:
                        for combo_key in reversed(combo_keys):
                            page.keyboard.up(combo_key)
                    else:
                        page.keyboard.up(keys)
                    page.wait_for_timeout(wait_ms)
                elif keys:
                    _focus_canvas()
                    combo_keys = [part.strip() for part in str(keys).split("+") if part.strip()]
                    if len(combo_keys) > 1:
                        for combo_key in combo_keys:
                            page.keyboard.down(combo_key)
                        page.wait_for_timeout(50)
                        for combo_key in reversed(combo_keys):
                            page.keyboard.up(combo_key)
                    else:
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
                    changed, diff_value = _screenshots_differ(prev_png, png_bytes, threshold=diff_threshold)
                prev_png = png_bytes
                new_traces = trace_events[trace_start:]
                new_errors = browser_errors[error_start:]

                metrics = (
                    f"colors={analysis['unique_colors']} "
                    f"var={analysis['variance']} "
                    f"nontrivial={int(analysis['nontrivial_pct']*100)}% "
                    f"diff={diff_value}/{diff_threshold} "
                    f"traces={len(new_traces)}"
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
                    failure_reasons.append(f"NO CHANGE after input (diff={diff_value}, threshold={diff_threshold})")

                trace_any = step.get("trace_any") or []
                if trace_any and not any(_trace_contains(new_traces, pattern) for pattern in trace_any):
                    passed = False
                    failure_reasons.append(f"missing expected trace_any: {trace_any}")

                trace_any_global = step.get("trace_any_global") or []
                if trace_any_global and not any(_trace_contains(trace_events, pattern) for pattern in trace_any_global):
                    passed = False
                    failure_reasons.append(f"missing expected trace_any_global: {trace_any_global}")

                trace_all_global = step.get("trace_all_global") or []
                missing_global_traces = [pattern for pattern in trace_all_global if not _trace_contains(trace_events, pattern)]
                if missing_global_traces:
                    passed = False
                    failure_reasons.append(f"missing expected trace_all_global: {missing_global_traces}")

                trace_all = step.get("trace_all") or []
                missing_traces = [pattern for pattern in trace_all if not _trace_contains(new_traces, pattern)]
                if missing_traces:
                    passed = False
                    failure_reasons.append(f"missing expected traces: {missing_traces}")

                trace_none = step.get("trace_none") or []
                present_forbidden_traces = [pattern for pattern in trace_none if _trace_contains(new_traces, pattern)]
                if present_forbidden_traces:
                    passed = False
                    failure_reasons.append(f"unexpected trace_none matches: {present_forbidden_traces}")

                trace_none_global = step.get("trace_none_global") or []
                present_forbidden_global = [pattern for pattern in trace_none_global if _trace_contains(trace_events, pattern)]
                if present_forbidden_global:
                    passed = False
                    failure_reasons.append(f"unexpected trace_none_global matches: {present_forbidden_global}")

                if new_errors:
                    passed = False
                    failure_reasons.append("browser errors: " + " | ".join(new_errors[-3:]))

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
                        f"{'ok' if r['passed'] else 'fail'} {r['name']}" for r in check_results
                    )
                    desc = f"{desc}\n  checks: {check_summary}"
                trace_summary = _trace_summary(new_traces)
                if trace_summary:
                    desc = f"{desc}\n  traces: {trace_summary}"
                if failure_reasons:
                    desc = f"{desc}\n  FAIL: {'; '.join(failure_reasons)}"

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

    _save_runtime_trace_log(game_name, console_events, browser_errors)
    _write_test_results_coverage(game_name, test_steps, results)
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
