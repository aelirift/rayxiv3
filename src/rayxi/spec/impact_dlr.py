"""DLR per-system typed-value fill — every property and edge gets concrete data.

Strategy: one LLM call per system (same slicing as MLR), asking the LLM to
fill every missing initial_value / derivation / formula as a typed expression.

This keeps prompt sizes small (per-system slices, not global) and lets the LLM
see the whole neighborhood of each property (writers, readers, peer constants).

Output format is a big JSON blob per call:
  - node_fills: {property_id → {"initial_value": <Expr>, "derivation": <Expr>}}
  - edge_fills: list of {system, target, write_kind, formula: <Expr>}

We parse and validate the Expr trees before merging back.
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import logging
from pathlib import Path

from rayxi.llm.callers import CallerRouter
from rayxi.llm.json_tools import parse_json_response
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from .expr import parse_expr, validate_expr
from .impact_map import Category, ImpactMap, WriteEdge, WriteKind
from .models import GameIdentity, MechanicSpec

_log = logging.getLogger("rayxi.spec.impact_dlr")
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "impact_dlr"


ORPHAN_FILL_PROMPT = """\
You are a game implementation engineer. You are filling character-definition \
config values — properties that are set once at game load time and never \
mutated at runtime. You will receive ONE entity's worth of unfilled properties.

Fill EVERY unfilled property with a typed initial_value (or derivation for \
derived nodes) using the JSON expression grammar:

  Literal:  {"kind": "literal", "type": "int|float|bool|string", "value": ...}
  Ref:      {"kind": "ref", "path": "fighter.max_health" | "const.foo"}
  BinOp:    {"kind": "op", "op": "add|sub|mul|div|...", "left": <Expr>, "right": <Expr>}
  FnCall:   {"kind": "call", "fn": "clamp|min|max|abs|...", "args": [<Expr>, ...]}
  Cond:     {"kind": "cond", "condition": <Expr>, "then_val": <Expr>, "else_val": <Expr>}

Infer scales and defaults from the supplied systems, properties, constants, \
and game rules. Keep all values internally consistent with the actual req \
artifacts instead of borrowing assumptions from unrelated games.

Output format:

{
  "owner": "string",
  "node_fills": {
    "property_id": {"initial_value": <Expr>} | {"derivation": <Expr>}
  }
}

Rules:
- EVERY property in the input must have a fill.
- For Vector2, use {"kind": "literal", "type": "vector2", "value": [x, y]}.
- For Color, use {"kind": "literal", "type": "color", "value": "#rrggbb"}.
- For Rect2 (hitboxes, hurtboxes), use {"kind": "literal", "type": "rect2", "value": [x, y, w, h]}.
- For arrays (input buffers), use {"kind": "literal", "type": "list", "value": []}.
- For structured dicts (rare), use {"kind": "literal", "type": "dict", "value": {...}}.
- Output ONLY the JSON.
"""


DLR_SYSTEM_PROMPT = """\
You are a game implementation engineer. Your job is to fill every missing \
concrete value and formula for ONE game system in a property-level impact graph.

You will receive:
  - The game's high-level context
  - Your system's slice of the impact graph (its nodes + write edges + read edges)
  - Any mechanic_spec constants_for_dlr relevant to this system
  - KB game data (frame data, balance, colors) if available

For EVERY node in your slice that has category 'config' or 'state', fill \
`initial_value` as a typed expression. For every node with category 'derived', \
fill `derivation`. For every write edge, fill `formula` — the typed update \
expression applied when the edge fires.

## Expression grammar (JSON — no prose math anywhere)

  Literal:  {"kind": "literal", "type": "int|float|bool|string", "value": 3}
  Ref:      {"kind": "ref", "path": "fighter.current_health" | "const.max_rage_stacks" | "event.damage_taken"}
  BinOp:    {"kind": "op", "op": "add|sub|mul|div|mod|lt|le|gt|ge|eq|ne|and|or",
             "left": <Expr>, "right": <Expr>}
  FnCall:   {"kind": "call", "fn": "clamp|min|max|abs|floor|ceil|sign|not", "args": [<Expr>, ...]}
  Cond:     {"kind": "cond", "condition": <Expr>, "then_val": <Expr>, "else_val": <Expr>}

