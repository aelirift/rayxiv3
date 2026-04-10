"""Run SF2 Ryu mirror match pipeline to just before build (steps 1-6)."""

import asyncio
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
sys.stdout.reconfigure(line_buffering=True)

_KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
_TEMPLATE_DIR = _KNOWLEDGE_DIR / "mechanic_templates"

PROMPT = "Street Fighter 2, only Ryu, mirror match (Ryu vs Ryu CPU), classic 2D fighting game"


async def main():
    from rayxi.build.dag import build_dag, format_dag_summary, validate_dag
    from rayxi.knowledge.mechanic_loader import format_schema_summary, load_game_schema
    from rayxi.llm.callers import build_callers, build_router
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

    callers = build_callers()
    router = build_router(callers)
    caller = router.primary
    t0 = time.time()
    print(f"Primary: {type(caller).__name__}")

    # STEP 1: HLR
    print("\n" + "=" * 70)
    print("STEP 1: HLR")
    print("=" * 70)
    t = time.time()
    hlr, dynamic = await run_hlr(PROMPT, caller)
    print(f"  [{time.time()-t:.1f}s] {hlr.game_name}")
    for e in hlr.enums:
        flag = " [entity]" if e.entity else ""
        print(f"  {e.name}: {e.values}{flag}")
    errors = validate_hlr(hlr, dynamic)
    if errors:
        for e in errors:
            print(f"  x {e}")
        return
    print("  PASSED")

    # STEP 2: KB Template
    print("\n" + "=" * 70)
    print("STEP 2: KB Template")
    print("=" * 70)
    genre = hlr.genre.replace(" ", "_").lower()
    template_path = _TEMPLATE_DIR / f"{genre}.json"
    if not template_path.exists():
        for tp in _TEMPLATE_DIR.glob("*.json"):
            if genre.replace("_", "") in tp.stem.replace("_", ""):
                template_path = tp
                break
    if not template_path.exists():
        print(f"  No template for genre '{genre}'")
        return
    schema = load_game_schema(template_path, hlr)
    print(format_schema_summary(schema))

    # STEP 3: Impact Matrix
    print("\n" + "=" * 70)
    print("STEP 3: Impact Matrix")
    print("=" * 70)
    t = time.time()
    impact = await run_impact_matrix(hlr, caller, schema=schema)
    print(f"  [{time.time()-t:.1f}s] {len(impact.entries)} entries, {len(impact.mechanics)} mechanics")
    print(f"  Properties traced: {len(impact.all_properties())}")
    impact_errors = validate_impact_matrix(hlr, impact)
    if impact_errors:
        print(f"  {len(impact_errors)} issue(s):")
        for e in impact_errors[:10]:
            print(f"    x {e}")
    else:
        print("  PASSED")

    # STEP 4: Scene Manifest
    print("\n" + "=" * 70)
    print("STEP 4: Scene Manifest")
    print("=" * 70)
    t = time.time()
    manifest = await run_scene_manifest(hlr, impact, caller)
    print(f"  [{time.time()-t:.1f}s]")
    print(format_scene_manifest(manifest))
    manifest_errors = validate_scene_manifest(hlr, impact, manifest)
    if manifest_errors:
        print(f"  {len(manifest_errors)} issue(s):")
        for e in manifest_errors[:10]:
            print(f"    x {e}")

    # STEP 5: MLR
    print("\n" + "=" * 70)
    print("STEP 5: MLR")
    print("=" * 70)
    t = time.time()
    mlr_scenes = await run_mlr(hlr, router, schema=schema)
    print(f"  [{time.time()-t:.1f}s] {len(mlr_scenes)} scenes")
    for scene_mlr in mlr_scenes:
        fsm_states = len(scene_mlr.fsm.states) if scene_mlr.fsm else 0
        collisions = len(scene_mlr.collisions.collision_pairs) if scene_mlr.collisions else 0
        interactions = len(scene_mlr.system_interactions)
        entities = len(scene_mlr.entities)
        actions = sum(len(e.action_sets) for e in scene_mlr.entities)
        print(f"  {scene_mlr.scene_name}: fsm={fsm_states} states, {collisions} collisions, {interactions} interactions, {entities} entities, {actions} action_sets")
    mlr_errors = validate_mlr(hlr, mlr_scenes)
    if mlr_errors:
        print(f"  {len(mlr_errors)} issue(s):")
        for e in mlr_errors[:15]:
            print(f"    x {e}")
    else:
        print("  PASSED")

    # STEP 6: Build DAG
    print("\n" + "=" * 70)
    print("STEP 6: Build DAG")
    print("=" * 70)
    scene_fsms = {}
    scene_collisions = {}
    scene_interactions = {}
    entity_action_sets = {}
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

    print(f"\n{'=' * 70}")
    print(f"STOPPED BEFORE BUILD — {time.time()-t0:.0f}s total")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
