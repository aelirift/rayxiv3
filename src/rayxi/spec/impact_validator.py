"""Impact Matrix validator — structural consistency checks.

Validates:
  - No orphan properties (written but never read)
  - No phantom properties (read but never written and not a constant)
  - No dead mechanics (mechanic references properties that don't exist)
  - Every entity referenced in SEES/DOES/TRACKS traces to an HLR enum
  - Every system referenced in RUNS is in HLR game_systems enum
  - Property ownership scope is consistent across entries

Returns a list of errors. Empty list = valid.
"""

from __future__ import annotations

from .models import GameIdentity, ImpactMatrix

from rayxi.trace import get_trace


def validate_impact_matrix(hlr: GameIdentity, matrix: ImpactMatrix) -> list[str]:
    """Validate the impact matrix against HLR. Returns list of issues."""
    trace = get_trace()
    errors: list[str] = []

    errors.extend(_check_orphan_properties(matrix))
    errors.extend(_check_phantom_properties(matrix))
    errors.extend(_check_entity_references(hlr, matrix))
    errors.extend(_check_system_references(hlr, matrix))
    errors.extend(_check_scope_consistency(matrix))
    errors.extend(_check_mechanics(matrix))

    if trace:
        trace.validation("impact_matrix", "impact_validator",
                          passed=len(errors) == 0, errors=errors)

    return errors


def _check_orphan_properties(matrix: ImpactMatrix) -> list[str]:
    """Properties that are written but never read by anything."""
    errors: list[str] = []
    by_owner = matrix.properties_by_owner()

    for owner, props in by_owner.items():
        for prop in props:
            if prop.written_by and not prop.read_by:
                errors.append(
                    f"Orphan property: {owner}.{prop.name} — written by "
                    f"{prop.written_by} but never read"
                )

    return errors


def _check_phantom_properties(matrix: ImpactMatrix) -> list[str]:
    """Properties that are read but never written (and not constants)."""
    errors: list[str] = []
    by_owner = matrix.properties_by_owner()

    for owner, props in by_owner.items():
        for prop in props:
            if prop.read_by and not prop.written_by:
                if prop.owner_scope not in ("character_def", "game"):
                    errors.append(
                        f"Phantom property: {owner}.{prop.name} — read by "
                        f"{prop.read_by} but never written "
                        f"(scope={prop.owner_scope}, should be character_def or game if constant)"
                    )

    return errors


def _check_entity_references(hlr: GameIdentity, matrix: ImpactMatrix) -> list[str]:
    """Every entity referenced should trace back to an HLR enum or role."""
    errors: list[str] = []
    # Build set of all known entity names from HLR enums
    known_entities: set[str] = set()
    for enum_def in hlr.enums:
        if enum_def.entity:
            known_entities.update(enum_def.values)

    # Also allow common role names and generic references
    role_patterns = {"p1_", "p2_", "cpu_", "player_", "active_", "target", "attacker", "defender"}

    # Collect all entity references from the matrix
    referenced: set[str] = set()
    for entry in matrix.entries:
        for s in entry.sees:
            referenced.add(s.entity)
        for d in entry.does:
            referenced.add(d.target_entity)
        for t in entry.tracks:
            referenced.add(t.owner)

    for entity in referenced:
        if entity in known_entities:
            continue
        # Allow role-patterned names
        if any(entity.startswith(p) for p in role_patterns):
            continue
        # Allow "game" as an owner
        if entity in ("game", "scene", "round", "match"):
            continue
        errors.append(
            f"Entity '{entity}' referenced in impact matrix but not in any HLR entity enum"
        )

    return errors