Examples:

  # current_health starts at max_health
  "initial_value": {"kind": "ref", "path": "fighter.max_health"}

  # current_health decreases by incoming damage (formula for combat_system's write)
  "formula": {"kind": "op", "op": "sub",
              "left": {"kind": "ref", "path": "fighter.current_health"},
              "right": {"kind": "ref", "path": "event.damage_taken"}}

  # rage_stacks at round start: 0
  "initial_value": {"kind": "literal", "type": "int", "value": 0}

  # hp_bar.fill_percent = clamp(current_health / max_health, 0, 1)
  "derivation": {"kind": "call", "fn": "clamp", "args": [
    {"kind": "op", "op": "div",
     "left": {"kind": "ref", "path": "fighter.current_health"},
     "right": {"kind": "ref", "path": "fighter.max_health"}},
    {"kind": "literal", "type": "float", "value": 0.0},
    {"kind": "literal", "type": "float", "value": 1.0}
  ]}

## Ref path namespaces

  fighter.<prop>       — property on the fighter entity
  projectile.<prop>    — property on a projectile entity
  game.<prop>          — global game-level property
  hud.<widget>.<prop>  — property on a named HUD widget
  const.<name>         — a constant from mechanic_spec.constants_for_dlr — YOU fill these too
  event.<name>         — a runtime event parameter (damage_taken, direction, attacker_id)
  literal              — wrap raw values in {"kind": "literal", ...}

## Output format

One JSON object:

{
  "system": "string",
  "constants": [
    {"name": "max_rage_stacks", "type": "int", "value": 3}
  ],
  "node_fills": {
    "fighter.rage_stacks": {
      "initial_value": {"kind": "literal", "type": "int", "value": 0}
    }
  },
  "edge_fills": [
    {
      "target": "fighter.rage_fill_value",
      "write_kind": "frame_update",
      "formula": {"kind": "op", "op": "add",
                  "left": {"kind": "ref", "path": "fighter.rage_fill_value"},
                  "right": {"kind": "op", "op": "div",
                            "left": {"kind": "ref", "path": "event.damage_taken"},
                            "right": {"kind": "ref", "path": "const.rage_fill_threshold"}}}
    },
    {
      "target": "fighter.input_buffer",
      "write_kind": "frame_update",
      "procedural_note": "Push latest input onto buffer, drop oldest if length exceeds 60."
    }
  ]
}

Rules:
- EVERY node in your slice must have a fill — no skipping.
- EVERY write edge in your slice must have EITHER a `formula` (typed expression) OR a `procedural_note` (prose) — never leave a write edge empty.
- Use `procedural_note` ONLY for operations that genuinely cannot be expressed as a pure expression: circular buffers, multi-step state machines, rect2 updates, input-buffer ops, hitbox activation by frame index. Prefer `formula` whenever possible.
- Use the mechanic_spec constants as `const.<name>` refs where applicable.
- Reuse the exact property ids already present in the slice. Do NOT create or
  reinforce duplicate alias families like `current_hp`/`max_hp` if the slice
  already uses `current_health`/`max_health`.
- Infer frame, timing, distance, damage, health, speed, and HUD scales from the
  provided systems, objects, constants, and global rules. Keep values internally
  consistent with this game's req artifacts rather than with any named reference game.
