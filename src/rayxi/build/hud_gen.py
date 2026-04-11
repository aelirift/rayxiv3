"""LLM-driven HUD widget generator for custom mechanic_spec HUD entities.

Replaces the previous hardcoded "if 'rage' in name: return rage_template" with
a generic LLM call that produces a complete Godot Control node GDScript from
the mechanic_spec.hud_entities declaration. Works for ANY feature: rage meters,
mana bars, combo gauges, stamina, super meters — zero code changes per feature.

Input to the LLM: the HudEntity spec fields (name, godot_node, reads, displays,
visual_states prose). Output: a full GDScript file that extends Control, reads
from the fighter(s), overrides _draw() to render according to visual_states.

Cache key: hash of the spec fields. Reruns with the same spec hit cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from rayxi.llm.callers import build_callers, build_router
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from rayxi.spec.models import GameIdentity, MechanicHudEntity, MechanicSpec

_log = logging.getLogger("rayxi.build.hud_gen")
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "hud_gen"


_HUD_WIDGET_SYSTEM_PROMPT = """\
You are a Godot 4.4 GDScript engineer. Your job is to write ONE complete Control \
node script that renders a HUD widget for a fighting-game (or similar action-game) \
custom feature.

You will receive a HUD entity declaration from the game's HLR mechanic spec:
  - name:           the widget's node name (used for references)
  - godot_node:     base class (typically "Control")
  - displays:       prose description of what the widget shows
  - reads:          property names the widget reads from the fighter each frame
  - visual_states:  prose contract describing what the widget must look like for \
every possible state of the underlying data (e.g. "0 stacks = all dim, 1 stack = \
one lit, 2 stacks = two lit, 3 stacks = all lit + pulsing")

You will also receive the fighter's relevant config constants (max_rage_stacks, \
rage_fill_threshold, etc.) if this is a resource meter — use them to size the widget.

## Requirements for the output GDScript

1. `extends <godot_node>` (e.g. `extends Control`)
2. An `@export var fighter_path: NodePath` so the scene can wire the widget to p1 or p2
3. `_ready()`: resolves fighter_path → `var fighter: Node`, sets `custom_minimum_size`
4. `_process(_delta)`: calls `queue_redraw()` so the widget updates every frame
5. `_draw()`: reads the listed properties from `fighter` via `fighter.get("property_name")`, \
renders the widget exactly according to the visual_states contract
6. Use `draw_rect`, `draw_circle`, `draw_line`, `draw_string` — stay within Godot's basic 2D draw API
7. Include @export vars for all visual knobs (segment colors, sizes, spacing) with sensible defaults \
so the scene can override them
8. NEVER reference mechanic systems directly — read only from `fighter.<property>`
9. Animate time-varying visuals (pulse at max, flash on change) using `Engine.get_physics_frames()` \
or an internal counter — do not rely on tweens or AnimationPlayer

## Output format

Output ONLY raw GDScript. No markdown, no code fences, no prose, no leading blank lines. \
The first line must be `extends <node_class>`. The file will be saved directly as a .gd file \
and must compile in Godot 4.4.
"""


def _cache_key(entity: MechanicHudEntity, constants: dict) -> str:
    payload = json.dumps(
        {"e": entity.model_dump(), "c": constants},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    path = _CACHE_DIR / f"{key}.gd"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _cache_put(key: str, gd: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.gd").write_text(gd, encoding="utf-8")


async def _generate_widget_via_llm(
    entity: MechanicHudEntity,
    constants: dict,
    caller: LLMCaller,
) -> str:
    """Generate one HUD widget GDScript file via LLM. Cached by spec hash."""
    key = _cache_key(entity, constants)
    cached = _cache_get(key)
    trace = get_trace()
    caller_name = type(caller).__name__
    label = f"hud_widget[{entity.name}]"
    if cached:
        _log.info("%s — cache hit", label)
        if trace:
            cid = trace.llm_start("codegen", label, caller_name, 0)
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return cached

    prompt_parts = [
        "## HUD Entity Spec",
        f"```json\n{json.dumps(entity.model_dump(), indent=2)}\n```",
    ]
    if constants:
        prompt_parts.append("## Relevant constants (use these for sizing/thresholds)")
        prompt_parts.append(f"```json\n{json.dumps(constants, indent=2)}\n```")

    prompt = "\n\n".join(prompt_parts)

    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("codegen", label, caller_name, len(prompt)) if trace else ""
        try:
            raw = await caller(_HUD_WIDGET_SYSTEM_PROMPT, prompt, json_mode=False, label=label)
            gd = raw.strip()
            # Strip any accidental markdown fences the LLM might add
            if gd.startswith("```"):
                lines = gd.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                gd = "\n".join(lines).strip()
            if not gd.startswith("extends"):
                raise RuntimeError(f"output does not start with 'extends': {gd[:80]!r}")
            _cache_put(key, gd)
            if trace:
                trace.llm_end(cid, output_chars=len(gd))
            return gd
        except Exception as exc:
            last_err = exc
            if trace:
                trace.llm_end(cid, output_chars=0, error=str(exc)[:120])
            _log.warning("%s attempt %d failed: %s", label, attempt + 1, str(exc)[:120])
    raise RuntimeError(f"{label} failed after 3 attempts: {last_err}")


def _flatten_constants_for_system(constants_path: Path | None, system_name: str) -> dict:
    if not constants_path or not constants_path.exists():
        return {}
    raw = json.loads(constants_path.read_text())
    bucket = raw.get(system_name) or {}
    if isinstance(bucket, dict) and "constants" in bucket and isinstance(bucket["constants"], list):
        return {c["name"]: c.get("value") for c in bucket["constants"] if c.get("name")}
    if isinstance(bucket, dict):
        return {name: info.get("value") if isinstance(info, dict) else info
                for name, info in bucket.items()}
    return {}


async def generate_custom_hud_widgets(
    hlr: GameIdentity,
    output_scripts_dir: Path,
    constants_path: Path | None = None,
    caller: LLMCaller | None = None,
) -> list[Path]:
    """Generate a .gd file for every mechanic_spec.hud_entities entry via LLM.

    No hardcoded widget templates. Every widget is produced from its spec.
    """
    output_scripts_dir.mkdir(parents=True, exist_ok=True)
    if caller is None:
        router = build_router(build_callers())
        caller = router.get("mlr_interactions")

    written: list[Path] = []
    for spec in hlr.mechanic_specs:
        constants = _flatten_constants_for_system(constants_path, spec.system_name)
        for widget in spec.hud_entities:
            gd = await _generate_widget_via_llm(widget, constants, caller)
            out = output_scripts_dir / f"{widget.name}.gd"
            out.write_text(gd, encoding="utf-8")
            written.append(out)
            _log.info("hud_gen: wrote %s (%d bytes)", out, len(gd))
    return written


def generate_custom_hud_widgets_sync(
    hlr: GameIdentity,
    output_scripts_dir: Path,
    constants_path: Path | None = None,
) -> list[Path]:
    """Sync wrapper for contexts that can't await (e.g., inside run_spec.py)."""
    return asyncio.run(generate_custom_hud_widgets(hlr, output_scripts_dir, constants_path))
