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
    from rayxi.spec.deterministic import (
        build_all_interactions,
        build_entity_spec,
        build_impact_matrix as build_impact_deterministic,
    )
    from rayxi.spec.genre_detector import detect_genre
    from rayxi.spec.kb_retrieval import retrieve_relevant_chunks
    from rayxi.spec.system_mapper import map_hlr_to_template
    from rayxi.spec.hlr import run_hlr
    from rayxi.spec.hlr_validator import validate_hlr
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

    kb_dir = Path("knowledge")

    # STEP 0: Detect genre from prompt
    print("\n" + "=" * 70)
    print("STEP 0: Genre Detection")
    print("=" * 70)
    detected_genre = detect_genre(PROMPT, kb_dir)
    print(f"  Detected genre: {detected_genre}")
    if not detected_genre:
        print("  ERROR: could not detect genre — aborting")
        return

    # STEP 1: Load HLT (High-Level Template) — for HLR reference only
    print("\n" + "=" * 70)
    print("STEP 1: Load HLT")
    print("=" * 70)
    hlt_path = _TEMPLATE_DIR / f"{detected_genre}_hlt.json"
    if not hlt_path.exists():
        print(f"  No HLT for genre '{detected_genre}' at {hlt_path}")
        return

    import json as _json
    hlt = _json.loads(hlt_path.read_text())
    hlt_systems = {name: info.get("description", "") for name, info in hlt.get("systems", {}).items()}
    hlis_groups = hlt.get("hlis_groups", [])
    print(f"  HLT: {hlt_path.name}")
    print(f"  Systems: {len(hlt_systems)}")
    print(f"  HLIS groups: {len(hlis_groups)}")

    # The full template (still used by deterministic Impact/MLR for property structure)
    template_path = _TEMPLATE_DIR / f"{detected_genre}.json"
    if not template_path.exists():
        print(f"  No full template at {template_path}")
        return

    # STEP 2: Retrieve relevant KB chunks via embedding
    print("\n" + "=" * 70)
    print("STEP 2: KB Retrieval")
    print("=" * 70)
    kb_chunks = retrieve_relevant_chunks(PROMPT, kb_dir, top_k=10)
    print(f"  Retrieved {len(kb_chunks)} chunks")
    for label, _, score in kb_chunks[:5]:
        print(f"    {score:.3f}  {label}")

    # STEP 3: HLR (with HLT + KB chunks as context)
    print("\n" + "=" * 70)
    print("STEP 3: HLR")
    print("=" * 70)
    t = time.time()
    hlr, dynamic = await run_hlr(
        PROMPT, caller,
        template_systems=hlt_systems,
        kb_chunks=kb_chunks,
    )
    print(f"  [{time.time()-t:.1f}s] {hlr.game_name}")
    for e in hlr.enums:
        flag = " [entity]" if e.entity else ""
        print(f"  {e.name}: {e.values}{flag}")
    errors = validate_hlr(hlr, dynamic, hlt_provided=True)
    if errors:
        for e in errors:
            print(f"  x {e}")
        return
    print("  PASSED")

    # Re-load template now that we have real characters from HLR
    schema = load_game_schema(template_path, hlr)
    print(f"\n  Reloaded template with HLR characters: {hlr.get_enum('characters')}")
    print(format_schema_summary(schema))

    # STEP 4: Impact Matrix (deterministic — zero LLM calls)
    print("\n" + "=" * 70)
    print("STEP 4: Impact Matrix (deterministic)")
    print("=" * 70)
    t = time.time()
    systems = hlr.get_enum("game_systems")
    characters = hlr.get_enum("characters")

    # Map HLR system names to template system names via embeddings
    system_mapping = map_hlr_to_template(hlr, schema)
    if system_mapping:
        mapped_count = sum(1 for k, v in system_mapping.items() if k != v)
        print(f"  System mapping: {mapped_count}/{len(system_mapping)} HLR systems remapped to template")

    impact = build_impact_deterministic(schema, systems, characters, system_mapping=system_mapping)
    print(f"  [{time.time()-t:.3f}s] {len(impact.entries)} entries, {len(impact.mechanics)} mechanics")
    print(f"  Properties traced: {len(impact.all_properties())}")
    impact_errors = validate_impact_matrix(hlr, impact)
    if impact_errors:
        print(f"  {len(impact_errors)} issue(s):")
        for e in impact_errors[:10]:
            print(f"    x {e}")
    else:
        print("  PASSED")

    # STEP 5: Scene Manifest
    print("\n" + "=" * 70)
    print("STEP 5: Scene Manifest")
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

    # STEP 6: MLR (hybrid — FSM/collisions from LLM, interactions/entities deterministic)
    print("\n" + "=" * 70)
    print("STEP 6: MLR (hybrid)")
    print("=" * 70)
    t = time.time()

    # FSM + collisions still need LLM (game flow is genre-specific)
    # Interactions + entities are deterministic from template
    mlr_scenes = await run_mlr(hlr, router, schema=schema, fsm_only=True)

    # Replace LLM-generated interactions + entities with deterministic versions
    from rayxi.spec.mlr import _flatten_scenes, _entities_for_scene, SceneMLR
    for scene_mlr in mlr_scenes:
        # Deterministic interactions
        scene_mlr.system_interactions = build_all_interactions(
            schema, systems, scene_mlr.scene_name, system_mapping=system_mapping,
        )
        # Deterministic entity specs
        hlr_scene = next(
            (s for s in _flatten_scenes(hlr.scenes) if s.scene_name == scene_mlr.scene_name),
            None,
        )
        if hlr_scene:
            scene_mlr.entities = [
                build_entity_spec(schema, ename, penum, scene_mlr.scene_name)
                for ename, penum in _entities_for_scene(hlr, hlr_scene)
            ]

    print(f"  [{time.time()-t:.1f}s] {len(mlr_scenes)} scenes")
    for scene_mlr in mlr_scenes:
        fsm_states = len(scene_mlr.fsm.states) if scene_mlr.fsm else 0
        collisions = len(scene_mlr.collisions.collision_pairs) if scene_mlr.collisions else 0
        interactions = len(scene_mlr.system_interactions)
        entities = len(scene_mlr.entities)
        actions = sum(len(e.action_sets) for e in scene_mlr.entities)
        print(f"  {scene_mlr.scene_name}: fsm={fsm_states}, {collisions} collisions, {interactions} interactions(det), {entities} entities(det), {actions} action_sets")
    mlr_errors = validate_mlr(hlr, mlr_scenes)
    if mlr_errors:
        print(f"  {len(mlr_errors)} issue(s):")
        for e in mlr_errors[:15]:
            print(f"    x {e}")
    else:
        print("  PASSED")

    # STEP 7: Build DAG
    print("\n" + "=" * 70)
    print("STEP 7: Build DAG")
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
