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
    MechanicHudEntity,
    MechanicSpec,
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

# Default verb if template doesn't specify (template SHOULD specify write_verb on every written property)
_DEFAULT_VERB = "set"


def _get_verb(prop: "PropertySpec") -> str:
    """Get the write verb from the template — falls back to 'set' if missing."""
    return prop.write_verb or _DEFAULT_VERB


# ---------------------------------------------------------------------------
# #1: MLR Interactions — deterministic from template written_by/read_by
# ---------------------------------------------------------------------------

def build_interactions_for_system(
    schema: "ExpandedGameSchema",
    system_name: str,
    scene_name: str,
    template_system_name: str | None = None,
) -> SystemInteractions:
    """Build interactions for one system in one scene from template data.

    system_name: the name to use in output (HLR name)
    template_system_name: the name to look up in template (if HLR mapped to a different template name)
    """
    lookup_name = template_system_name or system_name
    writes: list[tuple[str, "PropertySpec"]] = []  # (owner_role, prop)
    reads: list[tuple[str, "PropertySpec"]] = []

    def _scan(role: str, props: list["PropertySpec"]) -> None:
        for p in props:
            w_by = p.written_by if isinstance(p.written_by, list) else [p.written_by]
            r_by = p.read_by if isinstance(p.read_by, list) else [p.read_by]
            if lookup_name in w_by:
                writes.append((role, p))
            if lookup_name in r_by:
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
        verb = _get_verb(p)
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

    desc = schema.mechanic_descriptions.get(lookup_name, system_name)
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
    system_mapping: dict[str, str] | None = None,
    mechanic_specs: list[MechanicSpec] | None = None,
) -> list[SystemInteractions]:
    """Build interactions for all systems in a scene. Returns non-empty ones.

    For CUSTOM (new) systems present in mechanic_specs, interactions come from
    the spec's trigger/condition/effects entries. For template systems, they come
    from the written_by/read_by metadata in the template as before.
    """
    results = []
    mapping = system_mapping or {}
    specs_by_name = {m.system_name: m for m in (mechanic_specs or [])}

    for sys_name in system_names:
        if sys_name in specs_by_name:
            # Custom system — scaffold from the mechanic_spec
            si = build_interactions_from_spec(specs_by_name[sys_name], scene_name)
        else:
            # Template system — derive from written_by/read_by metadata
            template_name = mapping.get(sys_name)
            si = build_interactions_for_system(schema, sys_name, scene_name, template_system_name=template_name)
        if si.interactions:
            results.append(si)
    return results


