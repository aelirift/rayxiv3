"""HLR — High-Level Requirements phase.

Two-step process:
  1. Schema expansion — LLM proposes genre-specific fields beyond the base set.
  2. HLR generation — LLM fills in the expanded schema.

Input:  user prompt (e.g. "I want to build a Marvel vs Capcom")
Output: GameIdentity — base fields + dynamic extras.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from rayxi.knowledge import KnowledgeBase, KnowledgeContext
from rayxi.llm.json_tools import parse_json_response
from rayxi.llm.protocol import LLMCaller
from rayxi.spec.genre_expectations import expectations_prompt_text

from rayxi.trace import get_trace

from .hlr_validator import REQUIRED_ENUMS
from .models import (
    GameIdentity,
    MechanicEffectSpec,
    MechanicInteractionSpec,
    MechanicPropertySpec,
    MechanicSpec,
    SchemaField,
)
from .schema_expander import expand_schema, fields_to_schema_text

_log = logging.getLogger("rayxi.spec.hlr")

_KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "knowledge"


# ---------------------------------------------------------------------------
# Output schema — lives in Python as a dict, serialized to JSON for the prompt.
# Single source of truth. No brace escaping, no .replace/.format tricks.
# ---------------------------------------------------------------------------

_SCHEMA_SHAPE: dict = {
    "game_name": "<string — snake_case name for the game>",
    "genre": "<string — genre identifier (e.g. 2d_fighter, kart_racer)>",
    "player_mode": "<string — e.g. '1P vs CPU', '2P local', '1P solo'>",
    "win_condition": "<string or null — how the player wins>",
    "scenes": [
        {
            "scene_name": "<string — snake_case>",
            "purpose": "<string>",
            "fsm_state": "<string — S_STATE_NAME>",
            "children": [],
        }
    ],
    "global_fsm": {
        "states": ["<S_STATE_1>", "<S_STATE_2>"],
        "transitions": ["<S_STATE_1 -> S_STATE_2: on condition>"],
    },
    "global_rules": ["<string — high-level rule for how the game plays>"],
    "enums": [
        {
            "name": "<string — enum group name>",
            "values": ["<string — each valid value>"],
            "description": "<string>",
            "entity": "<bool — true if instantiable in scenes, false if metadata>",
            "value_descriptions": {
                "<value_name>": "<rich description — REQUIRED for game_systems enum>"
            },
            "value_template_origins": {
                "<value_name>": "<hlt_system_name or '(new)' — REQUIRED for game_systems when HLT provided>"
            },
        }
    ],
    "unused_template_systems": {
        "<hlt_system_name_not_included>": "<reason for not including this system>"
    },
    "mechanic_specs": [
        {
            "system_name": "<must match a game_systems value whose origin is '(new)'>",
            "summary": "<one-sentence description of the feature>",
            "properties": [
                {
                    "role": "<snake_case runtime owner role — e.g. fighter, vehicle, projectile, hud, game, stage, character>",
                    "name": "<snake_case>",
                    "type": "<int|float|bool|string|Vector2>",
                    "scope": "<instance|player_slot|game|character_def>",
                    "purpose": "<why this property exists>",
                    "written_by": ["<system_name>"],
                    "read_by": ["<system_name>"],
                    "reset_on": "<round_start|match_start|game_start|'' if persistent>"
                }
            ],
            "hud_entities": [
                {
                    "name": "<must match an hud_elements enum value>",
                    "godot_node": "<Control|ProgressBar|Label|TextureRect|...>",
                    "displays": "<what the widget shows>",
                    "reads": ["<property_name>"],
                    "visual_states": "<how user can tell each state apart — e.g. '3 filled segments for 3 stacks, 2 for 2, 1 for 1, empty for 0'>"
                }
            ],
            "interactions": [
                {
                    "trigger": "<what starts this interaction — reference another system or game event>",
                    "condition": "<guard>",
                    "effects": [
                        {
                            "verb": "<one of: set|add|subtract|increment|decrement|spawn|destroy|reset|apply|enable|disable|move|set_state>",
                            "target": "<entity.property — e.g. fighter.rage_stacks — OR a role.property like projectile.active — OR an entity name for spawn/destroy>",
                            "description": "<qualitative what-happens, no numeric values>"
                        }
                    ]
                }
            ],
            "constants_for_dlr": [
                {
                    "name": "<snake_case>",
                    "type": "<int|float>",
                    "purpose": "<what this constant controls>",
                    "value_hint": "<rough guidance or range — DLR fills the concrete value>"
                }
            ]
        }
    ]
}


_FILLED_EXAMPLE: dict = {
    "enums (showing only game_systems entry)": [
        {
            "name": "game_systems",
            "values": ["combat_system", "movement_system", "health_system"],
            "description": "Runtime gameplay systems",
            "entity": False,
            "value_descriptions": {
                "combat_system": "Hit detection and damage. Objects: fighter, hitbox, hurtbox. Interactions: reads active hitboxes on attacker, checks overlap with defender hurtbox, applies damage. Effects: subtract current_health, set hitstun_timer, trigger hitstop.",
                "movement_system": "Walking, jumping, facing. Objects: fighter, stage. Interactions: reads input direction, physics applies gravity, clamps to stage bounds. Effects: set position, set velocity, set facing_direction, set is_airborne.",
                "health_system": "HP tracking. Objects: fighter, hud_bar. Interactions: combat_system writes damage, round_system reads is_ko, hud_bar reads health_percent. Effects: set current_health, set is_ko when health reaches zero.",
            },
            "value_template_origins": {
                "combat_system": "combat_system",
                "movement_system": "movement_system",
                "health_system": "health_system",
            },
        }
    ],
    "unused_template_systems (when some HLT systems are not used)": {
        "charge_system": "No charge-motion special moves in this game",
        "tag_system": "Single-character game, no tag-in mechanics",
    },
    "unused_template_systems (when ALL HLT systems are used)": {},
    "mechanic_specs (example — a custom 'rage_meter_system' feature)": [
        {
            "system_name": "rage_meter_system",
            "summary": "Fighter accumulates rage from taking damage, stackable up to 3, powers up the next special move consumed.",
            "properties": [
                {
                    "role": "fighter",
                    "name": "rage_stacks",
                    "type": "int",
                    "scope": "instance",
                    "purpose": "Current powered-special charges available, 0 to max_rage_stacks.",
                    "written_by": ["rage_meter_system", "round_system"],
                    "read_by": ["rage_meter_system", "special_move_system", "animation_system"],
                    "reset_on": "round_start"
                },
                {
                    "role": "fighter",
                    "name": "rage_fill_value",
                    "type": "float",
                    "scope": "instance",
                    "purpose": "Fractional progress 0..1 toward the next stack; increments as fighter takes damage.",
                    "written_by": ["rage_meter_system"],
                    "read_by": ["rage_meter_system", "hud"],
                    "reset_on": "round_start"
                },
                {
                    "role": "fighter",
                    "name": "is_powered_special",
                    "type": "bool",
                    "scope": "instance",
                    "purpose": "True while the current special move is the powered-up variant (set when a stack is consumed).",
                    "written_by": ["rage_meter_system", "special_move_system"],
                    "read_by": ["combat_system", "animation_system", "projectile_system"],
                    "reset_on": ""
                }
            ],
            "hud_entities": [
                {
                    "name": "p1_rage_meter",
                    "godot_node": "Control",
                    "displays": "Three segment boxes plus a fractional fill bar underneath; each segment lights up when a stack is earned.",
                    "reads": ["rage_stacks", "rage_fill_value"],
                    "visual_states": "0 stacks = all segments dim; 1 = first segment lit; 2 = first two lit; 3 = all three lit and glowing; partial fill shown on the bar beneath."
                }
            ],
            "interactions": [
                {
                    "trigger": "combat_system applies damage to fighter.current_health",
                    "condition": "damage > 0 and fighter.rage_stacks < max_rage_stacks",
                    "effects": [
                        {"verb": "add", "target": "fighter.rage_fill_value", "description": "Increase by damage_taken / rage_fill_threshold."}
                    ]
                },
                {
                    "trigger": "fighter.rage_fill_value reaches 1.0",
                    "condition": "fighter.rage_stacks < max_rage_stacks",
                    "effects": [
                        {"verb": "increment", "target": "fighter.rage_stacks", "description": "One more stack earned."},
                        {"verb": "set", "target": "fighter.rage_fill_value", "description": "Wrap remainder back to zero."},
                        {"verb": "spawn", "target": "rage_burst_vfx", "description": "Visual feedback at fighter position."}
                    ]
                },
                {
                    "trigger": "special_move_system executes a special move",
                    "condition": "fighter.rage_stacks > 0",
                    "effects": [
                        {"verb": "decrement", "target": "fighter.rage_stacks", "description": "Consume one stack."},
                        {"verb": "set", "target": "fighter.is_powered_special", "description": "True for the duration of this move; combat_system reads this flag and applies powered_special_damage_multiplier internally."}
                    ]
                },
                {
                    "trigger": "round_system enters pre_round state",
                    "condition": "always",
                    "effects": [
                        {"verb": "set", "target": "fighter.rage_stacks", "description": "Reset to zero for both fighters."},
                        {"verb": "set", "target": "fighter.rage_fill_value", "description": "Reset to zero for both fighters."}
                    ]
                }
            ],
            "constants_for_dlr": [
                {"name": "max_rage_stacks", "type": "int", "purpose": "Maximum stacks a fighter can hold.", "value_hint": "3 (from user prompt)"},
                {"name": "rage_fill_threshold", "type": "float", "purpose": "Damage required to earn one stack.", "value_hint": "around one quarter of starting health"},
                {"name": "powered_special_damage_multiplier", "type": "float", "purpose": "Damage multiplier applied to powered-up special moves.", "value_hint": "about 1.5 to 2.0"}
            ]
        }
    ]
}


HLR_REPAIR_PROMPT = """\
You are repairing an existing HLR JSON so it passes deterministic validation
without changing the game's intended design.

