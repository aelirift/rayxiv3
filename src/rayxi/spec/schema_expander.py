"""Schema Expander — LLM proposes dynamic HLR fields based on genre + KB.

Given the user prompt and KB context, determines what additional fields
the HLR schema needs beyond the base set. Only additions, never deletions.

Examples:
  - Fighting game → adds: characters, theme, lose_condition
  - Card game (YuGiOh) → adds: cards, deck_rules, duel_phases, life_points
  - Racing game → adds: vehicles, track_rules, item_system, lap_count
  - MTG → adds: planeswalkers, mana_system, card_types, turn_phases
"""

from __future__ import annotations

import json
import logging

from rayxi.knowledge import KnowledgeContext
from rayxi.llm.protocol import LLMCaller

from .models import SchemaField

_log = logging.getLogger("rayxi.spec.schema_expander")

BASE_SCHEMA_DESCRIPTION = """\
The HLR base schema already includes these fields (DO NOT propose these):
- game_name: string — snake_case identifier
- genre: string — genre identifier
- player_mode: string — e.g. "1P vs CPU", "2P local"
- scenes: list of {scene_name, purpose, fsm_state}
- global_fsm: {states: list[string], transitions: list[string]}
- global_rules: list[string] — high-level rules
- win_condition: string or null
- kb_sources: list[string]
"""

EXPANDER_SYSTEM_PROMPT = """\
You are a game schema architect. Your job is to look at a game concept and its \
knowledge base context, then propose ADDITIONAL fields that the High-Level \
Requirements (HLR) schema needs for this specific game type.

""" + BASE_SCHEMA_DESCRIPTION + """

Rules:
- Only propose fields that the base schema does NOT already cover.
- Each field must be essential to defining WHAT this game IS at a high level.
- No implementation details (pixel sizes, frame counts, key bindings) — those are mid/detail level.
- field_type must be one of: "string", "list[string]", "list[object]", "object", "int", "bool"
- For list[object] types, describe the object structure in the description field.
- If a field could be null (e.g. lose_condition for games where you can't lose), set required: false.
- Think about what makes this genre unique. A fighting game needs characters. A card game needs cards/decks. A racing game needs vehicles/tracks.

Output a JSON object:
{
  "proposed_fields": [
    {
      "field_name": "string — snake_case",
      "field_type": "string — one of the allowed types",
      "description": "string — what this field represents and why this game needs it",
      "required": true
    }
  ],
  "reasoning": "string — brief explanation of why these fields are needed for this genre"
}

Output ONLY the JSON. No markdown, no explanation outside the JSON.
"""


async def expand_schema(
    user_prompt: str,
    kb_context: KnowledgeContext,
    caller: LLMCaller,
) -> list[SchemaField]:
    """Ask LLM what additional HLR fields this game concept needs."""

    prompt_parts = [f"## Game Concept\n{user_prompt}"]
    if not kb_context.is_empty():
        # Schema expander only needs to know WHAT the game is, not full frame data.
        # Send source names + genre docs only, skip bulky game_data JSON.
        summary_parts = []
        if kb_context.source_names:
            summary_parts.append(f"KB sources: {', '.join(kb_context.source_names)}")
        if kb_context.process_doc:
            # Just the first 500 chars of process doc — enough for structure
            summary_parts.append(f"Process doc summary:\n{kb_context.process_doc[:500]}")
        for doc in kb_context.genre_docs:
            summary_parts.append(doc[:500])
        for doc in kb_context.watchout_docs:
            summary_parts.append(doc[:300])
        if summary_parts:
            prompt_parts.append(f"## Knowledge Base Context (summary)\n" + "\n\n".join(summary_parts))

    full_prompt = "\n\n".join(prompt_parts)
    _log.info("Schema expander: sending to LLM (%d chars)", len(full_prompt))

    raw = await caller(EXPANDER_SYSTEM_PROMPT, full_prompt, json_mode=True)
    parsed = json.loads(raw)

    fields = [SchemaField.model_validate(f) for f in parsed.get("proposed_fields", [])]
    reasoning = parsed.get("reasoning", "")

    # Filter out any fields that duplicate base schema
    base_names = {"game_name", "genre", "player_mode", "scenes", "global_fsm",
                  "global_rules", "win_condition", "kb_sources"}
    fields = [f for f in fields if f.field_name not in base_names]

    _log.info("Schema expander: %d additional fields proposed. Reasoning: %s",
              len(fields), reasoning[:120])
    for f in fields:
        _log.info("  + %s (%s): %s", f.field_name, f.field_type, f.description[:80])

    return fields


def fields_to_schema_text(fields: list[SchemaField]) -> str:
    """Convert proposed fields to a JSON schema snippet for the HLR prompt."""
    if not fields:
        return ""
    lines = []
    for f in fields:
        nullable = " (nullable)" if not f.required else ""
        lines.append(f'  "{f.field_name}": "{f.field_type}{nullable} — {f.description}"')
    return "Additional genre-specific fields:\n{\n" + ",\n".join(lines) + "\n}"
