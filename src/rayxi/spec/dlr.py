"""DLR — Detail-Level Requirements phase.

Takes MLR products and fills in every actual value:
  - Property values (health=1000, position=Vector2(200,380), speed=5.0)
  - Key bindings (A=walk_left, U=light_punch, QCF+punch=hadouken)
  - Frame data (startup=7, active=3, recovery=12, damage=50)
  - Timers (round_time=99, ko_delay=2.5)
  - Physics (gravity=0.8, jump_velocity=-16, floor_y=380)

Sources values from KB game data JSON (frame data, character stats).
Cannot create new objects or categories — only adds detail to MLR declarations.

Decomposed like MLR:
  - One call per entity (fill property values + action details)
  - One call per system interaction set (fill effect values)
  - All calls parallelized

Each call is cached by content hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from rayxi.knowledge import KnowledgeBase
from rayxi.llm.callers import CallerRouter, call_type_for_entity
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from .mlr import SceneMLR
from .models import GameIdentity

_log = logging.getLogger("rayxi.spec.dlr")
_KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "knowledge"
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "dlr"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_key(system: str, prompt: str) -> str:
    return hashlib.sha256((system + prompt).encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _cache_put(key: str, data: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(data, encoding="utf-8")


async def _call_llm(caller: LLMCaller, system: str, prompt: str, label: str) -> str:
    """Call LLM with cache + retry logic. label flows to caller for pool stats."""
    trace = get_trace()
    caller_name = type(caller).__name__
    key = _cache_key(system, prompt)
    cached = _cache_get(key)
    if cached is not None:
        _log.info("%s — cache hit (%s)", label, key)
        if trace:
            cid = trace.llm_start("dlr", label, caller_name, len(system) + len(prompt))
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return cached
    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("dlr", label, caller_name, len(system) + len(prompt)) if trace else ""
        try:
            raw = await caller(system, prompt, json_mode=True, label=label)
            json.loads(raw)
            _cache_put(key, raw)
            if trace:
                trace.llm_end(cid, output_chars=len(raw))
            return raw
        except (json.JSONDecodeError, RuntimeError, Exception) as exc:
            last_err = exc
            if trace:
                trace.llm_end(cid, output_chars=0, error=str(exc)[:120])
            _log.warning("%s attempt %d failed: %s", label, attempt + 1, str(exc)[:120])
    raise RuntimeError(f"{label} failed after 3 attempts: {last_err}")


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

ENTITY_DETAIL_SYSTEM_PROMPT = """\
You are a game implementation architect. Your job is to fill in ALL actual values \
for ONE entity within ONE scene.

You will receive:
1. The entity's MLR spec (properties declared with types but NO values)
2. The entity's action_sets (what it can do)
3. KB game data (frame data, character stats, physics constants)
4. The HLR for global context

For every property, provide the actual value. For every action, provide the detail.

Output a JSON object:
{
  "scene_name": "string",
  "entity_name": "string",
  "property_values": {
    "property_name": "actual_value as string"
  },
  "action_details": [
    {
      "action_name": "string",
      "category": "string",
      "key_binding": "string or null — the physical key (e.g. U, I, O, J, K, L, SPACE)",
      "input_sequence": "string or null — for special moves (e.g. down, down_fwd, fwd + U)",
      "frame_data": {
        "startup": "int or null",
        "active": "int or null",
        "recovery": "int or null"
      },
      "damage": "int or null",
      "description": "string — what this action does with specific values"
    }
  ],
  "physics": {
    "walk_speed": "float or null",
    "jump_velocity": "float or null",
    "gravity": "float or null"
  }
}

Rules:
- Use KB game data as the source of truth for values. Adapt from the reference game, don't copy blindly.
- Every declared property MUST have a value. No nulls for required properties.
- Key bindings: use the actual keyboard keys (A/D/W/S for movement, U/I/O for punches, J/K/L for kicks, SPACE for confirm).
- Frame data at 60fps. If KB doesn't specify, use reasonable defaults for the attack strength.
- For non-character entities (HUD, backgrounds), physics and frame_data can be null/empty.
- AI entities: no key bindings (CPU uses random action selection).
- Output ONLY the JSON. No markdown, no explanation.
"""

MECHANIC_CONSTANTS_SYSTEM_PROMPT = """\
You are a game balance engineer. Your job is to fill in concrete numeric values for \
the constants that drive a custom (non-template) game mechanic.

You will receive:
1. The full mechanic spec (summary, properties, hud_entities, interactions, constants_for_dlr)
2. The HLR game context (game_name, genre, global_rules, relevant enums)
3. KB game data for the genre (frame data, balance values, colors)

For EVERY constant listed in constants_for_dlr, provide a concrete value consistent \
with the game's balance and the mechanic's intent.

Output a JSON object:
{
  "system_name": "string — must match the mechanic_spec system_name",
  "constants": [
    {
      "name": "string — must match one constant_for_dlr.name",
      "type": "string — int, float, bool, string, hex_color, or Vector2",
      "value": "string — the actual concrete value (e.g. '3', '0.25', '1.75', '#ff4400', '20')",
      "unit": "string — hp, frames, pixels, ratio, hex, or empty",
      "rationale": "string — one sentence explaining why this value balances the mechanic"
    }
  ]
}

