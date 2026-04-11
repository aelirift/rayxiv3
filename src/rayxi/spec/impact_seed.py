"""Deterministic seed builder: HLR + template + mechanic_specs → ImpactMap.

This is the STRICT-scope freeze. After this runs, the systems[] and scenes[]
lists are final — MLR cannot add to them, only refine what's inside.

No LLM calls. Pure data transform.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .impact_map import (
    Category,
    ImpactMap,
    PropertyNode,
    ReadEdge,
    Scope,
    WriteEdge,
    WriteKind,
)
from .models import GameIdentity, MechanicInteractionSpec, MechanicSpec

if TYPE_CHECKING:
    from rayxi.knowledge.mechanic_loader import ExpandedGameSchema, PropertySpec

_log = logging.getLogger("rayxi.spec.impact_seed")


# ---------------------------------------------------------------------------
# Write-kind inference
# ---------------------------------------------------------------------------

_LIFECYCLE_TRIGGER_KEYWORDS = (
    "round_start", "pre_round", "round_reset", "match_start", "new_round",
    "game_start", "reset", "respawn",
)


def _infer_write_kind(category: str, trigger: str) -> WriteKind:
    """Guess write_kind from property category + trigger prose.

    - config  → CONFIG_INIT always
    - derived → DERIVED always (shouldn't have writers, but…)
    - state with lifecycle-trigger prose → LIFECYCLE
    - state otherwise → FRAME_UPDATE
    """
    if category == "config":
        return WriteKind.CONFIG_INIT
    if category == "derived":
        return WriteKind.DERIVED
    t = trigger.lower()
    if any(k in t for k in _LIFECYCLE_TRIGGER_KEYWORDS):
        return WriteKind.LIFECYCLE
    return WriteKind.FRAME_UPDATE


def _map_category(template_cat: str) -> Category:
    if template_cat == "config":
        return Category.CONFIG
    if template_cat == "derived":
        return Category.DERIVED
    return Category.STATE


def _map_scope(template_scope: str) -> Scope:
    if template_scope in ("role_generic", "instance_unique"):
        return Scope.INSTANCE
    if template_scope == "character_def":
        return Scope.CHARACTER_DEF
    return Scope.INSTANCE


# ---------------------------------------------------------------------------
# Seed builder
# ---------------------------------------------------------------------------


def build_impact_seed(
    hlr: GameIdentity,
    schema: "ExpandedGameSchema",
    system_mapping: dict[str, str] | None = None,
    system_phases: dict[str, str] | None = None,
    property_enums: dict[str, list[str]] | None = None,
) -> ImpactMap:
    """Produce the impact-map seed from HLR + expanded template schema.

    Steps:
      1. Freeze scope (systems, scenes) from HLR.
      2. Emit one PropertyNode per template property (fighter, projectile, etc.)
      3. Emit write/read edges from template written_by / read_by.
      4. Emit one PropertyNode per mechanic_spec property.
      5. Emit write/read edges from mechanic_spec interactions.
      6. Emit PropertyNodes + read edges for mechanic_spec HUD widgets.

    Returns an ImpactMap that passes structural validation but has NO typed
    formulas, initial_values, or derivations — DLR fills those later.
    """
    imap = ImpactMap(
        game_name=hlr.game_name,
        systems=list(hlr.get_enum("game_systems")),
        scenes=[s.scene_name for s in _flat_scenes(hlr.scenes)],
    )

    mapping = system_mapping or {}
    template_to_hlr = {v: k for k, v in mapping.items() if k != v}

    # Copy phase metadata from the template (keyed by HLR system name). If a
    # system is an HLR rename of a template system, look up the template name
    # via system_mapping and use its phase. New (HLR-invented) systems with no
    # template origin fall back to "physics" via imap.phase_for().
    if system_phases:
        for hlr_system in imap.systems:
            template_name = mapping.get(hlr_system, hlr_system)
            phase = system_phases.get(template_name)
            if phase is None and hlr_system in system_phases:
                phase = system_phases[hlr_system]
            if phase:
                imap.phases[hlr_system] = phase


    def translate_system(name: str) -> str:
        """Map a template system name to its HLR equivalent (if mapped)."""
        return template_to_hlr.get(name, name)

    valid_systems = set(imap.systems)

    # ----- 1. Template fighter properties -----------------------------------
    _seed_role_props(
        imap=imap,
        role_owner="fighter",
        role_props=schema.fighter_schema.properties,
        valid_systems=valid_systems,
        translate_system=translate_system,
    )

    # Per-character archetype properties — each character gets its own entity.
    # No character should impact another's build. Instance-state (current_hp,
    # position, etc.) lives on the generic 'fighter' owner above; archetype data
    # (Ryu's hadouken damage, Ken's shoryuken startup) lives per-character here.
    for char_name, props in schema.per_character_unique.items():
        char_owner = f"character.{char_name}"
        for p in props:
            _seed_one_template_prop(
                imap=imap,
                owner=char_owner,
                prop=p,
                scope_override=Scope.CHARACTER_DEF,
                valid_systems=valid_systems,
                translate_system=translate_system,
            )

    # ----- 2. Template projectile properties --------------------------------
    if schema.projectile_schema.properties:
        _seed_role_props(
            imap=imap,
            role_owner="projectile",
            role_props=schema.projectile_schema.properties,
            valid_systems=valid_systems,
            translate_system=translate_system,
        )

    # ----- 3. Game-level properties -----------------------------------------
    for p in list(schema.game_config) + list(schema.game_state) + list(schema.game_derived):
        _seed_one_template_prop(
            imap=imap,
            owner="game",
            prop=p,
            scope_override=Scope.GAME,
            valid_systems=valid_systems,
            translate_system=translate_system,
        )

    # ----- 4. HUD bar / text schemas ----------------------------------------
    for p in schema.hud_bar_schema.properties:
        _seed_one_template_prop(
            imap=imap, owner="hud_bar", prop=p,
            valid_systems=valid_systems, translate_system=translate_system,
        )

    # ----- 5. Mechanic spec properties + interactions -----------------------
    for spec in hlr.mechanic_specs:
        _seed_mechanic_spec(imap, spec, valid_systems)

    # ----- 6. Mechanic spec HUD entities ------------------------------------
    for spec in hlr.mechanic_specs:
        _seed_mechanic_hud(imap, spec, valid_systems)

    # ----- 7. Enum value domains --------------------------------------------
    # Apply canonical enum_values from the HLT's property_enums block to every
    # matching node. This is the authoritative cross-system contract for
    # string-valued handoffs like fighter.current_action.
    if property_enums:
        enum_applied = 0
        for prop_id, values in property_enums.items():
            if not prop_id or prop_id.startswith("_") or not isinstance(values, list):
                continue
            node = imap.nodes.get(prop_id)
            if node is None:
                _log.warning(
                    "property_enums: no node for '%s' — enum values skipped", prop_id
                )
                continue
            node.enum_values = list(values)
            enum_applied += 1
        _log.info("Impact seed: applied enum_values to %d nodes", enum_applied)

    _log.info(
        "Impact seed: %d nodes, %d write edges, %d read edges, %d systems, %d scenes",
        len(imap.nodes), len(imap.write_edges), len(imap.read_edges),
        len(imap.systems), len(imap.scenes),
    )
    return imap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flat_scenes(scenes):
    flat = []
    for s in scenes:
        flat.append(s)
        if s.children:
            flat.extend(_flat_scenes(s.children))
    return flat


def _seed_role_props(imap, role_owner, role_props, valid_systems, translate_system):
    for p in role_props:
        _seed_one_template_prop(
            imap=imap,
            owner=role_owner,
            prop=p,
            valid_systems=valid_systems,
            translate_system=translate_system,
        )


def _seed_one_template_prop(
    imap: ImpactMap,
    owner: str,
    prop: "PropertySpec",
    valid_systems: set[str],
    translate_system,
    scope_override: Scope | None = None,
) -> None:
    """Convert one template PropertySpec into a PropertyNode + its write/read edges."""
    node_id = f"{owner}.{prop.name}"
    node = PropertyNode(
        id=node_id,
        owner=owner,
        name=prop.name,
        type=prop.type,
        category=_map_category(prop.category),
        scope=scope_override or _map_scope(prop.scope),
        description=prop.purpose or "",
        declared_by="template",
    )
    imap.add_node(node)

    # Write edges from written_by list
    written_by = prop.written_by if isinstance(prop.written_by, list) else [prop.written_by]
    for raw_system in written_by:
        if not raw_system:
            continue
        sys_name = translate_system(raw_system)
        if sys_name not in valid_systems:
            continue  # template system stripped from HLR
        imap.add_write_edge(WriteEdge(
            system=sys_name,
            target=node_id,
            write_kind=_infer_write_kind(prop.category, trigger=""),
            trigger="",
            declared_by="template",
        ))

    # Read edges
    read_by = prop.read_by if isinstance(prop.read_by, list) else [prop.read_by]
    for raw_system in read_by:
        if not raw_system:
            continue
        sys_name = translate_system(raw_system)
        if sys_name not in valid_systems:
            continue
        imap.add_read_edge(ReadEdge(
            system=sys_name,
            source=node_id,
            purpose=prop.purpose or "",
            declared_by="template",
        ))


# ---------------------------------------------------------------------------
# Mechanic spec seeding
# ---------------------------------------------------------------------------


_OWNER_ROLE_MAP = {
    "fighter": "fighter",
    "character": "fighter",
    "projectile": "projectile",
    "game": "game",
    "stage": "stage",
    "hud": "hud_bar",
}


def _seed_mechanic_spec(imap: ImpactMap, spec: MechanicSpec, valid_systems: set[str]) -> None:
    """Add a mechanic_spec's property nodes + interaction edges to the map."""
    origin = f"hlr_seed:{spec.system_name}"

    for p in spec.properties:
        owner = _OWNER_ROLE_MAP.get(p.role, p.role)
        node_id = f"{owner}.{p.name}"
        imap.add_node(PropertyNode(
            id=node_id,
            owner=owner,
            name=p.name,
            type=p.type,
            category=Category.STATE,  # mechanic_spec props are runtime state by default
            scope=Scope.INSTANCE if p.scope == "instance" else Scope(p.scope),
            description=p.purpose,
            declared_by=origin,
        ))

        # Write edges from property's written_by
        for s in p.written_by:
            if s in valid_systems:
                imap.add_write_edge(WriteEdge(
                    system=s,
                    target=node_id,
                    write_kind=WriteKind.LIFECYCLE if s == "round_system" and p.reset_on else WriteKind.FRAME_UPDATE,
                    trigger=p.reset_on if (s == "round_system" and p.reset_on) else "",
                    declared_by=origin,
                ))

        # Read edges — only include system-level reads here; HUD reads come via hud_entities
        for s in p.read_by:
            if s in valid_systems:
                imap.add_read_edge(ReadEdge(
                    system=s, source=node_id,
                    purpose=p.purpose, declared_by=origin,
                ))

    # Interactions → typed edges
    for i, it in enumerate(spec.interactions):
        _seed_interaction(imap, spec.system_name, it, i, valid_systems, origin)


def _seed_interaction(
    imap: ImpactMap,
    owning_system: str,
    interaction: MechanicInteractionSpec,
    idx: int,
    valid_systems: set[str],
    origin: str,
) -> None:
    """Convert one MechanicInteractionSpec into WriteEdges on the impact map."""
    if owning_system not in valid_systems:
        return
    trigger = interaction.trigger
    wk = _infer_write_kind("state", trigger)
    for eff in interaction.effects:
        # Target format: entity.property OR bare entity name (spawn/destroy)
        if eff.verb in ("spawn", "destroy"):
            # Spawn effects need a corresponding entity — handled by entity seeding elsewhere,
            # here we just log an audit so DLR knows about it
            imap.audit.append(f"interaction {origin}[{idx}] {eff.verb} target={eff.target} (entity)")
            continue
        if "." not in eff.target:
            imap.audit.append(f"interaction {origin}[{idx}] {eff.verb} target '{eff.target}' not entity.prop — skipped")
            continue
        target_id = eff.target
        if target_id not in imap.nodes:
            # This is OK — MLR may refine later. Log.
            imap.audit.append(f"interaction {origin}[{idx}] targets {target_id} — not yet in seed, MLR may add")
            continue
        imap.add_write_edge(WriteEdge(
            system=owning_system,
            target=target_id,
            write_kind=wk,
            trigger=trigger,
            declared_by=origin,
        ))


def _seed_mechanic_hud(imap: ImpactMap, spec: MechanicSpec, valid_systems: set[str]) -> None:
    """Emit PropertyNodes + read edges for every mechanic_spec HUD widget."""
    origin = f"hlr_seed:{spec.system_name}:hud"
    for hud in spec.hud_entities:
        owner = f"hud.{hud.name}"

        # Declare visual config properties for the widget itself
        config_props = [
            ("position", "Vector2", Category.CONFIG, f"Screen position of {hud.name}"),
            ("size", "Vector2", Category.CONFIG, f"Size of {hud.name}"),
            ("visible", "bool", Category.STATE, f"Whether {hud.name} is shown"),
        ]
        for name, ptype, cat, desc in config_props:
            pid = f"{owner}.{name}"
            imap.add_node(PropertyNode(
                id=pid, owner=owner, name=name, type=ptype,
                category=cat, scope=Scope.INSTANCE,
                description=desc, declared_by=origin,
            ))

        # Widget reads fighter.<prop> for each property in hud.reads
        for read_prop in hud.reads:
            source_id = f"fighter.{read_prop}"
            imap.add_read_edge(ReadEdge(
                system=hud.name,   # widget name as the reader
                source=source_id,
                purpose=f"{hud.name} displays {read_prop}. Visual states: {hud.visual_states[:80]}",
                declared_by=origin,
            ))
