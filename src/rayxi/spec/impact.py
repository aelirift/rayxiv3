"""HLR Step 2 — Impact Matrix.

Traces every game requirement through SEES / DOES / TRACKS / RUNS.
Produces a complete, justified property graph for the entire game.

Process:
  1. Extract requirements from HLR (deterministic)
  2. Trace each requirement via LLM (parallel calls)
  3. Merge + deduplicate properties
  4. Extract game-level mechanic definitions
  5. Validate (no orphans, no broken chains)

Generic across all game types — the LLM provides game-specific knowledge,
the code enforces structural rules.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from rayxi.knowledge import KnowledgeBase, KnowledgeContext
from rayxi.knowledge.mechanic_loader import ExpandedGameSchema
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from .models import (
    GameIdentity,
    ImpactEntry,
    ImpactMatrix,
    InputImplication,
    MechanicDefinition,
    PropertyImplication,
    SystemRole,
    VisualImplication,
)

_log = logging.getLogger("rayxi.spec.impact")
_KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "knowledge"
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "impact"


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
    """Call LLM with cache + retry + trace."""
    trace = get_trace()
    caller_name = type(caller).__name__
    key = _cache_key(system, prompt)
    cached = _cache_get(key)
    if cached is not None:
        _log.info("%s — cache hit (%s)", label, key)
        if trace:
            cid = trace.llm_start("impact", label, caller_name, len(system) + len(prompt))
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return cached

    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("impact", label, caller_name, len(system) + len(prompt)) if trace else ""
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
# Requirement extraction (deterministic — no LLM)
# ---------------------------------------------------------------------------

@dataclass
class Requirement:
    req_id: str
    text: str
    source_type: str   # game_system, special_move, character, global_rule, scene
    source_ref: str    # the specific item


def extract_requirements(hlr: GameIdentity) -> list[Requirement]:
    """Extract all traceable requirements from HLR. Generic across game types."""
    reqs: list[Requirement] = []
    idx = 0

    def _add(text: str, stype: str, sref: str) -> None:
        nonlocal idx
        idx += 1
        reqs.append(Requirement(
            req_id=f"REQ_{idx:03d}",
            text=text,
            source_type=stype,
            source_ref=sref,
        ))

    # Game systems — each is a requirement to trace
    for system in hlr.get_enum("game_systems"):
        _add(f"Game system: {system}", "game_system", system)

    # Characters — each character's unique traits
    # Include ALL special moves — character↔move binding is game knowledge
    # that the LLM resolves, not something we hardcode by name matching.
    special_moves = hlr.get_enum("special_moves")
    for char in hlr.get_enum("characters"):
        if special_moves:
            moves_str = ", ".join(special_moves)
            _add(f"Character {char} (game has special moves: {moves_str})",
                 "character", char)
        else:
            _add(f"Character {char}", "character", char)

    # Global rules with gameplay implications
    skip_words = {"snake_case", "naming", "format", "output", "json", "markdown"}
    for rule in hlr.global_rules:
        if any(sw in rule.lower() for sw in skip_words):
            continue
        _add(rule, "global_rule", rule[:50])

    # Scene purposes — what each scene needs to function
    for scene in hlr.scenes:
        _add(f"Scene '{scene.scene_name}': {scene.purpose}",
             "scene", scene.scene_name)

    _log.info("Impact: extracted %d requirements from HLR", len(reqs))
    return reqs


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

IMPACT_SYSTEM_PROMPT = """\
You are a game design analyst. Your job is to trace ONE game requirement through \
its FULL implications across 4 dimensions: SEES, DOES, TRACKS, RUNS.

You will receive a game's HLR and ONE specific requirement to trace.

## CRITICAL: Allowed Names (enum constraints)

You MUST ONLY use names from the lists provided below. Do NOT invent new names.

### Allowed OWNER names (for "owner" field in TRACKS, "entity" in SEES, "target_entity" in DOES):
{allowed_owners}

### Allowed SYSTEM names (for "written_by"/"read_by" in TRACKS, "system" in RUNS):
{allowed_systems}

### Allowed PROPERTY names (for "name" field in TRACKS):
{allowed_properties}