Rules:
- EVERY constant from the mechanic_spec.constants_for_dlr list MUST appear in output.constants.
- Use the value_hint in each constant_for_dlr as a starting point, refine with KB data where applicable.
- Numeric constants: match the game's balance (e.g. fighting game health ~1000, frames at 60fps).
- For color constants, use hex codes like "#ff4400".
- The constant names in your output MUST match the spec exactly — do not rename or abbreviate.
- Output ONLY the JSON object. No markdown, no explanation.
"""


INTERACTION_DETAIL_SYSTEM_PROMPT = """\
You are a game implementation architect. Your job is to fill in ALL actual values \
for interactions within ONE game system in ONE scene.

You will receive:
1. The system's MLR interaction specs (qualitative effects with structured verbs)
2. KB game data (damage values, hitstop frames, stun values)
3. The scene's entity list with their property declarations
4. The HLR for global context

For every effect, provide the actual numeric value.

Output a JSON object:
{
  "scene_name": "string",
  "game_system": "string",
  "interaction_details": [
    {
      "trigger": "string — from MLR",
      "condition": "string — from MLR",
      "effect_details": [
        {
          "verb": "string — from MLR",
          "target": "string — from MLR",
          "value": "string — the ACTUAL value (e.g. '10', '0.25', 'true', 'hit_stun')",
          "unit": "string — frames, pixels, hp, seconds, percent, or empty",
          "description": "string — precise description with numbers"
        }
      ]
    }
  ]
}

