"""Deterministic spec generators — replace LLM calls with template-driven code.

Every function here produces the SAME Pydantic models the LLM would return,
but derived from the mechanic template structure. Game-agnostic — works for
any genre template that follows the PropertySpec schema.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models import (
    ActionSet,
    Effect,
    EntitySpec,
    ImpactEntry,
    ImpactMatrix,
    InputImplication,
    Interaction,
    MechanicDefinition,
    PropertyDecl,
    PropertyImplication,
    SystemInteractions,
    SystemRole,
    VisualImplication,
)

if TYPE_CHECKING:
    from rayxi.knowledge.mechanic_loader import ExpandedGameSchema, PropertySpec

_log = logging.getLogger("rayxi.spec.deterministic")


# ---------------------------------------------------------------------------
# Verb inference: what verb does a system use when writing a property?
# ---------------------------------------------------------------------------

_TYPE_TO_WRITE_VERB: dict[str, str] = {
    "int": "set",
    "float": "set",
    "bool": "set",
    "string": "set_state",
    "Vector2": "set",
    "object": "set",
    "array": "set",
    "enum": "set_state",
}

# Properties whose names imply subtraction/addition rather than set
_SUBTRACT_HINTS = {"current_health", "life_points", "mana", "stamina", "energy", "fuel"}
_INCREMENT_HINTS = {"combo_count", "hit_count", "stun_meter", "juggle_count", "round_wins", "score"}
_TOGGLE_HINTS = {"is_ko", "is_stunned", "is_blocking", "is_airborne", "is_crouching",
                 "is_firing_projectile", "charge_ready", "is_drifting", "is_eliminated"}


def _infer_verb(prop_name: str, prop_type: str) -> str:
    """Infer the most appropriate verb for writing a property."""
    if prop_name in _SUBTRACT_HINTS:
        return "subtract"
    if prop_name in _INCREMENT_HINTS:
        return "increment"
    if prop_name in _TOGGLE_HINTS:
        return "set"
    return _TYPE_TO_WRITE_VERB.get(prop_type, "set")


# ---------------------------------------------------------------------------
# #1: MLR Interactions — deterministic from template written_by/read_by
# ---------------------------------------------------------------------------

def build_interactions_for_system(
    schema: "ExpandedGameSchema",
    system_name: str,
    scene_name: str,
) -> SystemInteractions:
    """Build interactions for one system in one scene from template data.

    Reads template properties' written_by/read_by to produce effects.
    Game-agnostic — works for any template.
    """
    writes: list[tuple[str, "PropertySpec"]] = []  # (owner_role, prop)
    reads: list[tuple[str, "PropertySpec"]] = []

    def _scan(role: str, props: list["PropertySpec"]) -> None:
        for p in props:
            w_by = p.written_by if isinstance(p.written_by, list) else [p.written_by]
            r_by = p.read_by if isinstance(p.read_by, list) else [p.read_by]
            if system_name in w_by:
                writes.append((role, p))
            if system_name in r_by:
                reads.append((role, p))

    _scan("fighter", schema.fighter_schema.properties)
    for char_props in schema.per_character_unique.values():
        _scan("fighter", char_props)
        break
    _scan("game", schema.game_config + schema.game_state + schema.game_derived)
    _scan("projectile", schema.projectile_schema.properties)

    if not writes:
        return SystemInteractions(scene_name=scene_name, game_system=system_name)

    # Group writes by category for cleaner interactions
    config_writes = [(r, p) for r, p in writes if p.category == "config"]
    state_writes = [(r, p) for r, p in writes if p.category == "state"]

    effects: list[Effect] = []
    for role, p in state_writes:
        verb = _infer_verb(p.name, p.type)
        effects.append(Effect(
            verb=verb,
            target=f"{role}.{p.name}",
            description=p.purpose or f"{system_name} writes {p.name}",
        ))

    if not effects:
        return SystemInteractions(scene_name=scene_name, game_system=system_name)

    # Build condition from the system's read properties
    read_names = [f"{r}.{p.name}" for r, p in reads[:3]]
    condition = f"reads {', '.join(read_names)}" if read_names else "always"

    desc = schema.mechanic_descriptions.get(system_name, system_name)
    interaction = Interaction(
        trigger=f"{system_name} processes: {desc}",
        condition=condition,
        effects=effects,
    )

    return SystemInteractions(
        scene_name=scene_name,
        game_system=system_name,
        interactions=[interaction],
    )


def build_all_interactions(
    schema: "ExpandedGameSchema",
    system_names: list[str],
    scene_name: str,
) -> list[SystemInteractions]:
    """Build interactions for all systems in a scene. Returns non-empty ones."""
    results = []
    for sys_name in system_names:
        si = build_interactions_for_system(schema, sys_name, scene_name)
        if si.interactions:
            results.append(si)
    return results


# ---------------------------------------------------------------------------
# #2: MLR Entity specs — deterministic from template RoleSchema
# ---------------------------------------------------------------------------

_ROLE_TO_OBJECT_TYPE = {
    "fighter": "character",
    "projectile": "transient",
    "hud_bar": "hud",
    "hud_text": "hud",
    "stage": "background",
}

_HUD_NAME_TO_NODE_TYPE = {
    "health": "ProgressBar",
    "bar": "ProgressBar",
    "meter": "ProgressBar",
    "timer": "Label",
    "display": "Label",
    "counter": "Label",
    "label": "Label",
    "text": "Label",
    "pips": "Label",
    "score": "Label",
    "announce": "Label",
    "banner": "Label",
}


def build_entity_spec(
    schema: "ExpandedGameSchema",
    entity_name: str,
    parent_enum: str,
    scene_name: str,
    role: str = "",
) -> EntitySpec:
    """Build an EntitySpec from template data. Game-agnostic.

    role: "fighter", "projectile", "hud_bar", "hud_text", "stage"
    If role is empty, inferred from parent_enum.
    """
    if not role:
        enum_to_role = {
            "characters": "fighter",
            "game_objects": "projectile",
            "hud_elements": "hud_bar",
            "stages": "stage",
        }
        role = enum_to_role.get(parent_enum, "hud_bar")

    # Get the right schema
    role_schema = {
        "fighter": schema.fighter_schema,
        "projectile": schema.projectile_schema,
        "hud_bar": schema.hud_bar_schema,
        "hud_text": schema.hud_text_schema,
        "stage": schema.stage_schema,
    }.get(role, schema.hud_bar_schema)

    # Properties
    properties = [
        PropertyDecl(name=p.name, type=p.type, description=p.purpose)
        for p in role_schema.properties
    ]

    # For fighters, add per-character unique properties
    if role == "fighter" and entity_name in schema.per_character_unique:
        for p in schema.per_character_unique[entity_name]:
            properties.append(PropertyDecl(name=p.name, type=p.type, description=p.purpose))

    # Godot node type
    godot_node_type = role_schema.godot_base_node
    if not godot_node_type and role in ("hud_bar", "hud_text"):
        # Infer from entity name
        name_lower = entity_name.lower()
        for hint, node_type in _HUD_NAME_TO_NODE_TYPE.items():
            if hint in name_lower:
                godot_node_type = node_type
                break
        if not godot_node_type:
            godot_node_type = "Label"

    # Object type
    object_type = _ROLE_TO_OBJECT_TYPE.get(role, "ui")

    # Action sets for fighters — derive from template mechanics
    action_sets: list[ActionSet] = []
    if role == "fighter":
        # Normal attacks from template
        normal_attacks = [
            p.name.replace("_startup", "")
            for p in role_schema.properties
            if p.name.endswith("_startup") and p.source_mechanic == "special_move_system"
        ]
        if not normal_attacks:
            # Try from attack template naming
            normal_attacks = [
                p.name.replace("_damage", "")
                for p in role_schema.properties
                if p.name.endswith("_damage") and "punch" in p.name or "kick" in p.name
            ]
        if normal_attacks:
            action_sets.append(ActionSet(
                owner=entity_name,
                category="normal_attacks",
                actions=sorted(set(normal_attacks)),
            ))

        # Movement actions
        action_sets.append(ActionSet(
            owner=entity_name,
            category="movement",
            actions=["walk_forward", "walk_backward", "jump", "crouch"],
        ))

        # Defense
        action_sets.append(ActionSet(
            owner=entity_name,
            category="defense",
            actions=["block_stand", "block_crouch"],
        ))

    return EntitySpec(
        scene_name=scene_name,
        entity_name=entity_name,
        parent_enum=parent_enum,
        object_type=object_type,
        godot_node_type=godot_node_type,
        properties=properties,
        action_sets=action_sets,
    )


# ---------------------------------------------------------------------------
# #4: Impact TRACKS — deterministic from template property metadata
# ---------------------------------------------------------------------------

def build_impact_entries(
    schema: "ExpandedGameSchema",
    system_names: list[str],
    characters: list[str],
) -> list[ImpactEntry]:
    """Build Impact entries from template data. One entry per system.

    Derives TRACKS from written_by/read_by, RUNS from system existence.
    Game-agnostic — reads from whatever template is loaded.
    """
    entries: list[ImpactEntry] = []
    req_idx = 0

    for sys_name in system_names:
        req_idx += 1
        desc = schema.mechanic_descriptions.get(sys_name, sys_name)

        tracks: list[PropertyImplication] = []
        runs: list[SystemRole] = [SystemRole(system=sys_name, responsibility=desc)]

        def _scan(role: str, props: list["PropertySpec"]) -> None:
            for p in props:
                w_by = p.written_by if isinstance(p.written_by, list) else [p.written_by]
                r_by = p.read_by if isinstance(p.read_by, list) else [p.read_by]
                if sys_name in w_by or sys_name in r_by:
                    tracks.append(PropertyImplication(
                        name=p.name,
                        owner=role,
                        owner_scope="instance" if p.scope == "role_generic" else "character_def",
                        type=p.type,
                        written_by=[s for s in w_by if s],
                        read_by=[s for s in r_by if s],
                        purpose=p.purpose,
                    ))

        _scan("fighter", schema.fighter_schema.properties)
        _scan("game", schema.game_config + schema.game_state + schema.game_derived)
        _scan("projectile", schema.projectile_schema.properties)

        entries.append(ImpactEntry(
            requirement_id=f"REQ_{req_idx:03d}",
            requirement_text=f"Game system: {sys_name} — {desc}",
            source_type="game_system",
            source_ref=sys_name,
            tracks=tracks,
            runs=runs,
        ))

    return entries


# ---------------------------------------------------------------------------
# #5: Impact Mechanics — deterministic from template structure
# ---------------------------------------------------------------------------

def build_mechanics(
    schema: "ExpandedGameSchema",
    system_names: list[str],
) -> list[MechanicDefinition]:
    """Build mechanic definitions from template data.

    Each system that writes state properties becomes a mechanic.
    Game-agnostic.
    """
    mechanics: list[MechanicDefinition] = []

    for sys_name in system_names:
        desc = schema.mechanic_descriptions.get(sys_name, "")
        if not desc:
            continue

        reads: list[str] = []
        writes: list[str] = []

        def _scan(role: str, props: list["PropertySpec"]) -> None:
            for p in props:
                w_by = p.written_by if isinstance(p.written_by, list) else [p.written_by]
                r_by = p.read_by if isinstance(p.read_by, list) else [p.read_by]
                if sys_name in r_by:
                    reads.append(f"{role}.{p.name}")
                if sys_name in w_by:
                    writes.append(f"{role}.{p.name}")

        _scan("fighter", schema.fighter_schema.properties)
        for char_props in schema.per_character_unique.values():
            _scan("fighter", char_props)
            break
        _scan("game", schema.game_config + schema.game_state + schema.game_derived)
        _scan("projectile", schema.projectile_schema.properties)

        if not writes:
            continue

        mechanics.append(MechanicDefinition(
            name=f"{sys_name.replace('_system', '')}_mechanic",
            system=sys_name,
            description=desc,
            trigger=f"{sys_name} processes entity",
            effect=f"Updates: {', '.join(writes[:5])}",
            properties_read=reads,
            properties_written=writes,
        ))

    _log.info("Deterministic: %d mechanics from %d systems", len(mechanics), len(system_names))
    return mechanics


def build_impact_matrix(
    schema: "ExpandedGameSchema",
    system_names: list[str],
    characters: list[str],
) -> ImpactMatrix:
    """Build complete Impact Matrix deterministically. Zero LLM calls."""
    entries = build_impact_entries(schema, system_names, characters)
    mechanics = build_mechanics(schema, system_names)

    _log.info(
        "Deterministic Impact: %d entries, %d mechanics, %d total properties",
        len(entries), len(mechanics),
        sum(len(e.tracks) for e in entries),
    )

    return ImpactMatrix(
        game_name=schema.game_name,
        entries=entries,
        mechanics=mechanics,
    )
