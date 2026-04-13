"""codegen_runner — the genre-agnostic system dispatcher.

For each system in impact_map.systems, picks a codegen strategy:

  1. Genre-template Python generator at
     knowledge/mechanic_templates/{genre}/codegen/python/{system}.py
     (opt-in, set by the template author for determinism)

  2. Typed-expression walker (mechanic_gen) — if the slice is fully typed
     (every write edge has a typed Expr formula, no procedural_notes)

  3. LLM generator (system_gen_llm) — universal fallback

Also tracks *who built what* so the output is auditable: the returned manifest
maps system → strategy used, file path, file size.

Core pipeline has ZERO game-specific names. It iterates whatever the impact
map declares. Tetris, card-game, fighter — all go through the same path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path
from typing import Any

from rayxi.llm.callers import build_callers, build_router
from rayxi.spec.impact_map import ImpactMap, WriteKind
from rayxi.spec.models import GameIdentity, MechanicSpec

from . import mechanic_gen
from .system_gen_llm import _godot_check_script, generate_system_via_llm

_log = logging.getLogger("rayxi.build.codegen_runner")
_KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "knowledge" / "mechanic_templates"


def _find_genre_python_generator(genre: str, system: str) -> Any | None:
    """Look for a hand-written Python generator in the genre template dir.

