"""Run only the HLR phase to test HLT-driven generation."""

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
    from rayxi.llm.callers import build_callers, build_router
    from rayxi.spec.genre_detector import detect_genre
    from rayxi.spec.hlr import run_hlr
    from rayxi.spec.kb_retrieval import retrieve_relevant_chunks
    from rayxi.trace import start_trace

    callers = build_callers()
    router = build_router(callers)
    caller = router.primary
    kb_dir = Path("knowledge")
    t0 = time.time()

    # Start the e2e trace — captured by every phase via get_trace().
    trace = start_trace(user_prompt=PROMPT)

    # Genre detection
    print("=" * 70)
    print("STEP 0: Genre Detection")
    print("=" * 70)
    if GENRE_OVERRIDE:
        genre = GENRE_OVERRIDE
        print(f"  Override: {genre}")
    else:
        genre = detect_genre(PROMPT, kb_dir)
        print(f"  Detected: {genre}")

    # Load HLT
    print("\n" + "=" * 70)
    print("STEP 1: Load HLT")
    print("=" * 70)
    hlt_path = _TEMPLATE_DIR / f"{genre}_hlt.json"
    hlt = json.loads(hlt_path.read_text())
    hlt_systems = {name: info.get("description", "") for name, info in hlt.get("systems", {}).items()}
    print(f"  HLT: {hlt_path.name}")
    print(f"  Systems ({len(hlt_systems)}):")
    for name in sorted(hlt_systems.keys()):
        print(f"    - {name}")

    # KB retrieval
    print("\n" + "=" * 70)
    print("STEP 2: KB Retrieval")
    print("=" * 70)
    kb_chunks = retrieve_relevant_chunks(PROMPT, kb_dir, top_k=10)
    print(f"  Retrieved {len(kb_chunks)} chunks")

    # HLR
    print("\n" + "=" * 70)
    print("STEP 3: HLR")
    print("=" * 70)
    trace.phase_start("hlr")
    t = time.time()
    hlr, dynamic = await run_hlr(
        PROMPT, caller,
        template_systems=hlt_systems,
        kb_chunks=kb_chunks,
    )
    print(f"\n  [{time.time()-t:.1f}s] {hlr.game_name}")

    # Persist HLR product so downstream phases (and humans) can read it.
    hlr_out_dir = Path("output") / hlr.game_name
    hlr_out_dir.mkdir(parents=True, exist_ok=True)
    hlr_path = hlr_out_dir / "hlr.json"
    hlr_path.write_text(hlr.model_dump_json(indent=2) + "\n")
    trace.project_name = hlr.game_name
    trace.phase_end("hlr", artifacts=["hlr.json"])
    print(f"  Saved: {hlr_path}")
    print()

    # Show game_systems with origins
    for e in hlr.enums:
        if e.name == "game_systems":
            print(f"  game_systems ({len(e.values)}):")
            for v in e.values:
                origin = e.value_template_origins.get(v, "(missing)")
                desc = e.value_descriptions.get(v, "")[:70]
                print(f"    - {v}  [from: {origin}]")
                print(f"      {desc}")
            print()

    # Compare HLR systems vs HLT systems
    hlr_systems = set()
    hlr_origins = {}
    for e in hlr.enums:
        if e.name == "game_systems":
            hlr_systems = set(e.values)
            hlr_origins = dict(e.value_template_origins)

    used_template = {orig for orig in hlr_origins.values() if orig != "(new)" and orig}
    new_systems = {v for v in hlr_systems if hlr_origins.get(v) == "(new)"}
    missing_template = set(hlt_systems.keys()) - used_template

    print(f"  HLT systems used: {len(used_template)}/{len(hlt_systems)}")
    print(f"  New systems added: {len(new_systems)}")
    if missing_template:
        print(f"  HLT systems NOT used: {sorted(missing_template)}")

    # Check unused log
    log_path = Path("logs") / f"{hlr.game_name}_unused_template_systems.json"
    if log_path.exists():
        unused = json.loads(log_path.read_text())
        print(f"\n  Unused systems log ({log_path}):")
        for name, reason in unused.items():
            print(f"    - {name}: {reason}")

    # Finalize trace and save it next to the HLR product so the gallery can find it.
    trace.end()
    trace_path = hlr_out_dir / "trace.json"
    trace.save(trace_path)
    print(f"  Trace: {trace_path}")

    print(f"\n[{time.time()-t0:.1f}s total]")


if __name__ == "__main__":
    asyncio.run(main())
