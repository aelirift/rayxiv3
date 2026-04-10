"""RayXI v3 pipeline: HLR → KB Template → DAG → DLR Fill → GDScript → Godot Project.

No more LLM-invented property schemas. The KB mechanic template defines the
structure, the LLM fills values, deterministic code generates the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
sys.stdout.reconfigure(line_buffering=True)

_TRACE_DIR = Path(__file__).parent / ".trace"
_OUTPUT_DIR = Path(__file__).parent / "output"
_KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
_TEMPLATE_DIR = _KNOWLEDGE_DIR / "mechanic_templates"


async def main(user_prompt: str, auto_approve: bool = True) -> None:
    from rayxi.build.dag import build_dag, format_dag_summary, validate_dag
    from rayxi.build.dlr_fill import fill_dag_from_kb, fill_dag_from_llm
    from rayxi.build.gdscript_gen import save_entity_script
    from rayxi.build.godot_project import generate_godot_project
    from rayxi.knowledge.mechanic_loader import (
        format_schema_summary,
        load_game_schema,
    )
    from rayxi.llm.callers import build_callers, build_router
    from rayxi.llm.pool import get_pool
    from rayxi.spec.hlr import run_hlr
    from rayxi.spec.hlr_validator import validate_hlr
    from rayxi.spec.impact import run_impact_matrix
    from rayxi.spec.impact_validator import validate_impact_matrix
    from rayxi.spec.mlr import run_mlr
    from rayxi.spec.mlr_validator import validate_mlr
    from rayxi.spec.scene_manifest import (
        format_scene_manifest,
        run_scene_manifest,
        validate_scene_manifest,
    )
    from rayxi.trace import start_trace

    trace = start_trace(user_prompt)
    callers = build_callers()
    router = build_router(callers)
    hlr_caller = router.primary
    t0 = time.time()

    print(f"Primary: {type(router.primary).__name__}")

    # =======================================================================
    # STEP 1: HLR — Game Identity
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 1: HLR — Game Identity")
    print("=" * 70)

    trace.phase_start("hlr")
    t_hlr = time.time()
    hlr, dynamic_fields = await run_hlr(user_prompt, hlr_caller)
    trace.project_name = hlr.game_name
    print(f"  [{time.time() - t_hlr:.1f}s] {hlr.game_name}")

    for e in hlr.enums:
        flag = " [entity]" if e.entity else ""
        print(f"  {e.name}: {e.values}{flag}")

    errors = validate_hlr(hlr, dynamic_fields)
    trace.validation("hlr", "hlr_validator", passed=len(errors) == 0, errors=errors)
    trace.phase_end("hlr")

    if errors:
        print(f"  FAILED — {len(errors)} error(s):")
        for e in errors:
            print(f"    x {e}")
        trace.end()
        trace.save(_TRACE_DIR / f"{hlr.game_name}_failed.json")
        return
    print("  PASSED")

    # =======================================================================
    # STEP 2: KB Template → Schema
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 2: KB Template → Schema")
    print("=" * 70)

    trace.phase_start("template")

    # Find the right template for this genre
    genre = hlr.genre.replace(" ", "_").lower()
    template_path = _TEMPLATE_DIR / f"{genre}.json"
    if not template_path.exists():
        # Try without underscores
        for tp in _TEMPLATE_DIR.glob("*.json"):
            if genre.replace("_", "") in tp.stem.replace("_", ""):
                template_path = tp
                break

    if not template_path.exists():
        print(f"  No template for genre '{genre}' at {template_path}")
        print(f"  Available: {list(_TEMPLATE_DIR.glob('*.json'))}")
        trace.end()
        return

    schema = load_game_schema(template_path, hlr)
    print(format_schema_summary(schema))
    trace.phase_end("template")

    # =======================================================================
    # STEP 3: Impact Matrix
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 3: Impact Matrix")
    print("=" * 70)

    trace.phase_start("impact")
    t_impact = time.time()
    impact = await run_impact_matrix(hlr, hlr_caller, schema=schema)
    print(f"  [{time.time() - t_impact:.1f}s] {len(impact.entries)} entries, {len(impact.mechanics)} mechanics")
    print(f"  Properties traced: {len(impact.all_properties())}")

    impact_errors = validate_impact_matrix(hlr, impact)
    trace.validation("impact", "impact_validator", passed=len(impact_errors) == 0, errors=impact_errors)
    trace.phase_end("impact")

    if impact_errors:
        print(f"  {len(impact_errors)} issue(s):")
        for e in impact_errors[:10]:
            print(f"    x {e}")
    else:
        print("  PASSED")

    # =======================================================================
    # STEP 4: Scene Manifest (LLM — what goes where)
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 4: Scene Manifest")
    print("=" * 70)

    trace.phase_start("scene_manifest")
    t_manifest = time.time()

    manifest = await run_scene_manifest(hlr, impact, hlr_caller)
    print(f"  [{time.time() - t_manifest:.1f}s]")
    print(format_scene_manifest(manifest))

    manifest_errors = validate_scene_manifest(hlr, impact, manifest)
    trace.validation("scene_manifest", "manifest_validator",
                      passed=len(manifest_errors) == 0, errors=manifest_errors)
    trace.phase_end("scene_manifest")

    if manifest_errors:
        print(f"  {len(manifest_errors)} issue(s):")
        for e in manifest_errors[:10]:
            print(f"    x {e}")

    # =======================================================================
    # STEP 5: MLR — Mid-Level Requirements
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 5: MLR — Mid-Level Requirements")
    print("=" * 70)

    trace.phase_start("mlr")
    t_mlr = time.time()
    mlr_scenes = await run_mlr(hlr, router, schema=schema)
    print(f"  [{time.time() - t_mlr:.1f}s] {len(mlr_scenes)} scenes")

    for scene_mlr in mlr_scenes:
        fsm_states = len(scene_mlr.fsm.states) if scene_mlr.fsm else 0
        collisions = len(scene_mlr.collisions.collision_pairs) if scene_mlr.collisions else 0
        interactions = len(scene_mlr.system_interactions)
        entities = len(scene_mlr.entities)
        actions = sum(len(e.action_sets) for e in scene_mlr.entities)
        print(f"  {scene_mlr.scene_name}: fsm={fsm_states} states, {collisions} collisions, {interactions} interactions, {entities} entities, {actions} action_sets")

    mlr_errors = validate_mlr(hlr, mlr_scenes)
    trace.validation("mlr", "mlr_validator", passed=len(mlr_errors) == 0, errors=mlr_errors)
    trace.phase_end("mlr")

    if mlr_errors:
        print(f"  {len(mlr_errors)} issue(s):")
        for e in mlr_errors[:10]:
            print(f"    x {e}")
    else:
        print("  PASSED")

    # =======================================================================
    # STEP 6: Build DAG
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 6: Build DAG")
    print("=" * 70)

    trace.phase_start("dag")

    # Extract MLR products for DAG
    scene_fsms: dict = {}
    scene_collisions: dict = {}
    scene_interactions: dict = {}
    entity_action_sets: dict = {}
    for scene_mlr in mlr_scenes:
        if scene_mlr.fsm:
            scene_fsms[scene_mlr.scene_name] = scene_mlr.fsm
        if scene_mlr.collisions and scene_mlr.collisions.collision_pairs:
            scene_collisions[scene_mlr.scene_name] = scene_mlr.collisions.collision_pairs
        if scene_mlr.system_interactions:
            scene_interactions[scene_mlr.scene_name] = scene_mlr.system_interactions
        for entity_spec in scene_mlr.entities:
            if entity_spec.action_sets:
                entity_action_sets[entity_spec.entity_name] = entity_spec.action_sets

    dag = build_dag(
        hlr, schema, manifest,
        impact=impact,
        scene_fsms=scene_fsms,
        scene_collisions=scene_collisions,
        scene_interactions=scene_interactions,
        entity_action_sets=entity_action_sets,
    )
    print(format_dag_summary(dag))

    dag_errors = validate_dag(dag, impact=impact)
    if dag_errors:
        print(f"  {len(dag_errors)} issue(s):")
        for e in dag_errors:
            print(f"    x {e}")
    else:
        print("  DAG validation: PASSED")
    trace.phase_end("dag")

    # =======================================================================
    # STEP 7: DLR — Fill DAG leaves
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 7: DLR — Fill DAG Leaves")
    print("=" * 70)

    trace.phase_start("dlr")
    t_dlr = time.time()

    # First pass: fill from KB game data (deterministic, no LLM)
    kb_filled = fill_dag_from_kb(dag, hlr, _KNOWLEDGE_DIR)
    print(f"  KB fill: {kb_filled} properties filled from game data")

    # Second pass: LLM fills remaining unfilled leaves
    llm_filled = await fill_dag_from_llm(dag, hlr, hlr_caller)
    print(f"  LLM fill: {llm_filled} properties filled by LLM")

    print(f"  [{time.time() - t_dlr:.1f}s]")
    print(f"  Total: {dag.total_properties} properties, {dag.total_unfilled} still unfilled")
    trace.phase_end("dlr")

    # =======================================================================
    # STEP 8: Generate GDScript
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 8: Generate GDScript")
    print("=" * 70)

    trace.phase_start("codegen")
    project_dir = _OUTPUT_DIR / hlr.game_name / "godot"
    scripts_dir = project_dir / "scripts"

    generated = 0
    for name, entity in dag.fighter_entities.items():
        path = save_entity_script(entity, scripts_dir / "characters")
        print(f"  {name}: {path} ({path.stat().st_size} bytes, {len(entity.properties)} props)")
        generated += 1

    for name, entity in dag.projectile_entities.items():
        path = save_entity_script(entity, scripts_dir / "projectiles")
        print(f"  {name}: {path}")
        generated += 1

    print(f"  {generated} scripts generated")
    trace.phase_end("codegen")

    # =======================================================================
    # STEP 9: Generate Godot Project
    # =======================================================================
    print("\n" + "=" * 70)
    print("STEP 9: Generate Godot Project")
    print("=" * 70)

    trace.phase_start("godot_project")
    generated_files = generate_godot_project(dag, hlr, schema, manifest, project_dir)
    for f in generated_files:
        print(f"  {f}")
    print(f"  {len(generated_files)} files generated")
    trace.phase_end("godot_project")

    # =======================================================================
    # Summary
    # =======================================================================
    total_time = time.time() - t0
    trace.end()
    trace.save(_TRACE_DIR / f"{hlr.game_name}.json")

    print(f"\n{'=' * 70}")
    print("DONE")
    print(f"{'=' * 70}")
    print(f"  Game: {hlr.game_name}")
    print(f"  Total time: {total_time:.0f}s")
    print(f"  DAG: {dag.total_properties} properties, {dag.total_unfilled} unfilled")
    print(f"  Scripts: {generated}")
    print(f"  Godot project: {project_dir}")
    print(f"  Trace: {_TRACE_DIR / hlr.game_name}.json")

    # Pool stats
    print(f"\n{'=' * 70}")
    print(get_pool().stats.format())


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "I want to build Street Fighter 2 with only Ryu as the playable character, 1P vs CPU, classic 2D fighting game"
    asyncio.run(main(prompt, auto_approve=True))