def build_interactions_from_spec(spec: MechanicSpec, scene_name: str) -> SystemInteractions:
    """Convert a MechanicSpec's interactions into a SystemInteractions for one scene.

    Effects in the mechanic_spec are already structured {verb, target, description},
    so this is a straight 1:1 conversion.

    Scene filtering is the caller's responsibility — the builder emits interactions
    uniformly; the caller decides which scenes they apply to (via scene_manifest
    active_systems).
    """
    interactions: list[Interaction] = []
    for it in spec.interactions:
        effects = [Effect(verb=e.verb, target=e.target, description=e.description) for e in it.effects]
        interactions.append(Interaction(
            trigger=it.trigger,
            condition=it.condition or "always",
            effects=effects,
        ))
    return SystemInteractions(
        scene_name=scene_name,
        game_system=spec.system_name,
        interactions=interactions,
    )


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
    mechanic_specs: list[MechanicSpec] | None = None,
) -> EntitySpec:
    """Build an EntitySpec from template data. Game-agnostic.

    role: "fighter", "projectile", "hud_bar", "hud_text", "stage"
    If role is empty, inferred from parent_enum.

    mechanic_specs: optional list of custom feature specs from HLR. For fighter
    entities, their fighter/character-role properties are appended to the template
    properties. For HUD entities matching a spec.hud_entities entry, a custom HUD
    entity is produced using the spec's godot_node and reads.
    """
    # ---- Custom HUD widget: mechanic_spec.hud_entities entry takes priority ----
    if parent_enum == "hud_elements" and mechanic_specs:
        for spec in mechanic_specs:
            for hud in spec.hud_entities:
                if hud.name == entity_name:
                    return _build_custom_hud_entity(hud, spec, entity_name, parent_enum, scene_name)

    if not role:
        enum_to_role = {
            "characters": "fighter",
            "game_objects": "projectile",
            "hud_elements": "hud_bar",
            "stages": "stage",
        }
        role = enum_to_role.get(parent_enum, "hud_bar")
        # game_objects is heterogeneous: fighters, projectiles, VFX. Infer role
        # from the name so fighter instances get fighter_schema (current_health,
        # rage_stacks, etc.) and VFX get a minimal transient shape.
        if parent_enum == "game_objects":
            name_lower = entity_name.lower()
            if "fighter" in name_lower:
                role = "fighter"
            elif any(tok in name_lower for tok in ("vfx", "_fx", "spark", "burst", "particle")):
                role = "effect"
            else:
                role = "projectile"

    # "effect" role: minimal transient entity, no template schema.
    if role == "effect":
        return EntitySpec(
            scene_name=scene_name,
            entity_name=entity_name,
            parent_enum=parent_enum,
            object_type="transient",
            godot_node_type="Node2D",
            properties=[
                PropertyDecl(name="position", type="Vector2",
                             description="Spawn position, set by the spawning system."),
                PropertyDecl(name="active", type="bool",
                             description="Whether the effect is currently alive."),
                PropertyDecl(name="frames_alive", type="int",
                             description="Frames since spawn; destroyed when >= lifetime_frames."),
                PropertyDecl(name="lifetime_frames", type="int",
                             description="Total lifetime in frames before auto-despawn."),
                PropertyDecl(name="spawned_by", type="string",
                             description="System that spawned this effect."),
            ],
            action_sets=[],
            scene_enums=[],
        )

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

    # For fighters, inject mechanic_spec fighter/character-role properties
    if role == "fighter" and mechanic_specs:
        existing_names = {p.name for p in properties}
        for spec in mechanic_specs:
            for p in spec.properties:
                if p.role in ("fighter", "character") and p.name not in existing_names:
                    properties.append(PropertyDecl(
                        name=p.name,
                        type=p.type,
                        description=f"{p.purpose} [from {spec.system_name}]",
                    ))
                    existing_names.add(p.name)

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


def _build_custom_hud_entity(
    hud: MechanicHudEntity,
    spec: MechanicSpec,
    entity_name: str,
    parent_enum: str,
    scene_name: str,
) -> EntitySpec:
    """Build an EntitySpec for an HUD widget declared in a mechanic_spec.hud_entities entry.

    Declares display layout properties (position, size, colors, segment count, flash
    duration) that DLR fills with concrete values. The widget's runtime code reads
    fighter.{reads[i]} each frame and renders according to visual_states.
    """
    # Pull DLR-ready properties from the mechanic spec's constants when they look
    # like visual knobs — segment count, colors, flash frames, size hints.
    constant_hints = ", ".join(f"{c.name} ({c.purpose})" for c in spec.constants_for_dlr[:3])

    properties: list[PropertyDecl] = [
        PropertyDecl(
            name="position",
            type="Vector2",
            description=f"Screen position of the widget. Widget shows: {hud.displays}",
        ),
        PropertyDecl(
            name="size",
            type="Vector2",
            description="Widget size in pixels.",
        ),
        PropertyDecl(
            name="visible",
            type="bool",
            description="Whether the widget is currently rendered.",
        ),
        PropertyDecl(
            name="visual_states_description",
            type="string",
            description=(
                f"DISPLAY CONTRACT — widget MUST render each state distinguishably: "
                f"{hud.visual_states} "
                f"Runtime reads fighter.[{', '.join(hud.reads)}] each frame. "
                f"DLR-fillable visual knobs: {constant_hints}."
            ),
        ),
    ]
    # One binding property per fighter property this widget reads — so the DAG
    # can trace that this HUD depends on those fighter state properties.
    for r in hud.reads:
        properties.append(PropertyDecl(
            name=f"binds_fighter_{r}",
            type="string",
            description=(
                f"Runtime binding token — this widget reads fighter.{r} each frame to "
                f"update its display. Written by: {spec.system_name}."
            ),
        ))

    return EntitySpec(
        scene_name=scene_name,
        entity_name=entity_name,
        parent_enum=parent_enum,
        object_type="hud",
        godot_node_type=hud.godot_node,
        properties=properties,
        action_sets=[],
        scene_enums=[],
    )


