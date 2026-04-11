"""Run HLR → Impact → Scene Manifest → MLR → DLR for a prompt, then stop.

Persists every phase product to output/{game_name}/ and saves a full trace
with per-phase artifacts so the gallery/log viewer can render it.

Usage:
    python run_to_dlr.py "your prompt"
    python run_to_dlr.py "your prompt" 2d_fighter
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
sys.stdout.reconfigure(line_buffering=True)

_TEMPLATE_DIR = Path(__file__).parent / "knowledge" / "mechanic_templates"

DEFAULT_PROMPT = "Street Fighter 2, only Ryu, mirror match (Ryu vs Ryu CPU), classic 2D fighting game"
PROMPT = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
GENRE_OVERRIDE = sys.argv[2] if len(sys.argv) > 2 else None


async def main():
    from rayxi.build.dag import build_dag, format_dag_summary, validate_dag
    from rayxi.knowledge import KnowledgeBase
    from rayxi.knowledge.mechanic_loader import load_game_schema
    from rayxi.llm.callers import build_callers, build_router
    from rayxi.spec.deterministic import (
        build_all_interactions,
        build_entity_spec,
        build_impact_matrix as build_impact_deterministic,
    )
    from rayxi.spec.dlr import fill_mechanic_constants, run_dlr
    from rayxi.spec.genre_detector import detect_genre
    from rayxi.spec.hlr import run_hlr
    from rayxi.spec.hlr_validator import validate_hlr
    from rayxi.spec.impact_validator import validate_impact_matrix
    from rayxi.spec.kb_retrieval import retrieve_relevant_chunks
    from rayxi.spec.mlr import _flatten_scenes, run_mlr
    from rayxi.spec.mlr_validator import validate_mlr
    from rayxi.spec.scene_manifest import (
        format_scene_manifest,
        run_scene_manifest,
        validate_scene_manifest,
    )
    from rayxi.spec.system_mapper import map_hlr_to_template
    from rayxi.trace import start_trace

    callers = build_callers()
    router = build_router(callers)
    caller = router.primary
    kb_dir = Path("knowledge")
    t0 = time.time()
    trace = start_trace(user_prompt=PROMPT)

    # --- Genre + HLT setup ------------------------------------------------
    print("=" * 70)
    print("STEP 0: Genre + HLT")
    print("=" * 70)
    if GENRE_OVERRIDE:
        genre = GENRE_OVERRIDE
        print(f"  Override: {genre}")
    else:
        genre = detect_genre(PROMPT, kb_dir)
        print(f"  Detected: {genre}")
    if not genre:
        print("  ERROR: could not detect genre")
        return

    hlt_path = _TEMPLATE_DIR / f"{genre}_hlt.json"
    hlt = json.loads(hlt_path.read_text())
    hlt_systems = {name: info.get("description", "") for name, info in hlt.get("systems", {}).items()}
    template_path = _TEMPLATE_DIR / f"{genre}.json"
    kb_chunks = retrieve_relevant_chunks(PROMPT, kb_dir, top_k=10)
    print(f"  HLT: {len(hlt_systems)} systems. KB: {len(kb_chunks)} chunks.")

    # --- HLR --------------------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 1: HLR")
    print("=" * 70)
    trace.phase_start("hlr")
    t = time.time()
    hlr, dynamic = await run_hlr(
        PROMPT, caller,
        template_systems=hlt_systems,
        kb_chunks=kb_chunks,
    )
    out_dir = Path("output") / hlr.game_name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hlr.json").write_text(hlr.model_dump_json(indent=2) + "\n")
    trace.project_name = hlr.game_name
    print(f"  [{time.time()-t:.1f}s] {hlr.game_name}")

    errors = validate_hlr(hlr, dynamic, hlt_provided=True)
    if errors:
        print(f"  HLR validation: {len(errors)} errors")
        for e in errors:
            print(f"    x {e}")
        trace.phase_end("hlr", artifacts=["hlr.json"])
        trace.end()
        trace.save(out_dir / "trace.json")
        return
    print("  HLR validation: PASSED")
    print(f"  Custom mechanic_specs: {len(hlr.mechanic_specs)}")
    trace.phase_end("hlr", artifacts=["hlr.json"])

    schema = load_game_schema(template_path, hlr)

    # --- Impact -----------------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 2: Impact Matrix (deterministic)")
    print("=" * 70)
    trace.phase_start("impact")
    systems = hlr.get_enum("game_systems")
    characters = hlr.get_enum("characters")
    system_mapping = map_hlr_to_template(hlr, schema)
    impact = build_impact_deterministic(
        schema, systems, characters,
        system_mapping=system_mapping,
        mechanic_specs=hlr.mechanic_specs,
    )
    (out_dir / "impact.json").write_text(impact.model_dump_json(indent=2) + "\n")
    print(f"  {len(impact.entries)} entries, {len(impact.mechanics)} mechanics, "
          f"{len(impact.all_properties())} properties traced")
    impact_errors = validate_impact_matrix(hlr, impact)
    if impact_errors:
        print(f"  Impact validation: {len(impact_errors)} issue(s)")
    else:
        print("  Impact validation: PASSED")
    trace.phase_end("impact", artifacts=["impact.json"])

    # --- Scene Manifest ---------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 3: Scene Manifest (LLM)")
    print("=" * 70)
    trace.phase_start("manifest")
    t = time.time()
    manifest = await run_scene_manifest(hlr, impact, caller)
    (out_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    print(f"  [{time.time()-t:.1f}s]")
    manifest_errors = validate_scene_manifest(hlr, impact, manifest)
    if manifest_errors:
        print(f"  Manifest validation: {len(manifest_errors)} issue(s)")
    else:
        print("  Manifest validation: PASSED")
    trace.phase_end("manifest", artifacts=["manifest.json"])

    # --- MLR --------------------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 4: MLR (hybrid)")
    print("=" * 70)
    trace.phase_start("mlr")
    t = time.time()

    mlr_scenes = await run_mlr(hlr, router, schema=schema, fsm_only=True)
    manifest_by_scene = {s.scene_name: s for s in manifest.scenes}

    for scene_mlr in mlr_scenes:
        mf = manifest_by_scene.get(scene_mlr.scene_name)
        if mf is None:
            scene_mlr.system_interactions = []
            scene_mlr.entities = []
            if scene_mlr.collisions:
                scene_mlr.collisions.collision_pairs = []
            continue

        active_systems = list(mf.active_systems)
        if "collision_system" not in active_systems and scene_mlr.collisions:
            scene_mlr.collisions.collision_pairs = []

        scene_mlr.system_interactions = build_all_interactions(
            schema, active_systems, scene_mlr.scene_name,
            system_mapping=system_mapping,
            mechanic_specs=hlr.mechanic_specs,
        )

        seen: set[str] = set()
        deduped: list[tuple[str, str]] = []
        for e in mf.entities:
            if e.entity_name in seen:
                continue
            seen.add(e.entity_name)
            deduped.append((e.entity_name, e.from_enum))

        scene_mlr.entities = [
            build_entity_spec(
                schema, ename, penum, scene_mlr.scene_name,
                mechanic_specs=hlr.mechanic_specs,
            )
            for ename, penum in deduped
        ]

    mlr_artifacts: list[str] = []
    for scene_mlr in mlr_scenes:
        fname = f"mlr_{scene_mlr.scene_name}.json"
        (out_dir / fname).write_text(json.dumps(scene_mlr.to_dict(), indent=2) + "\n")
        mlr_artifacts.append(fname)
        print(f"  {scene_mlr.scene_name}: "
              f"fsm={len(scene_mlr.fsm.states) if scene_mlr.fsm else 0}, "
              f"collisions={len(scene_mlr.collisions.collision_pairs) if scene_mlr.collisions else 0}, "
              f"interactions={len(scene_mlr.system_interactions)}, "
              f"entities={len(scene_mlr.entities)}")

    mlr_errors = validate_mlr(hlr, mlr_scenes)
    if mlr_errors:
        print(f"  MLR validation: {len(mlr_errors)} issue(s)")
        for e in mlr_errors[:10]:
            print(f"    x {e}")
    else:
        print(f"  MLR validation: PASSED ({time.time()-t:.1f}s)")
    trace.phase_end("mlr", artifacts=mlr_artifacts)

    # --- DAG --------------------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 4b: Build DAG (deterministic)")
    print("=" * 70)
    trace.phase_start("dag")
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
    from dataclasses import asdict
    (out_dir / "dag.json").write_text(json.dumps(asdict(dag), indent=2, default=str) + "\n")
    print(f"  DAG: {dag.total_properties} properties, {len(dag.systems)} systems, {len(dag.scenes)} scenes")
    dag_errors = validate_dag(dag, impact=impact)
    if dag_errors:
        print(f"  DAG validation: {len(dag_errors)} issue(s)")
        for e in dag_errors[:10]:
            print(f"    x {e}")
    else:
        print("  DAG validation: PASSED")
    trace.phase_end("dag", artifacts=["dag.json"])

    # --- DLR --------------------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 5: DLR")
    print("=" * 70)
    trace.phase_start("dlr")
    t = time.time()

    # 1) Per-scene entity + interaction detail fills
    scene_dlrs = await run_dlr(hlr, mlr_scenes, router, knowledge_dir=kb_dir)

    # 2) Mechanic constant fills (one LLM call per mechanic_spec)
    kb = KnowledgeBase(kb_dir)
    kb_context = kb.retrieve_context(hlr.game_name) or kb.retrieve_context(hlr.genre)
    kb_game_data_text = json.dumps(kb_context.game_data, indent=2) if kb_context.game_data else "{}"
    mechanic_constants = await fill_mechanic_constants(hlr, router, kb_game_data_text)

    dlr_artifacts: list[str] = []
    for sd in scene_dlrs:
        fname = f"dlr_{sd.scene_name}.json"
        (out_dir / fname).write_text(json.dumps(sd.to_dict(), indent=2) + "\n")
        dlr_artifacts.append(fname)
        print(f"  {sd.scene_name}: entity_details={len(sd.entity_details)}, "
              f"interaction_details={len(sd.interaction_details)}")

    if mechanic_constants:
        (out_dir / "dlr_mechanic_constants.json").write_text(
            json.dumps(mechanic_constants, indent=2) + "\n"
        )
        dlr_artifacts.append("dlr_mechanic_constants.json")
        for sys_name, data in mechanic_constants.items():
            print(f"  [mechanic] {sys_name}: {len(data.get('constants', []))} constants filled")

    print(f"  [{time.time()-t:.1f}s] DLR complete")
    trace.phase_end("dlr", artifacts=dlr_artifacts)

    # --- Finalize ---------------------------------------------------------
    trace.end()
    trace_path = out_dir / "trace.json"
    trace.save(trace_path)

    print(f"\n{'=' * 70}")
    print(f"STOPPED AFTER DLR — {time.time()-t0:.0f}s total")
    print(f"  Output: {out_dir}")
    print(f"  Trace: {trace_path}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