Convention: knowledge/mechanic_templates/{genre}/codegen/python/{system}.py
    The file must define a function `emit_system(slice_data, hlr, spec, constants) -> str`
    that returns the GDScript source for that system. Template authors opt in
    by dropping a file; zero core changes.
    """
    path = _KNOWLEDGE_DIR / genre / "codegen" / "python" / f"{system}.py"
    if not path.exists():
        return None
    spec_obj = importlib.util.spec_from_file_location(
        f"rayxi_genre_codegen_{genre}_{system}", path
    )
    if not spec_obj or not spec_obj.loader:
        return None
    module = importlib.util.module_from_spec(spec_obj)
    try:
        spec_obj.loader.exec_module(module)
    except Exception as exc:
        _log.warning("Genre %s python generator for %s failed to load: %s", genre, system, exc)
        return None
    fn = getattr(module, "emit_system", None)
    if callable(fn):
        return fn
    return None


def _slice_is_fully_typed(slice_data: dict) -> bool:
    """Return True iff every write edge in the slice has a typed formula and
    there are no procedural_notes. In that case mechanic_gen's walker can
    translate the whole thing 1:1 to GDScript without LLM.
    """
    writes = slice_data.get("own_writes", [])
    if not writes:
        return False  # nothing to walk; use LLM or skip
    for edge in writes:
        if edge.get("procedural_note"):
            return False
        # write kinds other than DERIVED require a formula for deterministic codegen
        wk = edge.get("write_kind", "")
        if wk in ("frame_update", "config_init", "lifecycle"):
            if edge.get("formula") is None:
                return False
    return True


def _pick_strategy(
    system: str,
    slice_data: dict,
    spec: MechanicSpec | None,
    genre: str,
) -> str:
    """Return the strategy name: 'template_python' | 'typed_walker' | 'llm'."""
    if _find_genre_python_generator(genre, system) is not None:
        return "template_python"
    if mechanic_gen.has_specialized_generator(system):
        return "typed_walker"
    if _slice_is_fully_typed(slice_data):
        return "typed_walker"
    return "llm"


async def _emit_one_system(
    system: str,
    imap: ImpactMap,
    hlr: GameIdentity,
    output_dir: Path,
    caller,
    constants: dict | None,
    pool_names: list[str] | None,
    system_descriptions: dict[str, str] | None,
    role_defs: dict | None,
    role_groups: dict[str, list[str]] | None,
    capabilities: dict[str, bool] | None,
) -> dict:
    slice_data = imap.slice_for_system(system)
    spec = next((m for m in hlr.mechanic_specs if m.system_name == system), None)
    strategy = _pick_strategy(system, slice_data, spec, hlr.genre)
    system_constants = (constants or {}).get(system, {})
    system_description = (system_descriptions or {}).get(system)

    _log.info("codegen_runner: %s → %s", system, strategy)

    out_path = output_dir / f"{system}.gd"
    source: str = ""

    if strategy == "template_python":
        fn = _find_genre_python_generator(hlr.genre, system)
        source = fn(slice_data, hlr, spec, system_constants)

    elif strategy == "typed_walker":
        candidate = mechanic_gen.generate_system_gdscript(
            system_name=system,
            imap=imap,
            constants=system_constants,
            role_groups=role_groups,
            capabilities=capabilities,
        )
        ok, err = _godot_check_script(candidate)
        if ok:
            source = candidate
        else:
            _log.info("typed_walker output for %s did not compile → LLM fallback: %s",
                      system, err.splitlines()[0] if err else "unknown error")
            source = await generate_system_via_llm(
                system, imap, hlr,
                caller=caller,
                constants=system_constants,
                pool_names=pool_names,
                system_description=system_description,
                role_defs=role_defs,
                role_groups=role_groups,
                capabilities=capabilities,
            )
            strategy = "llm_after_walker_fail"

    elif strategy == "llm":
        source = await generate_system_via_llm(
            system, imap, hlr,
            caller=caller,
            constants=system_constants,
            pool_names=pool_names,
            system_description=system_description,
            role_defs=role_defs,
            role_groups=role_groups,
            capabilities=capabilities,
        )

    else:
        raise RuntimeError(f"unknown strategy {strategy}")

    ok, err = _godot_check_script(source)
    if not ok:
        raise RuntimeError(
            f"{system} generated invalid GDScript via {strategy}: "
            f"{err.splitlines()[0] if err else 'unknown parse error'}"
        )

    out_path.write_text(source, encoding="utf-8")
    return {
        "system": system,
        "strategy": strategy,
        "file": str(out_path),
        "bytes": len(source),
    }


def _pool_names_from_imap(imap: ImpactMap, role_defs: dict | None) -> list[str]:
    """Pool names the LLM should know about for THIS game.

    Delegates to scene_gen so both the emitter and the LLM prompt share one
    canonical req-owned view.
    """
    from . import scene_gen
    return [scene_gen.pool_name_for(o)
            for o in scene_gen.pool_owners_from_imap(imap, role_defs)]


async def generate_all_systems(
    imap: ImpactMap,
    hlr: GameIdentity,
    output_dir: Path,
    constants: dict | None = None,
    role_defs: dict | None = None,
    role_groups: dict[str, list[str]] | None = None,
    capabilities: dict[str, bool] | None = None,
    system_descriptions: dict[str, str] | None = None,
    concurrency: int = 4,
) -> list[dict]:
    """Run the full per-system dispatch over impact_map.systems.

    Returns a manifest: list of {system, strategy, file, bytes}. The manifest
    answers the question "who built what".
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    router = build_router(build_callers())
    caller = router.get("mlr_interactions")
    pool_names = _pool_names_from_imap(imap, role_defs)
    _log.info("codegen_runner: entity pools derived from imap: %s", pool_names)

    # Parallelize LLM calls since each is independent
    sem = asyncio.Semaphore(concurrency)

    async def _do(system: str):
        async with sem:
            try:
                return await _emit_one_system(
                    system, imap, hlr, output_dir, caller,
                    constants, pool_names, system_descriptions, role_defs,
                    role_groups, capabilities,
                )
            except Exception as exc:
                _log.exception("codegen_runner: %s failed", system)
                return {
                    "system": system,
                    "strategy": "FAILED",
                    "file": "",
                    "bytes": 0,
                    "error": str(exc)[:200],
                }

    results = await asyncio.gather(*[_do(s) for s in imap.systems])
    return list(results)


def generate_all_systems_sync(
    imap: ImpactMap,
    hlr: GameIdentity,
    output_dir: Path,
    constants: dict | None = None,
    role_defs: dict | None = None,
    role_groups: dict[str, list[str]] | None = None,
    capabilities: dict[str, bool] | None = None,
    system_descriptions: dict[str, str] | None = None,
    concurrency: int = 4,
) -> list[dict]:
    """Sync wrapper for callers without an event loop."""
    return asyncio.run(
        generate_all_systems(
            imap,
            hlr,
            output_dir,
            constants=constants,
            role_defs=role_defs,
            role_groups=role_groups,
            capabilities=capabilities,
            system_descriptions=system_descriptions,
            concurrency=concurrency,
        )
    )
