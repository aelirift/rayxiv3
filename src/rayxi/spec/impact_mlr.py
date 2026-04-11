"""MLR per-system drill-down — refines the impact map within each system's scope.

For each game system in the seed map, one LLM call:
  - input:  a scoped slice of the impact map (the system's own nodes + peer
            writes + downstream readers + mechanic_spec if any)
  - output: JSON with added nodes (within this system's ownership), added
            interactions (write edges with trigger + optional condition),
            and scene_scope refinements on existing edges
  - merge:  additions flow back into the impact map with declared_by=mlr_{sys}

Strictness: no new systems, no new scenes, no removed pre-existing nodes/edges.
Anything the LLM tries to strip is ignored (the seed is authoritative).

All calls parallelized. Cached by system-slice hash so reruns are cheap.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from rayxi.llm.callers import CallerRouter
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from .impact_map import (
    Category,
    ImpactMap,
    PropertyNode,
    ReadEdge,
    Scope,
    WriteEdge,
    WriteKind,
)
from .models import GameIdentity, MechanicSpec

_log = logging.getLogger("rayxi.spec.impact_mlr")
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "impact_mlr"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


MLR_SYSTEM_PROMPT = """\
You are a game design architect. Your job is to refine ONE game system within \
an existing property-level impact graph.

You will receive:
  - The game's high-level context (game name, genre, global rules)
  - Your system's current slice of the impact graph: properties you write, \
properties you read, peer writers for those properties, and downstream readers
  - (If your system is a custom feature) your mechanic_spec

You MUST operate strictly within your system's scope. You may:
  - ADD new properties that your system owns the writing of (provide owner, \
name, type, category, purpose)
  - ADD new write edges from your system to properties (your own or others, \
if the interaction is real)
  - ADD new read edges for properties your system needs to read
  - ADD scene_scope to any write edge (list of scene names where the write is active)
  - ADD trigger prose (qualitative — no numeric values)
  - ADD condition (optional typed guard — use the expression grammar below)

You MUST NOT:
  - Add new systems or scenes — they are frozen
  - Remove or rename any pre-existing node or edge
  - Invent properties that belong to a different system's ownership

## Expression grammar (for condition and formula)

All formulas are typed JSON expressions, never prose:

  Literal:  {"kind": "literal", "type": "int|float|bool", "value": 3}
  Ref:      {"kind": "ref", "path": "fighter.current_hp"}
  BinOp:    {"kind": "op", "op": "add|sub|mul|div|mod|lt|le|gt|ge|eq|ne|and|or",
             "left": <Expr>, "right": <Expr>}
  FnCall:   {"kind": "call", "fn": "clamp|min|max|abs|floor|ceil|sign|not", "args": [<Expr>, ...]}
  Cond:     {"kind": "cond", "condition": <Expr>, "then_val": <Expr>, "else_val": <Expr>}

Example: (fighter.rage_stacks > 0) as a guard:
  {"kind": "op", "op": "gt",
   "left": {"kind": "ref", "path": "fighter.rage_stacks"},
   "right": {"kind": "literal", "type": "int", "value": 0}}

## Output format

A single JSON object:

{
  "system": "string — must match the system you were asked to refine",
  "added_nodes": [
    {
      "id": "owner.name",
      "owner": "fighter|projectile|game|stage|hud.widget_name",
      "name": "snake_case",
      "type": "int|float|bool|string|Vector2|Color",
      "category": "config|state|derived",
      "scope": "instance|player_slot|character_def|game",
      "description": "what this property is and why it exists"
    }
  ],
  "added_writes": [
    {
      "target": "owner.property",
      "write_kind": "config_init|lifecycle|frame_update|derived",
      "scene_scope": ["scene_name", ...],
      "trigger": "qualitative description of what fires this write",
      "condition": null | <Expr>
    }
  ],
  "added_reads": [
    {
      "source": "owner.property",
      "scene_scope": ["scene_name", ...],
      "purpose": "why this system needs to read it"
    }
  ],
  "rationale": "one paragraph explaining your refinements"
}