Rules:
- Use KB game data as source of truth. Adapt values to match the game's balance.
- Every effect MUST have a concrete value. No placeholders.
- Units matter: specify frames (at 60fps), pixels, hp, seconds, or percent.
- For hitstop: use KB values (light=5, medium=8, heavy=12, special=10 frames).
- For block damage: typically 25% of full damage.
- For stun: use KB values if available, else reasonable defaults.
- Output ONLY the JSON. No markdown, no explanation.
"""


# ---------------------------------------------------------------------------
# DLR result containers
# ---------------------------------------------------------------------------

class EntityDetail:
    def __init__(self, scene_name: str, entity_name: str, data: dict) -> None:
        self.scene_name = scene_name
        self.entity_name = entity_name
        self.data = data

    def to_dict(self) -> dict:
        return self.data


class InteractionDetail:
    def __init__(self, scene_name: str, game_system: str, data: dict) -> None:
        self.scene_name = scene_name
        self.game_system = game_system
        self.data = data

    def to_dict(self) -> dict:
        return self.data


class SceneDLR:
    def __init__(self, scene_name: str) -> None:
        self.scene_name = scene_name
        self.entity_details: list[EntityDetail] = []
        self.interaction_details: list[InteractionDetail] = []

    def to_dict(self) -> dict:
        return {
            "scene_name": self.scene_name,
            "entity_details": [e.to_dict() for e in self.entity_details],
            "interaction_details": [i.to_dict() for i in self.interaction_details],
        }


# ---------------------------------------------------------------------------
# Per-scene worker
# ---------------------------------------------------------------------------

async def _build_scene_dlr(
    scene_mlr: SceneMLR,
    hlr: GameIdentity,
    router: CallerRouter,
    kb_game_data_text: str,
) -> SceneDLR:
    """Build DLR for one scene. Routes entity calls by object_type.

    Simple entities (hud, background, ui) → fast caller (MiniMax)
    Characters, game_objects, interactions → primary (Claude CLI)
    """
    scene_dlr = SceneDLR(scene_mlr.scene_name)
    sn = scene_mlr.scene_name

    # Build context shared across calls
    hlr_summary = json.dumps({
        "game_name": hlr.game_name,
        "genre": hlr.genre,
        "player_mode": hlr.player_mode,
        "global_rules": hlr.global_rules,
        "enums": {e.name: e.values for e in hlr.enums},
    }, indent=2)

    entity_summary = json.dumps(
        [{"name": e.entity_name, "type": e.object_type, "properties": [p.name for p in e.properties]}
         for e in scene_mlr.entities],
        indent=2,
    )

    base_ctx = f"## HLR Summary\n```json\n{hlr_summary}\n```\n\n## KB Game Data\n```json\n{kb_game_data_text}\n```"

    # Entity detail calls — all in parallel
    async def _do_entity(entity) -> None:
        call_type = call_type_for_entity("dlr", entity.object_type)
        caller = router.get(call_type)
        entity_json = json.dumps({
            "entity_name": entity.entity_name,
            "object_type": entity.object_type,
            "properties": [p.model_dump() for p in entity.properties],
            "action_sets": [a.model_dump() for a in entity.action_sets],
        }, indent=2)
        prompt = f"{base_ctx}\n\n## Entity MLR Spec\n```json\n{entity_json}\n```\n\n## Scene: {sn}"
        label = f"dlr_entity_{entity.object_type}[{sn}/{entity.entity_name}]"
        raw = await _call_llm(caller, ENTITY_DETAIL_SYSTEM_PROMPT, prompt, label)
        parsed = json.loads(raw)
        scene_dlr.entity_details.append(EntityDetail(sn, entity.entity_name, parsed))
        _log.info("DLR: %s/%s — %d property values, %d action details",
                   sn, entity.entity_name,
                   len(parsed.get("property_values", {})),
                   len(parsed.get("action_details", [])))

    # Interaction detail calls — all in parallel → primary
    async def _do_interactions(si) -> None:
        caller = router.get("dlr_interactions")
        si_json = json.dumps({
            "game_system": si.game_system,
            "interactions": [i.model_dump() for i in si.interactions],
        }, indent=2)
        prompt = (
            f"{base_ctx}\n\n## Scene: {sn}\n\n"
            f"## Entity List\n```json\n{entity_summary}\n```\n\n"
            f"## System Interactions MLR Spec\n```json\n{si_json}\n```"
        )
        label = f"dlr_interactions[{sn}/{si.game_system}]"
        raw = await _call_llm(caller, INTERACTION_DETAIL_SYSTEM_PROMPT, prompt, label)
        parsed = json.loads(raw)
        scene_dlr.interaction_details.append(InteractionDetail(sn, si.game_system, parsed))
        _log.info("DLR: %s/%s — %d interaction details",
                   sn, si.game_system, len(parsed.get("interaction_details", [])))

    tasks: list = []
    for entity in scene_mlr.entities:
        tasks.append(_do_entity(entity))
    for si in scene_mlr.system_interactions:
        tasks.append(_do_interactions(si))

    if tasks:
        await asyncio.gather(*tasks)

    _log.info("DLR: %s — complete (%d entity details, %d interaction details)",
               sn, len(scene_dlr.entity_details), len(scene_dlr.interaction_details))
    return scene_dlr


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def fill_mechanic_constants(
    hlr: GameIdentity,
    router: CallerRouter,
    kb_game_data_text: str = "{}",
) -> dict:
    """Fill concrete values for every mechanic_spec's constants_for_dlr list.

    Returns a dict keyed by system_name:
        {"rage_meter_system": {"constants": [{name, type, value, unit, rationale}, ...]}}
    One LLM call per mechanic_spec. Missing constants from a mechanic_spec are an error.
    """
    if not hlr.mechanic_specs:
        return {}

    hlr_ctx = json.dumps({
        "game_name": hlr.game_name,
        "genre": hlr.genre,
        "player_mode": hlr.player_mode,
        "global_rules": hlr.global_rules,
        "enums": {e.name: e.values for e in hlr.enums},
    }, indent=2)

    results: dict = {}
    caller = router.get("dlr_interactions")

    async def _fill(spec) -> None:
        spec_json = json.dumps(spec.model_dump(), indent=2)
        prompt = (
            f"## HLR\n```json\n{hlr_ctx}\n```\n\n"
            f"## KB Game Data\n```json\n{kb_game_data_text}\n```\n\n"
            f"## Mechanic Spec\n```json\n{spec_json}\n```"
        )
        label = f"dlr_mechanic_constants[{spec.system_name}]"
        raw = await _call_llm(caller, MECHANIC_CONSTANTS_SYSTEM_PROMPT, prompt, label)
        parsed = json.loads(raw)
        results[spec.system_name] = parsed
        filled = {c["name"] for c in parsed.get("constants", [])}
        required = {c.name for c in spec.constants_for_dlr}
        missing = required - filled
        if missing:
            _log.warning("DLR: %s — missing constants: %s", spec.system_name, missing)
        _log.info("DLR: %s — %d/%d constants filled",
                  spec.system_name, len(filled & required), len(required))

    await asyncio.gather(*[_fill(m) for m in hlr.mechanic_specs])
    return results


async def run_dlr(
    hlr: GameIdentity,
    scene_mlrs: list[SceneMLR],
    router: CallerRouter,
    knowledge_dir: Path | None = None,
) -> list[SceneDLR]:
    """Run DLR: fill in all actual values for MLR products.

    This is where the KB game data JSON is consumed (frame data, character stats).
    """
    kb = KnowledgeBase(knowledge_dir or _KNOWLEDGE_DIR)
    kb_context = kb.retrieve_context(hlr.game_name)
    if kb_context.is_empty():
        kb_context = kb.retrieve_context(hlr.genre)

    # DLR gets the full game data JSON — this is where frame data matters
    kb_game_data_text = json.dumps(kb_context.game_data, indent=2) if kb_context.game_data else "{}"
    _log.info("DLR: KB game data = %d chars", len(kb_game_data_text))

    # All scenes in parallel
    _log.info("DLR: launching %d scenes in parallel", len(scene_mlrs))
    tasks = [
        _build_scene_dlr(scene_mlr, hlr, router, kb_game_data_text)
        for scene_mlr in scene_mlrs
    ]
    results = await asyncio.gather(*tasks)
    return list(results)
