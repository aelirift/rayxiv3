"""Impact-map pipeline: HLR → Seed → MLR drill-down → DLR fill → views.

This is the v2 pipeline built on the property-level impact graph. It replaces
the per-scene MLR/DLR bundling with a single central impact map and per-system
drill-downs that stay small and focused.

Output structure (in output/{game_name}/):
  hlr.json                        — from run_hlr
  impact_map_seed.json            — deterministic, post-HLR
  impact_map_mlr.json             — after MLR drill-down
  impact_map_final.json           — after DLR fill
  views/scene_{scene}.json        — one per scene (projection)
  views/system_{system}.json      — one per system (projection)
  views/entity_{owner}.json       — one per entity (projection)
  dlr_mechanic_constants.json     — concrete constant values
  trace.json                      — full pipeline trace with artifact buttons

Usage:
    python run_to_impact.py "your prompt"
    python run_to_impact.py "your prompt" 2d_fighter
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, str(Path(__file__).parent / "src"))
_TEMPLATE_DIR = Path(__file__).parent / "knowledge" / "mechanic_templates"

DEFAULT_PROMPT = "Street Fighter 2, only Ryu, mirror match (Ryu vs Ryu CPU), classic 2D fighting game"
PROMPT = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
GENRE_OVERRIDE = sys.argv[2] if len(sys.argv) > 2 else None


async def main():
    from rayxi.knowledge import KnowledgeBase
    from rayxi.knowledge.mechanic_loader import load_game_schema
    from rayxi.llm.callers import build_callers, build_router
    from rayxi.spec.genre_detector import detect_genre
    from rayxi.spec.hlr import run_hlr
    from rayxi.spec.hlr_validator import validate_hlr
    from rayxi.spec.impact_dlr import fill_dlr, validate_impact_dlr
    from rayxi.spec.impact_map import (
        validate_impact_map_structural,
        validate_impact_seed,
    )
    from rayxi.spec.impact_mlr import drill_down_mlr, validate_impact_mlr
    from rayxi.spec.impact_seed import build_impact_seed
    from rayxi.spec.kb_retrieval import retrieve_relevant_chunks
    from rayxi.spec.system_mapper import map_hlr_to_template
    from rayxi.trace import start_trace

    callers = build_callers()
    router = build_router(callers)
    caller = router.primary
    kb_dir = Path("knowledge")
    t0 = time.time()
    trace = start_trace(user_prompt=PROMPT)

    # --- Genre + HLT setup -------------------------------------------------
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
    hlt_phases = {name: info.get("phase", "physics") for name, info in hlt.get("systems", {}).items()}
    hlt_property_enums = hlt.get("property_enums", {})
    template_path = _TEMPLATE_DIR / f"{genre}.json"
    kb_chunks = retrieve_relevant_chunks(PROMPT, kb_dir, top_k=10)
    print(f"  HLT: {len(hlt_systems)} systems. KB: {len(kb_chunks)} chunks.")

    # --- HLR ---------------------------------------------------------------
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
    views_dir = out_dir / "views"
    views_dir.mkdir(exist_ok=True)

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
    print(f"  HLR validation: PASSED ({len(hlr.mechanic_specs)} custom mechanic_specs)")
    trace.phase_end("hlr", artifacts=["hlr.json"])

    schema = load_game_schema(template_path, hlr)
    system_mapping = map_hlr_to_template(hlr, schema)

    # --- Impact seed (deterministic) ---------------------------------------
    print("\n" + "=" * 70)
    print("STEP 2: Impact Seed (deterministic)")
    print("=" * 70)
    trace.phase_start("impact_seed")
    imap = build_impact_seed(
        hlr, schema,
        system_mapping=system_mapping,
        system_phases=hlt_phases,
        property_enums=hlt_property_enums,
    )
    (out_dir / "impact_map_seed.json").write_text(imap.model_dump_json(indent=2) + "\n")
    print(f"  {len(imap.nodes)} nodes, {len(imap.write_edges)} writes, "
          f"{len(imap.read_edges)} reads, {len(imap.systems)} systems, {len(imap.scenes)} scenes")

    struct_errs = validate_impact_map_structural(imap)
    scope_errs = validate_impact_seed(imap, hlr)
    if struct_errs or scope_errs:
        print(f"  seed validation: {len(struct_errs)} structural, {len(scope_errs)} scope errors")
        for e in (struct_errs + scope_errs)[:10]:
            print(f"    x {e}")
        if scope_errs:
            trace.phase_end("impact_seed", artifacts=["impact_map_seed.json"])
            trace.end()
            trace.save(out_dir / "trace.json")
            return
    else:
        print("  seed validation: PASSED")
    trace.phase_end("impact_seed", artifacts=["impact_map_seed.json"])

    # Record seed scope for MLR strict check
    seed_systems = set(imap.systems)
    seed_scenes = set(imap.scenes)

    # --- MLR drill-down ----------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 3: MLR drill-down (per-system)")
    print("=" * 70)
    trace.phase_start("mlr")
    t = time.time()
    mlr_summary = await drill_down_mlr(imap, hlr, router)
    print(f"  [{time.time()-t:.1f}s]")
    for sys_name, stats in mlr_summary.items():
        additions = stats["nodes_added"] + stats["writes_added"] + stats["reads_added"]
        if additions:
            print(f"  {sys_name}: +{stats['nodes_added']} nodes, "
                  f"+{stats['writes_added']} writes, +{stats['reads_added']} reads")
    (out_dir / "impact_map_mlr.json").write_text(imap.model_dump_json(indent=2) + "\n")
    (out_dir / "mlr_summary.json").write_text(json.dumps(mlr_summary, indent=2) + "\n")

    mlr_errs = validate_impact_mlr(imap, seed_systems, seed_scenes)
    if mlr_errs:
        print(f"  MLR validation: {len(mlr_errs)} issue(s)")
        for e in mlr_errs[:10]:
            print(f"    x {e}")
    else:
        print("  MLR validation: PASSED")
    trace.phase_end("mlr", artifacts=["impact_map_mlr.json", "mlr_summary.json"])

    # --- DLR fill ----------------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 4: DLR fill (per-system typed values)")
    print("=" * 70)
    trace.phase_start("dlr")
    t = time.time()
    kb = KnowledgeBase(kb_dir)
    kb_context = kb.retrieve_context(hlr.game_name) or kb.retrieve_context(hlr.genre)
    kb_text = json.dumps(kb_context.game_data, indent=2) if kb_context.game_data else "{}"

    dlr_summary, mech_constants = await fill_dlr(imap, hlr, router, kb_game_data_text=kb_text)
    print(f"  [{time.time()-t:.1f}s]")
    total_filled = sum(s["nodes_filled"] + s["edges_filled"] for s in dlr_summary.values())
    total_errors = sum(len(s["errors"]) for s in dlr_summary.values())
    print(f"  {total_filled} total fills, {total_errors} errors across {len(dlr_summary)} systems")

    (out_dir / "impact_map_final.json").write_text(imap.model_dump_json(indent=2) + "\n")
    (out_dir / "dlr_summary.json").write_text(json.dumps(dlr_summary, indent=2) + "\n")
    (out_dir / "dlr_mechanic_constants.json").write_text(json.dumps(mech_constants, indent=2) + "\n")

    dlr_errs = validate_impact_dlr(imap)
    if dlr_errs:
        print(f"  DLR validation: {len(dlr_errs)} unfilled items")
        for e in dlr_errs[:15]:
            print(f"    x {e}")
    else:
        print("  DLR validation: PASSED (every node + frame-update edge has a typed value)")
    trace.phase_end("dlr", artifacts=[
        "impact_map_final.json", "dlr_summary.json", "dlr_mechanic_constants.json",
    ])

    # --- Views (deterministic projections) ---------------------------------
    print("\n" + "=" * 70)
    print("STEP 5: Views (deterministic projections)")
    print("=" * 70)
    trace.phase_start("views")
    view_artifacts: list[str] = []

    # Scene views
    for scene in imap.scenes:
        data = imap.scene_view(scene)
        fname = f"views/scene_{scene}.json"
        (out_dir / fname).write_text(json.dumps(data, indent=2, default=str) + "\n")
        view_artifacts.append(fname)

    # System views
    for system in imap.systems:
        data = imap.slice_for_system(system)
        fname = f"views/system_{system}.json"
        (out_dir / fname).write_text(json.dumps(data, indent=2, default=str) + "\n")
        view_artifacts.append(fname)

    # Entity views — unique owners
    owners = sorted({n.owner for n in imap.nodes.values()})
    for owner in owners:
        safe_owner = owner.replace(".", "_")
        data = imap.entity_view(owner)
        fname = f"views/entity_{safe_owner}.json"
        (out_dir / fname).write_text(json.dumps(data, indent=2, default=str) + "\n")
        view_artifacts.append(fname)

    print(f"  Wrote {len(view_artifacts)} view files: "
          f"{len(imap.scenes)} scenes, {len(imap.systems)} systems, {len(owners)} entities")
    trace.phase_end("views", artifacts=view_artifacts)

    # --- Finalize ----------------------------------------------------------
    trace.end()
    trace_path = out_dir / "trace.json"
    trace.save(trace_path)

    print(f"\n{'=' * 70}")
    print(f"DONE — {time.time()-t0:.0f}s total")
    print(f"  Output: {out_dir}")
    print(f"  Trace: {trace_path}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