# ---------------------------------------------------------------------------
# #4: Impact TRACKS — deterministic from template property metadata
# ---------------------------------------------------------------------------

def build_impact_entries(
    schema: "ExpandedGameSchema",
    system_names: list[str],
    characters: list[str],
    system_mapping: dict[str, str] | None = None,
    skip_systems: set[str] | None = None,
) -> list[ImpactEntry]:
    """Build Impact entries from template data. One entry per system.

    Derives TRACKS from written_by/read_by, RUNS from system existence.
    Game-agnostic — reads from whatever template is loaded.

    system_mapping: optional {hlr_name: template_name} for systems where HLR
    invented a different name for an existing template mechanic.

    Strips template systems not in HLR — users build games incrementally,
    only what HLR declares is included. Template systems referenced in
    written_by/read_by but absent from HLR are filtered out.
    """
    entries: list[ImpactEntry] = []
    req_idx = 0
    mapping = system_mapping or {}

    # Build the set of allowed template system names (those mapped from HLR)
    allowed_template_systems = set(mapping.values()) | set(system_names)
    # Also build template→HLR reverse mapping for translating written_by/read_by
    template_to_hlr = {v: k for k, v in mapping.items() if k != v}

    def _filter_systems(sys_list: list[str]) -> list[str]:
        """Strip template systems not in HLR; translate mapped ones to HLR names."""
        result = []
        for s in sys_list:
            if not s:
                continue
            if s in template_to_hlr:
                result.append(template_to_hlr[s])
            elif s in system_names:
                result.append(s)
            # else: template-only system, drop it
        return result

    skip = skip_systems or set()
    for hlr_sys_name in system_names:
        if hlr_sys_name in skip:
            continue  # handled by mechanic_specs loop in build_impact_matrix
        req_idx += 1
        # Use the template name to look up properties, but report the HLR name
        template_sys_name = mapping.get(hlr_sys_name, hlr_sys_name)
        desc = schema.mechanic_descriptions.get(template_sys_name, hlr_sys_name)

        tracks: list[PropertyImplication] = []
        runs: list[SystemRole] = [SystemRole(system=hlr_sys_name, responsibility=desc)]

        def _scan(role: str, props: list["PropertySpec"]) -> None:
            for p in props:
                w_by = p.written_by if isinstance(p.written_by, list) else [p.written_by]
                r_by = p.read_by if isinstance(p.read_by, list) else [p.read_by]
                if template_sys_name in w_by or template_sys_name in r_by:
                    tracks.append(PropertyImplication(
                        name=p.name,
                        owner=role,
                        owner_scope="instance" if p.scope == "role_generic" else "character_def",
                        type=p.type,
                        written_by=_filter_systems(w_by),
                        read_by=_filter_systems(r_by),
                        purpose=p.purpose,
                    ))

        _scan("fighter", schema.fighter_schema.properties)
        _scan("game", schema.game_config + schema.game_state + schema.game_derived)
        _scan("projectile", schema.projectile_schema.properties)

        entries.append(ImpactEntry(
            requirement_id=f"REQ_{req_idx:03d}",
            requirement_text=f"Game system: {hlr_sys_name} — {desc}",
            source_type="game_system",
            source_ref=hlr_sys_name,
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
    system_mapping: dict[str, str] | None = None,
    skip_systems: set[str] | None = None,
) -> list[MechanicDefinition]:
    """Build mechanic definitions from template data.

    Each system that writes state properties becomes a mechanic.
    Game-agnostic. Strips template systems not in HLR.
    """
    mechanics: list[MechanicDefinition] = []
    mapping = system_mapping or {}
    skip = skip_systems or set()

    for hlr_sys_name in system_names:
        if hlr_sys_name in skip:
            continue  # handled by mechanic_specs loop in build_impact_matrix
        template_sys_name = mapping.get(hlr_sys_name, hlr_sys_name)
        desc = schema.mechanic_descriptions.get(template_sys_name, "")
        if not desc:
            continue

        reads: list[str] = []
        writes: list[str] = []

        def _scan(role: str, props: list["PropertySpec"]) -> None:
            for p in props:
                w_by = p.written_by if isinstance(p.written_by, list) else [p.written_by]
                r_by = p.read_by if isinstance(p.read_by, list) else [p.read_by]
                if template_sys_name in r_by:
                    reads.append(f"{role}.{p.name}")
                if template_sys_name in w_by:
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
            name=f"{hlr_sys_name.replace('_system', '')}_mechanic",
            system=hlr_sys_name,
            description=desc,
            trigger=f"{hlr_sys_name} processes entity",
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
    system_mapping: dict[str, str] | None = None,
    mechanic_specs: list[MechanicSpec] | None = None,
) -> ImpactMatrix:
    """Build complete Impact Matrix deterministically. Zero LLM calls.

    mechanic_specs: optional list of custom-feature specs from HLR. For each
    spec, injects an ImpactEntry that traces the spec's properties so the DAG
    and impact matrix include custom (non-template) mechanics. The template
    loop skips these systems so each appears exactly once.
    """
    skip = {m.system_name for m in (mechanic_specs or [])}
    entries = build_impact_entries(schema, system_names, characters,
                                   system_mapping=system_mapping, skip_systems=skip)
    mechanics = build_mechanics(schema, system_names,
                                system_mapping=system_mapping, skip_systems=skip)

    if mechanic_specs:
        # Only game_systems are valid entries in impact read_by / written_by;
        # HUD widget names or role labels (e.g. "hud") must be stripped.
        valid_systems = set(system_names)

        def _filter(lst: list[str]) -> list[str]:
            return [s for s in lst if s in valid_systems]

        # Custom mechanic entries — one per mechanic_spec
        offset = len(entries)
        for i, spec in enumerate(mechanic_specs):
            tracks = [
                PropertyImplication(
                    name=p.name,
                    owner=p.role,
                    owner_scope=p.scope or "instance",
                    type=p.type,
                    written_by=_filter(p.written_by),
                    read_by=_filter(p.read_by),
                    purpose=p.purpose,
                )
                for p in spec.properties
            ]
            entries.append(ImpactEntry(
                requirement_id=f"REQ_CUSTOM_{offset + i + 1:03d}",
                requirement_text=f"Custom feature: {spec.system_name} — {spec.summary}",
                source_type="game_system",
                source_ref=spec.system_name,
                tracks=tracks,
                runs=[SystemRole(system=spec.system_name, responsibility=spec.summary)],
            ))

            # Custom mechanic entry — one per mechanic_spec
            props_written = [f"{p.role}.{p.name}" for p in spec.properties if spec.system_name in p.written_by]
            props_read = [f"{p.role}.{p.name}" for p in spec.properties if spec.system_name in p.read_by]
            mechanics.append(MechanicDefinition(
                name=f"{spec.system_name.replace('_system', '')}_mechanic",
                system=spec.system_name,
                description=spec.summary,
                trigger=f"{spec.system_name} processes interactions each frame",
                effect=f"Writes: {', '.join(props_written[:5])}",
                properties_read=props_read,
                properties_written=props_written,
            ))

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
