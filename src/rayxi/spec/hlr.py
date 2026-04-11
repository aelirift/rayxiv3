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
from pathlib import Path

from rayxi.knowledge import KnowledgeBase, KnowledgeContext
from rayxi.llm.protocol import LLMCaller

from rayxi.trace import get_trace

from .hlr_validator import REQUIRED_ENUMS
from .models import GameIdentity, SchemaField
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
                    "role": "<fighter|projectile|hud|game|stage>",
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

    parsed = json.loads(raw)
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

    base_count = len(result.model_fields_set - (result.model_extra or {}).keys())
    extra_count = len(result.extra_fields())
    _log.info("HLR: produced GameIdentity for %s (%d scenes, %d base fields, %d dynamic fields)",
              result.game_name, len(result.scenes), base_count, extra_count)

    return result, dynamic_fields
