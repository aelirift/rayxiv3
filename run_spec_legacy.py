"""Run the HLR -> MLR -> DLR spec pipeline with full E2E tracing."""

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


async def main(user_prompt: str, auto_approve: bool = True) -> None:
    from rayxi.build.codegen import build_deterministic_entities
    from rayxi.llm.callers import build_callers, build_router
    from rayxi.llm.pool import get_pool
    from rayxi.spec.dlr import run_dlr
    from rayxi.spec.dlr_validator import validate_dlr
    from rayxi.spec.hlr import run_hlr
    from rayxi.spec.hlr_validator import validate_hlr
    from rayxi.spec.impact import run_impact_matrix
    from rayxi.spec.impact_validator import format_impact_summary, validate_impact_matrix
    from rayxi.spec.linker import generate_link_doc
    from rayxi.spec.mlr import run_mlr
    from rayxi.spec.mlr_validator import validate_mlr
    from rayxi.spec.scene_manifest import (
        format_scene_manifest,
        run_scene_manifest,
        validate_scene_manifest,
    )
    from rayxi.trace import start_trace
    from rayxi.verify.interaction_map import validate_interactions
    from rayxi.verify.race_map import detect_races, format_races
    from rayxi.verify.state_map import generate_dot, validate_state_map

    # --- Start trace ---
    trace = start_trace(user_prompt)

    callers = build_callers()
    router = build_router(callers)
    hlr_caller = router.primary
    print(f"Primary: {type(router.primary).__name__}")
    if router.fast:
        print(f"Fast:    {type(router.fast).__name__} (collisions, HUD, backgrounds, UI)")
    t0 = time.time()

    # =======================================================================
    # HLR
    # =======================================================================
    print("\n" + "=" * 70)
    print("HLR — High-Level Requirements")
    print("=" * 70)

    trace.phase_start("hlr")
    t_hlr = time.time()
    hlr, dynamic_fields = await run_hlr(user_prompt, hlr_caller)
    trace.project_name = hlr.game_name
    print(f"  [{time.time() - t_hlr:.1f}s]")

    print("\n--- Dynamic Schema Fields ---")
    if dynamic_fields:
        for f in dynamic_fields:
            print(f"  + {f.field_name} ({f.field_type}): {f.description[:80]}")
    else:
        print("  (none)")

    print("\n--- Enums ---")
    for e in hlr.enums:
        flag = " [entity]" if e.entity else ""
        print(f"  {e.name}: {e.values}{flag}")

    print("\n--- Game Identity ---")
    print(json.dumps(hlr.model_dump(), indent=2))

    # --- Validate HLR ---
    print("\n" + "-" * 70)
    print("HLR Validation")
    print("-" * 70)

    errors = validate_hlr(hlr, dynamic_fields)
    trace.validation("hlr", "hlr_validator", passed=len(errors) == 0, errors=errors)
    trace.phase_end("hlr")

    if errors:
        print(f"FAILED — {len(errors)} error(s):")
        for e in errors:
            print(f"  x {e}")
        trace.end()
        trace.save(_TRACE_DIR / f"{hlr.game_name}_failed.json")
        print(f"\nTrace saved to {_TRACE_DIR}")
        return
    print("PASSED — HLR is internally consistent.")

    # =======================================================================
    # HLR Step 2: Impact Matrix
    # =======================================================================
    print("\n" + "=" * 70)
    print("HLR Step 2 — Impact Matrix (SEES / DOES / TRACKS / RUNS)")
    print("=" * 70)

    t_impact = time.time()
    impact_matrix = await run_impact_matrix(hlr, hlr_caller)
    print(f"  [{time.time() - t_impact:.1f}s]")

    # Show summary
    by_owner = impact_matrix.properties_by_owner()
    print(f"\n  Entries: {len(impact_matrix.entries)}")
    print(f"  Mechanics: {len(impact_matrix.mechanics)}")
    print(f"  Entities with properties: {len(by_owner)}")
    for owner, props in sorted(by_owner.items()):
        print(f"    {owner}: {len(props)} properties")
    print(f"  Assets: {len(impact_matrix.all_assets())}")

    # Validate
    print("\n" + "-" * 70)
    print("Impact Matrix Validation")
    print("-" * 70)

    impact_errors = validate_impact_matrix(hlr, impact_matrix)
    if impact_errors:
        print(f"  {len(impact_errors)} issue(s):")
        for e in impact_errors:
            print(f"    x {e}")
    else:
        print("  PASSED — no orphan properties, no broken chains.")

    # Save full summary
    dot_dir = _OUTPUT_DIR / hlr.game_name
    dot_dir.mkdir(parents=True, exist_ok=True)
    (dot_dir / "impact_matrix.txt").write_text(
        format_impact_summary(impact_matrix), encoding="utf-8")
    (dot_dir / "impact_matrix.json").write_text(
        impact_matrix.model_dump_json(indent=2), encoding="utf-8")
    print(f"\n  Saved: {dot_dir / 'impact_matrix.json'}")

    # =======================================================================
    # HLR Step 3: Scene Manifest
    # =======================================================================
    print("\n" + "=" * 70)
    print("HLR Step 3 — Scene Manifest")
    print("=" * 70)

    t_manifest = time.time()
    scene_manifest = await run_scene_manifest(hlr, impact_matrix, hlr_caller)
    print(f"  [{time.time() - t_manifest:.1f}s]")

    print(format_scene_manifest(scene_manifest))

    # Validate
    print("-" * 70)
    print("Scene Manifest Validation")
    print("-" * 70)

    manifest_errors = validate_scene_manifest(hlr, impact_matrix, scene_manifest)
    if manifest_errors:
        print(f"  {len(manifest_errors)} issue(s):")
        for e in manifest_errors:
            print(f"    x {e}")
    else:
        print("  PASSED — all entities and systems trace to HLR.")

    (dot_dir / "scene_manifest.json").write_text(
        scene_manifest.model_dump_json(indent=2), encoding="utf-8")
    print(f"\n  Saved: {dot_dir / 'scene_manifest.json'}")

    if not auto_approve:
        print("\nReview above. Proceed to MLR? [y/n] ", end="", flush=True)
        answer = input().strip().lower()
        if answer != "y":
            print("Aborted by user.")
            trace.end()
            trace.save(_TRACE_DIR / f"{hlr.game_name}_aborted.json")
            return

    # =======================================================================
    # MLR
    # =======================================================================
    print("\n" + "=" * 70)
    print("MLR — Mid-Level Requirements (parallel)")
    print("=" * 70)

    trace.phase_start("mlr")
    t_mlr = time.time()
    scene_mlrs = await run_mlr(hlr, router)
    print(f"  [{time.time() - t_mlr:.1f}s]")

    for scene_mlr in scene_mlrs:
        print(f"\n  Scene: {scene_mlr.scene_name}")
        if scene_mlr.fsm:
            print(f"    FSM: {len(scene_mlr.fsm.states)} states")
        if scene_mlr.collisions:
            print(f"    Collisions: {len(scene_mlr.collisions.collision_pairs)} pairs")
        for si in scene_mlr.system_interactions:
            print(f"    {si.game_system}: {len(si.interactions)} interactions")
        print(f"    Entities: {[e.entity_name for e in scene_mlr.entities]}")

    # --- Validate MLR ---
    print("\n" + "-" * 70)
    print("MLR Validation")
    print("-" * 70)

    mlr_errors = validate_mlr(hlr, scene_mlrs)
    trace.validation("mlr", "mlr_validator", passed=len(mlr_errors) == 0, errors=mlr_errors)
    trace.phase_end("mlr")

    if mlr_errors:
        print(f"FAILED — {len(mlr_errors)} error(s):")
        for e in mlr_errors:
            print(f"  x {e}")
    else:
        print("PASSED — MLR is consistent with HLR.")

    # =======================================================================
    # VERIFY: state map + race map + interaction map (on MLR products)
    # =======================================================================
    print("\n" + "=" * 70)
    print("VERIFY — State Map / Race Map / Interaction Map")
    print("=" * 70)

    trace.phase_start("verify_spec")

    # State map
    state_issues = validate_state_map(hlr, scene_mlrs)
    if state_issues:
        print(f"\n  State Map: {len(state_issues)} issues")
        for issue in state_issues:
            print(f"    x {issue}")
    else:
        print("\n  State Map: OK")

    # Save DOT for visualization
    dot_dir = _OUTPUT_DIR / hlr.game_name
    dot_dir.mkdir(parents=True, exist_ok=True)
    dot_str = generate_dot(hlr, scene_mlrs)
    (dot_dir / "state_map.dot").write_text(dot_str, encoding="utf-8")
    print(f"  State DOT: {dot_dir / 'state_map.dot'}")

    # Race conditions
    races = detect_races(scene_mlrs)
    if races:
        print(f"\n  Race Map: {len(races)} potential races")
        print(format_races(races))
    else:
        print("  Race Map: no races detected")

    # Interaction map
    interaction_issues = validate_interactions(scene_mlrs)
    if interaction_issues:
        print(f"\n  Interaction Map: {len(interaction_issues)} issues")
        for issue in interaction_issues:
            print(f"    x {issue}")
    else:
        print("  Interaction Map: OK")

    trace.phase_end("verify_spec")

    # =======================================================================
    # DLR
    # =======================================================================
    print("\n" + "=" * 70)
    print("DLR — Detail-Level Requirements (parallel)")
    print("=" * 70)

    trace.phase_start("dlr")
    t_dlr = time.time()
    scene_dlrs = await run_dlr(hlr, scene_mlrs, router)
    print(f"  [{time.time() - t_dlr:.1f}s]")

    for scene_dlr in scene_dlrs:
        print(f"\n  Scene: {scene_dlr.scene_name}")
        print(f"    Entity details: {len(scene_dlr.entity_details)}")
        print(f"    Interaction details: {len(scene_dlr.interaction_details)}")

    # --- Validate DLR ---
    print("\n" + "-" * 70)
    print("DLR Validation")
    print("-" * 70)

    dlr_errors = validate_dlr(scene_mlrs, scene_dlrs)
    trace.validation("dlr", "dlr_validator", passed=len(dlr_errors) == 0, errors=dlr_errors)
    trace.phase_end("dlr")

    if dlr_errors:
        print(f"FAILED — {len(dlr_errors)} error(s):")
        for e in dlr_errors:
            print(f"  x {e}")
    else:
        print("PASSED — DLR is complete and consistent with MLR.")

    # =======================================================================
    # BUILD — deterministic codegen
    # =======================================================================
    print("\n" + "=" * 70)
    print("BUILD — Deterministic Entity Codegen")
    print("=" * 70)

    trace.phase_start("build_deterministic")
    build_dir = _OUTPUT_DIR / hlr.game_name / "scripts"

    det_total = 0
    det_ok = 0
    llm_queued = 0
    for scene_mlr, scene_dlr in zip(scene_mlrs, scene_dlrs):
        scene_dir = build_dir / scene_mlr.scene_name
        results = build_deterministic_entities(scene_mlr, scene_dlr, scene_dir)
        for r in results:
            if r["method"] == "deterministic":
                det_total += 1
                if r["success"]:
                    det_ok += 1
                    print(f"  OK  {scene_mlr.scene_name}/{r['entity_name']} → {r['output_file']}")
                else:
                    print(f"  ERR {scene_mlr.scene_name}/{r['entity_name']}: {r['error']}")
            else:
                llm_queued += 1

    print(f"\n  Deterministic: {det_ok}/{det_total} built | LLM queued: {llm_queued}")
    trace.phase_end("build_deterministic")

    # =======================================================================
    # Link Document
    # =======================================================================
    print("\n" + "=" * 70)
    print("LINK DOCUMENT")
    print("=" * 70)

    link_doc = generate_link_doc(hlr, scene_mlrs)
    print(link_doc)

    # Save link doc
    (dot_dir / "link_doc.md").write_text(link_doc, encoding="utf-8")

    # =======================================================================
    # Summary + Trace
    # =======================================================================
    total_entities = sum(len(m.entities) for m in scene_mlrs)
    total_interactions = sum(len(si.interactions) for m in scene_mlrs for si in m.system_interactions)
    total_time = time.time() - t0

    trace.end()

    # Save trace
    trace_file = _TRACE_DIR / f"{hlr.game_name}.json"
    trace.save(trace_file)

    print(f"\n{'=' * 70}")
    print(f"Done. {len(scene_mlrs)} scenes, {total_entities} entities, {total_interactions} interactions.")
    print(f"HLR: {time.time() - t_hlr:.0f}s | MLR: {time.time() - t_mlr:.0f}s | DLR: {time.time() - t_dlr:.0f}s | Total: {total_time:.0f}s")
    print(f"Deterministic builds: {det_ok}/{det_total} | LLM queued: {llm_queued}")

    # Pool stats
    print(f"\n{'=' * 70}")
    print("POOL STATS")
    print("=" * 70)
    pool = get_pool()
    print(pool.stats.format())

    # Trace summary
    print(f"\n{'=' * 70}")
    print("TRACE SUMMARY")
    print("=" * 70)
    print(trace.format_summary())
    print(f"\nFull trace: {trace_file}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "I want to build a Marvel vs Capcom, it's a SF2 like game"
    asyncio.run(main(prompt, auto_approve=True))
