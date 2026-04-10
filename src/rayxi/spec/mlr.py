"""MLR — Mid-Level Requirements phase.

Decomposes each HLR scene into small, focused LLM calls:
  1. _fsm.json — scene sub-states and transitions
  2. _collisions.json — collision pairs
  3. _interactions_{system}.json — one per game_system enum value
  4. {entity}.json — one per entity relevant to this scene

Each call is small and independently validatable.
The orchestrator iterates HLR enums to determine what calls to make.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from rayxi.knowledge import KnowledgeBase, KnowledgeContext
from rayxi.llm.callers import CallerRouter, call_type_for_entity
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from .models import (
    EntitySpec,
    GameIdentity,
    SceneCollisions,
    SceneFSM,
    SceneListEntry,
    SystemInteractions,
)

_log = logging.getLogger("rayxi.spec.mlr")
_KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "knowledge"
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "mlr"


def _cache_key(system: str, prompt: str) -> str:
    h = hashlib.sha256((system + prompt).encode()).hexdigest()[:16]
    return h


def _cache_get(key: str) -> str | None:
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _cache_put(key: str, data: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(data, encoding="utf-8")


# ---------------------------------------------------------------------------
# System prompts — one per file type
# ---------------------------------------------------------------------------

FSM_SYSTEM_PROMPT = """\
You are a game design architect. Produce the scene FSM (sub-states and transitions) \
for ONE scene of a game.

You will receive the full HLR, KB context, and which scene to detail.

Output a JSON object:
{
  "scene_name": "string",
  "fsm_state": "string — the global FSM state",
  "states": ["string — sub-states within this scene"],
  "transitions": ["STATE_A -> STATE_B: on condition"]
}

Rules:
- States are sub-states WITHIN this scene, not global FSM states.
- Every state must be reachable and have an exit (except terminal states that transition out of the scene).
- If the KB has a process doc, follow it for this scene's flow.
- Output ONLY the JSON. No markdown, no explanation.
"""

COLLISIONS_SYSTEM_PROMPT = """\
You are a game design architect. List ALL collision pairs for ONE scene of a game.

You will receive the full HLR, KB context, the scene FSM, and which scene to detail.

Output a JSON object:
{
  "scene_name": "string",
  "collision_pairs": [
    {"object_a": "string", "object_b": "string", "result": "string — what happens"}
  ]
}

Rules:
- Only list pairs that are relevant to this scene.
- Use generic object type names (e.g. "fighter_hitbox", "fighter_hurtbox", "projectile", "wall").
- result is qualitative — WHAT happens, not HOW MUCH.
- If this scene has no collisions (e.g. a menu), return an empty list.
- Output ONLY the JSON. No markdown, no explanation.
"""

INTERACTIONS_SYSTEM_PROMPT = """\
You are a game design architect. List ALL interactions for ONE game system within \
ONE scene of a game.

You will receive the full HLR, KB context, scene FSM, collision pairs, and which \
game system to detail.

Output a JSON object:
{
  "scene_name": "string",
  "game_system": "string — the system being detailed",
  "interactions": [
    {
      "trigger": "string — what starts this interaction",
      "condition": "string — guard condition",
      "effects": [
        {
          "verb": "subtract|add|spawn|destroy|set_state|apply|move|reset|increment|decrement|enable|disable",
          "target": "object.property",
          "description": "string — what this does (qualitative, no values)"
        }
      ]
    }
  ]
}

## Allowed property names (effect targets MUST use these)
{allowed_properties}

Rules:
- ONLY list interactions for the specified game system. Ignore other systems.
- Effects use structured verbs. Target must be object.property format.
- The property in each target MUST be from the allowed property names above. Do NOT invent new property names.
- Descriptions are qualitative — no numbers, no frame counts, no pixel values. DLR fills those.
- If this system has no interactions in this scene, return an empty list.
- Output ONLY the JSON. No markdown, no explanation.
"""

ENTITY_SYSTEM_PROMPT = """\
You are a game design architect. Produce the entity specification for ONE object \
within ONE scene of a game.

You will receive the full HLR, KB context, scene FSM, and which entity to detail.

Output a JSON object:
{
  "scene_name": "string",
  "entity_name": "string",
  "parent_enum": "string — which HLR enum this belongs to",
  "object_type": "character|hud|effect|transient|background|ui",
  "godot_node_type": "string — the Godot node class to use. Pick from: Label, RichTextLabel, ProgressBar, TextureProgressBar, Sprite2D, AnimatedSprite2D, TextureRect, ColorRect, Button, Panel, Control, NinePatchRect, Area2D, CharacterBody2D, StaticBody2D, RigidBody2D, Node2D, Camera2D, CanvasLayer",
  "properties": [
    {"name": "string", "type": "int|float|bool|string|Vector2", "description": "string"}
  ],
  "action_sets": [
    {
      "owner": "string — this entity name",
      "category": "string — action category",
      "actions": ["string — action names"]
    }
  ],
  "scene_enums": [
    {"name": "string", "values": ["string"], "description": "string"}
  ]
}

