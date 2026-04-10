"""Interaction map — cross-reference interactions against entities.

Validates that every interaction effect targets an entity+property
that actually exists in the scene. Flags:
  - Orphan targets: effect references entity not in scene
  - Missing properties: effect references property not declared on entity
  - Verb/type mismatch: e.g. "subtract" on a bool property

Usage:
    from rayxi.verify.interaction_map import validate_interactions, build_interaction_map

    issues = validate_interactions(scene_mlrs)
    imap = build_interaction_map(scene_mlrs)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rayxi.spec.mlr import SceneMLR
from rayxi.trace import get_trace


@dataclass
class InteractionRef:
    """One interaction effect and what it targets."""
    scene: str
    system: str
    trigger: str
    verb: str
    target: str  # "entity.property"
    entity: str  # parsed from target
    property: str  # parsed from target


@dataclass
class InteractionMap:
    """Map of which entities/properties are touched by which interactions."""
    # entity_name → {property_name → list of InteractionRef}
    entity_refs: dict[str, dict[str, list[InteractionRef]]] = field(default_factory=dict)
    # Entities referenced but not declared
    orphan_targets: list[InteractionRef] = field(default_factory=list)
    # Properties referenced but not declared on entity
    missing_properties: list[InteractionRef] = field(default_factory=list)


def _parse_target(target: str) -> tuple[str, str]:
    """Parse 'entity.property' → (entity, property). Falls back to (target, '')."""
    if "." in target:
        parts = target.split(".", 1)
        return parts[0], parts[1]
    return target, ""


def build_interaction_map(scene_mlr: SceneMLR) -> InteractionMap:
    """Build an interaction map for one scene."""
    imap = InteractionMap()

    # Build entity→property index from scene entities
    entity_props: dict[str, set[str]] = {}
    for entity in scene_mlr.entities:
        entity_props[entity.entity_name] = {p.name for p in entity.properties}

    # Walk all interactions
    for si in scene_mlr.system_interactions:
        for interaction in si.interactions:
            for eff in interaction.effects:
                entity_name, prop_name = _parse_target(eff.target)
                ref = InteractionRef(
                    scene=scene_mlr.scene_name,
                    system=si.game_system,
                    trigger=interaction.trigger,
                    verb=eff.verb,
                    target=eff.target,
                    entity=entity_name,
                    property=prop_name,
                )

                # Check entity exists
                if entity_name not in entity_props:
                    imap.orphan_targets.append(ref)
                    continue

                # Check property exists
                if prop_name and prop_name not in entity_props[entity_name]:
                    imap.missing_properties.append(ref)

                # Record reference
                imap.entity_refs.setdefault(entity_name, {}).setdefault(prop_name, []).append(ref)

    return imap


# Verbs that expect numeric targets
_NUMERIC_VERBS = {"subtract", "add", "increment", "decrement", "multiply", "divide"}
# Verbs that expect state/enum targets
_STATE_VERBS = {"set_state"}
# Verbs that expect bool targets
_BOOL_VERBS = {"enable", "disable"}


def validate_interactions(scene_mlrs: list[SceneMLR]) -> list[str]:
    """Validate all interaction targets across all scenes. Returns issues."""
    trace = get_trace()
    issues: list[str] = []

    for scene_mlr in scene_mlrs:
        imap = build_interaction_map(scene_mlr)
        sn = scene_mlr.scene_name

        # Orphan targets
        for ref in imap.orphan_targets:
            issues.append(
                f"{sn}/{ref.system}: effect targets entity '{ref.entity}' "
                f"not in scene (verb={ref.verb}, target={ref.target})"
            )

        # Missing properties
        for ref in imap.missing_properties:
            issues.append(
                f"{sn}/{ref.system}: effect targets property '{ref.property}' "
                f"not declared on '{ref.entity}' (verb={ref.verb})"
            )

        # Verb/type mismatch (best-effort — check declared types)
        entity_types: dict[str, dict[str, str]] = {}
        for entity in scene_mlr.entities:
            entity_types[entity.entity_name] = {p.name: p.type for p in entity.properties}

        for entity_name, props in imap.entity_refs.items():
            if entity_name not in entity_types:
                continue
            for prop_name, refs in props.items():
                if not prop_name or prop_name not in entity_types[entity_name]:
                    continue
                prop_type = entity_types[entity_name][prop_name].lower()
                for ref in refs:
                    if ref.verb in _NUMERIC_VERBS and prop_type in ("bool", "string"):
                        issues.append(
                            f"{sn}/{ref.system}: numeric verb '{ref.verb}' on "
                            f"{prop_type} property '{entity_name}.{prop_name}'"
                        )
                    if ref.verb in _BOOL_VERBS and prop_type not in ("bool",):
                        issues.append(
                            f"{sn}/{ref.system}: bool verb '{ref.verb}' on "
                            f"{prop_type} property '{entity_name}.{prop_name}'"
                        )

    if trace:
        trace.verify("interaction_map", "all_scenes", passed=len(issues) == 0, issues=issues)

    return issues


def format_interaction_map(scene_mlr: SceneMLR) -> str:
    """Human-readable interaction map for one scene."""
    imap = build_interaction_map(scene_mlr)
    lines = [f"Interaction map: {scene_mlr.scene_name}", ""]

    for entity_name, props in sorted(imap.entity_refs.items()):
        lines.append(f"  {entity_name}:")
        for prop_name, refs in sorted(props.items()):
            systems = sorted({r.system for r in refs})
            verbs = sorted({r.verb for r in refs})
            lines.append(f"    .{prop_name or '(self)'}: {', '.join(verbs)} by [{', '.join(systems)}]")
    lines.append("")

    if imap.orphan_targets:
        lines.append(f"  Orphan targets: {len(imap.orphan_targets)}")
        for ref in imap.orphan_targets:
            lines.append(f"    {ref.system}: {ref.verb}({ref.target})")
    if imap.missing_properties:
        lines.append(f"  Missing properties: {len(imap.missing_properties)}")
        for ref in imap.missing_properties:
            lines.append(f"    {ref.system}: {ref.verb}({ref.target})")

    return "\n".join(lines)
