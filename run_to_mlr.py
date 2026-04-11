"""Run HLR → Impact → Scene Manifest → MLR for a prompt, then stop.

Persists every phase product to output/{game_name}/ and saves a full trace
to output/{game_name}/trace.json so the gallery/log viewer can render it.

Usage:
    python run_to_mlr.py "your prompt"                 # auto-detect genre
    python run_to_mlr.py "your prompt" 2d_fighter      # genre override
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
    from rayxi.knowledge.mechanic_loader import load_game_schema
    from rayxi.llm.callers import build_callers, build_router
    from rayxi.spec.deterministic import (
        build_all_interactions,
        build_entity_spec,
        build_impact_matrix as build_impact_deterministic,
    )
    from rayxi.spec.genre_detector import detect_genre
    from rayxi.spec.hlr import run_hlr
    from rayxi.spec.hlr_validator import validate_hlr
    from rayxi.spec.impact_validator import validate_impact_matrix
    from rayxi.spec.kb_retrieval import retrieve_relevant_chunks
    from rayxi.spec.mlr import (
        SceneMLR,
        _flatten_scenes,
        run_mlr,
    )
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

    # --- Genre detection --------------------------------------------------
    print("=" * 70)
    print("STEP 0: Genre Detection")
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

    # --- HLT + KB setup ---------------------------------------------------
    hlt_path = _TEMPLATE_DIR / f"{genre}_hlt.json"
    hlt = json.loads(hlt_path.read_text())
    hlt_systems = {name: info.get("description", "") for name, info in hlt.get("systems", {}).items()}
    template_path = _TEMPLATE_DIR / f"{genre}.json"
    print(f"  HLT: {hlt_path.name} ({len(hlt_systems)} systems)")

    print("\n" + "=" * 70)
    print("STEP 1: KB Retrieval")
    print("=" * 70)
    kb_chunks = retrieve_relevant_chunks(PROMPT, kb_dir, top_k=10)
    print(f"  Retrieved {len(kb_chunks)} chunks")

    # --- HLR phase --------------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 2: HLR")
    print("=" * 70)
    trace.phase_start("hlr")
    t = time.time()
    hlr, dynamic = await run_hlr(
        PROMPT, caller,
        template_systems=hlt_systems,
        kb_chunks=kb_chunks,
    )
    print(f"  [{time.time()-t:.1f}s] {hlr.game_name}")

    out_dir = Path("output") / hlr.game_name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hlr.json").write_text(hlr.model_dump_json(indent=2) + "\n")
    trace.project_name = hlr.game_name

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
    print(f"  Mechanic specs: {len(hlr.mechanic_specs)} custom feature(s)")
    for m in hlr.mechanic_specs:
        print(f"    - {m.system_name}: {len(m.properties)} props, "
              f"{len(m.hud_entities)} hud, {len(m.interactions)} interactions, "
              f"{len(m.constants_for_dlr)} constants")
    trace.phase_end("hlr", artifacts=["hlr.json"])

    # --- Reload schema with HLR characters --------------------------------
    schema = load_game_schema(template_path, hlr)

    # --- Impact phase -----------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 3: Impact Matrix (deterministic)")
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
        print(f"  Impact validation: {len(impact_errors)} issue(s):")
        for e in impact_errors[:10]:
            print(f"    x {e}")
    else:
        print("  Impact validation: PASSED")
    trace.phase_end("impact", artifacts=["impact.json"])

    # --- Scene Manifest phase --------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 4: Scene Manifest (LLM)")
    print("=" * 70)
    trace.phase_start("manifest")
    t = time.time()
    manifest = await run_scene_manifest(hlr, impact, caller)
    (out_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    print(f"  [{time.time()-t:.1f}s]")
    print(format_scene_manifest(manifest))
    manifest_errors = validate_scene_manifest(hlr, impact, manifest)
    if manifest_errors:
        print(f"  Manifest validation: {len(manifest_errors)} issue(s):")
        for e in manifest_errors[:10]:
            print(f"    x {e}")
    else:
        print("  Manifest validation: PASSED")
    trace.phase_end("manifest", artifacts=["manifest.json"])

    # --- MLR phase --------------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 5: MLR (hybrid)")
    print("=" * 70)
    trace.phase_start("mlr")
    t = time.time()

    mlr_scenes = await run_mlr(hlr, router, schema=schema, fsm_only=True)

    # Scene scoping comes from the scene_manifest. Each scene gets ONLY:
    #   - interactions for systems listed in manifest.active_systems
    #   - entities listed in manifest.entities (deduped by name)
    manifest_by_scene = {s.scene_name: s for s in manifest.scenes}

    for scene_mlr in mlr_scenes:
        mf = manifest_by_scene.get(scene_mlr.scene_name)
        if mf is None:
            # No manifest entry → no interactions/entities for this scene
            scene_mlr.system_interactions = []
            scene_mlr.entities = []
            if scene_mlr.collisions:
                scene_mlr.collisions.collision_pairs = []
            continue

        # Drop collisions for scenes that don't have collision_system active —
        # otherwise the collisions LLM hallucinates combat pairs on menu screens.
        active_systems = list(mf.active_systems)
        if "collision_system" not in active_systems and scene_mlr.collisions:
            scene_mlr.collisions.collision_pairs = []
        scene_mlr.system_interactions = build_all_interactions(
            schema, active_systems, scene_mlr.scene_name,
            system_mapping=system_mapping,
            mechanic_specs=hlr.mechanic_specs,
        )

        # Dedupe entities by name while preserving (entity_name, from_enum) pairs
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

    print(f"  [{time.time()-t:.1f}s] {len(mlr_scenes)} scenes")

    # Persist MLR as per-scene files
    mlr_artifacts: list[str] = []
    for scene_mlr in mlr_scenes:
        fname = f"mlr_{scene_mlr.scene_name}.json"
        (out_dir / fname).write_text(
            json.dumps(scene_mlr.to_dict(), indent=2) + "\n"
        )
        mlr_artifacts.append(fname)
        fsm_states = len(scene_mlr.fsm.states) if scene_mlr.fsm else 0
        collisions = len(scene_mlr.collisions.collision_pairs) if scene_mlr.collisions else 0
        interactions = len(scene_mlr.system_interactions)
        entities = len(scene_mlr.entities)
        actions = sum(len(e.action_sets) for e in scene_mlr.entities)
        print(f"  {scene_mlr.scene_name}: fsm={fsm_states}, collisions={collisions}, "
              f"interactions={interactions}, entities={entities}, actions={actions}")

    mlr_errors = validate_mlr(hlr, mlr_scenes)
    if mlr_errors:
        print(f"  MLR validation: {len(mlr_errors)} issue(s):")
        for e in mlr_errors[:15]:
            print(f"    x {e}")
    else:
        print("  MLR validation: PASSED")
    trace.phase_end("mlr", artifacts=mlr_artifacts)

    # --- Finalize trace ---------------------------------------------------
    trace.end()
    trace_path = out_dir / "trace.json"
    trace.save(trace_path)

    print(f"\n{'=' * 70}")
    print(f"STOPPED AFTER MLR — {time.time()-t0:.0f}s total")
    print(f"  Output: {out_dir}")
    print(f"  Trace: {trace_path}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
