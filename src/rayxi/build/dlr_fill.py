"""DLR Fill — populates DAG leaf values from KB game data and LLM.

Two-pass approach:
  Pass 1 (deterministic): Read KB game data JSON, match properties by name,
    fill config values directly. No LLM needed.
  Pass 2 (LLM): For remaining unfilled properties, batch them per entity
    and ask the LLM to fill values using KB context.

The DAG defines WHAT needs values. This module fills them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from rayxi.knowledge import KnowledgeBase
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from .dag import EntityNode, GameDAG, PropertyNode

_log = logging.getLogger("rayxi.build.dlr_fill")
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "dlr_fill"


# ---------------------------------------------------------------------------
# Pass 1: KB game data fill (deterministic)
# ---------------------------------------------------------------------------

def _match_kb_value(prop_name: str, char_data: dict, global_data: dict) -> str | None:
    """Try to find a value for a property name in KB data."""
    # Direct match in character data
    if prop_name in char_data:
        val = char_data[prop_name]
        if isinstance(val, dict):
            return json.dumps(val)
        return str(val)

    # Match in normals (e.g. light_punch_damage → normals.light_punch.damage)
    for attack_name in ("light_punch", "medium_punch", "heavy_punch",
                         "light_kick", "medium_kick", "heavy_kick"):
        if prop_name.startswith(attack_name + "_"):
            suffix = prop_name[len(attack_name) + 1:]
            normals = char_data.get("normals", {})
            attack_data = normals.get(attack_name, {})
            if suffix in attack_data:
                return str(attack_data[suffix])

    # Match crouch/jump variants
    for prefix in ("crouch_", "jump_"):
        for attack_name in ("light_punch", "medium_punch", "heavy_punch",
                             "light_kick", "medium_kick", "heavy_kick"):
            full_name = prefix + attack_name
            if prop_name.startswith(full_name + "_"):
                suffix = prop_name[len(full_name) + 1:]
                normals = char_data.get("normals", {})
                attack_data = normals.get(full_name, {})
                if suffix in attack_data:
                    return str(attack_data[suffix])

    # Match special moves (e.g. hadouken_damage → specials.hadouken.damage)
    specials = char_data.get("specials", {})
    for move_name, move_data in specials.items():
        if prop_name.startswith(move_name + "_") and isinstance(move_data, dict):
            suffix = prop_name[len(move_name) + 1:]
            if suffix in move_data:
                return str(move_data[suffix])

    # Match in global data
    if prop_name in global_data:
        val = global_data[prop_name]
        if isinstance(val, dict):
            return json.dumps(val)
        return str(val)

    # Nested global (e.g. hitstop_light → hitstop_frames.light)
    for key, val in global_data.items():
        if isinstance(val, dict):
            for sub_key, sub_val in val.items():
                if prop_name == f"{key}_{sub_key}":
                    return str(sub_val)

    return None


def fill_dag_from_kb(dag: GameDAG, hlr, knowledge_dir: Path) -> int:
    """Pass 1: fill DAG properties from KB game data. Returns count filled."""
    trace = get_trace()
    kb = KnowledgeBase(knowledge_dir)
    kb_ctx = kb.retrieve_context(hlr.game_name)
    if kb_ctx.is_empty():
        kb_ctx = kb.retrieve_context(hlr.genre)

    game_data = kb_ctx.game_data or {}
    global_data = game_data.get("global", {})
    characters_data = game_data.get("characters", {})

    filled = 0

    # Fill fighter entities
    for char_name, entity in dag.fighter_entities.items():
        char_data = characters_data.get(char_name, {})
        for prop in entity.properties:
            if prop.is_filled:
                continue
            val = _match_kb_value(prop.name, char_data, global_data)
            if val is not None:
                if prop.category == "config":
                    prop.value = val
                elif prop.category == "state":
                    prop.initial = val
                filled += 1

    # Fill game properties
    for prop in dag.game_properties:
        if prop.is_filled:
            continue
        val = _match_kb_value(prop.name, {}, global_data)
        if val is not None:
            if prop.category == "config":
                prop.value = val
            elif prop.category == "state":
                prop.initial = val
            filled += 1

    # Fill projectile entities
    for proj_name, entity in dag.projectile_entities.items():
        # Try to find projectile data in specials
        for char_data in characters_data.values():
            specials = char_data.get("specials", {})
            for move_name, move_data in specials.items():
                if isinstance(move_data, dict) and move_data.get("is_projectile"):
                    for prop in entity.properties:
                        if prop.is_filled:
                            continue
                        if prop.name in move_data:
                            if prop.category == "config":
                                prop.value = str(move_data[prop.name])
                            filled += 1

    _log.info("KB fill: %d properties filled from game data", filled)
    if trace:
        trace.event("dlr", "kb_fill", filled=filled)

    return filled


# ---------------------------------------------------------------------------
# Pass 2: LLM fill for remaining unfilled
# ---------------------------------------------------------------------------

def _cache_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _cache_put(key: str, data: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(data, encoding="utf-8")


DLR_FILL_SYSTEM_PROMPT = """\
You are a game data specialist. Fill in the missing property values for a game entity.