You will receive:
1. The validation errors
2. The current HLR JSON
3. Optional template reference notes

Rules:
- Preserve gameplay intent, system names, scenes, enums, and mechanic structure
  whenever possible. Make the smallest fix that resolves each error.
- If a mechanic_spec HUD widget name is missing from `hud_elements`, prefer
  adding it to the enum rather than renaming the widget.
- If a spawn/destroy bare target is used consistently as a runtime object,
  make sure it is declared in `game_objects` (or the appropriate enum).
- Effect targets for verbs other than spawn/destroy must be `owner.property`
  or `role.property`. Do NOT target system names directly.
- Do NOT target `global_fsm` directly in interaction effects. Model the
  intended state transition through canonical game properties instead.
- In template-free mode, every `game_systems` value must remain req-defined
  and must have a corresponding `mechanic_spec`.
- Output ONLY the corrected full JSON object.
"""


def _build_required_enums_table() -> str:
    """Build the MUST HAVE enums table from the REQUIRED_ENUMS constant."""
    lines = [
        "| name | entity | description |",
        "|------|--------|-------------|",
    ]
    for name, entity, desc in REQUIRED_ENUMS:
        lines.append(f"| {name} | {str(entity).lower()} | {desc} |")
    return "\n".join(lines)


def _build_system_prompt(dynamic_schema_text: str) -> str:
    """Build the HLR system prompt from the Python schema shape + filled example.

    The prompt is assembled by json.dumps — no brace escaping, no .format tricks.
    """
    schema_json = json.dumps(_SCHEMA_SHAPE, indent=2)
    example_json = json.dumps(_FILLED_EXAMPLE, indent=2)
    enums_table = _build_required_enums_table()
    dynamic_section = dynamic_schema_text or "(No additional genre-specific fields needed.)"

    return f"""\