def _check_system_references(hlr: GameIdentity, matrix: ImpactMatrix) -> list[str]:
    """Every system referenced should be in HLR game_systems enum."""
    errors: list[str] = []
    known_systems = set(hlr.get_enum("game_systems"))

    referenced_systems: set[str] = set()
    for entry in matrix.entries:
        for r in entry.runs:
            referenced_systems.add(r.system)
        for t in entry.tracks:
            referenced_systems.update(t.written_by)
            referenced_systems.update(t.read_by)

    for system in referenced_systems:
        if system not in known_systems:
            errors.append(
                f"System '{system}' referenced in impact matrix but not in "
                f"HLR game_systems enum: {sorted(known_systems)}"
            )

    return errors


def _check_scope_consistency(matrix: ImpactMatrix) -> list[str]:
    """Same property on same owner should have consistent scope across entries."""
    errors: list[str] = []
    # owner.name → set of scopes seen
    scope_map: dict[str, set[str]] = {}

    for entry in matrix.entries:
        for prop in entry.tracks:
            key = f"{prop.owner}.{prop.name}"
            scope_map.setdefault(key, set()).add(prop.owner_scope)

    for key, scopes in scope_map.items():
        if len(scopes) > 1:
            errors.append(
                f"Inconsistent scope for {key}: {scopes} "
                f"(should be one of: instance, player_slot, character_def, game)"
            )

    return errors


def _check_mechanics(matrix: ImpactMatrix) -> list[str]:
    """Mechanic property references should exist in the matrix."""
    errors: list[str] = []
    # Build set of all owner.property
    all_props: set[str] = set()
    for entry in matrix.entries:
        for prop in entry.tracks:
            all_props.add(f"{prop.owner}.{prop.name}")

    for mech in matrix.mechanics:
        for prop_ref in mech.properties_read:
            if prop_ref not in all_props:
                # Allow generic references like "target.health"
                if not any(prop_ref.startswith(p) for p in ("target.", "attacker.", "defender.", "source.")):
                    errors.append(
                        f"Mechanic '{mech.name}' reads '{prop_ref}' "
                        f"but it's not in any impact entry's TRACKS"
                    )
        for prop_ref in mech.properties_written:
            if prop_ref not in all_props:
                if not any(prop_ref.startswith(p) for p in ("target.", "attacker.", "defender.", "source.")):
                    errors.append(
                        f"Mechanic '{mech.name}' writes '{prop_ref}' "
                        f"but it's not in any impact entry's TRACKS"
                    )

    return errors


def format_impact_summary(matrix: ImpactMatrix) -> str:
    """Human-readable impact matrix summary."""
    lines = [f"Impact Matrix: {matrix.game_name}", ""]

    # Properties by owner
    by_owner = matrix.properties_by_owner()
    lines.append(f"Entities with properties: {len(by_owner)}")
    for owner, props in sorted(by_owner.items()):
        scopes = {p.owner_scope for p in props}
        lines.append(f"  {owner} ({', '.join(scopes)}): {len(props)} properties")
        for prop in sorted(props, key=lambda p: p.name):
            w = ", ".join(prop.written_by) if prop.written_by else "(constant)"
            r = ", ".join(prop.read_by) if prop.read_by else "(unused)"
            lines.append(f"    .{prop.name}: {prop.type}  W={w}  R={r}")
            if prop.justified_by:
                lines.append(f"      justified by: {', '.join(prop.justified_by)}")
    lines.append("")

    # Mechanics
    lines.append(f"Mechanics: {len(matrix.mechanics)}")
    for mech in matrix.mechanics:
        lines.append(f"  {mech.name} [{mech.system}]")
        lines.append(f"    trigger: {mech.trigger}")
        lines.append(f"    effect:  {mech.effect}")
    lines.append("")

    # Assets
    assets = matrix.all_assets()
    lines.append(f"Asset requirements: {len(assets)}")
    by_type: dict[str, list[str]] = {}
    for a in assets:
        by_type.setdefault(a.asset_type, []).append(a.asset_id)
    for atype, aids in sorted(by_type.items()):
        lines.append(f"  {atype}: {len(aids)} — {', '.join(sorted(set(aids))[:10])}")

    return "\n".join(lines)