You MUST ONLY use property names from the list above. Do NOT invent new property names.
If a property doesn't fit any allowed owner, use "game" for global state.
If a system isn't in the list, do NOT reference it.

## SEES — What must be visible?
Every visual element this requirement creates. Each implies an asset.
Entity names MUST come from the allowed owners list above.

## DOES — What player input is involved?
Every input action. Reference the game's global input scheme, not per-character keys.
Characters don't own keys — the game's input system maps keys to actions.
target_entity MUST come from the allowed owners list above.

## TRACKS — What state does this create or modify?
THIS IS THE MOST IMPORTANT SECTION. For EACH property:
- name: the property name
- owner: MUST be from the allowed owners list
- owner_scope: one of "instance" (runtime state like current health), \
"player_slot" (per player like p1 combo count), "character_def" (per character type definition), \
"game" (global constant or state)
- type: int, float, bool, string, Vector2, enum
- written_by: MUST be from the allowed systems list
- read_by: MUST be from the allowed systems list
- purpose: WHY this property exists — link to this requirement

Rules for TRACKS:
- If a property has no reader, it shouldn't exist.
- If a property has no writer, it's a constant (owner_scope = "character_def" or "game").
- Mechanics are GAME-LEVEL: defined once. Characters provide VALUES the mechanic reads.
- Be exhaustive. A special move implies: input_buffer tracking, projectile_on_screen flag, \
  is_firing lock, projectile properties, spawn offset, recovery frames, etc.

## RUNS — What systems process this?
system MUST be from the allowed systems list.

Output a JSON object:
{{
  "requirement_id": "string",
  "requirement_text": "string",
  "source_type": "string",
  "source_ref": "string",
  "sees": [
    {{"entity": "string — from allowed owners", "visual": "string", "asset_type": "sprite|animation|particle|sound|ui_element", "asset_id": "string"}}
  ],
  "does": [
    {{"input_action": "string", "input_trigger": "string", "target_entity": "string — from allowed owners", "conditions": ["string"]}}
  ],
  "tracks": [
    {{"name": "string", "owner": "string — from allowed owners", "owner_scope": "instance|player_slot|character_def|game", "type": "string", "written_by": ["string — from allowed systems"], "read_by": ["string — from allowed systems"], "purpose": "string"}}
  ],
  "runs": [
    {{"system": "string — from allowed systems", "responsibility": "string"}}
  ]
}}

Output ONLY the JSON. No markdown, no explanation.
"""

MECHANICS_SYSTEM_PROMPT = """\
You are a game design analyst. Given a game's HLR and impact matrix entries, \
extract the GAME-LEVEL MECHANICS.

A mechanic is a rule that applies to ALL entities of a type, defined ONCE.
Characters provide values that mechanics read. Mechanics don't change per character.

## CRITICAL: Allowed property names
Property names are FLAT — there is no nesting. Use the exact names from the template:
{allowed_properties}

Use "owner.property_name" format where owner is fighter, game, projectile, etc.

Examples using CORRECT flat property names:
- stun_mechanic: properties_read=["fighter.stun_meter", "fighter.stun_threshold"], \
  properties_written=["fighter.is_stunned", "fighter.stun_timer"]
- damage_mechanic: properties_read=["fighter.current_action", "fighter.light_punch_damage", "fighter.damage_reduction"], \
  properties_written=["fighter.current_health", "fighter.hitstop_timer"]

Do NOT use nested names like "move.damage" or "active_hitbox.hit_connected". \
The property names are FLAT: light_punch_damage, heavy_kick_hitstun, etc.

For each mechanic:
{{
  "name": "string",
  "system": "string — which game_system owns it",
  "description": "string — plain English",
  "trigger": "string — what starts it",
  "effect": "string — what it does",
  "properties_read": ["owner.property — MUST use flat names from list above"],
  "properties_written": ["owner.property — MUST use flat names from list above"]
}}

