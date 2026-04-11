"""Generate GDScript for custom (non-template) systems from an ImpactMap.

Reads impact_map_final.json and emits {system_name}.gd for every system whose
write edges are declared by the mechanic_spec seed. Typed formulas map 1:1 to
GDScript; procedural_notes become comment blocks + hand-filled bodies using a
small library of well-known idioms (buffer ops, hitbox activation, etc).

Each generated system has a uniform interface:

    extends Node
    var fighters: Array = []
    func setup(fighter_list: Array): fighters = fighter_list
    func process(delta: float):  ...

The main scene or fighting scene instantiates it and calls process(delta) in
_physics_process.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rayxi.spec.expr import (
    BinOpExpr,
    CondExpr,
    Expr,
    FnCallExpr,
    LiteralExpr,
    RefExpr,
)
from rayxi.spec.impact_map import ImpactMap, WriteEdge, WriteKind

_log = logging.getLogger("rayxi.build.mechanic_gen")


# ---------------------------------------------------------------------------
# Expr → GDScript
# ---------------------------------------------------------------------------


_BINOP_SYMBOLS = {
    "add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%",
    "lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "==", "ne": "!=",
    "and": "and", "or": "or",
}


def expr_to_gdscript(expr: Expr, fighter_var: str = "fighter") -> str:
    """Convert a typed Expr to a GDScript expression string.

    fighter_var: the GDScript variable name for the 'fighter' owner in the
    current scope. For a per-fighter loop it's usually 'fighter'.
    """
    if isinstance(expr, LiteralExpr):
        return _literal_to_gd(expr)
    if isinstance(expr, RefExpr):
        return _ref_to_gd(expr.path, fighter_var)
    if isinstance(expr, BinOpExpr):
        sym = _BINOP_SYMBOLS.get(expr.op, expr.op)
        left = expr_to_gdscript(expr.left, fighter_var)
        right = expr_to_gdscript(expr.right, fighter_var)
        return f"({left} {sym} {right})"
    if isinstance(expr, FnCallExpr):
        args = ", ".join(expr_to_gdscript(a, fighter_var) for a in expr.args)
        # GDScript built-ins
        fn = {"not": "!"}.get(expr.fn, expr.fn)
        if fn == "!":
            return f"(!{args})"
        return f"{fn}({args})"
    if isinstance(expr, CondExpr):
        c = expr_to_gdscript(expr.condition, fighter_var)
        t = expr_to_gdscript(expr.then_val, fighter_var)
        e = expr_to_gdscript(expr.else_val, fighter_var)
        return f"({t} if {c} else {e})"
    return "null"


def _literal_to_gd(lit: LiteralExpr) -> str:
    t = lit.type
    v = lit.value
    if t == "bool":
        return "true" if v else "false"
    if t == "string":
        return f'"{v}"'
    if t == "int":
        return str(int(v))
    if t == "float":
        return f"{float(v)}"
    if t == "vector2":
        if isinstance(v, list) and len(v) >= 2:
            return f"Vector2({v[0]}, {v[1]})"
        return "Vector2.ZERO"
    if t == "color":
        if isinstance(v, str) and v.startswith("#"):
            return f'Color("{v}")'
        return "Color.WHITE"
    if t == "rect2":
        if isinstance(v, list) and len(v) >= 4:
            return f"Rect2({v[0]}, {v[1]}, {v[2]}, {v[3]})"
        return "Rect2()"
    if t == "list":
        return "[]"
    if t == "dict":
        return "{}"
    return str(v)


def _ref_to_gd(path: str, fighter_var: str) -> str:
    """Translate a property reference path to a GDScript accessor."""
    if "." not in path:
        return path
    head, tail = path.split(".", 1)
    if head == "fighter":
        return f"{fighter_var}.{tail}"
    if head == "event":
        # event refs are function parameters — pass through by name
        return tail
    if head == "const":
        # Constants live as @export vars on this system node
        return f"self.{tail}"
    if head == "game":
        return f"game.{tail}"
    if head == "projectile":
        return f"projectile.{tail}"
    if head.startswith("hud"):
        # hud.widget_name.prop — leave as prose comment; HUD sides read fighter state directly
        return f'"{path}"'
    return path


# ---------------------------------------------------------------------------
# System generator
# ---------------------------------------------------------------------------


def _gd_constants_block(constants: dict[str, Any]) -> str:
    """Emit @export var declarations for each constant with its filled value."""
    if not constants:
        return ""
    lines = ["# =========================================", "# Constants (filled by DLR)",
             "# ========================================="]
    for name, info in constants.items():
        raw_value = info.get("value") if isinstance(info, dict) else info
        ctype = info.get("type", "") if isinstance(info, dict) else ""
        gd_type, gd_value = _coerce_constant(ctype, raw_value)
        if gd_type:
            lines.append(f"@export var {name}: {gd_type} = {gd_value}")
        else:
            lines.append(f"@export var {name} = {gd_value}")
    return "\n".join(lines)


def _flatten_constants_for_system(raw: dict, system_name: str) -> dict[str, dict]:
    """Accept either of the two known constant-file shapes and normalize to
    {name: {type, value}}.

    Shape A (flat per system):
        {"rage_meter_system": {"max_rage_stacks": {"type": "int", "value": 3}, ...}}

    Shape B (nested, legacy):
        {"rage_meter_system": {"constants": [{"name": "max_rage_stacks", "type": "int", "value": 3}, ...]}}
    """
    bucket = raw.get(system_name) or {}
    if isinstance(bucket, dict) and "constants" in bucket and isinstance(bucket["constants"], list):
        return {c["name"]: {"type": c.get("type", ""), "value": c.get("value")}
                for c in bucket["constants"] if c.get("name")}
    if isinstance(bucket, dict):
        return bucket
    return {}


def _coerce_constant(ctype: str, raw_value: Any) -> tuple[str, str]:
    """Guess a GDScript type and literal for a mechanic_spec constant value."""
    if ctype in ("int", "integer"):
        try:
            return "int", str(int(raw_value))
        except (ValueError, TypeError):
            return "int", "0"
    if ctype in ("float", "number"):
        try:
            return "float", str(float(raw_value))
        except (ValueError, TypeError):
            return "float", "0.0"
    if ctype == "hex_color" or (isinstance(raw_value, str) and str(raw_value).startswith("#")):
        return "Color", f'Color("{raw_value}")'
    if ctype == "bool":
        return "bool", "true" if raw_value else "false"
    if ctype == "string":
        return "String", f'"{raw_value}"'
    # Fallback: infer from value
    if isinstance(raw_value, bool):
        return "bool", "true" if raw_value else "false"
    if isinstance(raw_value, int):
        return "int", str(raw_value)
    if isinstance(raw_value, float):
        return "float", str(raw_value)
    if isinstance(raw_value, str):
        return "String", f'"{raw_value}"'
    return "", str(raw_value)


def _route_by_trigger(edge: WriteEdge) -> str:
    """Decide which handler method a write edge belongs in, by trigger prose + formula refs.

    Returns one of: 'process' | 'damage_event' | 'special_event' | 'round_start' | 'config_init'
    """
    # config_init writes go into on_config_init
    if edge.write_kind == WriteKind.CONFIG_INIT:
        return "config_init"
    # lifecycle writes go into on_round_start (mechanic_spec only emits one lifecycle kind today)
    if edge.write_kind == WriteKind.LIFECYCLE:
        return "round_start"

    # Frame-update writes — check trigger prose and formula refs to route
    trig = (edge.trigger or "").lower()
    formula_text = ""
    if edge.formula is not None:
        try:
            formula_text = expr_to_gdscript(edge.formula, "fighter")
        except Exception:
            formula_text = ""

    has_event_ref = "event." in str(edge.formula) if edge.formula else False

    if "damage" in trig or "applies damage" in trig or "takes damage" in trig or has_event_ref and "damage" in formula_text:
        return "damage_event"
    if "special" in trig and ("begin" in trig or "execute" in trig or "start" in trig):
        return "special_event"
    if "round" in trig and ("start" in trig or "pre_round" in trig or "reset" in trig):
        return "round_start"
    return "process"


def _partition_writes(writes: list[WriteEdge]) -> dict[str, list[WriteEdge]]:
    """Partition writes by handler method."""
    buckets: dict[str, list[WriteEdge]] = {
        "process": [],
        "damage_event": [],
        "special_event": [],
        "round_start": [],
        "config_init": [],
    }
    for e in writes:
        buckets[_route_by_trigger(e)].append(e)
    return buckets


def _emit_write_stmt(edge: WriteEdge, fighter_var: str = "fighter") -> list[str]:
    """Emit a GDScript statement (possibly multi-line) for a single write edge."""
    target_gd = _ref_to_gd(edge.target, fighter_var)
    lines: list[str] = []
    if edge.trigger:
        lines.append(f"# trigger: {edge.trigger[:100]}")
    guard_open = ""
    guard_close = ""
    if edge.condition is not None:
        guard = expr_to_gdscript(edge.condition, fighter_var)
        guard_open = f"if {guard}:"
        guard_close = ""
    if edge.formula is not None:
        formula_gd = expr_to_gdscript(edge.formula, fighter_var)
        stmt = f"{target_gd} = {formula_gd}"
        if guard_open:
            lines.append(guard_open)
            lines.append(f"\t{stmt}")
        else:
            lines.append(stmt)
    elif edge.procedural_note:
        lines.append(f"# PROCEDURAL: {edge.procedural_note}")
        if guard_open:
            lines.append(guard_open)
            lines.append(f"\tpass  # TODO: implement procedural write to {edge.target}")
        else:
            lines.append(f"# TODO: implement procedural write to {edge.target}")
    else:
        lines.append(f"# WARNING: no formula or procedural note for {edge.target}")
    return lines


def generate_system_gdscript(
    system_name: str,
    imap: ImpactMap,
    constants: dict[str, Any] | None = None,
) -> str:
    """Produce a full {system_name}.gd file from the impact map.

    Output layout:
      - Header + docstring
      - @export constants (from DLR mechanic_constants)
      - process(delta)                — frame-poll writes
      - process_damage_event(...)     — writes triggered by combat damage
      - process_special_event(...)    — writes triggered by special move execution
      - on_round_start()              — writes fired at round transitions
      - on_config_init()              — one-time writes copying system constants onto fighters
    """
    slice_data = imap.slice_for_system(system_name)
    my_writes = [WriteEdge(**w) for w in slice_data["own_writes"]]
    my_reads = slice_data["own_reads"]
    buckets = _partition_writes(my_writes)

    parts: list[str] = []
    parts.append("extends Node")
    parts.append(f"## {system_name} — generated from impact map by mechanic_gen")
    parts.append(f"## Reads: {sorted({r['source'] for r in my_reads})}")
    parts.append("")
    parts.append("var entity_pools: Dictionary = {}")
    parts.append("var config: Dictionary = {}")
    parts.append("var fighters: Array = []")
    parts.append("")

    # Constants
    const_block = _gd_constants_block(constants or {})
    if const_block:
        parts.append(const_block)
        parts.append("")

    # Setup — matches the uniform scene_gen contract: setup(pools, cfg={}).
    # We alias fighters = entity_pools["fighters"] so the existing body code
    # (which iterates `fighters`) keeps working.
    parts.append("func setup(pools: Dictionary, cfg: Dictionary = {}) -> void:")
    parts.append("\tentity_pools = pools")
    parts.append("\tconfig = cfg")
    parts.append('\tfighters = pools.get("fighters", [])')
    parts.append("\ton_config_init()  # copy constants onto each fighter")
    parts.append("")

    # --- process(_delta) — frame-poll writes ---
    parts.append("func process(_delta: float) -> void:")
    parts.append("\tfor fighter in fighters:")
    frame_writes = buckets["process"]
    if not frame_writes:
        parts.append("\t\tpass")
    else:
        for edge in frame_writes:
            for line in _emit_write_stmt(edge, fighter_var="fighter"):
                parts.append(f"\t\t{line}")
    parts.append("")

    # --- process_damage_event(fighter, damage_taken) ---
    if buckets["damage_event"]:
        parts.append("func process_damage_event(fighter, damage_taken: float) -> void:")
        parts.append("\t## Called by combat_system when a fighter takes damage.")
        for edge in buckets["damage_event"]:
            for line in _emit_write_stmt(edge, fighter_var="fighter"):
                parts.append(f"\t{line}")
        parts.append("")

    # --- process_special_event(fighter, move_name) ---
    if buckets["special_event"]:
        parts.append("func process_special_event(fighter, move_name: String) -> void:")
        parts.append("\t## Called by special_move_system when a fighter executes a special move.")
        for edge in buckets["special_event"]:
            for line in _emit_write_stmt(edge, fighter_var="fighter"):
                parts.append(f"\t{line}")
        parts.append("")

    # --- on_round_start() ---
    parts.append("func on_round_start() -> void:")
    parts.append("\t## Called by round_system when a new round begins.")
    parts.append("\tfor fighter in fighters:")
    if not buckets["round_start"]:
        parts.append("\t\tpass")
    else:
        for edge in buckets["round_start"]:
            for line in _emit_write_stmt(edge, fighter_var="fighter"):
                parts.append(f"\t\t{line}")
    parts.append("")

    # --- on_config_init() ---
    if buckets["config_init"]:
        parts.append("func on_config_init() -> void:")
        parts.append("\t## Copy this system's constants onto each fighter at setup time.")
        parts.append("\tfor fighter in fighters:")
        for edge in buckets["config_init"]:
            for line in _emit_write_stmt(edge, fighter_var="fighter"):
                parts.append(f"\t\t{line}")
        parts.append("")
    else:
        parts.append("func on_config_init() -> void:")
        parts.append("\tpass")
        parts.append("")

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def generate_custom_systems(
    impact_map_path: Path,
    mechanic_constants_path: Path | None,
    output_scripts_dir: Path,
) -> list[Path]:
    """Produce {system_name}.gd for every mechanic-spec system in the impact map.

    Returns the list of written file paths.
    """
    import json
    imap = ImpactMap.model_validate_json(impact_map_path.read_text())

    # Only generate for systems that have mechanic_spec seed writes
    # (template systems are handled by the existing codegen path)
    mechanic_systems: set[str] = set()
    for edge in imap.write_edges:
        if edge.declared_by.startswith("hlr_seed:") and ":hud" not in edge.declared_by:
            sys_name = edge.declared_by.split(":", 1)[1]
            mechanic_systems.add(sys_name)

    constants_by_system: dict[str, dict] = {}
    if mechanic_constants_path and mechanic_constants_path.exists():
        raw = json.loads(mechanic_constants_path.read_text())
        for sys_name in raw.keys():
            constants_by_system[sys_name] = _flatten_constants_for_system(raw, sys_name)

    output_scripts_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for sys_name in sorted(mechanic_systems):
        gd = generate_system_gdscript(
            system_name=sys_name,
            imap=imap,
            constants=constants_by_system.get(sys_name, {}),
        )
        out = output_scripts_dir / f"{sys_name}.gd"
        out.write_text(gd, encoding="utf-8")
        written.append(out)
        _log.info("mechanic_gen: wrote %s (%d bytes)", out, len(gd))
    return written