You are a game design architect. Your job is to produce a complete High-Level \
Requirements (HLR) document for a game.

You will receive:
1. The user's game concept description
2. A High-Level Template (HLT) listing standard systems for this genre (when available)
3. Knowledge base context (reference data, process specs, genre docs)

## Output schema

Your output must be a single JSON object matching this structure:

{schema_json}

## Filled example (reference — do NOT copy these specific values)

This shows what a valid game_systems enum entry and unused_template_systems dict look like:

{example_json}

## Required enums

The "enums" field declares every named value that downstream phases can reference. \
If a name is not in an enum, it cannot be used later.

Your output MUST include ALL of the following enums. Each has a fixed name, entity \
flag, and purpose. You fill in the values based on the game concept.

{enums_table}

Additionally, add genre-specific enums as needed. Examples per genre:
- Fighting: attack_strengths, attack_poses, fighter_states, special_moves, input_motions
- Racing: vehicle_types, track_segments, item_types, placement_positions
- Card game: card_types, card_names, zones, phases
- Platformer: enemy_types, powerup_types, player_abilities, tile_types

entity flag meaning:
- true = values are instantiable objects that appear in scenes (the LLM generates an entity spec for each)
- false = values are metadata/categories used for classification (not instantiated, just referenced)

## Additional genre-specific fields

{dynamic_section}

## Rules

- Use the KB process doc as the authoritative source for game flow if available.
- If the user's game is similar to a known game in the KB, adapt from it — don't copy blindly.
- Every scene must have a unique FSM state.
- The global FSM must be a complete graph — every state must be reachable and have an exit.
- All names must be snake_case.
- Runtime owner roles in `mechanic_specs[*].properties[*].role` are canonical after HLR.
  Use the exact role names you want downstream to build from. Do NOT force
  fighter terminology for non-fighting games.
- Include ALL base fields AND all required enums. Set nullable fields to null rather than omitting.
- Be exhaustive. Where unsure, make a reasonable design decision and state it.

### value_descriptions (REQUIRED for game_systems enum)
Every game_systems value MUST have a rich description that explicitly mentions:
- Objects: which entities/roles the system touches
- Interactions: how it reads/writes/calls other systems
- Effects: concrete state changes it produces

### value_template_origins (REQUIRED for game_systems enum when HLT is provided)
For every game_systems value, set value_template_origins[value_name] to the HLT system name it descended from. If you kept the same name, repeat it. If you invented a new system not in the HLT, use "(new)".

### unused_template_systems (REQUIRED when HLT is provided)
A flat dict mapping HLT system names you did NOT include in game_systems to a brief reason. If you include ALL HLT systems, set it to an empty object: {{}}. Do NOT wrap it in "comment" or "format" keys — those are not part of the schema.

NEVER silently drop an HLT system. Every HLT system must either appear in game_systems (via value_template_origins) or in unused_template_systems.

### mechanic_specs (REQUIRED for every game_systems value whose origin is '(new)')
Template-origin systems already have machine-readable definitions in the HLT — they don't need a mechanic_spec. But systems you invented that aren't in the HLT must ship a full structured spec so downstream phases (MLR, DLR, DAG, Impact matrix) can scaffold them without guessing.