- Output ONLY the JSON. No markdown, no explanation.
"""


def _contextual_value_guidance(
    system: str,
    slice_ctx: dict,
    hlr: GameIdentity,
    mechanic_spec: MechanicSpec | None = None,
) -> str:
    spec_json = mechanic_spec.model_dump(mode="json") if mechanic_spec is not None else {}
    context_blob = json.dumps(
        {
            "system": system,
            "slice": slice_ctx,
            "global_rules": hlr.global_rules,
            "mechanic_spec": spec_json,
        },
        default=str,
    ).lower()
    guidance: list[str] = [
        "- Base every scale and default on the supplied objects, scenes, mechanics, systems, and constants."
    ]
    if any(token in context_blob for token in ("health", "damage", "projectile", "block", "hitbox", "hurtbox", "combo", "rage")):
        guidance.append(
            "- For combat-like mechanics, keep health, damage, cooldown, projectile, and HUD values internally consistent across the linked properties."
        )
    if any(token in context_blob for token in ("race", "lap", "checkpoint", "vehicle", "kart", "drift", "boost", "position_rank", "item_box", "camera")):
        guidance.append(
            "- For traversal or race-like mechanics, keep track coordinates, speed caps, boost windows, checkpoint progress, laps, and HUD positions mutually consistent."
        )
    if any(token in context_blob for token in ("hud", "bar", "meter", "display", "minimap", "color")):
        guidance.append(
            "- For HUD and presentation properties, keep colors, layout positions, and fill ratios aligned with the state properties they represent."
        )
    if len(guidance) == 1:
        guidance.append(
            "- Prefer neutral, mechanics-derived values and formulas over genre or franchise conventions that are not present in the req artifacts."
        )
    return "\n".join(guidance)


def _cache_key(system: str, slice_json: str, spec_json: str) -> str:
    return hashlib.sha256((system + slice_json + spec_json).encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    path = _CACHE_DIR / f"{key}.json"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _cache_put(key: str, data: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(data, encoding="utf-8")


async def _call_dlr_system(
    system: str,
    slice_ctx: dict,
    hlr: GameIdentity,
    mechanic_spec: MechanicSpec | None,
    kb_text: str,
    caller: LLMCaller,
) -> dict:
    hlr_ctx = {
        "game_name": hlr.game_name,
        "genre": hlr.genre,
        "global_rules": hlr.global_rules,
    }
    parts = [
        f"## Game context\n```json\n{json.dumps(hlr_ctx, indent=2)}\n```",
        f"## Your system: {system}",
        f"## Your slice\n```json\n{json.dumps(slice_ctx, indent=2)}\n```",
    ]
    if mechanic_spec is not None and mechanic_spec.constants_for_dlr:
        parts.append(
            f"## Mechanic constants you must fill (from mechanic_spec)\n"
            f"```json\n{json.dumps([c.model_dump() for c in mechanic_spec.constants_for_dlr], indent=2)}\n```"
        )
    parts.append(
        "## Value guidance\n"
        + _contextual_value_guidance(system, slice_ctx, hlr, mechanic_spec)
    )
    if kb_text and kb_text != "{}":
        parts.append(f"## KB Game Data (reference values)\n```json\n{kb_text[:8000]}\n```")
    prompt = "\n\n".join(parts)

    spec_json = mechanic_spec.model_dump_json() if mechanic_spec else ""
    key = _cache_key(system, prompt, spec_json)
    cached = _cache_get(key)
    trace = get_trace()
    caller_name = type(caller).__name__
    label = f"impact_dlr[{system}]"
    if cached is not None:
        _log.info("%s — cache hit (%s)", label, key)
        if trace:
            cid = trace.llm_start("dlr", label, caller_name, len(prompt))
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return parse_json_response(cached)

    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("dlr", label, caller_name, len(prompt)) if trace else ""
        try:
            raw = await caller(DLR_SYSTEM_PROMPT, prompt, json_mode=True, label=label)
            parsed = parse_json_response(raw)
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
# Merge fills back into the impact map
# ---------------------------------------------------------------------------


def _merge_fills(imap: ImpactMap, system: str, result: dict) -> tuple[int, int, list[str]]:
    """Attach typed fills to nodes and edges. Returns (node_fills, edge_fills, errors)."""
    node_count = 0
    edge_count = 0
    errors: list[str] = []

    # Node fills
    for pid, fill in (result.get("node_fills") or {}).items():
        if pid not in imap.nodes:
            errors.append(f"DLR[{system}]: fill for unknown node {pid}")
            continue
        node = imap.nodes[pid]
        try:
            if "initial_value" in fill and fill["initial_value"] is not None:
                expr = parse_expr(fill["initial_value"])
                vs = validate_expr(expr)
                if vs:
                    errors.append(f"DLR[{system}]: invalid initial_value for {pid}: {vs[0]}")
                else:
                    node.initial_value = expr
                    node_count += 1
            if "derivation" in fill and fill["derivation"] is not None:
                expr = parse_expr(fill["derivation"])
                vs = validate_expr(expr)
                if vs:
                    errors.append(f"DLR[{system}]: invalid derivation for {pid}: {vs[0]}")
                else:
                    node.derivation = expr
                    if not node_count:  # still count it
                        node_count += 1
        except Exception as exc:
            errors.append(f"DLR[{system}]: failed to parse fill for {pid}: {exc}")

    # Edge fills
    for ed in (result.get("edge_fills") or []):
        target = ed.get("target", "")
        wk_str = ed.get("write_kind", "frame_update")
        try:
            wk = WriteKind(wk_str)
        except ValueError:
            errors.append(f"DLR[{system}]: unknown write_kind {wk_str} on {target}")
            continue
        # Find matching edge
        matched: WriteEdge | None = None
        for e in imap.write_edges:
            if e.system == system and e.target == target and e.write_kind == wk:
                matched = e
                break
        if matched is None:
            errors.append(f"DLR[{system}]: no matching edge to fill for {target} [{wk_str}]")
            continue
        try:
            if ed.get("formula") is not None:
                expr = parse_expr(ed["formula"])
                vs = validate_expr(expr)
                if vs:
                    errors.append(f"DLR[{system}]: invalid formula for {target}: {vs[0]}")
                else:
                    matched.formula = expr
                    edge_count += 1
            # Accept procedural_note as an escape hatch for non-expressible writes
            if ed.get("procedural_note"):
                matched.procedural_note = ed["procedural_note"]
                if matched.formula is None:
                    edge_count += 1
        except Exception as exc:
            errors.append(f"DLR[{system}]: failed to parse formula for {target}: {exc}")

    return node_count, edge_count, errors


# ---------------------------------------------------------------------------
# Constants fills — stored on the imap as a side-channel dict
# ---------------------------------------------------------------------------


def _merge_constants(mech_constants: dict, system: str, result: dict) -> int:
    consts = result.get("constants") or []
    added = 0
    bucket = mech_constants.setdefault(system, {})
    for c in consts:
        name = c.get("name")
        if not name:
            continue
        bucket[name] = {
            "type": c.get("type", ""),
            "value": c.get("value"),
        }
        added += 1
    return added


def _literal_expr_for_value(type_name: str, value):
    normalized = (type_name or "").lower()
    if normalized in {"int", "integer"}:
        return parse_expr({"kind": "literal", "type": "int", "value": int(value)})
    if normalized in {"float", "number"}:
        return parse_expr({"kind": "literal", "type": "float", "value": float(value)})
    if normalized in {"bool", "boolean"}:
        return parse_expr({"kind": "literal", "type": "bool", "value": bool(value)})
    if normalized in {"string", "str"}:
        return parse_expr({"kind": "literal", "type": "string", "value": str(value)})
    if normalized == "vector2":
        if isinstance(value, str):
            parsed = ast.literal_eval(value)
            value = parsed
        if isinstance(value, (list, tuple)) and len(value) == 2:
            pair = [float(value[0]), float(value[1])]
            return parse_expr({"kind": "literal", "type": "vector2", "value": pair})
    if normalized == "color":
        if isinstance(value, str):
            return parse_expr({"kind": "literal", "type": "color", "value": value})
    if normalized == "rect2":
        if isinstance(value, str):
            parsed = ast.literal_eval(value)
            value = parsed
        if isinstance(value, (list, tuple)) and len(value) == 4:
            rect = [float(value[0]), float(value[1]), float(value[2]), float(value[3])]
            return parse_expr({"kind": "literal", "type": "rect2", "value": rect})
    if normalized in {"list", "array"}:
        if isinstance(value, str):
            parsed = ast.literal_eval(value)
            value = parsed
        if isinstance(value, list):
            return parse_expr({"kind": "literal", "type": "list", "value": value})
    if normalized in {"dict", "object"}:
        if isinstance(value, str):
            parsed = ast.literal_eval(value)
            value = parsed
        if isinstance(value, dict):
            return parse_expr({"kind": "literal", "type": "dict", "value": value})
    return None


def _neutral_state_expr(type_name: str):
    normalized = (type_name or "").lower()
    if normalized in {"int", "integer"}:
        return _literal_expr_for_value(type_name, 0)
    if normalized in {"float", "number"}:
        return _literal_expr_for_value(type_name, 0.0)
    if normalized in {"bool", "boolean"}:
        return _literal_expr_for_value(type_name, False)
    if normalized in {"string", "str"}:
        return _literal_expr_for_value(type_name, "")
    if normalized == "vector2":
        return _literal_expr_for_value(type_name, [0.0, 0.0])
    if normalized == "rect2":
        return _literal_expr_for_value(type_name, [0.0, 0.0, 0.0, 0.0])
    if normalized in {"list", "array"}:
        return _literal_expr_for_value(type_name, [])
    if normalized in {"dict", "object"}:
        return _literal_expr_for_value(type_name, {})
    return None


def _fill_neutral_state_defaults(imap: ImpactMap) -> int:
    filled = 0
    for node in imap.unfilled_nodes():
        if node.category != Category.STATE:
            continue
        expr = _neutral_state_expr(node.type)
        if expr is None:
            continue
        node.initial_value = expr
        imap.audit.append(f"dlr neutral state default applied: {node.id}")
        filled += 1
    return filled


_KNOWN_EDGE_PROCEDURAL_NOTES: dict[tuple[str, str, WriteKind], str] = {
    (
        "combat_system",
        "fighter.hitstun_timer",
        WriteKind.FRAME_UPDATE,
    ): (
        "If a defender is struck by an unblocked hit, set hitstun_timer to the "
        "resolved hitbox hitstun value. Otherwise, if the timer is above zero, "
        "decrement it by 1 each frame toward 0 and clear counter-hit state when it expires."
    ),
    (
        "stun_system",
        "fighter.stun_timer",
        WriteKind.FRAME_UPDATE,
    ): (
        "When stun threshold is reached, initialize stun_timer from the fighter's "
        "stun_duration value. While the fighter remains stunned, decrement the timer "
        "by 1 each update toward 0 and clear the stun state when it reaches zero."
    ),
    (
        "stun_system",
        "fighter.is_stunned",
        WriteKind.FRAME_UPDATE,
    ): (
        "Set is_stunned to true when the fighter's stun_meter reaches stun_threshold "
        "or while stun_timer remains above zero. Clear is_stunned once stun_timer "
        "expires and the fighter is no longer in the dizzy state."
    ),
}


def _fill_known_procedural_edges(imap: ImpactMap) -> int:
    filled = 0
    for edge in imap.unfilled_write_edges():
        key = (edge.system, edge.target, edge.write_kind)
        note = _KNOWN_EDGE_PROCEDURAL_NOTES.get(key)
        if not note:
            continue
        edge.procedural_note = note
        imap.audit.append(f"dlr known edge contract applied: {edge.system}->{edge.target}")
        filled += 1
    return filled


def _sync_mechanic_constants_into_game_nodes(
    imap: ImpactMap,
    hlr: GameIdentity,
    mech_constants: dict,
) -> int:
    synced = 0
    specs_by_system = {m.system_name: m for m in hlr.mechanic_specs}
    for system_name, spec in specs_by_system.items():
        bucket = mech_constants.get(system_name)
        if not isinstance(bucket, dict):
            continue
        for const in spec.constants_for_dlr:
            raw = bucket.get(const.name)
            node = imap.nodes.get(f"game.{const.name}")
            if node is None or raw is None:
                continue
            raw_type = raw.get("type") if isinstance(raw, dict) else const.type
            raw_value = raw.get("value") if isinstance(raw, dict) else raw
            expr = _literal_expr_for_value(str(raw_type or const.type or node.type), raw_value)
            if expr is None:
                continue
            node.initial_value = expr
            imap.audit.append(
                f"dlr constant synced into game node: game.{const.name} <- {system_name}.{const.name}"
            )
            synced += 1
    return synced


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def _fill_orphans_for_owner(
    owner: str,
    orphans: list,
    hlr: GameIdentity,
    kb_text: str,
    caller: LLMCaller,
) -> dict:
    """Fill every orphan (unfilled, system-less) node for ONE entity owner."""
    hlr_ctx = {"game_name": hlr.game_name, "genre": hlr.genre, "global_rules": hlr.global_rules}
    prompt = (
        f"## Game context\n```json\n{json.dumps(hlr_ctx, indent=2)}\n```\n\n"
        f"## Owner: {owner}\n\n"
        f"## Unfilled properties — fill EVERY one with a typed initial_value or derivation\n"
        f"```json\n{json.dumps([n.model_dump() for n in orphans], indent=2, default=str)}\n```"
    )
    prompt += (
        "\n\n## Value guidance\n"
        + _contextual_value_guidance(f"orphan:{owner}", {"owner": owner, "nodes": [n.model_dump() for n in orphans]}, hlr, None)
    )
    if kb_text and kb_text != "{}":
        prompt += f"\n\n## KB game data\n```json\n{kb_text[:8000]}\n```"

    spec_json = f"orphan:{owner}"
    key = _cache_key(owner, prompt, spec_json)
    cached = _cache_get(key)
    trace = get_trace()
    caller_name = type(caller).__name__
    label = f"impact_dlr_orphan[{owner}]"
    if cached is not None:
        _log.info("%s — cache hit", label)
        if trace:
            cid = trace.llm_start("dlr", label, caller_name, len(prompt))
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return parse_json_response(cached)

    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("dlr", label, caller_name, len(prompt)) if trace else ""
        try:
            raw = await caller(ORPHAN_FILL_PROMPT, prompt, json_mode=True, label=label)
            parsed = parse_json_response(raw)
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


async def fill_dlr(
    imap: ImpactMap,
    hlr: GameIdentity,
    router: CallerRouter,
    kb_game_data_text: str = "{}",
) -> tuple[dict, dict]:
    """Drill DLR values into the impact map, per system, in parallel.

    Returns (per_system_summary, mechanic_constants_bucket).
    """
    caller = router.get("dlr_interactions")
    specs_by_system = {m.system_name: m for m in hlr.mechanic_specs}

    async def _do(system: str):
        slice_ctx = imap.slice_for_system(system)
        result = await _call_dlr_system(
            system=system,
            slice_ctx=slice_ctx,
            hlr=hlr,
            mechanic_spec=specs_by_system.get(system),
            kb_text=kb_game_data_text,
            caller=caller,
        )
        return system, result

    _log.info("Impact DLR: filling %d systems in parallel", len(imap.systems))
    results = await asyncio.gather(*[_do(s) for s in imap.systems])

    summary: dict = {}
    mech_constants: dict = {}
    for system, result in results:
        nc, ec, errs = _merge_fills(imap, system, result)
        consts_added = _merge_constants(mech_constants, system, result)
        summary[system] = {
            "nodes_filled": nc,
            "edges_filled": ec,
            "constants_filled": consts_added,
            "errors": errs,
        }
        if errs:
            _log.warning("DLR[%s]: %d fill errors", system, len(errs))

    synced_constants = _sync_mechanic_constants_into_game_nodes(imap, hlr, mech_constants)
    if synced_constants:
        _log.info("Impact DLR: synced %d mechanic constants into matching game nodes", synced_constants)

    # -------- Orphan pass ---------------------------------------------------
    # Any node still unfilled after the per-system pass is an orphan — a
    # template config/state property that no system explicitly writes. We fill
    # these by entity owner in a second parallel wave.
    orphans = imap.unfilled_nodes()
    if orphans:
        by_owner: dict[str, list] = {}
        for n in orphans:
            by_owner.setdefault(n.owner, []).append(n)
        _log.info("Impact DLR orphan pass: %d orphans across %d owners",
                  len(orphans), len(by_owner))

        async def _do_owner(owner: str, nodes: list):
            result = await _fill_orphans_for_owner(owner, nodes, hlr, kb_game_data_text, caller)
            return owner, result

        orphan_results = await asyncio.gather(
            *[_do_owner(o, ns) for o, ns in by_owner.items()]
        )
        for owner, result in orphan_results:
            nc, _ec, errs = _merge_fills(imap, f"orphan:{owner}", result)
            summary.setdefault(f"orphan:{owner}", {"nodes_filled": 0, "edges_filled": 0, "constants_filled": 0, "errors": []})
            summary[f"orphan:{owner}"]["nodes_filled"] = nc
            summary[f"orphan:{owner}"]["errors"] = errs
            if errs:
                _log.warning("DLR orphan[%s]: %d fill errors", owner, len(errs))

    defaulted_states = _fill_neutral_state_defaults(imap)
    if defaulted_states:
        _log.info("Impact DLR: applied %d neutral state defaults", defaulted_states)

    filled_known_edges = _fill_known_procedural_edges(imap)
    if filled_known_edges:
        _log.info("Impact DLR: applied %d known procedural edge contracts", filled_known_edges)

    return summary, mech_constants


# ---------------------------------------------------------------------------
# Strict DLR validator
# ---------------------------------------------------------------------------


def validate_impact_dlr(imap: ImpactMap) -> list[str]:
    """Strict completeness: every node and every frame-update write must be typed-filled."""
    errors: list[str] = []
    for n in imap.unfilled_nodes():
        errors.append(
            f"node {n.id} [{n.category.value}]: missing "
            f"{'derivation' if n.category == Category.DERIVED else 'initial_value'}"
        )
    for e in imap.unfilled_write_edges():
        errors.append(
            f"write edge {e.system}->{e.target} [frame_update]: missing formula"
        )
    return errors
