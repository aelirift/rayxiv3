"""Deterministic seed builder: HLR + template + mechanic_specs → ImpactMap.

This is the STRICT-scope freeze. After this runs, the systems[] and scenes[]
lists are final — MLR cannot add to them, only refine what's inside.

No LLM calls. Pure data transform.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import TYPE_CHECKING

from .expr import parse_expr
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


_VECTOR2_RE = re.compile(r"^Vector2\(\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*\)$")
_RECT2_RE = re.compile(
    r"^Rect2\(\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*\)$"
)


def _template_literal_expr(type_name: str, raw_value):
    normalized = (type_name or "").strip().lower()

    if normalized in {"string", "str"}:
        return parse_expr({"kind": "literal", "type": "string", "value": "" if raw_value is None else str(raw_value)})

    if raw_value is None:
        return None

    if isinstance(raw_value, str):
        text = raw_value.strip()
        if text == "" or text.lower() == "null":
            return None
    else:
        text = raw_value

    if normalized in {"int", "integer"}:
        value = int(float(text))
        return parse_expr({"kind": "literal", "type": "int", "value": value})

    if normalized in {"float", "number"}:
        value = float(text)
        return parse_expr({"kind": "literal", "type": "float", "value": value})

    if normalized in {"bool", "boolean"}:
        if isinstance(text, bool):
            value = text
        else:
            lowered = str(text).strip().lower()
            if lowered not in {"true", "false"}:
                return None
            value = lowered == "true"
        return parse_expr({"kind": "literal", "type": "bool", "value": value})

    if normalized == "vector2":
        if isinstance(text, (list, tuple)) and len(text) == 2:
            pair = [float(text[0]), float(text[1])]
            return parse_expr({"kind": "literal", "type": "vector2", "value": pair})
        match = _VECTOR2_RE.match(str(text))
        if match:
            return parse_expr({
                "kind": "literal",
                "type": "vector2",
                "value": [float(match.group(1)), float(match.group(2))],
            })
        return None

    if normalized == "rect2":
        if isinstance(text, (list, tuple)) and len(text) == 4:
            rect = [float(text[0]), float(text[1]), float(text[2]), float(text[3])]
            return parse_expr({"kind": "literal", "type": "rect2", "value": rect})
        match = _RECT2_RE.match(str(text))
        if match:
            return parse_expr({
                "kind": "literal",
                "type": "rect2",
                "value": [float(match.group(i)) for i in range(1, 5)],
            })
        return None

    if normalized == "color":
        if isinstance(text, str) and text.startswith("#"):
            return parse_expr({"kind": "literal", "type": "color", "value": text})
        return None

    if normalized in {"list", "array"}:
        if isinstance(text, list):
            return parse_expr({"kind": "literal", "type": "list", "value": text})
        parsed = ast.literal_eval(str(text))
        if isinstance(parsed, list):
            return parse_expr({"kind": "literal", "type": "list", "value": parsed})
        return None

    if normalized in {"dict", "object"}:
        if isinstance(text, dict):
            return parse_expr({"kind": "literal", "type": "dict", "value": text})
        parsed = ast.literal_eval(str(text))
        if isinstance(parsed, dict):
            return parse_expr({"kind": "literal", "type": "dict", "value": parsed})
        return None

    return None


def _template_initial_expr(prop: "PropertySpec", category: Category):
    if category == Category.DERIVED:
        return None
    for candidate in (prop.initial, prop.default):
        try:
            expr = _template_literal_expr(prop.type, candidate)
        except (ValueError, SyntaxError):
            expr = None
        if expr is not None:
            return expr
    return None


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

    Returns an ImpactMap that passes structural validation. Borrowed template
    literals are preserved on nodes when available; DLR fills any remaining
    missing values and formulas later.
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
    valid_read_consumers = valid_systems | {"hud_bar", "hud_text"}

    # ----- 1. Template fighter properties -----------------------------------
    _seed_role_props(
        imap=imap,
        role_owner="fighter",
        role_props=schema.fighter_schema.properties,
        valid_systems=valid_systems,
        valid_read_consumers=valid_read_consumers,
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
                valid_read_consumers=valid_read_consumers,
                translate_system=translate_system,
            )

    # ----- 2. Template projectile properties --------------------------------
    if schema.projectile_schema.properties:
        _seed_role_props(
            imap=imap,
            role_owner="projectile",
            role_props=schema.projectile_schema.properties,
            valid_systems=valid_systems,
            valid_read_consumers=valid_read_consumers,
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
            valid_read_consumers=valid_read_consumers,
            translate_system=translate_system,
        )

    # ----- 4. HUD bar / text schemas ----------------------------------------
    for p in schema.hud_bar_schema.properties:
        _seed_one_template_prop(
            imap=imap, owner="hud_bar", prop=p,
            valid_systems=valid_systems,
            valid_read_consumers=valid_read_consumers,
            translate_system=translate_system,
        )

    # ----- 5. Mechanic spec properties + interactions -----------------------
    for spec in hlr.mechanic_specs:
        _seed_mechanic_spec(imap, hlr, spec, valid_systems)

    # ----- 6. Mechanic spec HUD entities ------------------------------------
    for spec in hlr.mechanic_specs:
        _seed_mechanic_hud(imap, hlr, spec, valid_systems)

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


def _seed_role_props(imap, role_owner, role_props, valid_systems, valid_read_consumers, translate_system):
    for p in role_props:
        _seed_one_template_prop(
            imap=imap,
            owner=role_owner,
            prop=p,
            valid_systems=valid_systems,
            valid_read_consumers=valid_read_consumers,
            translate_system=translate_system,
        )


def _seed_one_template_prop(
    imap: ImpactMap,
    owner: str,
    prop: "PropertySpec",
    valid_systems: set[str],
    valid_read_consumers: set[str],
    translate_system,
    scope_override: Scope | None = None,
) -> None:
    """Convert one template PropertySpec into a PropertyNode + its write/read edges."""
    node_id = f"{owner}.{prop.name}"
    category = _map_category(prop.category)
    node = PropertyNode(
        id=node_id,
        owner=owner,
        name=prop.name,
        type=prop.type,
        category=category,
        scope=scope_override or _map_scope(prop.scope),
        description=prop.purpose or "",
        initial_value=_template_initial_expr(prop, category),
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
            imap.audit.append(f"seed skipped unresolved template writer {raw_system} for {node_id}")
            continue
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
        if sys_name not in valid_read_consumers:
            imap.audit.append(f"seed skipped unresolved template reader {raw_system} for {node_id}")
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


def _mechanic_property_owners(hlr: GameIdentity, role: str, scope: str) -> list[str]:
    normalized_role = (role or "").strip()
    if normalized_role == "hud":
        return ["hud_bar"]
    if normalized_role in {"game", "stage", "projectile", "fighter"}:
        return [normalized_role]
    if normalized_role == "character" or scope == "character_def":
        characters = hlr.get_enum("characters")
        if characters:
            return [f"character.{char}" for char in characters]
        return ["character.default"]
    if normalized_role:
        return [normalized_role]
    return ["game"]


def _seed_mechanic_spec(
    imap: ImpactMap,
    hlr: GameIdentity,
    spec: MechanicSpec,
    valid_systems: set[str],
) -> None:
    """Add a mechanic_spec's property nodes + interaction edges to the map."""
    origin = f"hlr_seed:{spec.system_name}"

    for p in spec.properties:
        owners = _mechanic_property_owners(hlr, p.role, p.scope)
        node_scope = Scope.INSTANCE if p.scope == "instance" else Scope(p.scope)
        for owner in owners:
            node_id = f"{owner}.{p.name}"
            imap.add_node(PropertyNode(
                id=node_id,
                owner=owner,
                name=p.name,
                type=p.type,
                category=Category.STATE,  # mechanic_spec props are runtime state by default
                scope=node_scope,
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


def _seed_mechanic_hud(
    imap: ImpactMap,
    hlr: GameIdentity,
    spec: MechanicSpec,
    valid_systems: set[str],
) -> None:
    """Emit PropertyNodes + read edges for every mechanic_spec HUD widget."""
    origin = f"hlr_seed:{spec.system_name}:hud"
    prop_sources: dict[str, list[str]] = {}
    for prop in spec.properties:
        for owner in _mechanic_property_owners(hlr, prop.role, prop.scope):
            prop_sources.setdefault(prop.name, []).append(f"{owner}.{prop.name}")
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
            if "." in read_prop:
                source_id = read_prop
            else:
                candidates = prop_sources.get(read_prop, [])
                source_id = candidates[0] if candidates else f"fighter.{read_prop}"
            imap.add_read_edge(ReadEdge(
                system=hud.name,   # widget name as the reader
                source=source_id,
                purpose=f"{hud.name} displays {read_prop}. Visual states: {hud.visual_states[:80]}",
                declared_by=origin,
            ))