Output a JSON object: {{"mechanics": [...]}}
Output ONLY the JSON. No markdown, no explanation.
"""


# ---------------------------------------------------------------------------
# Per-requirement LLM call
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Embedding-based system matching (lazy-loaded model)
# ---------------------------------------------------------------------------

_embedding_model = None


def _get_embedding_model():
    """Lazy-load the sentence transformer model (once per process)."""
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(
            "all-MiniLM-L6-v2", device="cpu",
        )
        _log.info("Embedding model loaded: all-MiniLM-L6-v2")
    return _embedding_model


def _match_systems_by_embedding(
    rule_text: str,
    by_system: dict[str, set[str]],
    schema: ExpandedGameSchema | None,
    top_k: int = 5,
) -> set[str]:
    """Match a rule to relevant systems using semantic embeddings.

    Builds a rich text per system (name + description + property names),
    embeds it alongside the rule text, returns top-K by cosine similarity.
    """
    import numpy as np

    model = _get_embedding_model()

    # Build rich system texts for embedding
    descriptions = {}
    if schema and hasattr(schema, "mechanic_descriptions"):
        descriptions = schema.mechanic_descriptions

    sys_names: list[str] = []
    sys_texts: list[str] = []
    for sys_name in sorted(by_system):
        desc = descriptions.get(sys_name, "")
        props = ", ".join(sorted(by_system[sys_name])[:20])
        rich = f"{sys_name}: {desc}. Properties: {props}" if desc else f"{sys_name}. Properties: {props}"
        sys_names.append(sys_name)
        sys_texts.append(rich)

    if not sys_names:
        return set()

    # Encode
    sys_emb = model.encode(sys_texts, normalize_embeddings=True)
    rule_emb = model.encode([rule_text], normalize_embeddings=True)[0]

    # Cosine similarity (embeddings are normalized, so dot product = cosine)
    sims = np.dot(sys_emb, rule_emb)
    ranked = sorted(zip(sys_names, sims), key=lambda x: -x[1])

    # Take top-K
    return {name for name, _ in ranked[:top_k]}


def _build_allowed_properties_text_for_mechanics(schema: ExpandedGameSchema | None) -> str:
    """Build flat property name list for the mechanics prompt."""
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
    lines.append(f"**fighter**: {', '.join(fighter_props)}")
    lines.append(f"**game**: {', '.join(game_props)}")
    if schema.projectile_schema.properties:
        proj_props = sorted({p.name for p in schema.projectile_schema.properties})
        lines.append(f"**projectile**: {', '.join(proj_props)}")
    return "\n".join(lines)


def _build_allowed_owners(hlr: GameIdentity) -> list[str]:
    """Build the allowed owner names from HLR enums.

    Owners are:
      - "game" (global state/constants)
      - Each value from entity=true enums (characters, stages, hud_elements, game_objects)
      - Role names: p1_fighter, p2_fighter (derived from player_mode)
    """
    owners: list[str] = ["game"]

    for enum_def in hlr.enums:
        if enum_def.entity:
            owners.extend(enum_def.values)

    # Add role names based on player mode
    if "1p" in hlr.player_mode.lower() or "vs" in hlr.player_mode.lower():
        owners.extend(["p1_fighter", "p2_fighter"])
    if "2p" in hlr.player_mode.lower():
        owners.extend(["p1_fighter", "p2_fighter"])

    return sorted(set(owners))


def _build_allowed_systems(hlr: GameIdentity) -> list[str]:
    """Build allowed system names from HLR game_systems enum."""
    return sorted(hlr.get_enum("game_systems"))


def _build_allowed_properties(schema: ExpandedGameSchema | None) -> dict[str, list[str]]:
    """Build allowed property names from the mechanic template, grouped by owner role."""
    if not schema:
        return {}
    props: dict[str, list[str]] = {}
    props["fighter"] = sorted({p.name for p in schema.fighter_schema.properties})
    for char, char_props in schema.per_character_unique.items():
        for p in char_props:
            if p.name not in props["fighter"]:
                props.setdefault("fighter", []).append(p.name)
    props["game"] = sorted({
        p.name for p in schema.game_config + schema.game_state + schema.game_derived
    })
    if schema.projectile_schema.properties:
        props["projectile"] = sorted({p.name for p in schema.projectile_schema.properties})
    if schema.hud_bar_schema.properties:
        props["hud_bar"] = sorted({p.name for p in schema.hud_bar_schema.properties})
    if schema.stage_schema.properties:
        props["stage"] = sorted({p.name for p in schema.stage_schema.properties})
    return props


def _build_scoped_properties_for_requirement(
    schema: ExpandedGameSchema | None,
    req_source_type: str,
    req_source_ref: str,
) -> str:
    """Build property list scoped to one requirement's context.

    game_system → only that system's read/write properties (tight scope)
    character → all fighter + game properties (character touches everything)
    global_rule/scene → full list organized by system (cross-cutting, needs coherence)
    """
    if not schema:
        return "(no template loaded — use best judgment)"

    if req_source_type == "game_system":
        # Tight scope: only properties this system reads/writes
        system = req_source_ref
        reads: dict[str, list[str]] = {}
        writes: dict[str, list[str]] = {}

        def _scan(role: str, props) -> None:
            for p in props:
                r_by = p.read_by if isinstance(p.read_by, list) else [p.read_by]
                w_by = p.written_by if isinstance(p.written_by, list) else [p.written_by]
                if system in r_by:
                    reads.setdefault(role, []).append(p.name)
                if system in w_by:
                    writes.setdefault(role, []).append(p.name)

        _scan("fighter", schema.fighter_schema.properties)
        for cp in schema.per_character_unique.values():
            _scan("fighter", cp)
            break
        _scan("game", schema.game_config + schema.game_state + schema.game_derived)
        _scan("projectile", schema.projectile_schema.properties)

        lines = [f"Properties for {system}:"]
        for role in sorted(set(list(reads.keys()) + list(writes.keys()))):
            r = sorted(set(reads.get(role, [])))
            w = sorted(set(writes.get(role, [])))
            if r:
                lines.append(f"  {role} READS: {', '.join(r)}")
            if w:
                lines.append(f"  {role} WRITES: {', '.join(w)}")
        return "\n".join(lines) if len(lines) > 1 else "(system has no template properties)"

    elif req_source_type == "character":
        # All fighter properties (character is the fighter) + game
        fighter_props = sorted({p.name for p in schema.fighter_schema.properties})
        for cp in schema.per_character_unique.values():
            fighter_props = sorted(set(fighter_props) | {p.name for p in cp})
            break
        game_props = sorted({
            p.name for p in schema.game_config + schema.game_state + schema.game_derived
        })
        return (
            f"**fighter**: {', '.join(fighter_props)}\n"
            f"**game**: {', '.join(game_props)}"
        )

    else:
        # global_rule / scene — match rule text against system names to find relevant systems
        # Then only include those systems' properties
        all_props = (
            list(schema.fighter_schema.properties)
            + list(schema.game_config) + list(schema.game_state) + list(schema.game_derived)
            + list(schema.projectile_schema.properties)
        )
        for cp in schema.per_character_unique.values():
            all_props.extend(cp)
            break

        # Build per-system property index from BOTH source_mechanic AND written_by/read_by
        by_system: dict[str, set[str]] = {}
        for p in all_props:
            # source_mechanic = which mechanic/system defined this property
            if hasattr(p, "source_mechanic") and p.source_mechanic:
                by_system.setdefault(p.source_mechanic, set()).add(p.name)
            # written_by/read_by = which systems touch this property at runtime
            r_by = p.read_by if isinstance(p.read_by, list) else [p.read_by]
            w_by = p.written_by if isinstance(p.written_by, list) else [p.written_by]
            for sys in r_by + w_by:
                if sys:
                    by_system.setdefault(sys, set()).add(p.name)

        # Game-agnostic matching via semantic embeddings
        # Embeds system descriptions + property names, compares to rule text
        matched_systems: set[str] = set()

        try:
            matched_systems = _match_systems_by_embedding(
                req_source_ref, by_system, schema,
            )
        except Exception:
            # Fallback: system name keyword matching if embedding fails
            text_lower = req_source_ref.lower()
            for sys_name in by_system:
                sys_keyword = sys_name.replace("_system", "")
                if sys_keyword in text_lower:
                    matched_systems.add(sys_name)

        if not matched_systems:
            # Fallback: if nothing matched, include all (shouldn't happen often)
            matched_systems = set(by_system.keys())

        lines = [f"Properties for relevant systems ({len(matched_systems)} matched):"]
        for sys_name in sorted(matched_systems):
            if sys_name in by_system:
                props = sorted(by_system[sys_name])
                lines.append(f"  {sys_name}: {', '.join(props)}")
        return "\n".join(lines)


def _format_allowed_names(
    owners: list[str],
    systems: list[str],
    properties: dict[str, list[str]] | None = None,
) -> tuple[str, str, str]:
    """Format the allowed names for prompt injection. Returns (owners_text, systems_text, properties_text)."""
    owner_lines: list[str] = []
    owner_lines.append("- game (scope: game — global state and constants)")
    owner_lines.append("- p1_fighter, p2_fighter (scope: player_slot — runtime role bindings)")
    for name in owners:
        if name in ("game", "p1_fighter", "p2_fighter"):
            continue
        owner_lines.append(f"- {name}")

    prop_lines: list[str] = []
    if properties:
        for role, names in sorted(properties.items()):
            prop_lines.append(f"**{role}**: {', '.join(names)}")
    else:
        prop_lines.append("(no template loaded — use best judgment)")

    return "\n".join(owner_lines), ", ".join(systems), "\n".join(prop_lines)


def _build_hlr_context(hlr: GameIdentity, kb_context: KnowledgeContext) -> str:
    """Build the shared context for all impact calls."""
    parts = [f"## Game HLR\n```json\n{hlr.model_dump_json(indent=2)}\n```"]

    enum_lines = "## HLR Enums\n"
    for e in hlr.enums:
        enum_lines += f"- {e.name}: {e.values} {'[entity]' if e.entity else ''}\n"
    parts.append(enum_lines)

    if not kb_context.is_empty():
        kb_parts: list[str] = []
        if kb_context.process_doc:
            kb_parts.append(f"=== Game Process ===\n{kb_context.process_doc.strip()}")
        for doc in kb_context.genre_docs:
            kb_parts.append(f"=== Genre Knowledge ===\n{doc.strip()}")
        if kb_parts:
            parts.append("## Knowledge Base\n" + "\n\n".join(kb_parts))

    return "\n\n".join(parts)


async def _trace_requirement(
    req: Requirement,
    hlr_context: str,
    caller: LLMCaller,
    system_prompt: str,
) -> ImpactEntry:
    """Trace one requirement through SEES/DOES/TRACKS/RUNS."""
    prompt = (
        f"{hlr_context}\n\n"
        f"## Requirement to Trace\n"
        f"ID: {req.req_id}\n"
        f"Type: {req.source_type}\n"
        f"Reference: {req.source_ref}\n"
        f"Description: {req.text}\n"
    )

    label = f"impact_{req.source_type}[{req.source_ref}]"
    raw = await _call_llm(caller, system_prompt, prompt, label)
    parsed = json.loads(raw)

    # Ensure the requirement_id matches
    parsed["requirement_id"] = req.req_id
    parsed["source_type"] = req.source_type
    parsed["source_ref"] = req.source_ref
    parsed["requirement_text"] = req.text

    return ImpactEntry.model_validate(parsed)


# ---------------------------------------------------------------------------
# Mechanic extraction
# ---------------------------------------------------------------------------

async def _extract_mechanics(
    hlr: GameIdentity,
    entries: list[ImpactEntry],
    caller: LLMCaller,
    hlr_context: str,
    schema: ExpandedGameSchema | None = None,
) -> list[MechanicDefinition]:
    """Extract game-level mechanics from the impact entries."""
    # Summarize entries for the mechanics prompt
    entry_summaries: list[str] = []
    for entry in entries:
        systems = [r.system for r in entry.runs]
        props_written = [f"{p.owner}.{p.name}" for p in entry.tracks if p.written_by]
        entry_summaries.append(
            f"- {entry.requirement_id} ({entry.source_type}/{entry.source_ref}): "
            f"systems={systems}, writes={props_written[:10]}"
        )

    prompt = (
        f"{hlr_context}\n\n"
        f"## Impact Matrix Summary ({len(entries)} entries)\n"
        + "\n".join(entry_summaries) + "\n\n"
        f"Extract ALL game-level mechanics."
    )

    # Build property-constrained mechanics prompt
    props_text = _build_allowed_properties_text_for_mechanics(schema)
    mechanics_prompt = MECHANICS_SYSTEM_PROMPT.replace("{allowed_properties}", props_text)

    label = "impact_mechanics"
    raw = await _call_llm(caller, mechanics_prompt, prompt, label)
    parsed = json.loads(raw)

    mechanics: list[MechanicDefinition] = []
    for m in parsed.get("mechanics", []):
        mechanics.append(MechanicDefinition.model_validate(m))

    return mechanics


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run_impact_matrix(
    hlr: GameIdentity,
    caller: LLMCaller,
    knowledge_dir: Path | None = None,
    schema: ExpandedGameSchema | None = None,
) -> ImpactMatrix:
    """Run HLR Step 2: build the full impact matrix.

    1. Extract requirements from HLR
    2. Trace each requirement in parallel via LLM
    3. Extract game-level mechanics
    4. Return the merged impact matrix
    """
    trace = get_trace()
    if trace:
        trace.phase_start("impact_matrix")

    kb = KnowledgeBase(knowledge_dir or _KNOWLEDGE_DIR)
    kb_context = kb.retrieve_context(hlr.game_name)
    if kb_context.is_empty():
        kb_context = kb.retrieve_context(hlr.genre)

    hlr_context = _build_hlr_context(hlr, kb_context)

    # Build enum-constrained system prompt (owners + systems shared, properties per-requirement)
    allowed_owners = _build_allowed_owners(hlr)
    allowed_systems = _build_allowed_systems(hlr)
    owners_text, systems_text, _ = _format_allowed_names(allowed_owners, allowed_systems)
    _log.info("Impact: allowed owners = %s", allowed_owners)
    _log.info("Impact: allowed systems = %s", allowed_systems)

    # Step 1: Extract requirements
    requirements = extract_requirements(hlr)
    _log.info("Impact: %d requirements to trace", len(requirements))

    # Step 2: Build per-requirement system prompts (scoped property lists)
    def _build_req_prompt(req: Requirement) -> str:
        scoped_props = _build_scoped_properties_for_requirement(
            schema, req.source_type, req.source_ref,
        )
        return IMPACT_SYSTEM_PROMPT.format(
            allowed_owners=owners_text,
            allowed_systems=systems_text,
            allowed_properties=scoped_props,
        )

    # Step 3: Trace all requirements in parallel
    _log.info("Impact: launching %d requirement traces in parallel", len(requirements))
    trace_tasks = [
        _trace_requirement(req, hlr_context, caller, _build_req_prompt(req))
        for req in requirements
    ]
    entries = await asyncio.gather(*trace_tasks)
    entries = list(entries)
    _log.info("Impact: %d entries traced", len(entries))

    # Step 3: Extract mechanics
    _log.info("Impact: extracting game-level mechanics")
    mechanics = await _extract_mechanics(hlr, entries, caller, hlr_context, schema=schema)
    _log.info("Impact: %d mechanics extracted", len(mechanics))

    matrix = ImpactMatrix(
        game_name=hlr.game_name,
        entries=entries,
        mechanics=mechanics,
    )

    # Log summary
    all_props = matrix.all_properties()
    by_owner = matrix.properties_by_owner()
    all_assets = matrix.all_assets()
    _log.info(
        "Impact: complete — %d entries, %d mechanics, %d total properties across %d entities, %d assets",
        len(entries), len(mechanics), len(all_props), len(by_owner), len(all_assets),
    )

    if trace:
        trace.event("impact_matrix", "impact_summary",
                     entries=len(entries), mechanics=len(mechanics),
                     properties=len(all_props), entities=len(by_owner),
                     assets=len(all_assets))
        trace.phase_end("impact_matrix")

    return matrix
