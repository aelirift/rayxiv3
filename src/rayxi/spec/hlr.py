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


def _build_required_enums_table() -> str:
    """Build the MUST HAVE enums table from the REQUIRED_ENUMS constant."""
    lines = [
        "| name | entity | description |",
        "|------|--------|-------------|",
    ]
    for name, entity, desc in REQUIRED_ENUMS:
        lines.append(f"| {name} | {str(entity).lower()} | {desc} |")
    return "\n".join(lines)

HLR_SYSTEM_PROMPT_TEMPLATE = """\
You are a game design architect. Your job is to produce a complete High-Level \
Requirements (HLR) document for a game.

You will receive:
1. The user's game concept description
2. Knowledge base context (reference data, process specs, genre docs, watchouts)

Your output must be a single JSON object with these base fields:

{{
  "game_name": "string — snake_case name for the game",
  "genre": "string — genre identifier (e.g. 2d_fighter, kart_racer, card_game)",
  "player_mode": "string — e.g. 1P vs CPU, 2P local, 1P solo",
  "win_condition": "string or null — how the player wins (null if not applicable)",
  "scenes": [
    {{"scene_name": "string — snake_case", "purpose": "string", "fsm_state": "string — S_STATE_NAME", "children": []}}
  ],
  "global_fsm": {{
    "states": ["S_STATE_1", "S_STATE_2"],
    "transitions": ["S_STATE_1 -> S_STATE_2: on condition"]
  }},
  "global_rules": ["string — high-level rules for how the game plays"],
  "enums": [
    {{"name": "string — enum group name", "values": ["string — each valid value"], "description": "string", "entity": "bool — true if values are instantiable objects in scenes (characters, stages, items), false if metadata/categories (archetypes, input methods, attack types)"}}
  ]
}}

{dynamic_schema}

## Enum Registry

The "enums" field declares every named value that downstream phases can reference. \
If a name is not in an enum, it cannot be used later.

Your output MUST include ALL of the following enums. Each has a fixed name, entity \
flag, and purpose. You fill in the values based on the game concept.

{required_enums_table}

Additionally, add genre-specific enums as needed. Examples per genre:
- Fighting: attack_strengths, attack_poses, fighter_states, special_moves, input_motions
- Racing: vehicle_types, track_segments, item_types, placement_positions
- Card game: card_types, card_names, zones, phases
- Platformer: enemy_types, powerup_types, player_abilities, tile_types

entity flag meaning:
- true = values are instantiable objects that appear in scenes (the LLM generates an entity spec for each)
- false = values are metadata/categories used for classification (not instantiated, just referenced)

Rules:
- Use the KB process doc as the authoritative source for game flow if available.
- If the user's game is similar to a known game in the KB, adapt from it — don't copy blindly.
- Every scene must have a unique FSM state.
- The global FSM must be a complete graph — every state must be reachable and have an exit.
- All names must be snake_case.
- Include ALL base fields AND all additional genre-specific fields listed above.
- If a field can be null, set it to null rather than omitting it.
- Be exhaustive. If you're unsure about a detail, make a reasonable design decision and state it.
- Output ONLY the JSON object. No markdown, no explanation, no wrapping.
"""


async def run_hlr(
    user_prompt: str,
    caller: LLMCaller,
    knowledge_dir: Path | None = None,
) -> tuple[GameIdentity, list[SchemaField]]:
    """Run HLR: schema expansion → HLR generation.

    Returns (game_identity, dynamic_fields) so downstream phases know the schema.
    """
    kb = KnowledgeBase(knowledge_dir or _KNOWLEDGE_DIR)
    kb_context: KnowledgeContext = kb.retrieve_context(user_prompt)
    _log.info("HLR: KB sources = %s", kb_context.source_names)

    # Step 1: Schema expansion
    dynamic_fields = await expand_schema(user_prompt, kb_context, caller)

    # Step 2: Build the HLR prompt with expanded schema
    dynamic_schema = fields_to_schema_text(dynamic_fields)
    if not dynamic_schema:
        dynamic_schema = "(No additional genre-specific fields needed.)"

    system_prompt = (
        HLR_SYSTEM_PROMPT_TEMPLATE
        .replace("{dynamic_schema}", dynamic_schema)
        .replace("{required_enums_table}", _build_required_enums_table())
    )

    prompt_parts = [f"## User Game Concept\n{user_prompt}"]
    if not kb_context.is_empty():
        # HLR needs process doc + genre docs + watchouts, but NOT full game data JSON.
        # Game data (frame data, move lists, damage values) is DLR-level detail.
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
            prompt_parts.append(f"## Knowledge Base Context\n" + "\n\n".join(hlr_kb_parts))

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
    result = GameIdentity.model_validate(parsed)

    base_count = len(result.model_fields_set - (result.model_extra or {}).keys())
    extra_count = len(result.extra_fields())
    _log.info("HLR: produced GameIdentity for %s (%d scenes, %d base fields, %d dynamic fields)",
              result.game_name, len(result.scenes), base_count, extra_count)

    return result, dynamic_fields
