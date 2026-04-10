"""Pipeline phase validators — gates each phase before proceeding.

Each validator returns (passed: bool, errors: list[str]).
If not passed, the pipeline stops.

Phase order:
  1. HLR → validate_hlr (existing)
  2. Template → validate_template_coverage
  3. DAG → validate_dag (existing) + validate_dag_completeness
  4. DLR Fill → validate_dlr_fill
  5. GDScript Gen → validate_scripts_parse
  6. Godot Project → validate_godot_project
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from rayxi.knowledge.mechanic_loader import ExpandedGameSchema
from rayxi.spec.models import EntitySpec, GameIdentity, ImpactMatrix

from .dag import GameDAG

_log = logging.getLogger("rayxi.build.validators")


# ---------------------------------------------------------------------------
# Phase 2: Template coverage
# ---------------------------------------------------------------------------

def validate_template_coverage(
    hlr: GameIdentity,
    schema: ExpandedGameSchema,
) -> tuple[bool, list[str]]:
    """Verify the template covers all HLR requirements."""
    errors: list[str] = []

    # Every character must have an entry in per_character_unique
    for char in hlr.get_enum("characters"):
        if char not in schema.per_character_unique:
            errors.append(f"Character '{char}' has no unique properties in schema")

    # Fighter schema must have properties
    if not schema.fighter_schema.properties:
        errors.append("Fighter schema has no properties — template may be empty")

    # Must have animations defined
    if not schema.fighter_schema.animations_required:
        errors.append("No animations defined in fighter schema")

    # Game config must have basic properties
    game_prop_names = {p.name for p in schema.game_config}
    required_game_props = {"screen_w", "screen_h", "floor_y", "gravity"}
    missing = required_game_props - game_prop_names
    if missing:
        errors.append(f"Game config missing required properties: {missing}")

    passed = len(errors) == 0
    _log.info("Template validation: %s (%d errors)", "PASSED" if passed else "FAILED", len(errors))
    return passed, errors


# ---------------------------------------------------------------------------
# Phase 3: DAG completeness
# ---------------------------------------------------------------------------

def validate_dag_completeness(dag: GameDAG) -> tuple[bool, list[str]]:
    """Check DAG structural completeness beyond basic validation."""
    errors: list[str] = []

    # Must have at least one fighter
    if not dag.fighter_entities:
        errors.append("No fighter entities in DAG")

    # Must have at least one scene
    if not dag.scenes:
        errors.append("No scenes in DAG")

    # Every fighter must have config properties with types
    for name, entity in dag.fighter_entities.items():
        config = entity.config_props
        if not config:
            errors.append(f"Fighter '{name}' has no config properties")
        for p in config:
            if not p.type:
                errors.append(f"Fighter '{name}'.{p.name}: missing type")

    # All fighters must have same generic property count
    generic_counts = {
        name: len([p for p in e.properties if p.scope == "role_generic"])
        for name, e in dag.fighter_entities.items()
    }
    if generic_counts and len(set(generic_counts.values())) > 1:
        errors.append(f"Inconsistent generic property counts across fighters: {generic_counts}")

    passed = len(errors) == 0
    _log.info("DAG completeness: %s (%d errors)", "PASSED" if passed else "FAILED", len(errors))
    return passed, errors


# ---------------------------------------------------------------------------
# Phase 4: DLR fill
# ---------------------------------------------------------------------------

def validate_dlr_fill(dag: GameDAG) -> tuple[bool, list[str]]:
    """Check that critical properties have values after DLR fill."""
    errors: list[str] = []
    warnings: list[str] = []

    # Critical config properties that MUST have values
    critical_fighter_config = {
        "max_health", "walk_speed", "throw_damage",
    }

    for name, entity in dag.fighter_entities.items():
        for p in entity.config_props:
            if p.name in critical_fighter_config and not p.is_filled:
                errors.append(f"Fighter '{name}'.{p.name}: critical config unfilled")

    # Count unfilled
    total_unfilled = dag.total_unfilled
    total_props = dag.total_properties
    fill_pct = ((total_props - total_unfilled) / total_props * 100) if total_props else 0

    if fill_pct < 30:
        errors.append(f"Only {fill_pct:.0f}% properties filled — KB data may be missing")
    elif fill_pct < 60:
        warnings.append(f"{fill_pct:.0f}% properties filled — some gameplay may use defaults")

    _log.info("DLR fill: %d/%d filled (%.0f%%), %d errors, %d warnings",
               total_props - total_unfilled, total_props, fill_pct, len(errors), len(warnings))

    for w in warnings:
        _log.warning("DLR fill warning: %s", w)

    passed = len(errors) == 0
    return passed, errors + [f"WARNING: {w}" for w in warnings]


# ---------------------------------------------------------------------------
# Phase 5: GDScript parse
# ---------------------------------------------------------------------------

def validate_scripts_parse(project_dir: Path) -> tuple[bool, list[str]]:
    """Validate all .gd files parse correctly using Godot headless."""
    errors: list[str] = []

    gd_files = list(project_dir.rglob("*.gd"))
    if not gd_files:
        errors.append("No .gd files found in project")
        return False, errors

    # Run Godot headless import to check for parse errors
    try:
        result = subprocess.run(
            ["godot", "--headless", "--import"],
            capture_output=True, text=True, timeout=30,
            cwd=str(project_dir),
        )
        output = result.stdout + result.stderr

        for line in output.split("\n"):
            if "Parse Error" in line or "SCRIPT ERROR" in line:
                errors.append(line.strip())

    except FileNotFoundError:
        _log.warning("Godot not found — skipping parse validation")
        return True, ["WARNING: Godot not available for parse check"]
    except subprocess.TimeoutExpired:
        errors.append("Godot headless timed out after 30s")

    passed = len(errors) == 0
    _log.info("Script parse: %s (%d files, %d errors)",
               "PASSED" if passed else "FAILED", len(gd_files), len(errors))
    return passed, errors


# ---------------------------------------------------------------------------
# Phase 6: Godot project
# ---------------------------------------------------------------------------

def validate_godot_project(project_dir: Path) -> tuple[bool, list[str]]:
    """Validate the Godot project structure is complete."""
    errors: list[str] = []

    # Required files
    required = ["project.godot", "main.tscn", "main.gd"]
    for f in required:
        if not (project_dir / f).exists():
            errors.append(f"Missing required file: {f}")

    # Must have at least one scene
    scenes = list((project_dir / "scenes").glob("*.tscn")) if (project_dir / "scenes").exists() else []
    if not scenes:
        errors.append("No scene .tscn files in scenes/")

    # Every .tscn must have a matching .gd
    for tscn in scenes:
        gd = tscn.with_suffix(".gd")
        if not gd.exists():
            errors.append(f"Scene {tscn.name} has no matching .gd script")

    # project.godot must reference a valid main scene
    project_file = project_dir / "project.godot"
    if project_file.exists():
        content = project_file.read_text()
        if 'run/main_scene="res://main.tscn"' not in content:
            errors.append("project.godot: main_scene not set to res://main.tscn")

    passed = len(errors) == 0
    _log.info("Project structure: %s (%d errors)", "PASSED" if passed else "FAILED", len(errors))
    return passed, errors


# ---------------------------------------------------------------------------
# Scoping rule enforcement
# ---------------------------------------------------------------------------

def validate_impact_tracks_in_dag(
    impact: ImpactMatrix,
    dag: GameDAG,
) -> tuple[bool, list[str]]:
    """Verify every property Impact says is required actually exists in the DAG.

    This is the reconciliation gate: Impact (LLM) traced requirements → properties,
    the mechanic template (KB) defined property schemas. If they disagree, this catches it.
    """
    errors: list[str] = []

    # Collect all DAG property names by owner
    dag_props_by_owner: dict[str, set[str]] = {}
    for name, entity in dag.fighter_entities.items():
        dag_props_by_owner[name] = {p.name for p in entity.properties}
    for name, entity in dag.projectile_entities.items():
        dag_props_by_owner[name] = {p.name for p in entity.properties}
    dag_props_by_owner["game"] = {p.name for p in dag.game_properties}

    # Also collect role-level sets (Impact may say "fighter.health" not "ryu.health")
    fighter_props = set()
    for entity in dag.fighter_entities.values():
        fighter_props.update(p.name for p in entity.properties)
    dag_props_by_owner["fighter"] = fighter_props
    dag_props_by_owner["character"] = fighter_props

    projectile_props = set()
    for entity in dag.projectile_entities.values():
        projectile_props.update(p.name for p in entity.properties)
    dag_props_by_owner["projectile"] = projectile_props

    # Check each Impact TRACKS property
    for entry in impact.entries:
        for prop in entry.tracks:
            owner = prop.owner.lower()
            owner_props = dag_props_by_owner.get(owner, set())
            if not owner_props:
                # Try fuzzy: Impact might say "p1_fighter" → resolve to fighter role
                if "fighter" in owner:
                    owner_props = fighter_props
                elif "projectile" in owner:
                    owner_props = projectile_props
            if prop.name not in owner_props:
                errors.append(
                    f"Impact TRACKS '{prop.owner}.{prop.name}' (from {entry.requirement_id}) "
                    f"not found in DAG"
                )

    passed = len(errors) == 0
    _log.info("Impact→DAG reconciliation: %s (%d errors)", "PASSED" if passed else "FAILED", len(errors))
    return passed, errors


def validate_mlr_scoping(
    hlr: GameIdentity,
    mlr_entities: list[EntitySpec],
) -> tuple[bool, list[str]]:
    """Verify MLR doesn't create entities outside HLR enums.

    Scoping rule: HLR defines objects, MLR adds detail to them, never new objects.
    """
    errors: list[str] = []

    # Collect all HLR enum values (these are the allowed entity names)
    hlr_values: set[str] = set()
    for enum in hlr.enums:
        hlr_values.update(v.lower() for v in enum.values)

    # Also allow scene names as valid scopes
    hlr_values.update(s.scene_name.lower() for s in hlr.scenes)

    for entity in mlr_entities:
        entity_name = entity.entity_name.lower()
        if entity_name not in hlr_values:
            errors.append(
                f"MLR entity '{entity.entity_name}' in scene '{entity.scene_name}' "
                f"not found in any HLR enum"
            )

    passed = len(errors) == 0
    _log.info("MLR scoping: %s (%d errors)", "PASSED" if passed else "FAILED", len(errors))
    return passed, errors


def validate_dlr_scoping(
    dag: GameDAG,
    dlr_values: dict[str, dict[str, str]],
) -> tuple[bool, list[str]]:
    """Verify DLR doesn't fill properties not declared in the template/DAG.

    Scoping rule: DLR adds values to existing properties, never new properties.
    dlr_values: entity_name → {property_name: value} as returned by DLR fill.
    """
    errors: list[str] = []

    # Collect declared properties per entity
    declared: dict[str, set[str]] = {}
    for name, entity in dag.fighter_entities.items():
        declared[name] = {p.name for p in entity.properties}
    for name, entity in dag.projectile_entities.items():
        declared[name] = {p.name for p in entity.properties}

    for entity_name, props in dlr_values.items():
        entity_declared = declared.get(entity_name, set())
        if not entity_declared:
            errors.append(f"DLR fills entity '{entity_name}' which is not in DAG")
            continue
        for prop_name in props:
            if prop_name not in entity_declared:
                errors.append(
                    f"DLR fills '{entity_name}.{prop_name}' which was not declared in template"
                )

    passed = len(errors) == 0
    _log.info("DLR scoping: %s (%d errors)", "PASSED" if passed else "FAILED", len(errors))
    return passed, errors


# ---------------------------------------------------------------------------
# Run all validators
# ---------------------------------------------------------------------------

def run_all_validators(
    hlr: GameIdentity,
    schema: ExpandedGameSchema,
    dag: GameDAG,
    project_dir: Path,
) -> dict[str, tuple[bool, list[str]]]:
    """Run all validators in sequence. Returns {phase: (passed, errors)}."""
    results: dict[str, tuple[bool, list[str]]] = {}

    results["template_coverage"] = validate_template_coverage(hlr, schema)
    results["dag_completeness"] = validate_dag_completeness(dag)
    results["dlr_fill"] = validate_dlr_fill(dag)
    results["script_parse"] = validate_scripts_parse(project_dir)
    results["project_structure"] = validate_godot_project(project_dir)

    return results


def format_validation_report(results: dict[str, tuple[bool, list[str]]]) -> str:
    """Format validation results as a human-readable report."""
    lines = ["Validation Report:", ""]
    all_passed = True

    for phase, (passed, errors) in results.items():
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        lines.append(f"  [{status}] {phase}")
        for e in errors:
            prefix = "    ! " if e.startswith("WARNING") else "    x "
            lines.append(f"{prefix}{e}")

    lines.append("")
    lines.append(f"Overall: {'ALL PASSED' if all_passed else 'FAILED'}")
    return "\n".join(lines)