Rules:
- DO NOT fill formulas here — DLR fills them later.
- scene_scope is MANDATORY for every new write/read — omit only if truly every scene.
- Do not echo the existing graph back. Only include NEW entries.
- If no refinements are needed, return empty arrays but still include the system name.
- Output ONLY the JSON object. No markdown, no code fences, no explanation.
"""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_key(system: str, slice_json: str) -> str:
    return hashlib.sha256((system + slice_json).encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    path = _CACHE_DIR / f"{key}.json"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _cache_put(key: str, data: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(data, encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-system drill-down
# ---------------------------------------------------------------------------


async def _call_system(
    system: str,
    slice_ctx: dict,
    hlr: GameIdentity,
    mechanic_spec: MechanicSpec | None,
    caller: LLMCaller,
) -> dict:
    """Call the LLM for one system. Cache-first."""
    hlr_ctx = {
        "game_name": hlr.game_name,
        "genre": hlr.genre,
        "player_mode": hlr.player_mode,
        "global_rules": hlr.global_rules,
        "scenes": [s.scene_name for _, s in enumerate(hlr.scenes)],
    }
    prompt_parts = [
        f"## Game context\n```json\n{json.dumps(hlr_ctx, indent=2)}\n```",
        f"## Your system: {system}",
        f"## Your slice of the impact graph\n```json\n{json.dumps(slice_ctx, indent=2)}\n```",
    ]
    if mechanic_spec is not None:
        prompt_parts.append(
            f"## Your mechanic_spec (this system is custom — these are HLR-declared intent)\n"
            f"```json\n{mechanic_spec.model_dump_json(indent=2)}\n```"
        )
    prompt = "\n\n".join(prompt_parts)

    key = _cache_key(system, prompt)
    cached = _cache_get(key)
    trace = get_trace()
    caller_name = type(caller).__name__
    label = f"impact_mlr[{system}]"
    if cached is not None:
        _log.info("%s — cache hit (%s)", label, key)
        if trace:
            cid = trace.llm_start("mlr", label, caller_name, len(prompt))
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return json.loads(cached)

    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("mlr", label, caller_name, len(prompt)) if trace else ""
        try:
            raw = await caller(MLR_SYSTEM_PROMPT, prompt, json_mode=True, label=label)
            parsed = json.loads(raw)
            _cache_put(key, raw)
            if trace:
                trace.llm_end(cid, output_chars=len(raw))
            return parsed
        except (json.JSONDecodeError, RuntimeError, Exception) as exc:
            last_err = exc
            if trace:
                trace.llm_end(cid, output_chars=0, error=str(exc)[:120])
            _log.warning("%s attempt %d failed: %s", label, attempt + 1, str(exc)[:120])
    raise RuntimeError(f"{label} failed after 3 attempts: {last_err}")


# ---------------------------------------------------------------------------
# Merge additions back into the map (lenient within scope)
# ---------------------------------------------------------------------------


def _merge_additions(
    imap: ImpactMap,
    system: str,
    result: dict,
    valid_systems: set[str],
    valid_scenes: set[str],
) -> tuple[int, int, int, list[str]]:
    """Merge per-system LLM output back into the impact map.

    Returns (nodes_added, writes_added, reads_added, rejection_reasons).
    """
    from .expr import parse_expr
    origin = f"mlr:{system}"
    nodes_added = 0
    writes_added = 0
    reads_added = 0
    rejected: list[str] = []

    # Nodes
    for nd in result.get("added_nodes") or []:
        try:
            nid = nd.get("id") or f"{nd.get('owner','')}.{nd.get('name','')}"
            if nid in imap.nodes:
                continue  # already known, lenient — skip
            category = Category(nd.get("category", "state"))
            scope = Scope(nd.get("scope", "instance"))
            imap.add_node(PropertyNode(
                id=nid,
                owner=nd.get("owner", "game"),
                name=nd.get("name", ""),
                type=nd.get("type", "int"),
                category=category,
                scope=scope,
                description=nd.get("description", ""),
                declared_by=origin,
            ))
            nodes_added += 1
        except Exception as exc:
            rejected.append(f"node {nd}: {exc}")

    # Writes
    for wd in result.get("added_writes") or []:
        try:
            target = wd.get("target", "")
            if target not in imap.nodes:
                rejected.append(f"write {target}: target not in nodes (MLR cannot invent unowned properties)")
                continue
            scenes = [s for s in (wd.get("scene_scope") or []) if s in valid_scenes]
            bad_scenes = set(wd.get("scene_scope") or []) - set(scenes)
            if bad_scenes:
                rejected.append(f"write {target}: unknown scenes {bad_scenes} (stripped)")
            cond_raw = wd.get("condition")
            cond = parse_expr(cond_raw) if cond_raw else None
            imap.add_write_edge(WriteEdge(
                system=system,
                target=target,
                write_kind=WriteKind(wd.get("write_kind", "frame_update")),
                scene_scope=scenes,
                trigger=wd.get("trigger", ""),
                condition=cond,
                declared_by=origin,
            ))
            writes_added += 1
        except Exception as exc:
            rejected.append(f"write {wd.get('target','?')}: {exc}")

    # Reads
    for rd in result.get("added_reads") or []:
        try:
            source = rd.get("source", "")
            if source not in imap.nodes:
                rejected.append(f"read {source}: source not in nodes")
                continue
            scenes = [s for s in (rd.get("scene_scope") or []) if s in valid_scenes]
            imap.add_read_edge(ReadEdge(
                system=system,
                source=source,
                scene_scope=scenes,
                purpose=rd.get("purpose", ""),
                declared_by=origin,
            ))
            reads_added += 1
        except Exception as exc:
            rejected.append(f"read {rd.get('source','?')}: {exc}")

    return nodes_added, writes_added, reads_added, rejected


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def drill_down_mlr(
    imap: ImpactMap,
    hlr: GameIdentity,
    router: CallerRouter,
) -> dict:
    """Run per-system MLR drill-downs in parallel. Mutates imap in place.

    Returns a summary dict with per-system stats.
    """
    caller = router.get("mlr_interactions")
    specs_by_system = {m.system_name: m for m in hlr.mechanic_specs}

    valid_systems = set(imap.systems)
    valid_scenes = set(imap.scenes)

    async def _do(system: str) -> tuple[str, dict]:
        slice_ctx = imap.slice_for_system(system)
        result = await _call_system(
            system=system,
            slice_ctx=slice_ctx,
            hlr=hlr,
            mechanic_spec=specs_by_system.get(system),
            caller=caller,
        )
        return system, result

    _log.info("Impact MLR: drilling %d systems in parallel", len(imap.systems))
    results = await asyncio.gather(*[_do(s) for s in imap.systems])

    summary: dict = {}
    for system, result in results:
        n, w, r, rejected = _merge_additions(imap, system, result, valid_systems, valid_scenes)
        summary[system] = {
            "nodes_added": n,
            "writes_added": w,
            "reads_added": r,
            "rejected": rejected,
            "rationale": result.get("rationale", ""),
        }
        if rejected:
            _log.warning("MLR[%s]: %d rejected entries", system, len(rejected))
    return summary


# ---------------------------------------------------------------------------
# Validator — lenient within scope, strict on scope
# ---------------------------------------------------------------------------


def validate_impact_mlr(imap: ImpactMap, seed_systems: set[str], seed_scenes: set[str]) -> list[str]:
    """After MLR drill-down, verify scope was not violated.

    Lenient: new nodes/edges are OK if they fit an existing system.
    Strict: systems and scenes lists must be unchanged.
    """
    errors: list[str] = []
    if set(imap.systems) != seed_systems:
        errors.append(
            f"MLR violated strict scope — systems changed: "
            f"added={set(imap.systems)-seed_systems}, removed={seed_systems-set(imap.systems)}"
        )
    if set(imap.scenes) != seed_scenes:
        errors.append(
            f"MLR violated strict scope — scenes changed: "
            f"added={set(imap.scenes)-seed_scenes}, removed={seed_scenes-set(imap.scenes)}"
        )
    # Every write/read edge must reference a valid system and valid scenes
    for e in imap.write_edges:
        if e.system not in seed_systems:
            errors.append(f"write edge {e.system}→{e.target}: system not in seed scope")
        for s in e.scene_scope:
            if s not in seed_scenes:
                errors.append(f"write edge {e.system}→{e.target}: scene '{s}' not in seed scope")
    for e in imap.read_edges:
        for s in e.scene_scope:
            if s not in seed_scenes:
                errors.append(f"read edge {e.system}←{e.source}: scene '{s}' not in seed scope")
    return errors