For EVERY game_systems value with value_template_origins[value] == "(new)":
- Emit exactly one entry in `mechanic_specs` whose `system_name` matches the value.
- `properties`: every state variable the feature needs. Include role (which entity owns it), type, scope, purpose, written_by (which systems modify it), read_by (which systems or HUD widgets consume it), and reset_on (when it resets — empty string if persistent).
- `hud_entities`: one entry PER hud_elements enum value that visualizes this feature. Each must specify the godot_node class, what it displays, which properties it reads, and — critically — `visual_states`: a description of how the user can visually distinguish EVERY possible state of the feature (e.g. "0/1/2/3 stacks each look different: empty/one segment lit/two segments lit/three segments lit and glowing"). If the user cannot tell the states apart, the game is broken.
- `interactions`: every trigger → effect wire needed for the feature to actually work. Include: charging triggers, state transitions, effects on other systems, reset triggers. Each effect is an OBJECT with three fields:
    - `verb`: MUST be one of: `set`, `add`, `subtract`, `increment`, `decrement`, `spawn`, `destroy`, `reset`, `apply`, `enable`, `disable`, `move`, `set_state`. Do NOT use `clamp`, `scale`, `tint`, `multiply`, `clear` — rewrite those as `set` or `apply` with a description.
    - `target`: MUST be `entity.property` (a concrete state variable owned by an entity, like `fighter.rage_stacks` or `projectile.active`) — OR a bare entity name for `spawn`/`destroy` verbs. The entity name for spawn MUST match a value declared in the `game_objects` or `hud_elements` enum; do not invent a new name here.
    - `description`: qualitative — NO numeric values, DLR fills those.
  For effects like "scale damage by a multiplier": model this as `set fighter.is_powered_special` and let the receiving system (combat_system) read that flag and apply the multiplier internally at runtime (the multiplier lives in `constants_for_dlr`, not as an interaction effect).
- `constants_for_dlr`: every numeric knob DLR will need to fill (max values, thresholds, multipliers, durations, colors if custom). Include a value_hint so DLR has a ballpark.

If you skip a mechanic_spec for a (new) system, MLR will have no way to scaffold it and the feature will be missing at runtime.