You will receive:
1. The entity name and role
2. A list of unfilled properties with their names, types, and purposes
3. KB game data for reference

For EACH unfilled property, provide a reasonable value based on the game data.
Use the KB data as source of truth. If no exact match, use reasonable defaults for the genre.

Output a JSON object: {"values": {"property_name": "value", ...}}
Values must match the declared type (int→number, float→number, bool→true/false, string→text, Vector2→"Vector2(x,y)").
Output ONLY the JSON. No markdown, no explanation.
"""


async def fill_dag_from_llm(dag: GameDAG, hlr, caller: LLMCaller) -> int:
    """Pass 2: fill remaining unfilled properties via LLM. Returns count filled."""
    trace = get_trace()
    filled = 0

    # Collect unfilled properties per entity
    tasks = []

    for char_name, entity in dag.fighter_entities.items():
        unfilled = [p for p in entity.properties if not p.is_filled and p.category != "derived"]
        if unfilled:
            tasks.append(_fill_entity_llm(entity, unfilled, hlr, caller))

    for proj_name, entity in dag.projectile_entities.items():
        unfilled = [p for p in entity.properties if not p.is_filled and p.category != "derived"]
        if unfilled:
            tasks.append(_fill_entity_llm(entity, unfilled, hlr, caller))

    # Game properties
    unfilled_game = [p for p in dag.game_properties if not p.is_filled and p.category != "derived"]
    if unfilled_game:
        game_entity = EntityNode(name="game", role="game", from_enum="")
        game_entity.properties = dag.game_properties
        tasks.append(_fill_entity_llm(game_entity, unfilled_game, hlr, caller))

    if tasks:
        results = await asyncio.gather(*tasks)
        filled = sum(results)

    _log.info("LLM fill: %d properties filled", filled)
    if trace:
        trace.event("dlr", "llm_fill", filled=filled)

    return filled


async def _fill_entity_llm(
    entity: EntityNode,
    unfilled: list[PropertyNode],
    hlr,
    caller: LLMCaller,
) -> int:
    """Fill unfilled properties for one entity via LLM."""
    trace = get_trace()

    # Build the prompt
    props_desc = []
    for p in unfilled:
        props_desc.append(f"  {p.name} ({p.type}): {p.purpose}")

    prompt = (
        f"Entity: {entity.name} (role: {entity.role})\n"
        f"Game: {hlr.game_name} ({hlr.genre})\n\n"
        f"Unfilled properties ({len(unfilled)}):\n"
        + "\n".join(props_desc)
    )

    label = f"dlr_fill[{entity.name}]"
    key = _cache_key(DLR_FILL_SYSTEM_PROMPT + prompt)
    cached = _cache_get(key)

    if cached:
        _log.info("%s — cache hit", label)
        raw = cached
    else:
        caller_name = type(caller).__name__
        cid = trace.llm_start("dlr", label, caller_name, len(prompt)) if trace else ""
        try:
            raw = await caller(DLR_FILL_SYSTEM_PROMPT, prompt, json_mode=True, label=label)
            _cache_put(key, raw)
            if trace:
                trace.llm_end(cid, output_chars=len(raw))
        except Exception as exc:
            _log.warning("%s failed: %s", label, str(exc)[:120])
            if trace:
                trace.llm_end(cid, output_chars=0, error=str(exc)[:120])
            return 0

    try:
        parsed = json.loads(raw)
        values = parsed.get("values", parsed)  # handle both {"values": {...}} and flat {...}
    except json.JSONDecodeError:
        _log.warning("%s: invalid JSON response", label)
        return 0

    filled = 0
    for p in unfilled:
        if p.name in values:
            val = str(values[p.name])
            if p.category == "config":
                p.value = val
            elif p.category == "state":
                p.initial = val
            filled += 1

    _log.info("%s: filled %d/%d properties", label, filled, len(unfilled))
    return filled