## Allowed property names (declarations MUST use these for character/fighter entities)
{allowed_properties}

Rules:
- Properties are declarations only — name + type + description. NO values (DLR fills those).
- For character/fighter entities, property names MUST come from the allowed list above. Do NOT invent new property names.
- For HUD/UI/background entities, you may declare simple display properties (text, position, visible, color, size).
- For transient objects, include properties: spawned_by (string) and despawned_by (string).
- action_sets list what this entity CAN DO. Use shared categories from HLR enums where applicable.
- scene_enums enumerate sub-items of this entity (e.g. a character's animation states).
- AI behavior is RANDOM actions from the action enums. No decision logic.
- If this entity is simple (background, static UI), action_sets and scene_enums can be empty.
- Output ONLY the JSON. No markdown, no explanation.
"""


# ---------------------------------------------------------------------------
# MLR result container
# ---------------------------------------------------------------------------

class SceneMLR:
    """All MLR products for one scene."""
    def __init__(self, scene_name: str) -> None:
        self.scene_name = scene_name
        self.fsm: SceneFSM | None = None
        self.collisions: SceneCollisions | None = None
        self.system_interactions: list[SystemInteractions] = []
        self.entities: list[EntitySpec] = []

    def to_dict(self) -> dict:
        return {
            "scene_name": self.scene_name,
            "fsm": self.fsm.model_dump() if self.fsm else None,
            "collisions": self.collisions.model_dump() if self.collisions else None,
            "system_interactions": [si.model_dump() for si in self.system_interactions],
            "entities": [e.model_dump() for e in self.entities],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten_scenes(scenes: list[SceneListEntry]) -> list[SceneListEntry]:
    flat: list[SceneListEntry] = []
    for s in scenes:
        flat.append(s)
        if s.children:
            flat.extend(_flatten_scenes(s.children))
    return flat


def _build_allowed_properties_text(schema: object | None) -> str:
    """Build ALL allowed property names from template. Used for entity prompts."""
    if not schema:
        return "(no template loaded — use best judgment)"
    lines: list[str] = []
    fighter_props = sorted({p.name for p in schema.fighter_schema.properties})
    for char_props in schema.per_character_unique.values():
        for p in char_props:
            if p.name not in fighter_props:
                fighter_props.append(p.name)
        break
    fighter_props.sort()
    game_props = sorted({
        p.name for p in schema.game_config + schema.game_state + schema.game_derived
    })
    lines.append(f"**fighter/character**: {', '.join(fighter_props)}")
    lines.append(f"**game**: {', '.join(game_props)}")
    if schema.projectile_schema.properties:
        proj_props = sorted({p.name for p in schema.projectile_schema.properties})
        lines.append(f"**projectile**: {', '.join(proj_props)}")
    return "\n".join(lines)


def _build_system_scoped_properties(schema: object | None, system_name: str) -> str:
    """Build property names scoped to ONE system — only what it reads/writes.

    Much smaller than the full list. Grouped by role and read/write direction.
    """
    if not schema:
        return "(no template loaded — use best judgment)"

    reads: dict[str, list[str]] = {}   # role → [prop_names]
    writes: dict[str, list[str]] = {}

    def _scan_role(role_name: str, props) -> None:
        for p in props:
            if system_name in (p.read_by if isinstance(p.read_by, list) else [p.read_by]):
                reads.setdefault(role_name, []).append(p.name)
            if system_name in (p.written_by if isinstance(p.written_by, list) else [p.written_by]):
                writes.setdefault(role_name, []).append(p.name)

    _scan_role("fighter", schema.fighter_schema.properties)
    for char_props in schema.per_character_unique.values():
        _scan_role("fighter", char_props)
        break
    _scan_role("game", schema.game_config + schema.game_state + schema.game_derived)
    if schema.projectile_schema.properties:
        _scan_role("projectile", schema.projectile_schema.properties)
    if schema.hud_bar_schema.properties:
        _scan_role("hud_bar", schema.hud_bar_schema.properties)

    lines: list[str] = []
    all_roles = sorted(set(list(reads.keys()) + list(writes.keys())))
    for role in all_roles:
        r = sorted(set(reads.get(role, [])))
        w = sorted(set(writes.get(role, [])))
        if r:
            lines.append(f"  {role} READS: {', '.join(r)}")
        if w:
            lines.append(f"  {role} WRITES: {', '.join(w)}")

    if not lines:
        return f"(system '{system_name}' has no template properties — it may not need interactions)"

    total = sum(len(reads.get(r, [])) for r in all_roles) + sum(len(writes.get(r, [])) for r in all_roles)
    header = f"Properties for {system_name} ({total} total):"
    return header + "\n" + "\n".join(lines)


def _build_context(hlr: GameIdentity, kb_context: KnowledgeContext) -> str:
    parts = [f"## Game HLR\n```json\n{hlr.model_dump_json(indent=2)}\n```"]
    enum_lines = "## HLR Enums (you can ONLY reference these)\n"
    for e in hlr.enums:
        enum_lines += f"- {e.name}: {e.values}\n"
    parts.append(enum_lines)
    if not kb_context.is_empty():
        parts.append(f"## Knowledge Base Context\n{kb_context.to_prompt_text()}")
    return "\n\n".join(parts)


async def _call_llm(caller: LLMCaller, system: str, prompt: str, label: str) -> str:
    """Call LLM with cache + retry logic. label flows to caller for pool stats."""
    trace = get_trace()
    caller_name = type(caller).__name__
    key = _cache_key(system, prompt)
    cached = _cache_get(key)
    if cached is not None:
        _log.info("%s — cache hit (%s)", label, key)
        if trace:
            cid = trace.llm_start("mlr", label, caller_name, len(system) + len(prompt))
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return cached

    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("mlr", label, caller_name, len(system) + len(prompt)) if trace else ""
        try:
            raw = await caller(system, prompt, json_mode=True, label=label)
            json.loads(raw)  # validate it's parseable
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
# Per-scene entity list builder
# ---------------------------------------------------------------------------

def _entities_for_scene(hlr: GameIdentity, scene: SceneListEntry) -> list[tuple[str, str]]:
    """Determine which entities belong in this scene. Returns (entity_name, parent_enum).

    Only enums with entity=True are considered. The LLM flags this at HLR time.
    """
    entities: list[tuple[str, str]] = []

    gameplay_keywords = ("fight", "battle", "combat", "game", "play", "race", "duel", "match")
    is_gameplay = any(kw in scene.scene_name.lower() or kw in scene.purpose.lower()
                      for kw in gameplay_keywords)

    for enum_def in hlr.enums:
        if not enum_def.entity:
            continue

        for val in enum_def.values:
            if enum_def.name == "characters":
                if is_gameplay or "select" in scene.scene_name.lower():
                    entities.append((val, enum_def.name))
            elif enum_def.name == "stages":
                if is_gameplay or "intro" in scene.scene_name.lower():
                    entities.append((val, enum_def.name))
            else:
                if is_gameplay:
                    entities.append((val, enum_def.name))

    return entities


# ---------------------------------------------------------------------------
# Per-scene worker
# ---------------------------------------------------------------------------

async def _build_scene_mlr(
    scene: SceneListEntry,
    hlr: GameIdentity,
    router: CallerRouter,
    base_context: str,
    game_systems: list[str],
    interaction_prompts_by_system: dict[str, str] | None = None,
    entity_prompt: str = "",
    fsm_only: bool = False,
) -> SceneMLR:
    """Build all MLR products for one scene. Parallelizes within the scene.

    Routes each call type through the CallerRouter:
      FSM / interactions → primary (Claude CLI)
      collisions / simple entities → fast (MiniMax)

    If fsm_only=True, only generates FSM + collisions (interactions/entities
    will be filled deterministically by caller).
    """
    scene_mlr = SceneMLR(scene.scene_name)
    sn = scene.scene_name
    scene_ctx = f"{base_context}\n\n## Scene: {sn}\nPurpose: {scene.purpose}\nFSM state: {scene.fsm_state}"

    # Step 1: FSM first (everything else depends on it) → primary
    _log.info("MLR: %s — generating FSM", sn)
    fsm_caller = router.get("mlr_fsm")
    raw = await _call_llm(fsm_caller, FSM_SYSTEM_PROMPT, scene_ctx, f"mlr_fsm[{sn}]")
    scene_mlr.fsm = SceneFSM.model_validate(json.loads(raw))
    _log.info("MLR: %s — FSM: %d states", sn, len(scene_mlr.fsm.states))

    fsm_ctx = f"{scene_ctx}\n\n## Scene FSM\n```json\n{json.dumps(scene_mlr.fsm.model_dump(), indent=2)}\n```"

    # Step 2: Collisions + interactions + entities — all in parallel
    async def _do_collisions() -> None:
        _log.info("MLR: %s — generating collisions", sn)
        caller = router.get("mlr_collisions")
        raw = await _call_llm(caller, COLLISIONS_SYSTEM_PROMPT, fsm_ctx, f"mlr_collisions[{sn}]")
        scene_mlr.collisions = SceneCollisions.model_validate(json.loads(raw))
        _log.info("MLR: %s — %d collision pairs", sn, len(scene_mlr.collisions.collision_pairs))

    async def _do_interaction(system: str) -> None:
        _log.info("MLR: %s — generating interactions for '%s'", sn, system)
        caller = router.get("mlr_interactions")
        prompt = f"{fsm_ctx}\n\n## Game System to Detail: {system}"
        sys_prompt = (interaction_prompts_by_system or {}).get(system, INTERACTIONS_SYSTEM_PROMPT)
        raw = await _call_llm(caller, sys_prompt, prompt, f"mlr_interactions[{sn}/{system}]")
        si = SystemInteractions.model_validate(json.loads(raw))
        if si.interactions:
            scene_mlr.system_interactions.append(si)
            _log.info("MLR: %s/%s — %d interactions", sn, system, len(si.interactions))
        else:
            _log.info("MLR: %s/%s — no interactions (skipped)", sn, system)

    async def _do_entity(entity_name: str, parent_enum: str) -> None:
        _log.info("MLR: %s — generating entity '%s'", sn, entity_name)
        # Determine entity object_type from parent_enum to route correctly.
        # hud_elements → hud, stages → background, otherwise infer from parent enum name.
        etype_map = {"hud_elements": "hud", "stages": "background"}
        object_type = etype_map.get(parent_enum, "character")
        call_type = call_type_for_entity("mlr", object_type)
        caller = router.get(call_type)
        prompt = f"{fsm_ctx}\n\n## Entity to Detail\nName: {entity_name}\nParent enum: {parent_enum}"
        sys_prompt = entity_prompt or ENTITY_SYSTEM_PROMPT
        raw = await _call_llm(caller, sys_prompt, prompt, f"mlr_entity_{object_type}[{sn}/{entity_name}]")
        entity = EntitySpec.model_validate(json.loads(raw))
        scene_mlr.entities.append(entity)
        _log.info("MLR: %s/%s — %d properties, %d action_sets",
                   sn, entity_name, len(entity.properties), len(entity.action_sets))

    # Build task list
    tasks: list = [_do_collisions()]
    if not fsm_only:
        for system in game_systems:
            tasks.append(_do_interaction(system))
        for entity_name, parent_enum in _entities_for_scene(hlr, scene):
            tasks.append(_do_entity(entity_name, parent_enum))

    # Run all in parallel
    await asyncio.gather(*tasks)

    _log.info("MLR: %s — complete (%d entities, %d systems with interactions)",
               sn, len(scene_mlr.entities), len(scene_mlr.system_interactions))
    return scene_mlr


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run_mlr(
    hlr: GameIdentity,
    router: CallerRouter,
    knowledge_dir: Path | None = None,
    schema: object | None = None,
    fsm_only: bool = False,
) -> list[SceneMLR]:
    kb = KnowledgeBase(knowledge_dir or _KNOWLEDGE_DIR)
    kb_context = kb.retrieve_context(hlr.game_name)
    if kb_context.is_empty():
        kb_context = kb.retrieve_context(hlr.genre)

    _log.info("MLR: KB sources = %s", kb_context.source_names)

    game_systems = hlr.get_enum("game_systems")
    base_context = _build_context(hlr, kb_context)

    # Format entity prompt with full property list (entities need all props)
    props_text = _build_allowed_properties_text(schema)
    formatted_entity_prompt = ENTITY_SYSTEM_PROMPT.replace("{allowed_properties}", props_text)

    # Build per-system interaction prompts (scoped to each system's read/write props)
    interaction_prompts_by_system: dict[str, str] = {}
    for system in game_systems:
        scoped_props = _build_system_scoped_properties(schema, system)
        interaction_prompts_by_system[system] = INTERACTIONS_SYSTEM_PROMPT.replace(
            "{allowed_properties}", scoped_props,
        )
    all_scenes = _flatten_scenes(hlr.scenes)

    # All scenes in parallel
    _log.info("MLR: launching %d scenes in parallel", len(all_scenes))
    scene_tasks = [
        _build_scene_mlr(
            scene, hlr, router, base_context, game_systems,
            interaction_prompts_by_system=interaction_prompts_by_system,
            entity_prompt=formatted_entity_prompt,
            fsm_only=fsm_only,
        )
        for scene in all_scenes
    ]
    results = await asyncio.gather(*scene_tasks)

    return list(results)