- Output ONLY the JSON object. No markdown, no code fences, no explanation.
"""


async def run_hlr(
    user_prompt: str,
    caller: LLMCaller,
    knowledge_dir: Path | None = None,
    template_systems: dict[str, str] | None = None,
    kb_chunks: list[tuple[str, str, float]] | None = None,
) -> tuple[GameIdentity, list[SchemaField]]:
    """Run HLR: schema expansion → HLR generation.

    Args:
        template_systems: optional dict of {system_name: description} from the
            mechanic template. HLR uses these as the standard vocabulary
            but is free to add new systems for novel features.
        kb_chunks: optional list of (source_label, chunk_text, score) from
            embedding-based KB retrieval. Replaces the keyword-based KB context.

    Returns (game_identity, dynamic_fields) so downstream phases know the schema.
    """
    # Build KB context for schema expander — use embedded chunks if provided, else keyword fallback
    if kb_chunks:
        # Build a KnowledgeContext-like object from the embedded chunks
        from rayxi.knowledge import KnowledgeContext
        kb_context = KnowledgeContext(
            game_data={},
            process_doc="",
            genre_docs=[c[1] for c in kb_chunks],
            watchout_docs=[],
            source_names=[c[0] for c in kb_chunks],
        )
        _log.info("HLR: KB sources (embedding) = %d chunks", len(kb_chunks))
    else:
        kb = KnowledgeBase(knowledge_dir or _KNOWLEDGE_DIR)
        kb_context = kb.retrieve_context(user_prompt)
        _log.info("HLR: KB sources (keyword fallback) = %s", kb_context.source_names)

    # Step 1: Schema expansion
    dynamic_fields = await expand_schema(user_prompt, kb_context, caller)

    # Step 2: Build the HLR prompt with expanded schema
    dynamic_schema = fields_to_schema_text(dynamic_fields)
    system_prompt = _build_system_prompt(dynamic_schema)

    prompt_parts = [f"## User Game Concept\n{user_prompt}"]

    # Add HLT (High-Level Template) as reference — HLR must address every system
    if template_systems:
        sys_lines = "\n".join(f"- **{name}**: {desc}" for name, desc in sorted(template_systems.items()))
        prompt_parts.append(
            "## Template Reference (HLT — High-Level Template for this genre)\n"
            "These systems are the genre baseline. **You MUST address every one.** For each:\n"
            "- Either INCLUDE it in your `game_systems` enum (using the template name or a renamed version)\n"
            "- Or LIST it in `unused_template_systems` with a brief reason\n"
            "\n"
            "Set `value_template_origins[your_system_name]` to the HLT system name it descended from. "
            "If you invent a system not from the HLT, use `\"(new)\"`.\n"
            "\n"
            f"### HLT Systems\n{sys_lines}"
        )
    else:
        prompt_parts.append(
            "## No Template Available\n"
            "There is no HLT for this genre. You must define the canonical runtime "
            "systems and roles yourself.\n"
            "- Treat EVERY `game_systems` value as req-defined.\n"
            "- Set `value_template_origins[system_name]` to `\"(new)\"` for EVERY system.\n"
            "- Emit a complete `mechanic_spec` for EVERY system so downstream phases "
            "never need to guess or consult a missing template.\n"
            "- You may borrow ideas from the KB, but the names/structure you emit here "
            "become the authoritative contract."
        )

    hinted_genre = (
        kb_context.game_data.get("_meta", {}).get("genre", "")
        if isinstance(kb_context.game_data, dict)
        else ""
    )
    if not hinted_genre:
        lower_prompt = user_prompt.lower()
        lower_sources = " ".join(kb_context.source_names).lower()
        if "kart" in lower_prompt or "race" in lower_prompt or "kart_racer" in lower_sources:
            hinted_genre = "kart_racer"
        elif "fighter" in lower_prompt or "sf2" in lower_prompt or "2d_fighter" in lower_sources:
            hinted_genre = "2d_fighter"
    expectation_text = expectations_prompt_text(hinted_genre)
    if expectation_text:
        prompt_parts.append(
            "## Genre Expectation Hints\n"
            "Use these as deterministic expectation cues for the req stack.\n"
            "- If the prompt implies a modern mainstream interpretation of the genre, do not "
            "collapse it to the oldest or simplest historical version unless the user explicitly "
            "asks for retro, SNES, 8-bit, or Mode-7 styling.\n"
            "- Resolve these capabilities into canonical req-owned systems, properties, scenes, "
            "or interactions whenever they belong in this build.\n"
            "- If you intentionally omit one, the surrounding req design must clearly subsume it.\n"
            f"{expectation_text}"
        )

    # KB context — embedded chunks or keyword fallback
    if kb_chunks:
        chunk_text = "\n\n".join(
            f"=== {label} (score={score:.2f}) ===\n{text}" for label, text, score in kb_chunks
        )
        prompt_parts.append(f"## Knowledge Base Context (relevant chunks)\n{chunk_text}")
    elif not kb_context.is_empty():
        hlr_kb_parts: list[str] = []
        if kb_context.process_doc:
            hlr_kb_parts.append(
                f"=== Game Process Specification (authoritative — follow exactly) ===\n{kb_context.process_doc.strip()}"
            )
        for doc in kb_context.genre_docs:
            hlr_kb_parts.append(f"=== Genre Knowledge ===\n{doc.strip()}")
        for doc in kb_context.watchout_docs:
            hlr_kb_parts.append(f"=== Watchouts ===\n{doc.strip()}")
        if hlr_kb_parts:
            prompt_parts.append("## Knowledge Base Context\n" + "\n\n".join(hlr_kb_parts))

    full_prompt = "\n\n".join(prompt_parts)
    _log.info("HLR: sending to LLM (%d chars, %d dynamic fields)", len(full_prompt), len(dynamic_fields))

    trace = get_trace()
    caller_name = type(caller).__name__
    cid = trace.llm_start("hlr", "hlr_generate", caller_name, len(system_prompt) + len(full_prompt)) if trace else ""
    raw = await caller(system_prompt, full_prompt, json_mode=True, label="hlr_generate")
    if trace:
        trace.llm_end(cid, output_chars=len(raw))

    parsed = parse_json_response(raw)
    parsed["kb_sources"] = kb_context.source_names

    # Capture unused_template_systems for the audit log (not part of GameIdentity)
    unused_systems = parsed.pop("unused_template_systems", None) or {}

    if unused_systems:
        try:
            log_dir = Path(__file__).resolve().parents[3] / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{parsed.get('game_name', 'unknown')}_unused_template_systems.json"
            log_path.write_text(json.dumps(unused_systems, indent=2) + "\n")
            _log.info("HLR: %d template systems unused — logged to %s", len(unused_systems), log_path)
        except Exception as exc:
            _log.warning("HLR: failed to write unused systems log: %s", exc)

    result = GameIdentity.model_validate(parsed)
    result = _normalize_game_identity(result, template_provided=bool(template_systems))

    base_count = len(result.model_fields_set - (result.model_extra or {}).keys())
    extra_count = len(result.extra_fields())
    _log.info("HLR: produced GameIdentity for %s (%d scenes, %d base fields, %d dynamic fields)",
              result.game_name, len(result.scenes), base_count, extra_count)

    return result, dynamic_fields


def _enum_def(hlr: GameIdentity, name: str):
    for enum in hlr.enums:
        if enum.name == name:
            return enum
    return None


def _snake_case_name(text: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return clean or "value"


def _append_enum_values(hlr: GameIdentity, enum_name: str, values: list[str]) -> None:
    enum = _enum_def(hlr, enum_name)
    if enum is None:
        return
    for value in values:
        if value and value not in enum.values:
            enum.values.append(value)


def _player_mode_has_human_players(player_mode: str) -> bool:
    mode = (player_mode or "").strip().lower()
    if not mode:
        return True
    if "cpu vs cpu" in mode or "spectator" in mode:
        return False
    return any(token in mode for token in ("1p", "2p", "player", "local", "solo"))


def _is_non_ai_input_system(system_name: str) -> bool:
    normalized = (system_name or "").lower()
    if "ai" in normalized:
        return False
    return "input" in normalized or "control" in normalized


def _is_control_property(prop: MechanicPropertySpec) -> bool:
    name = (prop.name or "").lower()
    return (
        name.endswith("_input")
        or name.endswith("_pressed")
        or name.endswith("_held")
        or name in {"move_direction", "aim_direction", "block_state", "input_buffer"}
    )


def _find_runtime_property(
    hlr: GameIdentity,
    role: str,
    name: str,
) -> MechanicPropertySpec | None:
    for spec in hlr.mechanic_specs:
        for prop in spec.properties:
            if prop.role == role and prop.name == name:
                return prop
    return None


def _clone_property_for_system(
    prop: MechanicPropertySpec,
    system_name: str,
    *,
    add_writer: bool = False,
    add_reader: bool = False,
) -> MechanicPropertySpec:
    written_by = list(prop.written_by)
    read_by = list(prop.read_by)
    if add_writer and system_name not in written_by:
        written_by.append(system_name)
    if add_reader and system_name not in read_by:
        read_by.append(system_name)
    return MechanicPropertySpec(
        role=prop.role,
        name=prop.name,
        type=prop.type,
        scope=prop.scope,
        purpose=prop.purpose,
        written_by=written_by,
        read_by=read_by,
        reset_on=prop.reset_on,
    )


def _primary_runtime_role_for_spec(spec: MechanicSpec) -> str | None:
    role_counts: dict[str, int] = {}
    for prop in spec.properties:
        if prop.role in {"game", "stage", "hud", "character"}:
            continue
        if prop.scope == "character_def":
            continue
        role_counts[prop.role] = role_counts.get(prop.role, 0) + 1
    if not role_counts:
        return None
    return sorted(role_counts, key=lambda role: (-role_counts[role], role))[0]


def _merge_control_candidate(
    candidates: dict[str, dict[str, MechanicPropertySpec]],
    prop: MechanicPropertySpec,
) -> None:
    role_bucket = candidates.setdefault(prop.role, {})
    existing = role_bucket.get(prop.name)
    if existing is None:
        role_bucket[prop.name] = prop
        return
    for writer in prop.written_by:
        if writer not in existing.written_by:
            existing.written_by.append(writer)
    for reader in prop.read_by:
        if reader not in existing.read_by:
            existing.read_by.append(reader)
    if not existing.purpose and prop.purpose:
        existing.purpose = prop.purpose


def _inferred_input_props_for_trigger(
    role: str,
    system_name: str,
    trigger: str,
) -> list[MechanicPropertySpec]:
    lowered = (trigger or "").lower()
    if "input" not in lowered:
        return []

    inferred: list[MechanicPropertySpec] = []

    def _append(name: str, type_name: str, purpose: str) -> None:
        inferred.append(
            MechanicPropertySpec(
                role=role,
                name=name,
                type=type_name,
                scope="instance",
                purpose=purpose,
                written_by=[],
                read_by=[system_name],
                reset_on="match_start",
            )
        )

    if "acceler" in lowered or "throttle" in lowered:
        _append("acceleration_input", "float", f"Human throttle input consumed by {system_name}.")
    if "brake" in lowered:
        _append("brake_input", "float", f"Human braking input consumed by {system_name}.")
    if "steer" in lowered or "left/right" in lowered or "left or right" in lowered:
        _append("steer_input", "float", f"Human steering input consumed by {system_name}.")
    if "drift" in lowered:
        _append("drift_input", "bool", f"Human drift control flag consumed by {system_name}.")
    if "item" in lowered or "use_item" in lowered or "use item" in lowered:
        _append("item_input", "bool", f"Human item-use flag consumed by {system_name}.")
    if "jump" in lowered:
        _append("jump_input", "bool", f"Human jump input consumed by {system_name}.")
    if "block" in lowered:
        _append("block_input", "bool", f"Human block input consumed by {system_name}.")
    return inferred


def _player_input_effects_for(role: str, prop_name: str) -> list[MechanicInteractionSpec]:
    target = f"{role}.{prop_name}"
    lowered = prop_name.lower()
    if lowered == "acceleration_input":
        return [
            MechanicInteractionSpec(
                trigger="player holds the accelerate control during active gameplay",
                condition=f"{role}.is_ai_controlled is false",
                effects=[MechanicEffectSpec(
                    verb="set",
                    target=target,
                    description="Set throttle input from idle to full acceleration based on the player's accelerate control.",
                )],
            ),
            MechanicInteractionSpec(
                trigger="player releases the accelerate control or gameplay state disables manual control",
                condition="always",
                effects=[MechanicEffectSpec(
                    verb="set",
                    target=target,
                    description="Return throttle input to zero so the vehicle stops accelerating.",
                )],
            ),
        ]
    if lowered == "steer_input":
        return [
            MechanicInteractionSpec(
                trigger="player holds left or right steering controls during active gameplay",
                condition=f"{role}.is_ai_controlled is false",
                effects=[MechanicEffectSpec(
                    verb="set",
                    target=target,
                    description="Set steering input negative for left, positive for right, and neutral when no steer control is held.",
                )],
            ),
        ]
    if lowered in {"drift_input", "item_input", "jump_input", "boost_input", "brake_input"}:
        label = lowered.replace("_", " ")
        return [
            MechanicInteractionSpec(
                trigger=f"player presses or holds the {label} control during active gameplay",
                condition=f"{role}.is_ai_controlled is false",
                effects=[MechanicEffectSpec(
                    verb="set",
                    target=target,
                    description=f"Mirror the player's {label} control into the canonical runtime input flag.",
                )],
            ),
            MechanicInteractionSpec(
                trigger=f"player releases the {label} control",
                condition="always",
                effects=[MechanicEffectSpec(
                    verb="set",
                    target=target,
                    description=f"Clear the {label} flag back to its neutral state.",
                )],
            ),
        ]
    return [
        MechanicInteractionSpec(
            trigger=f"player manipulates the control mapped to {lowered} during active gameplay",
            condition=f"{role}.is_ai_controlled is false",
            effects=[MechanicEffectSpec(
                verb="set",
                target=target,
                description="Translate the player's live control state into this canonical runtime input property.",
            )],
        )
    ]


def _ensure_player_input_system(hlr: GameIdentity) -> None:
    if not _player_mode_has_human_players(hlr.player_mode):
        return

    game_systems = _enum_def(hlr, "game_systems")
    if game_systems is None:
        return
    if any(_is_non_ai_input_system(system_name) for system_name in game_systems.values):
        return

    role_candidates: dict[str, dict[str, MechanicPropertySpec]] = {}
    for spec in hlr.mechanic_specs:
        for prop in spec.properties:
            if prop.role in {"game", "stage", "hud", "character", "projectile", "collectible", "game_objects"}:
                continue
            if prop.scope == "character_def":
                continue
            if _is_control_property(prop):
                _merge_control_candidate(
                    role_candidates,
                    MechanicPropertySpec(
                        role=prop.role,
                        name=prop.name,
                        type=prop.type,
                        scope=prop.scope,
                        purpose=prop.purpose,
                        written_by=list(prop.written_by),
                        read_by=list(prop.read_by),
                        reset_on=prop.reset_on,
                    ),
                )
        primary_role = _primary_runtime_role_for_spec(spec)
        if primary_role is None:
            continue
        for interaction in spec.interactions:
            for inferred_prop in _inferred_input_props_for_trigger(primary_role, spec.system_name, interaction.trigger):
                _merge_control_candidate(role_candidates, inferred_prop)

    if not role_candidates:
        return

    primary_role = sorted(
        role_candidates,
        key=lambda role: (-len(role_candidates[role]), role),
    )[0]
    control_props = [role_candidates[primary_role][name] for name in sorted(role_candidates[primary_role])]
    if not control_props:
        return

    if _find_runtime_property(hlr, primary_role, "is_ai_controlled") is None and "cpu" in (hlr.player_mode or "").lower():
        control_props.append(
            MechanicPropertySpec(
                role=primary_role,
                name="is_ai_controlled",
                type="bool",
                scope="instance",
                purpose=f"Whether this {primary_role} instance is driven by AI instead of local player input.",
                written_by=[],
                read_by=[],
                reset_on="match_start",
            )
        )

    system_name = "player_input_system"
    insert_at = next(
        (idx for idx, name in enumerate(game_systems.values) if "ai" in name.lower() or "physics" in name.lower()),
        len(game_systems.values),
    )
    game_systems.values.insert(insert_at, system_name)
    game_systems.value_template_origins[system_name] = "(new)"
    control_prop_names = [prop.name for prop in control_props if prop.name != "is_ai_controlled"]
    game_systems.value_descriptions[system_name] = (
        f"Reads local human input for the {primary_role} role and translates it into canonical control properties. "
        f"Objects: {primary_role}, game. Interactions: reads live keyboard/controller state, "
        f"{primary_role}.is_ai_controlled, and active gameplay state; writes "
        f"{', '.join(f'{primary_role}.{name}' for name in control_prop_names)}. "
        f"Effects: updates per-frame player control state for human-controlled {primary_role} instances without overriding AI-controlled ones."
    )

    spec_props: list[MechanicPropertySpec] = [
        _clone_property_for_system(
            prop,
            system_name,
            add_writer=prop.name != "is_ai_controlled",
            add_reader=prop.name == "is_ai_controlled",
        )
        for prop in control_props
    ]
    support_keys: list[tuple[str, str]] = [
        (primary_role, "is_ai_controlled"),
        ("game", "race_state"),
        ("game", "round_state"),
        ("game", "match_state"),
    ]
    seen_prop_keys = {(prop.role, prop.name, prop.scope) for prop in spec_props}
    for role, prop_name in support_keys:
        support_prop = _find_runtime_property(hlr, role, prop_name)
        if support_prop is None:
            continue
        key = (support_prop.role, support_prop.name, support_prop.scope)
        if key in seen_prop_keys:
            continue
        spec_props.append(_clone_property_for_system(support_prop, system_name, add_reader=True))
        seen_prop_keys.add(key)

    interactions: list[MechanicInteractionSpec] = []
    for prop in control_props:
        interactions.extend(_player_input_effects_for(primary_role, prop.name))

    hlr.mechanic_specs.append(
        MechanicSpec(
            system_name=system_name,
            summary=(
                f"Captures local human controls for the {primary_role} role and writes canonical runtime input properties."
            ),
            properties=spec_props,
            interactions=interactions,
            hud_entities=[],
            constants_for_dlr=[],
        )
    )


def _normalize_collection_property_types(hlr: GameIdentity) -> None:
    for spec in hlr.mechanic_specs:
        for prop in spec.properties:
            lowered_type = (prop.type or "").strip().lower()
            lowered_name = (prop.name or "").strip().lower()
            lowered_purpose = (prop.purpose or "").strip().lower()
            if lowered_type not in {"vector2", "string"}:
                continue
            if (
                lowered_name.endswith("_positions")
                or lowered_name.endswith("_points")
                or lowered_name.endswith("_waypoints")
                or lowered_name.endswith("_checkpoints")
                or "array of" in lowered_purpose
                or "list of" in lowered_purpose
                or ("positions" in lowered_purpose and any(token in lowered_purpose for token in ("array", "list")))
            ):
                prop.type = "list"


def _normalize_game_system_names(hlr: GameIdentity) -> None:
    game_systems = _enum_def(hlr, "game_systems")
    if game_systems is None:
        return

    rename_map: dict[str, str] = {}
    used: set[str] = set()
    normalized_values: list[str] = []
    for original in list(game_systems.values):
        base = _snake_case_name(original)
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        rename_map[original] = candidate
        used.add(candidate)
        normalized_values.append(candidate)
    game_systems.values = normalized_values

    if game_systems.value_descriptions:
        game_systems.value_descriptions = {
            rename_map.get(name, _snake_case_name(name)): desc
            for name, desc in game_systems.value_descriptions.items()
        }
    if game_systems.value_template_origins:
        game_systems.value_template_origins = {
            rename_map.get(name, _snake_case_name(name)): origin
            for name, origin in game_systems.value_template_origins.items()
        }

    for spec in hlr.mechanic_specs:
        spec.system_name = rename_map.get(spec.system_name, _snake_case_name(spec.system_name))
        for prop in spec.properties:
            prop.written_by = [rename_map.get(name, _snake_case_name(name)) for name in prop.written_by]
            prop.read_by = [rename_map.get(name, _snake_case_name(name)) for name in prop.read_by]


def _normalize_game_identity(hlr: GameIdentity, *, template_provided: bool) -> GameIdentity:
    """Deterministic HLR cleanup for enum/spec drift.

    This keeps the req artifact authoritative by folding directly referenced
    mechanic-spec names back into the canonical enums instead of leaving later
    phases to guess.
    """
    game_systems = _enum_def(hlr, "game_systems")
    if game_systems is not None and not template_provided:
        for system_name in game_systems.values:
            game_systems.value_template_origins.setdefault(system_name, "(new)")

    _normalize_game_system_names(hlr)

    hud_names: list[str] = []
    spawn_targets: list[str] = []
    for spec in hlr.mechanic_specs:
        hud_names.extend(hud.name for hud in spec.hud_entities if hud.name)
        for interaction in spec.interactions:
            for effect in interaction.effects:
                if effect.verb in {"spawn", "destroy"} and "." not in effect.target and effect.target:
                    spawn_targets.append(effect.target)

    _append_enum_values(hlr, "hud_elements", hud_names)
    _append_enum_values(hlr, "game_objects", spawn_targets)

    valid_scopes = {"instance", "player_slot", "game", "character_def"}
    for spec in hlr.mechanic_specs:
        for prop in spec.properties:
            if prop.scope in valid_scopes:
                continue
            if prop.scope == "character" or prop.role == "character":
                prop.scope = "character_def"
            elif prop.scope == "game" or prop.role == "game":
                prop.scope = "game"
            else:
                prop.scope = "instance"
    _normalize_collection_property_types(hlr)
    _ensure_player_input_system(hlr)
    return hlr


async def repair_hlr(
    hlr: GameIdentity,
    validation_errors: list[str],
    caller: LLMCaller,
    *,
    template_systems: dict[str, str] | None = None,
    require_mechanic_specs_for_all_systems: bool = False,
) -> GameIdentity:
    prompt_parts = [
        "## Validation Errors\n```json\n"
        + json.dumps(validation_errors, indent=2)
        + "\n```",
        "## Current HLR JSON\n```json\n"
        + hlr.model_dump_json(indent=2)
        + "\n```",
    ]
    if template_systems:
        prompt_parts.append(
            "## Template Reference\n```json\n"
            + json.dumps(template_systems, indent=2)
            + "\n```"
        )
    if require_mechanic_specs_for_all_systems:
        prompt_parts.append(
            "## Template-Free Mode\n"
            "Every game_systems value must remain req-defined with a full mechanic_spec."
        )

    raw = await caller(
        HLR_REPAIR_PROMPT,
        "\n\n".join(prompt_parts),
        json_mode=True,
        label="hlr_repair",
    )
    parsed = parse_json_response(raw)
    parsed["kb_sources"] = hlr.kb_sources
    repaired = GameIdentity.model_validate(parsed)
    return _normalize_game_identity(repaired, template_provided=bool(template_systems))
