"""Deterministic typed-expression walker for system codegen."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rayxi.spec.expr import BinOpExpr, CondExpr, Expr, FnCallExpr, LiteralExpr, RefExpr
from rayxi.spec.impact_map import ImpactMap, WriteEdge, WriteKind
from rayxi.build.template_codegen import resolve_constant, resolve_property_name

_log = logging.getLogger("rayxi.build.mechanic_gen")

_BINOP_SYMBOLS = {
    "add": "+",
    "sub": "-",
    "mul": "*",
    "div": "/",
    "mod": "%",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "eq": "==",
    "ne": "!=",
    "and": "and",
    "or": "or",
}
_AMBIENT_REF_MAP = {
    "acceleration_axis": 'Input.get_action_strength("accelerate")',
    "accel_pressed": 'Input.is_action_pressed("accelerate")',
    "brake_axis": 'Input.get_action_strength("brake")',
    "brake_pressed": 'Input.is_action_pressed("brake")',
    "steer_axis": 'Input.get_axis("steer_left", "steer_right")',
    "drift_button": 'Input.is_action_pressed("drift")',
    "drift_pressed": 'Input.is_action_pressed("drift")',
    "item_button": 'Input.is_action_pressed("item")',
    "item_pressed": 'Input.is_action_pressed("item")',
}


def _literal_to_gd(lit: LiteralExpr) -> str:
    if lit.type == "bool":
        return "true" if lit.value else "false"
    if lit.type == "string":
        return f'"{lit.value}"'
    if lit.type == "int":
        return str(int(lit.value))
    if lit.type == "float":
        return str(float(lit.value))
    if lit.type == "vector2":
        value = lit.value
        if isinstance(value, list) and len(value) >= 2:
            return f"Vector2({value[0]}, {value[1]})"
        return "Vector2.ZERO"
    if lit.type == "color":
        return f'Color("{lit.value}")' if isinstance(lit.value, str) else "Color.WHITE"
    if lit.type == "rect2":
        value = lit.value
        if isinstance(value, list) and len(value) >= 4:
            return f"Rect2({value[0]}, {value[1]}, {value[2]}, {value[3]})"
        return "Rect2()"
    if lit.type == "list":
        return "[]"
    if lit.type == "dict":
        return "{}"
    return str(lit.value)


def _coerce_constant(ctype: str, raw_value: Any) -> tuple[str, str]:
    if ctype in ("int", "integer"):
        try:
            return "int", str(int(raw_value))
        except (TypeError, ValueError):
            return "int", "0"
    if ctype in ("float", "number"):
        try:
            return "float", str(float(raw_value))
        except (TypeError, ValueError):
            return "float", "0.0"
    if ctype == "bool":
        return "bool", "true" if raw_value else "false"
    if ctype == "string":
        return "String", f'"{raw_value}"'
    if isinstance(raw_value, bool):
        return "bool", "true" if raw_value else "false"
    if isinstance(raw_value, int):
        return "int", str(raw_value)
    if isinstance(raw_value, float):
        return "float", str(raw_value)
    if isinstance(raw_value, str):
        return "String", f'"{raw_value}"'
    return "", str(raw_value)


def _gd_constants_block(constants: dict[str, Any]) -> str:
    if not constants:
        return ""
    lines = [
        "# =========================================",
        "# Constants (filled by DLR)",
        "# =========================================",
    ]
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
    bucket = raw.get(system_name) or {}
    if isinstance(bucket, dict) and "constants" in bucket and isinstance(bucket["constants"], list):
        return {
            c["name"]: {"type": c.get("type", ""), "value": c.get("value")}
            for c in bucket["constants"]
            if c.get("name")
        }
    if isinstance(bucket, dict):
        return bucket
    return {}


def _constant_scalar(constants: dict[str, Any], candidates: tuple[str, ...], default: Any) -> Any:
    value = resolve_constant(constants, candidates, default)
    if isinstance(value, dict):
        return value.get("value", default)
    return value


def _resolve_game_ref(name: str, constants: dict[str, Any]) -> str:
    if name == "delta_time":
        return "_delta"
    if name in constants:
        return f"self.{name}"
    if f"{name}_seconds" in constants:
        return f"self.{name}_seconds"
    if name.endswith("_duration") and f"{name}_seconds" in constants:
        return f"self.{name}_seconds"
    return f'get_parent().{name}'


def _ref_to_gd(path: str, owner_vars: dict[str, str], constants: dict[str, Any]) -> str:
    if "." not in path:
        if path == "ai_control_value":
            return owner_vars.get("kart", owner_vars.get("actor", "actor")) + ".is_ai_controlled"
        return _AMBIENT_REF_MAP.get(path, path)
    head, tail = path.split(".", 1)
    if head in owner_vars:
        return f"{owner_vars[head]}.{tail}"
    if head == "event":
        if tail == "ai_control_value":
            return owner_vars.get("kart", owner_vars.get("actor", "actor")) + ".is_ai_controlled"
        return _AMBIENT_REF_MAP.get(tail, tail)
    if head == "const":
        return f"self.{tail}"
    if head == "game":
        return _resolve_game_ref(tail, constants)
    if head.startswith("hud"):
        return f'"{path}"'
    return path


def expr_to_gdscript(
    expr: Expr,
    owner_vars: dict[str, str] | None = None,
    constants: dict[str, Any] | None = None,
) -> str:
    owner_vars = owner_vars or {}
    constants = constants or {}
    if isinstance(expr, LiteralExpr):
        return _literal_to_gd(expr)
    if isinstance(expr, RefExpr):
        return _ref_to_gd(expr.path, owner_vars, constants)
    if isinstance(expr, BinOpExpr):
        left = expr_to_gdscript(expr.left, owner_vars, constants)
        right = expr_to_gdscript(expr.right, owner_vars, constants)
        return f"({left} {_BINOP_SYMBOLS.get(expr.op, expr.op)} {right})"
    if isinstance(expr, FnCallExpr):
        args = ", ".join(expr_to_gdscript(arg, owner_vars, constants) for arg in expr.args)
        if expr.fn == "not":
            return f"(not {args})"
        return f"{expr.fn}({args})"
    if isinstance(expr, CondExpr):
        condition = expr_to_gdscript(expr.condition, owner_vars, constants)
        then_val = expr_to_gdscript(expr.then_val, owner_vars, constants)
        else_val = expr_to_gdscript(expr.else_val, owner_vars, constants)
        return f"({then_val} if {condition} else {else_val})"
    return "null"


def _route_by_trigger(edge: WriteEdge) -> str:
    if edge.write_kind == WriteKind.CONFIG_INIT:
        return "config_init"
    if edge.write_kind == WriteKind.LIFECYCLE:
        return "round_start"

    trigger = (edge.trigger or "").lower()
    formula_text = ""
    if edge.formula is not None:
        try:
            formula_text = expr_to_gdscript(edge.formula, {"fighter": "fighter"})
        except Exception:
            formula_text = ""
    has_event_ref = "event." in str(edge.formula) if edge.formula else False
    if "damage" in trigger or ("damage" in formula_text and has_event_ref):
        return "damage_event"
    if "special" in trigger and any(token in trigger for token in ("begin", "execute", "start")):
        return "special_event"
    if "round" in trigger and any(token in trigger for token in ("start", "reset", "pre_round")):
        return "round_start"
    return "process"


def _partition_writes(writes: list[WriteEdge]) -> dict[str, list[WriteEdge]]:
    buckets = {
        "process": [],
        "damage_event": [],
        "special_event": [],
        "round_start": [],
        "config_init": [],
    }
    for edge in writes:
        buckets[_route_by_trigger(edge)].append(edge)
    return buckets


def _owner_counts(slice_data: dict) -> dict[str, int]:
    counts: dict[str, int] = {}

    def _add(ref: str) -> None:
        if not isinstance(ref, str) or "." not in ref:
            return
        owner = ref.split(".", 1)[0]
        counts[owner] = counts.get(owner, 0) + 1

    for edge in slice_data.get("own_reads", []):
        _add(edge.get("source", ""))
    for edge in slice_data.get("own_writes", []):
        _add(edge.get("target", ""))
    return counts


def _pool_name_for_owner(owner: str) -> str:
    return owner if owner.endswith("s") else owner + "s"


_ACTOR_ROLE_TOKENS = ("vehicle", "kart", "car", "bike", "ship", "racer", "driver", "pilot", "player")
_PICKUP_ROLE_TOKENS = ("item_box", "item", "pickup", "collectible", "crate", "box")
_PROJECTILE_ROLE_TOKENS = ("projectile", "shell", "bullet", "missile", "shot", "orb")
_HAZARD_ROLE_TOKENS = ("hazard", "banana", "peel", "trap", "mine", "bomb", "obstacle", "game_object")


def _first_pool_from_groups(role_groups: dict[str, list[str]] | None, *group_names: str) -> str | None:
    groups = role_groups or {}
    for group_name in group_names:
        roles = [role_name for role_name in groups.get(group_name, []) if role_name]
        if not roles:
            continue
        roles.sort(
            key=lambda role_name: (
                1 if any(token in role_name.lower() for token in ("ai", "cpu", "opponent", "enemy")) else 0,
                0 if any(token in role_name.lower() for token in ("player", "hero", "avatar", "kart", "vehicle", "actor", "fighter", "character")) else 1,
                role_name.lower(),
            )
        )
        return _pool_name_for_owner(roles[0])
    return None


def _first_owner_pool_matching(owners: set[str], tokens: tuple[str, ...]) -> str | None:
    for owner in sorted(owners):
        lower_name = owner.lower()
        if any(token in lower_name for token in tokens):
            return _pool_name_for_owner(owner)
    return None


def _specialized_role_context(
    slice_data: dict,
    role_groups: dict[str, list[str]] | None = None,
) -> dict[str, str | None]:
    owners = set(_owner_counts(slice_data).keys())
    actor_pool = (
        _first_pool_from_groups(role_groups, "vehicle_actor_roles", "actor_roles")
        or _first_owner_pool_matching(owners, _ACTOR_ROLE_TOKENS)
        or _pool_name_for_owner(_primary_owner(slice_data))
    )
    stage_pool = (
        _first_pool_from_groups(role_groups, "stage_roles")
        or (_pool_name_for_owner("stage") if "stage" in owners else None)
    )
    camera_pool = (
        _first_pool_from_groups(role_groups, "camera_roles")
        or (_pool_name_for_owner("camera") if "camera" in owners else None)
    )
    pickup_pool = (
        _first_pool_from_groups(role_groups, "pickup_roles")
        or _first_owner_pool_matching(owners, _PICKUP_ROLE_TOKENS)
    )
    projectile_pool = (
        _first_pool_from_groups(role_groups, "projectile_roles")
        or _first_owner_pool_matching(owners, _PROJECTILE_ROLE_TOKENS)
    )
    hazard_pools = [
        _pool_name_for_owner(role_name)
        for role_name in (role_groups or {}).get("hazard_roles", [])
        if role_name
    ]
    if not hazard_pools:
        for owner in sorted(owners):
            pool_name = _pool_name_for_owner(owner)
            if pool_name in {actor_pool, pickup_pool, projectile_pool}:
                continue
            if any(token in owner.lower() for token in _HAZARD_ROLE_TOKENS):
                hazard_pools.append(pool_name)
    trap_pool = next((pool for pool in hazard_pools if pool != projectile_pool), None)
    generic_object_pool = next(
        (
            pool for pool in hazard_pools
            if pool not in {projectile_pool, trap_pool}
        ),
        None,
    )
    if generic_object_pool is None and "game_objects" in owners:
        generic_object_pool = "game_objects"
    return {
        "actor_pool": actor_pool,
        "stage_pool": stage_pool,
        "camera_pool": camera_pool,
        "pickup_pool": pickup_pool,
        "projectile_pool": projectile_pool,
        "trap_pool": trap_pool,
        "generic_object_pool": generic_object_pool,
    }


def _primary_owner(slice_data: dict) -> str:
    counts = _owner_counts(slice_data)
    pooled = [owner for owner in counts if owner not in {"game", "stage", "camera"}]
    if pooled:
        pooled.sort(key=lambda owner: (-counts.get(owner, 0), owner))
        return pooled[0]
    return "fighter"


def _singleton_owner_lines(slice_data: dict, primary_owner: str) -> tuple[list[str], dict[str, str]]:
    counts = _owner_counts(slice_data)
    lines: list[str] = []
    owner_vars: dict[str, str] = {primary_owner: primary_owner}
    for owner in sorted(counts):
        if owner in {primary_owner, "game"}:
            continue
        pool_name = _pool_name_for_owner(owner)
        owner_vars[owner] = owner
        lines.append(f'var {pool_name}: Array = entity_pools.get("{pool_name}", [])')
        lines.append(f"var {owner} = {pool_name}[0] if {pool_name}.size() > 0 else null")
    return lines, owner_vars


def _emit_write_stmt(edge: WriteEdge, owner_vars: dict[str, str], constants: dict[str, Any]) -> list[str]:
    target_gd = _ref_to_gd(edge.target, owner_vars, constants)
    lines: list[str] = []
    if edge.trigger:
        lines.append(f"# trigger: {edge.trigger[:100]}")
    if edge.condition is not None:
        condition = expr_to_gdscript(edge.condition, owner_vars, constants)
        lines.append(f"if {condition}:")
        indent = "\t"
    else:
        indent = ""
    if edge.formula is not None:
        formula = expr_to_gdscript(edge.formula, owner_vars, constants)
        lines.append(f"{indent}{target_gd} = {formula}")
    elif edge.procedural_note:
        lines.append(f"{indent}pass  # TODO: {edge.procedural_note[:90]}")
    else:
        lines.append(f"{indent}pass  # TODO: missing formula for {edge.target}")
    return lines


_SPECIALIZED_SYSTEMS = {
    "vehicle_movement_system",
    "physics_system",
    "locomotion_system",
    "player_input_system",
    "input_system",
    "countdown_system",
    "ai_navigation_system",
    "ai_system",
    "drift_boost_system",
    "item_box_system",
    "item_usage_system",
    "item_system",
    "collision_resolution_system",
    "collision_system",
    "race_progress_system",
    "position_ranking_system",
    "camera_tracking_system",
    "camera_system",
    "hud_system",
}


def has_specialized_generator(system_name: str) -> bool:
    return system_name in _SPECIALIZED_SYSTEMS


def _specialized_system_source(
    system_name: str,
    reads: list[str],
    constants: dict[str, Any],
    *,
    primary_pool_var: str,
    primary_pool_key: str | None = None,
    extra_fields: list[str] | None = None,
    helper_blocks: list[str] | None = None,
    process_body: list[str],
    round_start_body: list[str],
    config_body: list[str] | None = None,
) -> str:
    parts = [
        "extends Node",
        f"## {system_name} — generated from impact map by mechanic_gen",
        f"## Reads: {sorted(set(reads))}",
        "",
        "var entity_pools: Dictionary = {}",
        "var config: Dictionary = {}",
        "var sibling_systems: Dictionary = {}",
        f"var {primary_pool_var}: Array = []",
    ]
    if extra_fields:
        parts.extend(extra_fields)
    parts.append("")
    const_block = _gd_constants_block(constants)
    if const_block:
        parts.append(const_block)
        parts.append("")
    pool_key = primary_pool_key or primary_pool_var
    parts.extend(
        [
            "func setup(pools: Dictionary, cfg: Dictionary = {}) -> void:",
            "\tentity_pools = pools",
            "\tconfig = cfg",
            f'\t{primary_pool_var} = pools.get("{pool_key}", [])',
            "\ton_config_init()",
            "",
            "func set_siblings(systems: Dictionary) -> void:",
            "\tsibling_systems = systems",
            "",
        ]
    )
    for block in helper_blocks or []:
        parts.append(block.rstrip())
        parts.append("")
    parts.append("func process(_delta: float) -> void:")
    parts.extend(process_body)
    parts.append("")
    parts.append("func process_round_start() -> void:")
    parts.extend(round_start_body)
    parts.append("")
    parts.append("func on_config_init() -> void:")
    parts.extend(config_body or ["\tpass"])
    parts.append("")
    return "\n".join(parts) + "\n"


def _specialized_player_input_system(
    reads: list[str],
    constants: dict[str, Any],
    context: dict[str, str | None],
    slice_data: dict,
    system_name: str = "player_input_system",
) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    actor_owner = actor_pool[:-1] if actor_pool.endswith("s") else "kart"
    accel_prop = resolve_property_name(slice_data, actor_owner, ("acceleration_input", "accel_input"), default="acceleration_input")
    brake_prop = resolve_property_name(slice_data, actor_owner, ("brake_input",), default="brake_input")
    steer_prop = resolve_property_name(slice_data, actor_owner, ("steer_input", "turn_input"), default="steer_input")
    drift_prop = resolve_property_name(slice_data, actor_owner, ("drift_input", "input_drift"), default="drift_input")
    item_prop = resolve_property_name(slice_data, actor_owner, ("item_trigger_input", "item_input", "input_item", "use_item_input"), default="item_trigger_input")
    ai_prop = resolve_property_name(slice_data, actor_owner, ("is_ai_controlled", "ai_controlled"), default="is_ai_controlled")
    steer_deadzone = float(_constant_scalar(constants, ("steer_deadzone", "input_deadzone"), 0.15))
    steer_deadzone_field = "steer_deadzone" if "steer_deadzone" in constants else "input_deadzone" if "input_deadzone" in constants else "steer_deadzone"
    extra_fields = ["var _last_trace_state: Dictionary = {}"]
    if steer_deadzone_field == "steer_deadzone" and "steer_deadzone" not in constants:
        extra_fields.append(f"@export var steer_deadzone: float = {steer_deadzone}")
    return _specialized_system_source(
        system_name,
        reads,
        constants,
        primary_pool_var="actors",
        primary_pool_key=actor_pool,
        extra_fields=extra_fields,
        helper_blocks=[
            f"""
const ACCEL_PROP := "{accel_prop}"
const BRAKE_PROP := "{brake_prop}"
const STEER_PROP := "{steer_prop}"
const DRIFT_PROP := "{drift_prop}"
const ITEM_PROP := "{item_prop}"
const AI_PROP := "{ai_prop}"
""".strip(),
            """
func _entity_value(obj, prop_name: String, fallback):
\tif obj == null or prop_name == "":
\t\treturn fallback
\tvar raw = obj.get(prop_name)
\treturn fallback if raw == null else raw
""".strip(),
            """
func _set_input(actor, prop_name: String, value) -> void:
\tif actor == null or prop_name == "":
\t\treturn
\tactor.set(prop_name, value)
""".strip(),
            """
func _idle_inputs(actor) -> void:
\t_set_input(actor, ACCEL_PROP, false)
\t_set_input(actor, BRAKE_PROP, false)
\t_set_input(actor, STEER_PROP, 0.0)
\t_set_input(actor, DRIFT_PROP, false)
\t_set_input(actor, ITEM_PROP, false)
""".strip(),
            """
func _race_state() -> String:
\tvar managers: Array = entity_pools.get("race_managers", [])
\tif not managers.is_empty():
\t\tvar manager = managers[0]
\t\tif manager != null:
\t\t\tvar raw_state = manager.get("current_state")
\t\t\tif raw_state != null:
\t\t\t\treturn str(raw_state).to_lower()
\tif get_parent() != null:
\t\tvar countdown_flag = get_parent().get("countdown_active")
\t\tif countdown_flag != null and bool(countdown_flag):
\t\t\treturn "countdown"
\t\tvar fsm_state = get_parent().get("fsm_state")
\t\tif fsm_state != null:
\t\t\treturn str(fsm_state).to_lower()
\treturn ""
""".strip(),
            """
func _gameplay_locked() -> bool:
\tvar state: String = _race_state()
\treturn state in ["menu", "character_select", "track_select", "countdown", "s_menu", "s_character_select", "s_track_select", "s_countdown"]
""".strip(),
        ],
        process_body=[
            "\tif actors.is_empty():",
            "\t\treturn",
            "\tvar actor = actors[0]",
            "\tif actor == null:",
            "\t\treturn",
            "\tvar gameplay_locked: bool = _gameplay_locked()",
            "\tif gameplay_locked:",
            "\t\t_idle_inputs(actor)",
            '\t\tvar countdown_trace_key: String = str(actor.name)',
            '\t\tif _last_trace_state.get(countdown_trace_key, "") != "countdown":',
            '\t\t\t_last_trace_state[countdown_trace_key] = "countdown"',
            '\t\t\tprint("[trace] input.update actor=%s accel=false brake=false steer=0.00 drift=false item=false countdown=true state=%s" % [actor.name, _race_state()])',
            "\t\treturn",
            "\tif AI_PROP != \"\" and bool(_entity_value(actor, AI_PROP, false)):",
            "\t\t_idle_inputs(actor)",
            "\t\treturn",
            '\tvar accel_pressed: bool = Input.is_action_pressed("accelerate")',
            '\tvar brake_pressed: bool = Input.is_action_pressed("brake")',
            '\tvar drift_pressed: bool = Input.is_action_pressed("drift")',
            '\tvar item_pressed: bool = Input.is_action_pressed("item")',
            '\tvar raw_steer: float = Input.get_axis("steer_left", "steer_right")',
            f"\tvar steer_value: float = 0.0 if abs(raw_steer) < self.{steer_deadzone_field} else raw_steer",
            "\t_set_input(actor, ACCEL_PROP, 1.0 if accel_pressed else 0.0)",
            "\t_set_input(actor, BRAKE_PROP, brake_pressed)",
            "\t_set_input(actor, STEER_PROP, steer_value)",
            "\t_set_input(actor, DRIFT_PROP, drift_pressed)",
            "\t_set_input(actor, ITEM_PROP, item_pressed)",
            '\tvar trace_key: String = str(actor.name)',
            '\tvar trace_value: String = "%s|%s|%.2f|%s|%s" % [str(accel_pressed), str(brake_pressed), steer_value, str(drift_pressed), str(item_pressed)]',
            '\tif _last_trace_state.get(trace_key, "") != trace_value:',
            "\t\t_last_trace_state[trace_key] = trace_value",
            '\t\tprint("[trace] input.update actor=%s accel=%s brake=%s steer=%.2f drift=%s item=%s" % [actor.name, str(accel_pressed), str(brake_pressed), steer_value, str(drift_pressed), str(item_pressed)])',
        ],
        round_start_body=[
            "\t_last_trace_state.clear()",
            "\tfor actor in actors:",
            "\t\tif actor == null:",
            "\t\t\tcontinue",
            "\t\t_idle_inputs(actor)",
        ],
    )


def _specialized_drift_boost_system(
    reads: list[str],
    constants: dict[str, Any],
    context: dict[str, str | None],
    slice_data: dict,
) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    actor_owner = actor_pool[:-1] if actor_pool.endswith("s") else "kart"
    steer_prop = resolve_property_name(slice_data, actor_owner, ("steer_input", "steering_input"), default="steer_input")
    drift_input_prop = resolve_property_name(slice_data, actor_owner, ("drift_input", "input_drift"), default="drift_input")
    drifting_prop = resolve_property_name(slice_data, actor_owner, ("is_drifting",), default="is_drifting")
    drift_charge_prop = resolve_property_name(slice_data, actor_owner, ("drift_charge",), default="drift_charge")
    drift_tier_prop = resolve_property_name(slice_data, actor_owner, ("mini_turbo_level", "drift_tier"), default="mini_turbo_level")
    boost_timer_prop = resolve_property_name(slice_data, actor_owner, ("boost_timer", "item_boost_timer"), default="boost_timer")
    boost_mult_prop = resolve_property_name(slice_data, actor_owner, ("boost_multiplier", "item_boost_multiplier"), default="boost_multiplier")
    spin_timer_prop = resolve_property_name(slice_data, actor_owner, ("spin_out_timer",), default="spin_out_timer")
    velocity_prop = resolve_property_name(slice_data, actor_owner, ("velocity", "linear_velocity"), default="velocity")
    return _specialized_system_source(
        "drift_boost_system",
        reads,
        constants,
        primary_pool_var="karts",
        primary_pool_key=actor_pool,
        extra_fields=[
            "@export var drift_tier_one_threshold: float = 0.18",
            "@export var drift_tier_two_threshold: float = 0.45",
            "@export var drift_tier_three_threshold: float = 0.8",
            "@export var tier_one_boost_multiplier: float = 1.12",
            "@export var tier_two_boost_multiplier: float = 1.25",
            "@export var tier_three_boost_multiplier: float = 1.4",
        ],
        helper_blocks=[
            f"""
const STEER_PROP := "{steer_prop}"
const DRIFT_INPUT_PROP := "{drift_input_prop}"
const DRIFTING_PROP := "{drifting_prop}"
const DRIFT_CHARGE_PROP := "{drift_charge_prop}"
const DRIFT_TIER_PROP := "{drift_tier_prop}"
const BOOST_TIMER_PROP := "{boost_timer_prop}"
const BOOST_MULT_PROP := "{boost_mult_prop}"
const SPIN_TIMER_PROP := "{spin_timer_prop}"
const VELOCITY_PROP := "{velocity_prop}"
""".strip(),
            """
func _entity_value(obj, prop_name: String, fallback):
\tif obj == null or prop_name == "":
\t\treturn fallback
\tvar raw = obj.get(prop_name)
\treturn fallback if raw == null else raw
""".strip(),
            """
func _tier_for_charge(charge: float) -> int:
\tif charge >= self.drift_tier_three_threshold:
\t\treturn 3
\tif charge >= self.drift_tier_two_threshold:
\t\treturn 2
\tif charge >= self.drift_tier_one_threshold:
\t\treturn 1
\treturn 0
""".strip(),
            """
func _boost_duration_for_tier(tier: int) -> float:
\tif tier >= 3:
\t\treturn self.boost_duration_tier_2
\tif tier == 2:
\t\treturn self.boost_duration_tier_2
\tif tier == 1:
\t\treturn self.boost_duration_tier_1
\tif tier == 0:
\t\treturn self.boost_duration_tier_0
\treturn 0.0
""".strip(),
            """
func _boost_multiplier_for_tier(tier: int) -> float:
\tif tier >= 3:
\t\treturn self.tier_three_boost_multiplier
\tif tier == 2:
\t\treturn self.tier_two_boost_multiplier
\tif tier == 1:
\t\treturn self.tier_one_boost_multiplier
\treturn 1.0
""".strip(),
            """
func _speed_value(kart) -> float:
\tvar velocity = kart.get(VELOCITY_PROP)
\tif velocity is Vector2:
\t\treturn (velocity as Vector2).length()
\treturn 0.0
""".strip(),
        ],
        process_body=[
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            '\t\tvar spinning_out: bool = float(_entity_value(kart, SPIN_TIMER_PROP, 0.0)) > 0.0',
            '\t\tvar speed: float = _speed_value(kart)',
            '\t\tvar steer_input: float = float(_entity_value(kart, STEER_PROP, 0.0))',
            '\t\tvar drift_pressed: bool = bool(_entity_value(kart, DRIFT_INPUT_PROP, false))',
            '\t\tvar is_drifting: bool = bool(_entity_value(kart, DRIFTING_PROP, false))',
            '\t\tvar drift_charge: float = float(_entity_value(kart, DRIFT_CHARGE_PROP, 0.0))',
            '\t\tvar drift_tier: int = int(_entity_value(kart, DRIFT_TIER_PROP, 0))',
            '\t\tvar boost_timer: float = max(float(_entity_value(kart, BOOST_TIMER_PROP, 0.0)), 0.0)',
            "\t\tif spinning_out:",
            '\t\t\tif is_drifting or boost_timer > 0.0 or drift_charge > 0.0:',
            '\t\t\t\tprint("[trace] drift_boost.cancel kart=%s reason=spin_out" % [kart.name])',
            '\t\t\tkart.set(DRIFTING_PROP, false)',
            '\t\t\tkart.set(DRIFT_CHARGE_PROP, 0.0)',
            '\t\t\tkart.set(DRIFT_TIER_PROP, 0)',
            '\t\t\tkart.set(BOOST_TIMER_PROP, 0.0)',
            '\t\t\tkart.set(BOOST_MULT_PROP, 1.0)',
            "\t\t\tcontinue",
            "\t\tvar drift_ready: bool = drift_pressed and abs(steer_input) > 0.1 and speed >= self.drift_min_speed",
            "\t\tif drift_ready:",
            "\t\t\tif not is_drifting:",
            '\t\t\t\tkart.set(DRIFTING_PROP, true)',
            '\t\t\t\tkart.set("drift_direction", -1 if steer_input < 0.0 else 1)',
            '\t\t\t\tprint("[trace] drift_boost.drift_start kart=%s speed=%.2f steer=%.2f" % [kart.name, speed, steer_input])',
            "\t\t\tdrift_charge = clampf(drift_charge + (self.drift_charge_rate * _delta), 0.0, 1.0)",
            "\t\t\tvar new_tier: int = _tier_for_charge(drift_charge)",
            "\t\t\tif new_tier > drift_tier:",
            '\t\t\t\tprint("[trace] drift_boost.tier_up kart=%s tier=%d charge=%.2f" % [kart.name, new_tier, drift_charge])',
            '\t\t\tkart.set(DRIFT_CHARGE_PROP, drift_charge)',
            '\t\t\tkart.set(DRIFT_TIER_PROP, new_tier)',
            '\t\t\tkart.set(BOOST_TIMER_PROP, 0.0)',
            '\t\t\tkart.set(BOOST_MULT_PROP, 1.0)',
            "\t\telse:",
            "\t\t\tif is_drifting:",
            '\t\t\t\tkart.set(DRIFTING_PROP, false)',
            '\t\t\t\tkart.set(DRIFT_CHARGE_PROP, 0.0)',
            '\t\t\t\tkart.set(DRIFT_TIER_PROP, 0)',
            '\t\t\t\tif drift_tier > 0:',
            "\t\t\t\t\tvar boost_duration: float = _boost_duration_for_tier(drift_tier)",
            "\t\t\t\t\tvar boost_multiplier: float = _boost_multiplier_for_tier(drift_tier)",
            '\t\t\t\t\tkart.set(BOOST_TIMER_PROP, boost_duration)',
            '\t\t\t\t\tkart.set(BOOST_MULT_PROP, boost_multiplier)',
            '\t\t\t\t\tprint("[trace] drift_boost.boost_start kart=%s tier=%d duration=%.2f" % [kart.name, drift_tier, boost_duration])',
            "\t\t\telif boost_timer > 0.0:",
            "\t\t\t\tboost_timer = max(boost_timer - _delta, 0.0)",
            '\t\t\t\tkart.set(BOOST_TIMER_PROP, boost_timer)',
            "\t\t\t\tif boost_timer <= 0.0:",
            '\t\t\t\t\tkart.set(BOOST_MULT_PROP, 1.0)',
            '\t\t\t\t\tprint("[trace] drift_boost.boost_end kart=%s" % [kart.name])',
        ],
        round_start_body=[
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            '\t\tkart.set(DRIFTING_PROP, false)',
            '\t\tkart.set(DRIFT_CHARGE_PROP, 0.0)',
            '\t\tkart.set(DRIFT_TIER_PROP, 0)',
            '\t\tkart.set(BOOST_TIMER_PROP, 0.0)',
            '\t\tkart.set(BOOST_MULT_PROP, 1.0)',
        ],
    )


def _specialized_race_progress_system(reads: list[str], constants: dict[str, Any], context: dict[str, str | None]) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    stage_pool = str(context.get("stage_pool") or "stages")
    checkpoint_radius = float(_constant_scalar(constants, ("checkpoint_radius", "waypoint_tolerance"), 96.0))
    max_laps = int(_constant_scalar(constants, ("max_laps",), 3))
    countdown_start_value = int(_constant_scalar(constants, ("countdown_start_value",), 3))
    seconds_per_count = float(_constant_scalar(constants, ("seconds_per_count",), 1.0))
    extra_fields = [
        f"@export var checkpoint_radius: float = {checkpoint_radius}",
        "var _checkpoint_gate: Dictionary = {}",
        "var _countdown_started: bool = false",
        "var _countdown_value: int = 0",
        "var _countdown_timer: float = 0.0",
    ]
    if "max_laps" not in constants:
        extra_fields.append(f"@export var max_laps: int = {max_laps}")
    if "countdown_start_value" not in constants:
        extra_fields.append(f"@export var countdown_start_value: int = {countdown_start_value}")
    if "seconds_per_count" not in constants:
        extra_fields.append(f"@export var seconds_per_count: float = {seconds_per_count}")
    return _specialized_system_source(
        "race_progress_system",
        reads,
        constants,
        primary_pool_var="karts",
        primary_pool_key=actor_pool,
        extra_fields=extra_fields,
        helper_blocks=[
            f"""
func _checkpoint_points() -> Array[Vector2]:
\tvar points: Array[Vector2] = []
\tvar stages: Array = entity_pools.get("{stage_pool}", [])
\tif stages.is_empty():
\t\treturn points
\tvar stage: Variant = stages[0]
\tif stage == null:
\t\treturn points
\tvar raw: Variant = stage.get("checkpoint_positions")
\tif (not (raw is Array) or (raw as Array).is_empty()) and stage.has_meta("checkpoint_positions"):
\t\traw = stage.get_meta("checkpoint_positions", [])
\tif raw is Array:
\t\tfor entry in raw:
\t\t\tif entry is Vector2:
\t\t\t\tpoints.append(entry)
\t\t\telif entry is Array and entry.size() >= 2:
\t\t\t\tpoints.append(Vector2(float(entry[0]), float(entry[1])))
\treturn points
""".strip(),
            """
func _entity_value(obj, prop_name: String, fallback):
\tif obj == null or prop_name == "":
\t\treturn fallback
\tvar raw = obj.get(prop_name)
\tif raw == null and obj.has_meta(prop_name):
\t\traw = obj.get_meta(prop_name, fallback)
\treturn fallback if raw == null else raw
""".strip(),
            """
func _race_manager():
\tvar managers: Array = entity_pools.get("race_managers", [])
\treturn managers[0] if not managers.is_empty() else null
""".strip(),
        ],
        process_body=[
            "\tvar race_manager = _race_manager()",
            '\tvar race_state: String = str(_entity_value(race_manager, "current_state", "racing")).to_lower()',
            '\tif race_manager != null and not sibling_systems.has("countdown_system"):',
            '\t\tif race_state in ["menu", "character_select", "track_select", "s_menu", "s_character_select", "s_track_select"]:',
            '\t\t\trace_manager.current_state = "countdown"',
            '\t\t\trace_state = "countdown"',
            '\t\tif race_state in ["countdown", "s_countdown"]:',
            '\t\t\tif not _countdown_started:',
            '\t\t\t\t_countdown_started = true',
            '\t\t\t\t_countdown_value = max(self.countdown_start_value, 1)',
            '\t\t\t\t_countdown_timer = max(self.seconds_per_count, 0.01)',
            '\t\t\t\tprint("[trace] countdown.start value=%d" % _countdown_value)',
            '\t\t\telse:',
            '\t\t\t\t_countdown_timer = max(_countdown_timer - _delta, 0.0)',
            '\t\t\t\tif _countdown_timer <= 0.0:',
            '\t\t\t\t\t_countdown_value -= 1',
            '\t\t\t\t\tif _countdown_value > 0:',
            '\t\t\t\t\t\t_countdown_timer = max(self.seconds_per_count, 0.01)',
            '\t\t\t\t\t\tprint("[trace] countdown.tick value=%d" % _countdown_value)',
            '\t\t\t\t\telse:',
            '\t\t\t\t\t\trace_manager.current_state = "racing"',
            '\t\t\t\t\t\trace_state = "racing"',
            '\t\t\t\t\t\t_countdown_timer = 0.0',
            '\t\t\t\t\t\tprint("[trace] countdown.complete state=S_RACING")',
            '\t\t\treturn',
            '\tif race_manager != null and race_state in ["racing", "s_racing"]:',
            '\t\trace_manager.race_timer = float(_entity_value(race_manager, "race_timer", 0.0)) + _delta',
            "\tvar checkpoints: Array[Vector2] = _checkpoint_points()",
            "\tvar checkpoint_total: int = checkpoints.size()",
            "\tif checkpoint_total <= 0:",
            "\t\treturn",
            "\tvar active_checkpoint_count: int = checkpoint_total",
            "\tvar progress_entries: Array = []",
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            '\t\tvar pos: Variant = _entity_value(kart, "position", null)',
            "\t\tif not (pos is Vector2):",
            "\t\t\tcontinue",
            '\t\tvar kart_pos: Vector2 = pos',
            '\t\tvar current_lap: int = max(int(_entity_value(kart, "current_lap", 1)), 1)',
            '\t\tvar checkpoint_index: int = posmod(int(_entity_value(kart, "waypoint_index", 0)), active_checkpoint_count)',
            "\t\tvar checkpoint: Vector2 = checkpoints[checkpoint_index]",
            "\t\tvar gate_key: String = str(kart.name)",
            "\t\tvar gate_marker: String = str(_checkpoint_gate.get(gate_key, \"\"))",
            "\t\tvar distance_to_checkpoint: float = kart_pos.distance_to(checkpoint)",
            "\t\tif distance_to_checkpoint <= checkpoint_radius and gate_marker != str(checkpoint_index):",
            "\t\t\t_checkpoint_gate[gate_key] = str(checkpoint_index)",
            "\t\t\tvar next_checkpoint: int = (checkpoint_index + 1) % active_checkpoint_count",
            "\t\t\tkart.waypoint_index = next_checkpoint",
            '\t\t\tprint("[trace] race_progress.checkpoint kart=%s checkpoint=%d next=%d" % [kart.name, checkpoint_index, next_checkpoint])',
            "\t\t\tif next_checkpoint == 0:",
            "\t\t\t\tcurrent_lap += 1",
            "\t\t\t\tkart.current_lap = current_lap",
            '\t\t\t\tprint("[trace] race_progress.lap_up kart=%s lap=%d" % [kart.name, current_lap])',
            "\t\t\t\tif current_lap > self.max_laps:",
            '\t\t\t\t\tif not bool(_entity_value(kart, "race_finished", false)):',
            '\t\t\t\t\t\tprint("[trace] race_progress.finish kart=%s lap=%d" % [kart.name, current_lap])',
            "\t\t\t\t\tkart.race_finished = true",
            "\t\telif distance_to_checkpoint > checkpoint_radius * 1.4 and gate_marker == str(checkpoint_index):",
            "\t\t\t_checkpoint_gate.erase(gate_key)",
            '\t\tvar progress_score: int = max(current_lap - 1, 0) * active_checkpoint_count + int(_entity_value(kart, "waypoint_index", 0))',
            '\t\tif bool(_entity_value(kart, "race_finished", false)):',
            "\t\t\tprogress_score = self.max_laps * active_checkpoint_count + active_checkpoint_count",
            '\t\tprogress_entries.append({"kart": kart, "score": progress_score, "name": str(kart.name)})',
            "\tif progress_entries.is_empty():",
            "\t\treturn",
            '\tprogress_entries.sort_custom(func(a, b):\n\t\tif int(a["score"]) != int(b["score"]):\n\t\t\treturn int(a["score"]) > int(b["score"])\n\t\treturn str(a["name"]) < str(b["name"])\n\t)',
            "\tvar all_finished: bool = true",
            "\tfor idx in range(progress_entries.size()):",
            '\t\tvar entry: Dictionary = progress_entries[idx]',
            '\t\tvar ranked_kart = entry.get("kart")',
            "\t\tif ranked_kart == null:",
            "\t\t\tcontinue",
            "\t\tranked_kart.race_position = idx + 1",
            '\t\tif not bool(_entity_value(ranked_kart, "race_finished", false)):',
            "\t\t\tall_finished = false",
            "\tif get_parent() != null and get_parent().get(\"all_karts_finished\") != null:",
            "\t\tget_parent().all_karts_finished = all_finished",
            '\tif race_manager != null and all_finished:',
            '\t\trace_manager.current_state = "finished"',
        ],
        round_start_body=[
            "\t_checkpoint_gate.clear()",
            "\t_countdown_started = false",
            "\t_countdown_value = 0",
            "\t_countdown_timer = 0.0",
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            "\t\tkart.waypoint_index = 0",
            "\t\tkart.current_lap = 1",
            "\t\tkart.race_position = 1",
            "\t\tkart.race_finished = false",
            "\tvar race_manager = _race_manager()",
            "\tif race_manager != null:",
            '\t\trace_manager.current_state = "countdown"',
            "\t\trace_manager.race_timer = 0.0",
            "\tif get_parent() != null and get_parent().get(\"all_karts_finished\") != null:",
            "\t\tget_parent().all_karts_finished = false",
        ],
    )


def _specialized_position_ranking_system(reads: list[str], constants: dict[str, Any], context: dict[str, str | None]) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    return _specialized_system_source(
        "position_ranking_system",
        reads,
        constants,
        primary_pool_var="karts",
        primary_pool_key=actor_pool,
        process_body=[
            "\tvar entries: Array = []",
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            "\t\tentries.append({",
            '\t\t\t"kart": kart,',
            '\t\t\t"score": int(kart.get("race_progress_score") if kart.get("race_progress_score") != null else 0),',
            '\t\t\t"name": str(kart.name),',
            "\t\t})",
            "\tif entries.is_empty():",
            "\t\treturn",
            "\tentries.sort_custom(func(a, b):",
            '\t\tif int(a["score"]) != int(b["score"]):',
            '\t\t\treturn int(a["score"]) > int(b["score"])',
            '\t\treturn str(a["name"]) < str(b["name"])',
            "\t)",
            "\tfor i in range(entries.size()):",
            "\t\tvar entry: Dictionary = entries[i]",
            '\t\tvar kart: Variant = entry.get("kart")',
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            "\t\tvar new_rank: int = i + 1",
            '\t\tvar old_rank: int = int(kart.get("position_rank") if kart.get("position_rank") != null else 0)',
            "\t\tif old_rank != new_rank:",
            '\t\t\tprint("[trace] position_ranking.update kart=%s old_rank=%d new_rank=%d score=%d" % [kart.name, old_rank, new_rank, int(entry.get("score", 0))])',
            "\t\tkart.position_rank = new_rank",
        ],
        round_start_body=[
            "\tfor i in range(karts.size()):",
            "\t\tvar kart: Variant = karts[i]",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            "\t\tkart.position_rank = i + 1",
        ],
    )


def _specialized_vehicle_movement_system(
    reads: list[str],
    constants: dict[str, Any],
    context: dict[str, str | None],
    slice_data: dict,
    system_name: str = "vehicle_movement_system",
) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    actor_owner = actor_pool[:-1] if actor_pool.endswith("s") else "kart"
    accel_prop = resolve_property_name(slice_data, actor_owner, ("acceleration_input", "accel_input", "input_accelerate"), default="acceleration_input")
    brake_prop = resolve_property_name(slice_data, actor_owner, ("brake_input", "input_brake"), default="brake_input")
    steer_prop = resolve_property_name(slice_data, actor_owner, ("steer_input", "turn_input"), default="steer_input")
    speed_prop = resolve_property_name(slice_data, actor_owner, ("speed", "forward_speed"), default="speed")
    velocity_prop = resolve_property_name(slice_data, actor_owner, ("velocity", "linear_velocity"), default="velocity")
    angle_prop = resolve_property_name(slice_data, actor_owner, ("facing_angle", "angle", "heading_degrees"), default="facing_angle")
    offroad_prop = resolve_property_name(slice_data, actor_owner, ("is_offroad",), default="is_offroad")
    spinning_prop = resolve_property_name(slice_data, actor_owner, ("is_spinning_out",), default="is_spinning_out")
    spin_timer_prop = resolve_property_name(slice_data, actor_owner, ("spin_out_timer",), default="spin_out_timer")
    boost_timer_prop = resolve_property_name(slice_data, actor_owner, ("boost_timer", "item_boost_timer"), default="boost_timer")
    boost_mult_prop = resolve_property_name(slice_data, actor_owner, ("boost_multiplier", "item_boost_multiplier"), default="boost_multiplier")
    ai_speed_prop = resolve_property_name(slice_data, actor_owner, ("rubber_band_multiplier", "ai_rubber_band_mult", "ai_speed_multiplier"), default="rubber_band_multiplier")
    max_speed_base = float(_constant_scalar(constants, ("max_speed_base", "max_speed", "top_speed"), 100.0))
    acceleration_rate = float(_constant_scalar(constants, ("acceleration_rate", "acceleration"), 40.0))
    braking_default = float(_constant_scalar(constants, ("braking_deceleration", "brake_deceleration"), acceleration_rate))
    turn_rate_base = float(_constant_scalar(constants, ("turn_rate_base", "turn_rate", "handling"), 2.2))
    friction_default = float(_constant_scalar(constants, ("friction_coefficient", "rolling_friction"), 0.92))
    offroad_default = float(_constant_scalar(constants, ("offroad_speed_multiplier",), 0.6))
    min_steer_default = float(_constant_scalar(constants, ("minimum_steering_speed", "min_steering_speed"), 6.0))
    extra_fields = ["var _last_trace_state: Dictionary = {}"]
    if "braking_deceleration" not in constants:
        extra_fields.append(f"@export var braking_deceleration: float = {braking_default}")
    if "minimum_steering_speed" not in constants:
        extra_fields.append(f"@export var minimum_steering_speed: float = {min_steer_default}")
    if "max_speed_base" not in constants:
        extra_fields.append(f"@export var max_speed_base: float = {max_speed_base}")
    if "acceleration_rate" not in constants:
        extra_fields.append(f"@export var acceleration_rate: float = {acceleration_rate}")
    if "turn_rate_base" not in constants:
        extra_fields.append(f"@export var turn_rate_base: float = {turn_rate_base}")
    if "friction_coefficient" not in constants:
        extra_fields.append(f"@export var friction_coefficient: float = {friction_default}")
    if "offroad_speed_multiplier" not in constants:
        extra_fields.append(f"@export var offroad_speed_multiplier: float = {offroad_default}")
    return _specialized_system_source(
        system_name,
        reads,
        constants,
        primary_pool_var="karts",
        primary_pool_key=actor_pool,
        extra_fields=extra_fields,
        helper_blocks=[
            """
const ACCEL_PROP := "%s"
const BRAKE_PROP := "%s"
const STEER_PROP := "%s"
const SPEED_PROP := "%s"
const VELOCITY_PROP := "%s"
const ANGLE_PROP := "%s"
const OFFROAD_PROP := "%s"
const SPINNING_PROP := "%s"
const SPIN_TIMER_PROP := "%s"
const BOOST_TIMER_PROP := "%s"
const BOOST_MULT_PROP := "%s"
const AI_SPEED_PROP := "%s"
"""
            % (
                accel_prop,
                brake_prop,
                steer_prop,
                speed_prop,
                velocity_prop,
                angle_prop,
                offroad_prop,
                spinning_prop,
                spin_timer_prop,
                boost_timer_prop,
                boost_mult_prop,
                ai_speed_prop,
            ),
            """
func _entity_value(obj, prop_name: String, fallback):
\tif obj == null or prop_name == "":
\t\treturn fallback
\tvar raw = obj.get(prop_name)
\tif raw == null and obj.has_meta(prop_name):
\t\traw = obj.get_meta(prop_name, fallback)
\tif raw == null and prop_name == SPEED_PROP:
\t\tvar velocity = obj.get(VELOCITY_PROP)
\t\tif velocity is Vector2:
\t\t\treturn (velocity as Vector2).length()
\tif raw == null and prop_name == ANGLE_PROP and obj.get("rotation") != null:
\t\treturn float(obj.rotation)
\tif raw == null and prop_name == SPINNING_PROP:
\t\tvar spin_timer = obj.get(SPIN_TIMER_PROP)
\t\tif spin_timer != null:
\t\t\treturn float(spin_timer) > 0.0
\treturn fallback if raw == null else raw
""".strip(),
            """
func _set_prop(obj, prop_name: String, value) -> void:
\tif obj == null or prop_name == "":
\t\treturn
\tif prop_name == ANGLE_PROP and obj.get(prop_name) == null and obj.get("rotation") != null:
\t\tobj.rotation = float(value)
\t\treturn
\tif prop_name == SPINNING_PROP and obj.get(prop_name) == null:
\t\tif not bool(value) and SPIN_TIMER_PROP != "":
\t\t\tobj.set(SPIN_TIMER_PROP, 0.0)
\t\treturn
\tif prop_name == SPEED_PROP and obj.get(prop_name) == null:
\t\treturn
\tobj.set(prop_name, value)
""".strip(),
            """
func _angle_radians(raw_angle: float) -> float:
\treturn deg_to_rad(raw_angle) if absf(raw_angle) > TAU * 2.0 else raw_angle
""".strip(),
            """
func _heading(raw_angle: float) -> Vector2:
\tvar radians: float = _angle_radians(raw_angle)
\treturn Vector2(cos(radians), sin(radians))
""".strip(),
            """
func _race_state() -> String:
\tvar managers: Array = entity_pools.get("race_managers", [])
\tif not managers.is_empty():
\t\tvar manager = managers[0]
\t\tif manager != null:
\t\t\tvar raw_state = manager.get("current_state")
\t\t\tif raw_state != null:
\t\t\t\treturn str(raw_state).to_lower()
\tif get_parent() != null:
\t\tvar countdown_flag = get_parent().get("countdown_active")
\t\tif countdown_flag != null and bool(countdown_flag):
\t\t\treturn "countdown"
\t\tvar fsm_state = get_parent().get("fsm_state")
\t\tif fsm_state != null:
\t\t\treturn str(fsm_state).to_lower()
\treturn ""
""".strip(),
            """
func _gameplay_locked() -> bool:
\tvar state: String = _race_state()
\treturn state in ["menu", "character_select", "track_select", "countdown", "s_menu", "s_character_select", "s_track_select", "s_countdown"]
""".strip(),
            """
func _trace_state(kart, speed: float, max_speed: float, position: Vector2) -> void:
\tvar trace_key: String = str(kart.name)
\tvar trace_value: String = "%.2f|%.2f|%.2f|%.1f|%.1f|%s" % [
\t\tspeed,
\t\tmax_speed,
\t\tfloat(_entity_value(kart, ANGLE_PROP, 0.0)),
\t\tposition.x,
\t\tposition.y,
\t\tstr(bool(_entity_value(kart, SPINNING_PROP, false))),
\t]
\tif _last_trace_state.get(trace_key, "") == trace_value:
\t\treturn
\t_last_trace_state[trace_key] = trace_value
\tprint("[trace] physics.update kart=%s pos=%s speed=%.2f max=%.2f angle=%.2f spinning=%s" % [
\t\tkart.name,
\t\tstr(position),
\t\tspeed,
\t\tmax_speed,
\t\tfloat(_entity_value(kart, ANGLE_PROP, 0.0)),
\t\tstr(bool(_entity_value(kart, SPINNING_PROP, false))),
\t])
""".strip(),
        ],
        process_body=[
            '\tvar stages: Array = entity_pools.get("%s", [])' % str(context.get("stage_pool") or "stages"),
            "\tvar road_width: float = 1000.0",
            "\tvar track_center_x: float = 960.0",
            "\tif not stages.is_empty():",
            "\t\tvar stage = stages[0]",
            "\t\tif stage != null:",
            '\t\t\troad_width = float(_entity_value(stage, "road_width", road_width))',
            '\t\t\tif _entity_value(stage, "checkpoint_positions", []).size() > 0:',
            '\t\t\t\tvar checkpoints: Array = _entity_value(stage, "checkpoint_positions", [])',
            "\t\t\t\tvar min_x: float = 1e9",
            "\t\t\t\tvar max_x: float = -1e9",
            "\t\t\t\tfor checkpoint in checkpoints:",
            "\t\t\t\t\tif checkpoint is Vector2:",
            "\t\t\t\t\t\tmin_x = min(min_x, float(checkpoint.x))",
            "\t\t\t\t\t\tmax_x = max(max_x, float(checkpoint.x))",
            "\t\t\t\tif min_x < 1e8 and max_x > -1e8:",
            "\t\t\t\t\ttrack_center_x = (min_x + max_x) * 0.5",
            "\tvar gameplay_locked: bool = _gameplay_locked()",
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            '\t\tvar speed: float = max(float(_entity_value(kart, SPEED_PROP, 0.0)), 0.0)',
            '\t\tvar accel_input: float = clampf(float(_entity_value(kart, ACCEL_PROP, 0.0)), 0.0, 1.0)',
            '\t\tvar brake_input: float = clampf(float(_entity_value(kart, BRAKE_PROP, 0.0)), 0.0, 1.0)',
            '\t\tvar steer_input: float = clampf(float(_entity_value(kart, STEER_PROP, 0.0)), -1.0, 1.0)',
            '\t\tvar angle_value: float = float(_entity_value(kart, ANGLE_PROP, 0.0))',
            '\t\tvar ai_multiplier: float = max(float(_entity_value(kart, AI_SPEED_PROP, 1.0)), 1.0)',
            '\t\tvar boost_timer: float = max(float(_entity_value(kart, BOOST_TIMER_PROP, 0.0)), 0.0)',
            '\t\tvar boost_multiplier: float = max(float(_entity_value(kart, BOOST_MULT_PROP, 1.0)), 1.0)',
            '\t\tvar offroad_state: bool = bool(_entity_value(kart, OFFROAD_PROP, false))',
            '\t\tvar spinning_out: bool = bool(_entity_value(kart, SPINNING_PROP, false))',
            '\t\tvar spin_timer: float = max(float(_entity_value(kart, SPIN_TIMER_PROP, 0.0)), 0.0)',
            '\t\tvar step_scale: float = max(_delta * 60.0, 0.0)',
            '\t\tvar max_speed_cap: float = self.max_speed_base * ai_multiplier',
            "\t\tif offroad_state:",
            "\t\t\tmax_speed_cap *= self.offroad_speed_multiplier",
            "\t\tif boost_timer > 0.0:",
            "\t\t\tmax_speed_cap *= boost_multiplier",
            "\t\tif gameplay_locked:",
            "\t\t\tspeed = 0.0",
            "\t\telif spinning_out:",
            "\t\t\tspeed = max(speed - self.braking_deceleration * 0.6 * _delta, 0.0)",
            "\t\t\tspin_timer = max(spin_timer - _delta, 0.0)",
            "\t\t\tif spin_timer <= 0.0:",
            '\t\t\t\t_set_prop(kart, SPINNING_PROP, false)',
            "\t\telse:",
            "\t\t\tspeed += accel_input * self.acceleration_rate * _delta",
            "\t\t\tspeed = max(speed - brake_input * self.braking_deceleration * _delta, 0.0)",
            "\t\t\tif accel_input <= 0.01 and brake_input <= 0.01:",
            "\t\t\t\tspeed *= max(1.0 - (self.friction_coefficient * step_scale), 0.0)",
            "\t\t\tspeed = clampf(speed, 0.0, max_speed_cap)",
            '\t\t\tif speed >= self.minimum_steering_speed and absf(steer_input) > 0.01:',
            '\t\t\t\tangle_value += steer_input * self.turn_rate_base * _delta',
            '\t\t\t\tprint("[trace] physics.turn kart=%s angle=%.2f steer=%.2f speed=%.2f" % [kart.name, angle_value, steer_input, speed])',
            '\t\tvar heading: Vector2 = _heading(angle_value)',
            '\t\tvar velocity: Vector2 = heading * speed',
            '\t\tvar position: Vector2 = Vector2(float(kart.position.x), float(kart.position.y)) + velocity * _delta',
            '\t\toffroad_state = absf(position.x - track_center_x) > road_width * 0.5',
            '\t\t_set_prop(kart, SPEED_PROP, speed)',
            '\t\t_set_prop(kart, VELOCITY_PROP, velocity)',
            '\t\t_set_prop(kart, ANGLE_PROP, angle_value)',
            '\t\t_set_prop(kart, OFFROAD_PROP, offroad_state)',
            '\t\t_set_prop(kart, SPIN_TIMER_PROP, spin_timer)',
            "\t\tkart.position = position",
            '\t\t_trace_state(kart, speed, max_speed_cap, position)',
        ],
        round_start_body=[
            "\t_last_trace_state.clear()",
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            '\t\t_set_prop(kart, SPEED_PROP, 0.0)',
            '\t\t_set_prop(kart, VELOCITY_PROP, Vector2.ZERO)',
            '\t\t_set_prop(kart, ACCEL_PROP, false)',
            '\t\t_set_prop(kart, BRAKE_PROP, false)',
            '\t\t_set_prop(kart, STEER_PROP, 0.0)',
            '\t\t_set_prop(kart, OFFROAD_PROP, false)',
            '\t\t_set_prop(kart, SPINNING_PROP, false)',
            '\t\t_set_prop(kart, SPIN_TIMER_PROP, 0.0)',
        ],
    )


def _specialized_item_box_system(reads: list[str], constants: dict[str, Any], context: dict[str, str | None]) -> str:
    pickup_pool = str(context.get("pickup_pool") or "item_boxs")
    return _specialized_system_source(
        "item_box_system",
        reads,
        constants,
        primary_pool_var="item_boxs",
        primary_pool_key=pickup_pool,
        helper_blocks=[
            """
const DEFENSIVE_ITEMS: Array[String] = ["banana", "green_shell"]
const OFFENSIVE_ITEMS: Array[String] = ["mushroom", "star", "green_shell"]
const ALL_ITEMS: Array[String] = ["banana", "green_shell", "mushroom", "star"]
""".strip(),
            """
func _weighted_item_for_rank(position_rank: int) -> String:
\tvar defensive_weight: float = clampf(self.first_place_item_weight_defensive, 0.05, 0.95)
\tvar offensive_weight: float = clampf(self.last_place_item_weight_offensive, 0.05, 0.95)
\tvar weights: Dictionary = {}
\tfor item in ALL_ITEMS:
\t\tif position_rank <= 1:
\t\t\tweights[item] = defensive_weight if item in DEFENSIVE_ITEMS else 1.0 - defensive_weight
\t\telse:
\t\t\tweights[item] = offensive_weight if item in OFFENSIVE_ITEMS else 1.0 - offensive_weight
\tvar total_weight: float = 0.0
\tfor weight in weights.values():
\t\ttotal_weight += float(weight)
\tvar roll: float = randf() * total_weight
\tvar cursor: float = 0.0
\tfor item in ALL_ITEMS:
\t\tcursor += float(weights.get(item, 0.0))
\t\tif roll <= cursor:
\t\t\treturn item
\treturn "green_shell"
""".strip(),
            """
func process_overlap_event(kart, item_box) -> void:
\tif kart == null or item_box == null:
\t\treturn
\tif not bool(item_box.get("active")):
\t\treturn
\tif str(kart.get("current_item") if kart.get("current_item") != null else "") != "":
\t\treturn
\titem_box.active = false
\titem_box.respawn_timer = self.respawn_delay_seconds
\tvar rank: int = max(int(kart.get("position_rank") if kart.get("position_rank") != null else 1), 1)
\tvar next_item: String = _weighted_item_for_rank(rank)
\tkart.current_item = next_item
\tprint("[trace] item_box.collected kart=%s box=%s item=%s position_rank=%d" % [
\t\tkart.name,
\t\titem_box.name,
\t\tnext_item,
\t\trank,
\t])
""".strip(),
        ],
        process_body=[
            "\tfor item_box in item_boxs:",
            "\t\tif item_box == null:",
            "\t\t\tcontinue",
            '\t\tif not bool(item_box.get("active")):',
            '\t\t\tvar respawn_timer: float = max(float(item_box.get("respawn_timer") if item_box.get("respawn_timer") != null else 0.0) - _delta, 0.0)',
            "\t\t\titem_box.respawn_timer = respawn_timer",
            "\t\t\tif respawn_timer <= 0.0:",
            "\t\t\t\titem_box.active = true",
            '\t\t\t\tprint("[trace] item_box.respawn box=%s" % [item_box.name])',
        ],
        round_start_body=[
            "\tfor item_box in item_boxs:",
            "\t\tif item_box == null:",
            "\t\t\tcontinue",
            "\t\titem_box.active = true",
            "\t\titem_box.respawn_timer = 0.0",
        ],
    )


def _specialized_item_usage_system(reads: list[str], constants: dict[str, Any], context: dict[str, str | None]) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    projectile_pool = str(context.get("projectile_pool") or "green_shells")
    trap_pool = str(context.get("trap_pool") or "banana_peels")
    return _specialized_system_source(
        "item_usage_system",
        reads,
        constants,
        primary_pool_var="karts",
        primary_pool_key=actor_pool,
        helper_blocks=[
            f"""
func _forward(angle_deg: float) -> Vector2:
\tvar radians: float = deg_to_rad(angle_deg)
\treturn Vector2(cos(radians), sin(radians))
""".strip(),
            """
func _first_inactive(pool_name: String):
\tfor entity in entity_pools.get(pool_name, []):
\t\tif entity != null and not bool(entity.get("active")):
\t\t\treturn entity
\treturn null
""".strip(),
            f"""
func _update_projectiles(_delta: float) -> void:
\tfor shell in entity_pools.get("{projectile_pool}", []):
\t\tif shell == null or not bool(shell.get("active")):
\t\t\tcontinue
\t\tvar velocity: Variant = shell.get("velocity")
\t\tif velocity is Vector2:
\t\t\tshell.position = shell.position + (velocity as Vector2) * max(_delta * 60.0, 1.0)
\t\t\tif shell.position.x < -160.0 or shell.position.x > 2080.0 or shell.position.y < -160.0 or shell.position.y > 1240.0:
\t\t\t\tshell.active = false
\t\t\t\tprint("[trace] item_usage.shell_despawn shell=%s reason=out_of_bounds" % [shell.name])
""".strip(),
            f"""
func _spawn_banana(kart) -> void:
\tvar peel = _first_inactive("{trap_pool}")
\tif peel == null:
\t\treturn
\tvar forward: Vector2 = _forward(float(kart.get("facing_angle") if kart.get("facing_angle") != null else 0.0))
\tpeel.position = kart.position - forward * 72.0
\tpeel.active = true
\tprint("[trace] item_usage.spawn_banana kart=%s pos=%s" % [kart.name, str(peel.position)])
""".strip(),
            f"""
func _spawn_shell(kart) -> void:
\tvar shell = _first_inactive("{projectile_pool}")
\tif shell == null:
\t\treturn
\tvar forward: Vector2 = _forward(float(kart.get("facing_angle") if kart.get("facing_angle") != null else 0.0))
\tshell.position = kart.position + forward * 84.0
\tshell.velocity = forward * self.shell_speed
\tshell.active = true
\tprint("[trace] item_usage.spawn_shell kart=%s pos=%s velocity=%s" % [kart.name, str(shell.position), str(shell.velocity)])
""".strip(),
            """
func _apply_item(kart, item_name: String) -> void:
\tif item_name == "banana":
\t\t_spawn_banana(kart)
\telif item_name == "green_shell":
\t\t_spawn_shell(kart)
\telif item_name == "mushroom":
\t\tkart.item_boost_active = true
\t\tkart.item_boost_timer = self.mushroom_boost_duration
\t\tprint("[trace] item_usage.boost_start kart=%s duration=%.2f" % [kart.name, self.mushroom_boost_duration])
\telif item_name == "star":
\t\tkart.is_invincible = true
\t\tkart.invincibility_timer = self.star_invincibility_duration
\t\tprint("[trace] item_usage.invincibility_start kart=%s duration=%.2f" % [kart.name, self.star_invincibility_duration])
""".strip(),
        ],
        process_body=[
            "\t_update_projectiles(_delta)",
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            '\t\tvar invincibility_timer: float = max(float(kart.get("invincibility_timer") if kart.get("invincibility_timer") != null else 0.0) - _delta, 0.0)',
            "\t\tif invincibility_timer != float(kart.get(\"invincibility_timer\") if kart.get(\"invincibility_timer\") != null else 0.0):",
            "\t\t\tkart.invincibility_timer = invincibility_timer",
            "\t\t\tif invincibility_timer <= 0.0 and bool(kart.get(\"is_invincible\")):",
            "\t\t\t\tkart.is_invincible = false",
            '\t\t\t\tprint("[trace] item_usage.invincibility_end kart=%s" % [kart.name])',
            '\t\tvar boost_timer: float = max(float(kart.get("item_boost_timer") if kart.get("item_boost_timer") != null else 0.0) - _delta, 0.0)',
            "\t\tif boost_timer != float(kart.get(\"item_boost_timer\") if kart.get(\"item_boost_timer\") != null else 0.0):",
            "\t\t\tkart.item_boost_timer = boost_timer",
            "\t\t\tif boost_timer <= 0.0 and bool(kart.get(\"item_boost_active\")):",
            "\t\t\t\tkart.item_boost_active = false",
            '\t\t\t\tprint("[trace] item_usage.boost_end kart=%s" % [kart.name])',
            '\t\tif not bool(kart.get("item_input")):',
            "\t\t\tcontinue",
            '\t\tvar current_item: String = str(kart.get("current_item") if kart.get("current_item") != null else "")',
            '\t\tif current_item == "":',
            "\t\t\tcontinue",
            '\t\tprint("[trace] item_usage.activate kart=%s item=%s" % [kart.name, current_item])',
            "\t\t_apply_item(kart, current_item)",
            '\t\tkart.current_item = ""',
            '\t\tprint("[trace] item_usage.clear_item kart=%s" % [kart.name])',
        ],
        round_start_body=[
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            '\t\tkart.current_item = ""',
            "\t\tkart.item_input = false",
            "\t\tkart.item_boost_active = false",
            "\t\tkart.item_boost_timer = 0.0",
            "\t\tkart.is_invincible = false",
            "\t\tkart.invincibility_timer = 0.0",
            f'\tfor shell in entity_pools.get("{projectile_pool}", []):',
            "\t\tif shell == null:",
            "\t\t\tcontinue",
            "\t\tshell.active = false",
            "\t\tshell.velocity = Vector2.ZERO",
            f'\tfor peel in entity_pools.get("{trap_pool}", []):',
            "\t\tif peel == null:",
            "\t\t\tcontinue",
            "\t\tpeel.active = false",
        ],
    )


def _specialized_item_system(
    reads: list[str],
    constants: dict[str, Any],
    context: dict[str, str | None],
    slice_data: dict,
) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    actor_owner = actor_pool[:-1] if actor_pool.endswith("s") else "kart"
    pickup_pool = str(context.get("pickup_pool") or "item_boxs")
    pickup_owner = pickup_pool[:-1] if pickup_pool.endswith("s") else "item_box"
    projectile_pool = str(context.get("projectile_pool") or "projectiles")
    projectile_owner = projectile_pool[:-1] if projectile_pool.endswith("s") else "projectile"
    trap_pool = str(context.get("trap_pool") or "banana_peels")
    trap_owner = trap_pool[:-1] if trap_pool.endswith("s") else "banana_peel"

    held_item_prop = resolve_property_name(slice_data, actor_owner, ("current_item", "held_item"), default="current_item")
    item_input_prop = resolve_property_name(slice_data, actor_owner, ("item_trigger_input", "item_input", "input_item", "use_item_input"), default="item_trigger_input")
    position_prop = resolve_property_name(slice_data, actor_owner, ("position",), default="position")
    angle_prop = resolve_property_name(slice_data, actor_owner, ("facing_angle", "angle"), default="facing_angle")
    rank_prop = resolve_property_name(slice_data, actor_owner, ("race_position", "position_rank", "placement"), default="race_position")
    boost_timer_prop = resolve_property_name(slice_data, actor_owner, ("boost_timer", "item_boost_timer"), default="boost_timer")
    boost_mult_prop = resolve_property_name(slice_data, actor_owner, ("boost_multiplier", "item_boost_multiplier"), default="boost_multiplier")
    invuln_prop = resolve_property_name(slice_data, actor_owner, ("invincibility_timer", "item_invincibility_timer"), default="invincibility_timer")
    actor_id_prop = resolve_property_name(slice_data, actor_owner, ("kart_id", "actor_id", "owner_id"), default="kart_id")

    pickup_active_prop = resolve_property_name(slice_data, pickup_owner, ("is_active", "active"), default="is_active")
    pickup_timer_prop = resolve_property_name(slice_data, pickup_owner, ("respawn_timer", "cooldown_timer"), default="respawn_timer")

    projectile_active_prop = resolve_property_name(slice_data, projectile_owner, ("is_active", "active"), default="is_active")
    projectile_owner_prop = resolve_property_name(slice_data, projectile_owner, ("owner_kart_id", "owner_id", "owner_actor_id"), default="owner_kart_id")
    trap_active_prop = resolve_property_name(slice_data, trap_owner, ("is_active", "active"), default="is_active")

    respawn_seconds = float(_constant_scalar(constants, ("item_box_respawn_time", "respawn_delay_seconds"), 8.0))
    shell_speed = float(_constant_scalar(constants, ("shell_speed", "projectile_speed"), 18.0))
    mushroom_duration = float(_constant_scalar(constants, ("mushroom_boost_duration", "boost_duration"), 1.6))
    mushroom_multiplier = float(_constant_scalar(constants, ("mushroom_boost_multiplier", "boost_multiplier"), 1.35))
    invincibility_duration = float(_constant_scalar(constants, ("star_invincibility_duration", "item_invincibility_duration"), 2.5))
    respawn_field = "item_box_respawn_time" if "item_box_respawn_time" in constants else "item_box_respawn_time"
    shell_speed_field = "shell_speed" if "shell_speed" in constants else "shell_speed"
    boost_duration_field = "mushroom_boost_duration" if "mushroom_boost_duration" in constants else "mushroom_boost_duration"
    boost_multiplier_field = "mushroom_boost_multiplier" if "mushroom_boost_multiplier" in constants else "mushroom_boost_multiplier"
    invuln_duration_field = "star_invincibility_duration" if "star_invincibility_duration" in constants else "item_invincibility_duration"
    extra_fields: list[str] = []
    if shell_speed_field not in constants:
        extra_fields.append(f"@export var shell_speed: float = {shell_speed}")
    if boost_multiplier_field not in constants:
        extra_fields.append(f"@export var mushroom_boost_multiplier: float = {mushroom_multiplier}")
    if respawn_field not in constants:
        extra_fields.append(f"@export var item_box_respawn_time: float = {respawn_seconds}")
    if boost_duration_field not in constants:
        extra_fields.append(f"@export var mushroom_boost_duration: float = {mushroom_duration}")
    if invuln_duration_field not in constants:
        extra_fields.append(f"@export var item_invincibility_duration: float = {invincibility_duration}")

    return _specialized_system_source(
        "item_system",
        reads,
        constants,
        primary_pool_var="actors",
        primary_pool_key=actor_pool,
        extra_fields=extra_fields,
        helper_blocks=[
            f"""
const HELD_ITEM_PROP := "{held_item_prop}"
const ITEM_INPUT_PROP := "{item_input_prop}"
const POSITION_PROP := "{position_prop}"
const ANGLE_PROP := "{angle_prop}"
const RANK_PROP := "{rank_prop}"
const BOOST_TIMER_PROP := "{boost_timer_prop}"
const BOOST_MULT_PROP := "{boost_mult_prop}"
const INVULN_PROP := "{invuln_prop}"
const ACTOR_ID_PROP := "{actor_id_prop}"
const PICKUP_ACTIVE_PROP := "{pickup_active_prop}"
const PICKUP_TIMER_PROP := "{pickup_timer_prop}"
const PROJECTILE_ACTIVE_PROP := "{projectile_active_prop}"
const PROJECTILE_OWNER_PROP := "{projectile_owner_prop}"
const TRAP_ACTIVE_PROP := "{trap_active_prop}"
""".strip(),
            """
func _entity_value(obj, prop_name: String, fallback):
\tif obj == null or prop_name == "":
\t\treturn fallback
\tvar raw = obj.get(prop_name)
\treturn fallback if raw == null else raw
""".strip(),
            """
func _set_prop(obj, prop_name: String, value) -> void:
\tif obj == null or prop_name == "":
\t\treturn
\tobj.set(prop_name, value)
""".strip(),
            """
func _ensure_pool(pool_name: String) -> Array:
\tif not entity_pools.has(pool_name):
\t\tentity_pools[pool_name] = []
\treturn entity_pools.get(pool_name, [])
""".strip(),
            """
func _spawn_entity(script_path: String, pool_name: String, base_name: String):
\tvar script_res = load(script_path)
\tif script_res == null:
\t\treturn null
\tvar entity = script_res.new()
\tentity.name = "%s_%d" % [base_name, Time.get_ticks_usec()]
\tif get_parent() != null:
\t\tget_parent().add_child(entity)
\tvar pool = _ensure_pool(pool_name)
\tpool.append(entity)
\tentity_pools[pool_name] = pool
\treturn entity
""".strip(),
            """
func _first_inactive(pool_name: String, active_prop: String, script_path: String, base_name: String):
\tfor entity in _ensure_pool(pool_name):
\t\tif entity != null and not bool(_entity_value(entity, active_prop, false)):
\t\t\treturn entity
\treturn _spawn_entity(script_path, pool_name, base_name)
""".strip(),
            """
func _forward(angle_radians: float) -> Vector2:
\treturn Vector2(cos(angle_radians), sin(angle_radians))
""".strip(),
            """
func _item_for_rank(rank: int) -> String:
\tif rank <= 1:
\t\treturn "trap"
\tif rank >= 3:
\t\treturn "projectile"
\treturn "boost"
""".strip(),
            f"""
func _spawn_projectile(actor) -> void:
\tvar projectile = _first_inactive("{projectile_pool}", PROJECTILE_ACTIVE_PROP, "res://scripts/entities/{projectile_owner}.gd", "{projectile_owner}")
\tif projectile == null:
\t\treturn
\tvar angle: float = float(_entity_value(actor, ANGLE_PROP, 0.0))
\tvar forward: Vector2 = _forward(angle)
\tprojectile.position = actor.position + forward * 84.0
\tprojectile.rotation = angle
\t_set_prop(projectile, PROJECTILE_ACTIVE_PROP, true)
\tif PROJECTILE_OWNER_PROP != "":
\t\t_set_prop(projectile, PROJECTILE_OWNER_PROP, int(_entity_value(actor, ACTOR_ID_PROP, 0)))
\tprint("[trace] item.spawn_projectile actor=%s pos=%s" % [actor.name, str(projectile.position)])
""".strip(),
            f"""
func _spawn_trap(actor) -> void:
\tvar trap = _first_inactive("{trap_pool}", TRAP_ACTIVE_PROP, "res://scripts/entities/{trap_owner}.gd", "{trap_owner}")
\tif trap == null:
\t\treturn
\tvar angle: float = float(_entity_value(actor, ANGLE_PROP, 0.0))
\tvar forward: Vector2 = _forward(angle)
\ttrap.position = actor.position - forward * 72.0
\t_set_prop(trap, TRAP_ACTIVE_PROP, true)
\tprint("[trace] item.spawn_trap actor=%s pos=%s" % [actor.name, str(trap.position)])
""".strip(),
            """
func _apply_item(actor, item_name: String) -> void:
\tvar lower_name := item_name.to_lower()
\tif lower_name.find("projectile") >= 0 or lower_name.find("shell") >= 0:
\t\t_spawn_projectile(actor)
\telif lower_name.find("trap") >= 0 or lower_name.find("banana") >= 0 or lower_name.find("peel") >= 0:
\t\t_spawn_trap(actor)
\telif lower_name.find("star") >= 0 or lower_name.find("invinc") >= 0 or lower_name.find("shield") >= 0:
\t\t_set_prop(actor, INVULN_PROP, self.""" + invuln_duration_field + """)
\t\tprint("[trace] item.invulnerability_start actor=%s duration=%.2f" % [actor.name, self.""" + invuln_duration_field + """])
\telse:
\t\tvar boost_timer: float = max(float(_entity_value(actor, BOOST_TIMER_PROP, 0.0)), 0.0)
\t\t_set_prop(actor, BOOST_TIMER_PROP, max(boost_timer, self.""" + boost_duration_field + """))
\t\t_set_prop(actor, BOOST_MULT_PROP, max(float(_entity_value(actor, BOOST_MULT_PROP, 1.0)), self.""" + boost_multiplier_field + """))
\t\tprint("[trace] item.boost_start actor=%s duration=%.2f multiplier=%.2f" % [actor.name, self.""" + boost_duration_field + """, self.""" + boost_multiplier_field + """])
""".strip(),
            """
func process_overlap_event(actor, pickup) -> void:
\tif actor == null or pickup == null:
\t\treturn
\tif not bool(_entity_value(pickup, PICKUP_ACTIVE_PROP, false)):
\t\treturn
\tif str(_entity_value(actor, HELD_ITEM_PROP, "")).strip_edges() != "":
\t\treturn
\tvar rank: int = max(int(_entity_value(actor, RANK_PROP, 1)), 1)
\tvar granted_item := _item_for_rank(rank)
\t_set_prop(actor, HELD_ITEM_PROP, granted_item)
\t_set_prop(pickup, PICKUP_ACTIVE_PROP, false)
\t_set_prop(pickup, PICKUP_TIMER_PROP, self.""" + respawn_field + """)
\tprint("[trace] item.collect actor=%s item=%s rank=%d" % [actor.name, granted_item, rank])
""".strip(),
        ],
        process_body=[
            f'\tvar pickups: Array = entity_pools.get("{pickup_pool}", [])',
            '\tfor pickup in pickups:',
            '\t\tif pickup == null:',
            '\t\t\tcontinue',
            '\t\tif not bool(_entity_value(pickup, PICKUP_ACTIVE_PROP, false)):',
            '\t\t\tvar respawn_timer: float = max(float(_entity_value(pickup, PICKUP_TIMER_PROP, 0.0)) - _delta, 0.0)',
            '\t\t\t_set_prop(pickup, PICKUP_TIMER_PROP, respawn_timer)',
            '\t\t\tif respawn_timer <= 0.0:',
            '\t\t\t\t_set_prop(pickup, PICKUP_ACTIVE_PROP, true)',
            '\t\t\t\tprint("[trace] item.pickup_respawn pickup=%s" % [pickup.name])',
            '\tfor projectile in _ensure_pool("' + projectile_pool + '"):',
            '\t\tif projectile == null or not bool(_entity_value(projectile, PROJECTILE_ACTIVE_PROP, false)):',
            '\t\t\tcontinue',
            '\t\tvar forward: Vector2 = _forward(float(projectile.rotation))',
            '\t\tprojectile.position = projectile.position + forward * self.' + shell_speed_field + ' * max(_delta * 60.0, 1.0)',
            '\t\tif projectile.position.x < -160.0 or projectile.position.x > 2080.0 or projectile.position.y < -160.0 or projectile.position.y > 1240.0:',
            '\t\t\t_set_prop(projectile, PROJECTILE_ACTIVE_PROP, false)',
            '\t\t\tprint("[trace] item.projectile_despawn projectile=%s" % [projectile.name])',
            '\tfor actor in actors:',
            '\t\tif actor == null:',
            '\t\t\tcontinue',
            '\t\tvar invuln_timer: float = max(float(_entity_value(actor, INVULN_PROP, 0.0)) - _delta, 0.0)',
            '\t\t_set_prop(actor, INVULN_PROP, invuln_timer)',
            '\t\tif POSITION_PROP != "":',
            '\t\t\tfor pickup in pickups:',
            '\t\t\t\tif pickup == null or not bool(_entity_value(pickup, PICKUP_ACTIVE_PROP, false)):',
            '\t\t\t\t\tcontinue',
            '\t\t\t\tif actor.position.distance_to(pickup.position) <= 84.0:',
            '\t\t\t\t\tprocess_overlap_event(actor, pickup)',
            '\t\tvar item_pressed: bool = bool(_entity_value(actor, ITEM_INPUT_PROP, false))',
            '\t\tif not item_pressed:',
            '\t\t\tcontinue',
            '\t\tvar current_item: String = str(_entity_value(actor, HELD_ITEM_PROP, "")).strip_edges()',
            '\t\tif current_item == "":',
            '\t\t\tcontinue',
            '\t\tprint("[trace] item.use actor=%s item=%s" % [actor.name, current_item])',
            '\t\t_apply_item(actor, current_item)',
            '\t\t_set_prop(actor, HELD_ITEM_PROP, "")',
        ],
        round_start_body=[
            '\tfor actor in actors:',
            '\t\tif actor == null:',
            '\t\t\tcontinue',
            '\t\t_set_prop(actor, HELD_ITEM_PROP, "")',
            '\t\t_set_prop(actor, ITEM_INPUT_PROP, false)',
            '\t\t_set_prop(actor, INVULN_PROP, 0.0)',
            '\tfor pickup in _ensure_pool("' + pickup_pool + '"):',
            '\t\tif pickup == null:',
            '\t\t\tcontinue',
            '\t\t_set_prop(pickup, PICKUP_ACTIVE_PROP, true)',
            '\t\t_set_prop(pickup, PICKUP_TIMER_PROP, 0.0)',
            '\tfor projectile in _ensure_pool("' + projectile_pool + '"):',
            '\t\tif projectile == null:',
            '\t\t\tcontinue',
            '\t\t_set_prop(projectile, PROJECTILE_ACTIVE_PROP, false)',
            '\tfor trap in _ensure_pool("' + trap_pool + '"):',
            '\t\tif trap == null:',
            '\t\t\tcontinue',
            '\t\t_set_prop(trap, TRAP_ACTIVE_PROP, false)',
        ],
    )


def _specialized_collision_resolution_system(reads: list[str], constants: dict[str, Any], context: dict[str, str | None]) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    pickup_pool = str(context.get("pickup_pool") or "item_boxs")
    projectile_pool = str(context.get("projectile_pool") or "green_shells")
    trap_pool = str(context.get("trap_pool") or "banana_peels")
    generic_object_pool = str(context.get("generic_object_pool") or "game_objects")
    push_force = float(_constant_scalar(constants, ("kart_kart_push_force", "collision_push_force"), 1.0))
    shell_radius = float(_constant_scalar(constants, ("shell_hit_radius", "projectile_hit_radius"), 28.0))
    return _specialized_system_source(
        "collision_resolution_system",
        reads,
        constants,
        primary_pool_var="karts",
        primary_pool_key=actor_pool,
        extra_fields=[
            f"@export var kart_kart_push_force: float = {push_force}",
            f"@export var shell_hit_radius: float = {shell_radius}",
        ],
        helper_blocks=[
            """
func _radius(value: Variant, fallback: float) -> float:
\tif value == null:
\t\treturn fallback
\tvar as_float: float = float(value)
\tif as_float <= 4.0:
\t\treturn fallback
\treturn as_float
""".strip(),
            """
func _spin_out(kart, source: String) -> void:
\tif kart == null or float(kart.get("invincibility_timer") if kart.get("invincibility_timer") != null else 0.0) > 0.0:
\t\treturn
\tif float(kart.get("spin_out_timer") if kart.get("spin_out_timer") != null else 0.0) <= 0.0:
\t\tkart.spin_out_timer = float(self.spin_out_duration) / 60.0
\t\tprint("[trace] collision.spin_out kart=%s source=%s" % [kart.name, source])
""".strip(),
        ],
        process_body=[
            "\tfor i in range(karts.size()):",
            "\t\tvar kart_a = karts[i]",
            "\t\tif kart_a == null:",
            "\t\t\tcontinue",
            '\t\tvar radius_a: float = _radius(kart_a.get("collision_radius"), 56.0)',
            "\t\tfor j in range(i + 1, karts.size()):",
            "\t\t\tvar kart_b = karts[j]",
            "\t\t\tif kart_b == null:",
            "\t\t\t\tcontinue",
            '\t\t\tvar radius_b: float = _radius(kart_b.get("collision_radius"), 56.0)',
            '\t\t\tvar delta_pos: Vector2 = Vector2(float(kart_a.position.x - kart_b.position.x), float(kart_a.position.y - kart_b.position.y))',
            '\t\t\tvar distance: float = max(delta_pos.length(), 0.001)',
            "\t\t\tvar min_distance: float = radius_a + radius_b",
            "\t\t\tif distance < min_distance:",
            "\t\t\t\tvar normal: Vector2 = delta_pos / distance",
            '\t\t\t\tvar overlap: float = (min_distance - distance) * 0.5',
            '\t\t\t\tvar push: Vector2 = normal * overlap * self.kart_kart_push_force',
            "\t\t\t\tkart_a.position += push",
            "\t\t\t\tkart_b.position -= push",
            '\t\t\t\tprint("[trace] collision.kart_kart kart_a=%s kart_b=%s overlap=%.2f" % [kart_a.name, kart_b.name, min_distance - distance])',
            '\tfor kart in karts:',
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            '\t\tvar kart_radius: float = _radius(kart.get("collision_radius"), 56.0)',
            f'\t\tfor item_box in entity_pools.get("{pickup_pool}", []):',
            "\t\t\tif item_box == null or not bool(item_box.get(\"active\")):",
            "\t\t\t\tcontinue",
            '\t\t\tvar item_distance: float = kart.position.distance_to(item_box.position)',
            "\t\t\tif item_distance <= kart_radius + 44.0:",
            '\t\t\t\tprint("[trace] collision.item_box kart=%s box=%s" % [kart.name, item_box.name])',
            '\t\t\t\tif sibling_systems.has("item_box_system") and sibling_systems["item_box_system"].has_method("process_overlap_event"):',
            '\t\t\t\t\tsibling_systems["item_box_system"].process_overlap_event(kart, item_box)',
            '\t\t\t\telif sibling_systems.has("item_system") and sibling_systems["item_system"].has_method("process_overlap_event"):',
            '\t\t\t\t\tsibling_systems["item_system"].process_overlap_event(kart, item_box)',
            "\t\t\t\telse:",
            "\t\t\t\t\titem_box.active = false",
            f'\t\tfor peel in entity_pools.get("{trap_pool}", []):',
            "\t\t\tif peel == null or not bool(peel.get(\"active\")):",
            "\t\t\t\tcontinue",
            '\t\t\tif kart.position.distance_to(peel.position) <= kart_radius + 32.0:',
            "\t\t\t\tpeel.active = false",
            '\t\t\t\tprint("[trace] collision.game_object kart=%s object=%s type=banana" % [kart.name, peel.name])',
            '\t\t\t\t_spin_out(kart, "banana")',
            f'\t\tfor shell in entity_pools.get("{projectile_pool}", []):',
            "\t\t\tif shell == null or not bool(shell.get(\"active\")):",
            "\t\t\t\tcontinue",
            '\t\t\tif kart.position.distance_to(shell.position) <= kart_radius + max(self.shell_hit_radius, 24.0):',
            "\t\t\t\tshell.active = false",
            "\t\t\t\tshell.velocity = Vector2.ZERO",
            '\t\t\t\tprint("[trace] collision.game_object kart=%s object=%s type=shell" % [kart.name, shell.name])',
            '\t\t\t\t_spin_out(kart, "shell")',
            f'\t\tfor obstacle in entity_pools.get("{generic_object_pool}", []):',
            "\t\t\tif obstacle == null or not bool(obstacle.get(\"active\")):",
            "\t\t\t\tcontinue",
            '\t\t\tvar obstacle_radius: float = _radius(obstacle.get("collision_radius"), 64.0)',
            '\t\t\tif kart.position.distance_to(obstacle.position) <= kart_radius + obstacle_radius:',
            "\t\t\t\tobstacle.active = false",
            '\t\t\t\tprint("[trace] collision.game_object kart=%s object=%s type=obstacle" % [kart.name, obstacle.name])',
            '\t\t\t\t_spin_out(kart, "obstacle")',
        ],
        round_start_body=[
            "\tfor kart in karts:",
            "\t\tif kart == null:",
            "\t\t\tcontinue",
            "\t\tkart.spin_out_timer = 0.0",
        ],
    )


def _specialized_countdown_system(
    reads: list[str],
    constants: dict[str, Any],
    system_name: str = "countdown_system",
) -> str:
    seconds_per_count = float(_constant_scalar(constants, ("seconds_per_count",), 1.0))
    start_value = int(_constant_scalar(constants, ("countdown_start_value", "start_value"), 3))
    return _specialized_system_source(
        system_name,
        reads,
        constants,
        primary_pool_var="anchors",
        primary_pool_key="game",
        extra_fields=[
            "var _initialized: bool = false",
            f"@export var start_value: int = {start_value}",
            f"@export var skip_countdown: bool = false",
        ],
        helper_blocks=[
            """
func _root():
\treturn get_parent()
""".strip(),
        ],
        process_body=[
            "\tvar root = _root()",
            "\tif root == null:",
            "\t\treturn",
            "\tif self.skip_countdown or self.seconds_per_count <= 0.0:",
            "\t\troot.countdown_value = 0",
            "\t\troot.countdown_timer = 0.0",
            "\t\troot.countdown_active = false",
            '\t\tif str(root.fsm_state).strip_edges() == "" or str(root.fsm_state).find("COUNTDOWN") >= 0:',
            '\t\t\troot.fsm_state = "S_RACING"',
            "\t\treturn",
            "\tif not _initialized:",
            "\t\troot.countdown_value = max(int(root.countdown_value), self.start_value)",
            "\t\troot.countdown_timer = self.seconds_per_count",
            "\t\troot.countdown_active = true",
            '\t\troot.fsm_state = "S_COUNTDOWN"',
            "\t\t_initialized = true",
            '\t\tprint("[trace] countdown.start value=%d" % [int(root.countdown_value)])',
            "\t\treturn",
            "\tif not bool(root.countdown_active):",
            "\t\treturn",
            "\troot.countdown_timer = max(float(root.countdown_timer) - _delta, 0.0)",
            "\tif float(root.countdown_timer) > 0.0:",
            "\t\treturn",
            "\tvar next_value: int = max(int(root.countdown_value) - 1, 0)",
            "\troot.countdown_value = next_value",
            "\troot.countdown_timer = self.seconds_per_count",
            '\tprint("[trace] countdown.tick value=%d" % [next_value])',
            "\tif next_value <= 0:",
            "\t\troot.countdown_active = false",
            '\t\troot.fsm_state = "S_RACING"',
            "\t\troot.countdown_timer = 0.0",
            '\t\tprint("[trace] countdown.complete state=%s" % [str(root.fsm_state)])',
        ],
        round_start_body=[
            "\tvar root = _root()",
            "\tif root == null:",
            "\t\treturn",
            "\t_initialized = false",
            "\troot.countdown_value = self.start_value",
            "\troot.countdown_timer = self.seconds_per_count",
            "\troot.countdown_active = not self.skip_countdown",
            '\troot.fsm_state = "S_COUNTDOWN" if not self.skip_countdown else "S_RACING"',
        ],
    )


def _specialized_ai_navigation_system(
    reads: list[str],
    constants: dict[str, Any],
    context: dict[str, str | None],
    slice_data: dict,
    system_name: str = "ai_navigation_system",
) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    stage_pool = str(context.get("stage_pool") or "stages")
    actor_owner = actor_pool[:-1] if actor_pool.endswith("s") else "kart"
    steer_prop = resolve_property_name(slice_data, actor_owner, ("steer_input", "turn_input"), default="steer_input")
    accel_prop = resolve_property_name(slice_data, actor_owner, ("acceleration_input", "accel_input"), default="acceleration_input")
    item_prop = resolve_property_name(slice_data, actor_owner, ("item_trigger_input", "item_input", "use_item_input"), default="item_trigger_input")
    ai_prop = resolve_property_name(slice_data, actor_owner, ("is_ai_controlled", "is_cpu"), default="is_ai_controlled")
    angle_prop = resolve_property_name(slice_data, actor_owner, ("facing_angle", "angle", "heading_degrees"), default="facing_angle")
    waypoint_prop = resolve_property_name(slice_data, actor_owner, ("ai_target_waypoint", "waypoint_index"), default="waypoint_index")
    ai_mult_prop = resolve_property_name(slice_data, actor_owner, ("rubber_band_multiplier", "ai_rubber_band_mult", "ai_speed_multiplier"), default="rubber_band_multiplier")
    ai_timer_prop = resolve_property_name(slice_data, actor_owner, ("ai_item_timer",), default="ai_item_timer")
    threshold = float(_constant_scalar(constants, ("ai_waypoint_threshold",), 96.0))
    rubber_far = float(_constant_scalar(constants, ("rubber_band_distance_far",), 250.0))
    rubber_max = float(_constant_scalar(constants, ("rubber_band_max_mult",), 1.3))
    item_min_delay = float(_constant_scalar(constants, ("ai_item_use_min_delay",), 30.0))
    item_max_delay = float(_constant_scalar(constants, ("ai_item_use_max_delay",), 60.0))
    extra_fields = ["var _last_trace_state: Dictionary = {}"]
    if "ai_waypoint_threshold" not in constants:
        extra_fields.append(f"@export var ai_waypoint_threshold: float = {threshold}")
    if "rubber_band_distance_far" not in constants:
        extra_fields.append(f"@export var rubber_band_distance_far: float = {rubber_far}")
    if "rubber_band_max_mult" not in constants:
        extra_fields.append(f"@export var rubber_band_max_mult: float = {rubber_max}")
    if "ai_item_use_min_delay" not in constants:
        extra_fields.append(f"@export var ai_item_use_min_delay: float = {item_min_delay}")
    if "ai_item_use_max_delay" not in constants:
        extra_fields.append(f"@export var ai_item_use_max_delay: float = {item_max_delay}")
    return _specialized_system_source(
        system_name,
        reads,
        constants,
        primary_pool_var="actors",
        primary_pool_key=actor_pool,
        extra_fields=extra_fields,
        helper_blocks=[
            """
const STEER_PROP := "%s"
const ACCEL_PROP := "%s"
const ITEM_PROP := "%s"
const AI_PROP := "%s"
const ANGLE_PROP := "%s"
const WAYPOINT_PROP := "%s"
const AI_MULT_PROP := "%s"
const AI_TIMER_PROP := "%s"
"""
            % (
                steer_prop,
                accel_prop,
                item_prop,
                ai_prop,
                angle_prop,
                waypoint_prop,
                ai_mult_prop,
                ai_timer_prop,
            ),
            """
func _entity_value(obj, prop_name: String, fallback):
\tif obj == null or prop_name == "":
\t\treturn fallback
\tvar raw = obj.get(prop_name)
\tif raw == null and obj.has_meta(prop_name):
\t\traw = obj.get_meta(prop_name, fallback)
\treturn fallback if raw == null else raw
""".strip(),
            """
func _set_prop(obj, prop_name: String, value) -> void:
\tif obj == null or prop_name == "":
\t\treturn
\tobj.set(prop_name, value)
""".strip(),
            """
func _angle_radians(raw_angle: float) -> float:
\treturn deg_to_rad(raw_angle) if absf(raw_angle) > TAU * 2.0 else raw_angle
""".strip(),
            """
func _normalize_angle(value: float) -> float:
\tvar angle: float = value
\twhile angle > PI:
\t\tangle -= TAU
\twhile angle < -PI:
\t\tangle += TAU
\treturn angle
""".strip(),
            f"""
func _checkpoint_points() -> Array[Vector2]:
\tvar points: Array[Vector2] = []
\tvar stages: Array = entity_pools.get("{stage_pool}", [])
\tif stages.is_empty():
\t\treturn points
\tvar stage: Variant = stages[0]
\tif stage == null:
\t\treturn points
\tvar raw: Variant = stage.get("checkpoint_positions")
\tif (not (raw is Array) or (raw as Array).is_empty()) and stage.has_meta("checkpoint_positions"):
\t\traw = stage.get_meta("checkpoint_positions", [])
\tif raw is Array:
\t\tfor entry in raw:
\t\t\tif entry is Vector2:
\t\t\t\tpoints.append(entry)
\t\t\telif entry is Array and entry.size() >= 2:
\t\t\t\tpoints.append(Vector2(float(entry[0]), float(entry[1])))
\treturn points
""".strip(),
            """
func _player_actor():
\tfor actor in actors:
\t\tif actor != null and not bool(_entity_value(actor, AI_PROP, false)):
\t\t\treturn actor
\treturn actors[0] if not actors.is_empty() else null
""".strip(),
            """
func _race_state() -> String:
\tvar managers: Array = entity_pools.get("race_managers", [])
\tif not managers.is_empty():
\t\tvar manager = managers[0]
\t\tif manager != null:
\t\t\tvar raw_state = manager.get("current_state")
\t\t\tif raw_state != null:
\t\t\t\treturn str(raw_state).to_lower()
\treturn ""
""".strip(),
            """
func _gameplay_locked() -> bool:
\tvar state: String = _race_state()
\treturn state in ["menu", "character_select", "track_select", "countdown", "s_menu", "s_character_select", "s_track_select", "s_countdown"]
""".strip(),
            """
func _trace_state(actor, target_waypoint: int, steer_value: float, accel_value: bool, item_value: bool, rubber_mult: float) -> void:
\tvar trace_key: String = str(actor.name)
\tvar trace_value: String = "%d|%.2f|%s|%s|%.2f" % [target_waypoint, steer_value, str(accel_value), str(item_value), rubber_mult]
\tif _last_trace_state.get(trace_key, "") == trace_value:
\t\treturn
\t_last_trace_state[trace_key] = trace_value
\tprint("[trace] ai_navigation.tick kart=%s waypoint=%d steer=%.2f accel=%s item=%s rubber=%.2f" % [actor.name, target_waypoint, steer_value, str(accel_value), str(item_value), rubber_mult])
""".strip(),
        ],
        process_body=[
            "\tvar player = _player_actor()",
            "\tvar checkpoints: Array[Vector2] = _checkpoint_points()",
            "\tif _gameplay_locked() or player == null or checkpoints.is_empty():",
            "\t\treturn",
            '\tvar player_pos: Vector2 = Vector2(float(player.position.x), float(player.position.y))',
            "\tfor actor in actors:",
            "\t\tif actor == null or not bool(_entity_value(actor, AI_PROP, false)):",
            "\t\t\tcontinue",
            '\t\tvar target_waypoint: int = posmod(int(_entity_value(actor, WAYPOINT_PROP, 0)), checkpoints.size())',
            "\t\tvar target_point: Vector2 = checkpoints[target_waypoint]",
            '\t\tif actor.position.distance_to(target_point) <= self.ai_waypoint_threshold:',
            "\t\t\ttarget_waypoint = (target_waypoint + 1) % checkpoints.size()",
            '\t\t\t_set_prop(actor, WAYPOINT_PROP, target_waypoint)',
            '\t\t\tprint("[trace] ai_navigation.waypoint kart=%s target=%d" % [actor.name, target_waypoint])',
            '\t\tvar to_target: Vector2 = target_point - actor.position',
            '\t\tvar target_angle: float = atan2(to_target.y, to_target.x)',
            '\t\tvar current_angle: float = _angle_radians(float(_entity_value(actor, ANGLE_PROP, 0.0)))',
            '\t\tvar steer_value: float = clampf(_normalize_angle(target_angle - current_angle) / PI, -1.0, 1.0)',
            '\t\tvar accel_value: bool = not bool(_entity_value(actor, "is_spinning_out", false))',
            '\t\tvar dist_to_player: float = actor.position.distance_to(player_pos)',
            '\t\tvar rubber_mult: float = clampf(1.0 + (dist_to_player / max(self.rubber_band_distance_far, 1.0)) * (self.rubber_band_max_mult - 1.0), 1.0, self.rubber_band_max_mult)',
            '\t\tvar item_timer: float = max(float(_entity_value(actor, AI_TIMER_PROP, randf_range(self.ai_item_use_min_delay, self.ai_item_use_max_delay))) - _delta, 0.0)',
            "\t\tvar item_value: bool = false",
            "\t\tif item_timer <= 0.0 and str(_entity_value(actor, \"current_item\", \"\")).strip_edges() != \"\":",
            "\t\t\titem_value = true",
            "\t\t\titem_timer = randf_range(self.ai_item_use_min_delay, self.ai_item_use_max_delay)",
            '\t\t_set_prop(actor, STEER_PROP, steer_value)',
            '\t\t_set_prop(actor, ACCEL_PROP, 1.0 if accel_value else 0.0)',
            '\t\t_set_prop(actor, ITEM_PROP, item_value)',
            '\t\t_set_prop(actor, AI_MULT_PROP, rubber_mult)',
            '\t\t_set_prop(actor, AI_TIMER_PROP, item_timer)',
            '\t\t_trace_state(actor, target_waypoint, steer_value, accel_value, item_value, rubber_mult)',
        ],
        round_start_body=[
            "\t_last_trace_state.clear()",
            "\tfor actor in actors:",
            "\t\tif actor == null or not bool(_entity_value(actor, AI_PROP, false)):",
            "\t\t\tcontinue",
            '\t\t_set_prop(actor, WAYPOINT_PROP, 0)',
            '\t\t_set_prop(actor, STEER_PROP, 0.0)',
            '\t\t_set_prop(actor, ACCEL_PROP, 0.0)',
            '\t\t_set_prop(actor, ITEM_PROP, false)',
            '\t\t_set_prop(actor, AI_MULT_PROP, 1.0)',
            '\t\t_set_prop(actor, AI_TIMER_PROP, randf_range(self.ai_item_use_min_delay, self.ai_item_use_max_delay))',
        ],
    )


def _specialized_camera_tracking_system(
    reads: list[str],
    constants: dict[str, Any],
    context: dict[str, str | None],
    slice_data: dict,
    system_name: str = "camera_tracking_system",
) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    camera_pool = str(context.get("camera_pool") or "cameras")
    actor_owner = actor_pool[:-1] if actor_pool.endswith("s") else "kart"
    camera_owner = camera_pool[:-1] if camera_pool.endswith("s") else "camera"
    angle_prop = resolve_property_name(slice_data, actor_owner, ("facing_angle", "angle", "heading_degrees"), default="facing_angle")
    speed_prop = resolve_property_name(slice_data, actor_owner, ("speed", "forward_speed"), default="speed")
    camera_x_prop = resolve_property_name(slice_data, camera_owner, ("world_x",), default="world_x")
    camera_y_prop = resolve_property_name(slice_data, camera_owner, ("world_y",), default="world_y")
    camera_angle_prop = resolve_property_name(slice_data, camera_owner, ("angle",), default="angle")
    follow_distance = float(_constant_scalar(constants, ("camera_follow_distance", "camera_distance_behind"), 120.0))
    camera_height = float(_constant_scalar(constants, ("camera_height", "camera_height_offset"), 60.0))
    focal_length = float(_constant_scalar(constants, ("focal_length",), 256.0))
    lookahead_factor = float(_constant_scalar(constants, ("lookahead_speed_factor",), 0.75))
    max_lookahead = float(_constant_scalar(constants, ("max_lookahead_distance",), 160.0))
    extra_fields = ['var _last_trace_state: String = ""']
    follow_distance_field = "camera_follow_distance" if "camera_follow_distance" in constants else "camera_distance_behind"
    camera_height_field = "camera_height" if "camera_height" in constants else "camera_height_offset" if "camera_height_offset" in constants else "camera_height"
    if follow_distance_field not in constants:
        extra_fields.append(f"@export var {follow_distance_field}: float = {follow_distance}")
    if camera_height_field not in constants:
        extra_fields.append(f"@export var {camera_height_field}: float = {camera_height}")
    if "focal_length" not in constants:
        extra_fields.append(f"@export var focal_length: float = {focal_length}")
    if "lookahead_speed_factor" not in constants:
        extra_fields.append(f"@export var lookahead_speed_factor: float = {lookahead_factor}")
    if "max_lookahead_distance" not in constants:
        extra_fields.append(f"@export var max_lookahead_distance: float = {max_lookahead}")
    return _specialized_system_source(
        system_name,
        reads,
        constants,
        primary_pool_var="cameras",
        primary_pool_key=camera_pool,
        extra_fields=extra_fields,
        helper_blocks=[
            """
const ANGLE_PROP := "%s"
const SPEED_PROP := "%s"
""" % (angle_prop, speed_prop),
            """
func _entity_value(obj, prop_name: String, fallback):
\tif obj == null or prop_name == "":
\t\treturn fallback
\tvar raw = obj.get(prop_name)
\treturn fallback if raw == null else raw
""".strip(),
            """
func _angle_radians(raw_angle: float) -> float:
\treturn deg_to_rad(raw_angle) if absf(raw_angle) > TAU * 2.0 else raw_angle
""".strip(),
            """
func _heading(raw_angle: float) -> Vector2:
\tvar radians: float = _angle_radians(raw_angle)
\treturn Vector2(cos(radians), sin(radians))
""".strip(),
            """
func _primary_actor(actors: Array):
\tfor actor in actors:
\t\tif actor != null and not bool(_entity_value(actor, "is_ai_controlled", false)):
\t\t\treturn actor
\treturn actors[0] if not actors.is_empty() else null
""".strip(),
        ],
        process_body=[
            f'\tvar karts: Array = entity_pools.get("{actor_pool}", [])',
            "\tif karts.is_empty() or cameras.is_empty():",
            "\t\treturn",
            '\tvar kart = _primary_actor(karts)',
            '\tvar camera = cameras[0]',
            "\tif kart == null or camera == null:",
            "\t\treturn",
            '\tvar kart_pos: Vector2 = Vector2(float(kart.position.x), float(kart.position.y))',
            '\tvar facing_angle: float = float(_entity_value(kart, ANGLE_PROP, 0.0))',
            '\tvar speed: float = max(float(_entity_value(kart, SPEED_PROP, 0.0)), 0.0)',
            f"\tcamera.height = self.{camera_height_field}",
            "\tcamera.focal_length = self.focal_length",
            '\tvar heading: Vector2 = _heading(facing_angle)',
            '\tvar lookahead: float = clampf(speed * self.lookahead_speed_factor, 0.0, self.max_lookahead_distance)',
            "\tcamera.lookahead_distance = lookahead",
            '\tvar camera_angle: float = _angle_radians(facing_angle)',
            f"\tvar camera_pos: Vector2 = kart_pos - heading * self.{follow_distance_field} + heading * lookahead",
            "\tcamera.position = camera_pos",
            f'\tif camera.get("{camera_x_prop}") != null:',
            f'\t\tcamera.set("{camera_x_prop}", camera_pos.x)',
            f'\tif camera.get("{camera_y_prop}") != null:',
            f'\t\tcamera.set("{camera_y_prop}", camera_pos.y)',
            f'\tif camera.get("{camera_angle_prop}") != null:',
            f'\t\tcamera.set("{camera_angle_prop}", camera_angle)',
            "\tcamera.angle = camera_angle",
            '\tvar trace_value: String = "%s|%s|%.2f" % [str(camera.position), str(camera.angle), lookahead]',
            "\tif _last_trace_state != trace_value:",
            "\t\t_last_trace_state = trace_value",
            '\t\tprint("[trace] camera.update position=%s angle=%.4f target_slot=0" % [str(camera.position), float(camera.angle)])',
        ],
        round_start_body=[
            '\tvar actors: Array = entity_pools.get("%s", [])' % actor_pool,
            '\tvar primary_actor = _primary_actor(actors)',
            "\tfor camera in cameras:",
            "\t\tif camera == null:",
            "\t\t\tcontinue",
            f"\t\tcamera.height = self.{camera_height_field}",
            "\t\tcamera.focal_length = self.focal_length",
            "\t\tcamera.lookahead_distance = 0.0",
            "\t\tif primary_actor != null:",
            "\t\t\tcamera.position = primary_actor.position",
        ],
    )


def _specialized_race_hud_system(
    reads: list[str],
    constants: dict[str, Any],
    context: dict[str, str | None],
    slice_data: dict,
    system_name: str = "hud_system",
) -> str:
    actor_pool = str(context.get("actor_pool") or "karts")
    hud_pool = str(context.get("hud_pool") or "hud_bars")
    actor_owner = actor_pool[:-1] if actor_pool.endswith("s") else "kart"
    speed_prop = resolve_property_name(slice_data, actor_owner, ("speed", "forward_speed"), default="speed")
    lap_prop = resolve_property_name(slice_data, actor_owner, ("current_lap",), default="current_lap")
    rank_prop = resolve_property_name(slice_data, actor_owner, ("race_position", "position_rank"), default="race_position")
    item_prop = resolve_property_name(slice_data, actor_owner, ("current_item", "held_item"), default="current_item")
    drift_prop = resolve_property_name(slice_data, actor_owner, ("is_drifting",), default="is_drifting")
    drift_charge_prop = resolve_property_name(slice_data, actor_owner, ("drift_charge",), default="drift_charge")
    spin_prop = resolve_property_name(slice_data, actor_owner, ("is_spinning_out",), default="is_spinning_out")
    return _specialized_system_source(
        system_name,
        reads,
        constants,
        primary_pool_var="hud_bars",
        primary_pool_key=hud_pool,
        extra_fields=['var _last_layout: String = ""', 'var _last_values: String = ""'],
        helper_blocks=[
            f"""
const SPEED_PROP := "{speed_prop}"
const LAP_PROP := "{lap_prop}"
const RANK_PROP := "{rank_prop}"
const ITEM_PROP := "{item_prop}"
const DRIFT_PROP := "{drift_prop}"
const DRIFT_CHARGE_PROP := "{drift_charge_prop}"
const SPIN_PROP := "{spin_prop}"
""".strip(),
            """
func _entity_value(obj, prop_name: String, fallback):
\tif obj == null or prop_name == "":
\t\treturn fallback
\tvar raw = obj.get(prop_name)
\treturn fallback if raw == null else raw
""".strip(),
            """
func _race_manager():
\tvar managers: Array = entity_pools.get("race_managers", [])
\treturn managers[0] if not managers.is_empty() else null
""".strip(),
            """
func _player_actor():
\tvar actors: Array = entity_pools.get("player_karts", [])
\tif not actors.is_empty() and actors[0] != null:
\t\treturn actors[0]
\tactors = entity_pools.get("karts", [])
\tfor actor in actors:
\t\tif actor != null and not bool(_entity_value(actor, "is_ai_controlled", false)):
\t\t\treturn actor
\treturn actors[0] if not actors.is_empty() else null
""".strip(),
        ],
        process_body=[
            "\tvar race_manager = _race_manager()",
            '\tvar state: String = str(_entity_value(race_manager, "current_state", "racing")).to_lower()',
            '\tvar target_layout: String = "racing"',
            '\tif state in ["menu", "character_select", "track_select"]:',
            '\t\ttarget_layout = "menu"',
            '\telif state in ["countdown", "s_countdown"]:',
            '\t\ttarget_layout = "countdown"',
            '\telif state in ["finished", "podium", "s_finished", "s_podium"]:',
            '\t\ttarget_layout = "finished"',
            '\tif _last_layout != target_layout:',
            '\t\t_last_layout = target_layout',
            '\t\tfor hud_bar in hud_bars:',
            '\t\t\tif hud_bar != null and hud_bar.get("layout_mode") != null:',
            '\t\t\t\thud_bar.set("layout_mode", target_layout)',
            '\t\tprint("[trace] hud.layout_change layout=%s state=%s" % [target_layout, state])',
            "\tvar display_actor = _player_actor()",
            "\tif display_actor == null:",
            "\t\treturn",
            '\tvar speed: float = max(float(_entity_value(display_actor, SPEED_PROP, 0.0)), 0.0)',
            '\tvar lap_value: int = max(int(_entity_value(display_actor, LAP_PROP, 1)), 1)',
            '\tvar rank_value: int = max(int(_entity_value(display_actor, RANK_PROP, 1)), 1)',
            '\tvar item_value: String = str(_entity_value(display_actor, ITEM_PROP, ""))',
            '\tvar drifting: bool = bool(_entity_value(display_actor, DRIFT_PROP, false))',
            '\tvar drift_charge: float = float(_entity_value(display_actor, DRIFT_CHARGE_PROP, 0.0))',
            '\tvar spinning: bool = bool(_entity_value(display_actor, SPIN_PROP, false))',
            '\tfor hud_bar in hud_bars:',
            '\t\tif hud_bar == null:',
            '\t\t\tcontinue',
            '\t\thud_bar.visible = target_layout != "menu"',
            '\t\tif hud_bar.get("speed_display_value") != null:',
            '\t\t\thud_bar.speed_display_value = int(round(speed))',
            '\t\tif hud_bar.get("lap_counter_value") != null:',
            '\t\t\thud_bar.lap_counter_value = lap_value',
            '\t\tif hud_bar.get("position_display") != null:',
            '\t\t\thud_bar.position_display = rank_value',
            '\t\tif hud_bar.get("drift_meter_fill") != null:',
            '\t\t\thud_bar.drift_meter_fill = clampf(drift_charge, 0.0, 1.0)',
            '\t\tif hud_bar.get("spin_out_active") != null:',
            '\t\t\thud_bar.spin_out_active = spinning',
            '\t\tif hud_bar.get("mini_map_scale") != null:',
            '\t\t\thud_bar.mini_map_scale = 1.0',
            '\tvar trace_value: String = "%.1f|%d|%d|%s|%s|%.2f|%s" % [speed, lap_value, rank_value, item_value, str(drifting), drift_charge, str(spinning)]',
            '\tif _last_values != trace_value:',
            '\t\t_last_values = trace_value',
            '\t\tprint("[trace] hud.update_values speed=%.1f item=\\"%s\\" drifting=%s charge=%.2f lap=%d position=%d" % [speed, item_value, str(drifting), drift_charge, lap_value, rank_value])',
        ],
        round_start_body=[
            '\t_last_layout = ""',
            '\t_last_values = ""',
        ],
    )


def _generate_specialized_system(
    system_name: str,
    reads: list[str],
    constants: dict[str, Any],
    slice_data: dict,
    role_groups: dict[str, list[str]] | None = None,
    capabilities: dict[str, bool] | None = None,
) -> str | None:
    _capabilities = capabilities or {}
    context = _specialized_role_context(slice_data, role_groups)
    if system_name in {"player_input_system", "input_system"}:
        return _specialized_player_input_system(reads, constants, context, slice_data, system_name=system_name)
    if system_name == "countdown_system":
        return _specialized_countdown_system(reads, constants, system_name=system_name)
    if system_name == "item_system":
        return _specialized_item_system(reads, constants, context, slice_data)
    if system_name in {"vehicle_movement_system", "locomotion_system"}:
        return _specialized_vehicle_movement_system(reads, constants, context, slice_data, system_name=system_name)
    if system_name == "physics_system" and bool(_capabilities.get("checkpoint_race")):
        return _specialized_vehicle_movement_system(reads, constants, context, slice_data, system_name=system_name)
    if system_name in {"ai_navigation_system", "ai_system"} and bool(_capabilities.get("checkpoint_race")):
        return _specialized_ai_navigation_system(reads, constants, context, slice_data, system_name=system_name)
    if system_name == "drift_boost_system":
        return _specialized_drift_boost_system(reads, constants, context, slice_data)
    if system_name == "item_box_system":
        return _specialized_item_box_system(reads, constants, context)
    if system_name == "item_usage_system":
        return _specialized_item_usage_system(reads, constants, context)
    if system_name == "collision_resolution_system":
        return _specialized_collision_resolution_system(reads, constants, context)
    if system_name == "collision_system" and bool(_capabilities.get("checkpoint_race")):
        return _specialized_collision_resolution_system(reads, constants, context)
    if system_name == "race_progress_system":
        return _specialized_race_progress_system(reads, constants, context)
    if system_name == "position_ranking_system":
        return _specialized_position_ranking_system(reads, constants, context)
    if system_name == "camera_tracking_system":
        return _specialized_camera_tracking_system(reads, constants, context, slice_data, system_name=system_name)
    if system_name == "camera_system" and bool(_capabilities.get("checkpoint_race")):
        return _specialized_camera_tracking_system(reads, constants, context, slice_data, system_name=system_name)
    if system_name == "hud_system" and bool(_capabilities.get("checkpoint_race")):
        return _specialized_race_hud_system(reads, constants, context, slice_data, system_name=system_name)
    return None


def generate_system_gdscript(
    system_name: str,
    imap: ImpactMap,
    constants: dict[str, Any] | None = None,
    role_groups: dict[str, list[str]] | None = None,
    capabilities: dict[str, bool] | None = None,
) -> str:
    slice_data = imap.slice_for_system(system_name)
    constants = constants or {}
    reads = [edge["source"] for edge in slice_data.get("own_reads", [])]
    specialized = _generate_specialized_system(
        system_name,
        reads,
        constants,
        slice_data,
        role_groups=role_groups,
        capabilities=capabilities,
    )
    if specialized is not None:
        return specialized
    writes = [WriteEdge(**edge) for edge in slice_data.get("own_writes", [])]
    reads = slice_data.get("own_reads", [])
    buckets = _partition_writes(writes)
    primary_owner = _primary_owner(slice_data)
    primary_pool = _pool_name_for_owner(primary_owner)
    singleton_lines, owner_vars = _singleton_owner_lines(slice_data, primary_owner)

    parts = [
        "extends Node",
        f"## {system_name} — generated from impact map by mechanic_gen",
        f"## Reads: {sorted({edge['source'] for edge in reads})}",
        "",
        "var entity_pools: Dictionary = {}",
        "var config: Dictionary = {}",
        "var sibling_systems: Dictionary = {}",
        f"var {primary_pool}: Array = []",
        "",
    ]
    const_block = _gd_constants_block(constants)
    if const_block:
        parts.append(const_block)
        parts.append("")

    parts.extend(
        [
            "func setup(pools: Dictionary, cfg: Dictionary = {}) -> void:",
            "\tentity_pools = pools",
            "\tconfig = cfg",
            f'\t{primary_pool} = pools.get("{primary_pool}", [])',
            "\ton_config_init()",
            "",
            "func set_siblings(systems: Dictionary) -> void:",
            "\tsibling_systems = systems",
            "",
            "func process(_delta: float) -> void:",
        ]
    )
    for line in singleton_lines:
        parts.append("\t" + line)
    parts.append(f"\tfor {primary_owner} in {primary_pool}:")
    parts.append(f"\t\tif {primary_owner} == null:")
    parts.append("\t\t\tcontinue")
    if not buckets["process"]:
        parts.append("\t\tpass")
    else:
        for edge in buckets["process"]:
            for line in _emit_write_stmt(edge, owner_vars, constants):
                parts.append(f"\t\t{line}")
    parts.append("")

    if buckets["damage_event"]:
        parts.append(f"func process_damage_event({primary_owner}, damage_taken: float) -> void:")
        for edge in buckets["damage_event"]:
            for line in _emit_write_stmt(edge, owner_vars, constants):
                parts.append(f"\t{line}")
        parts.append("")

    if buckets["special_event"]:
        parts.append(f"func process_special_event({primary_owner}, move_name: String) -> void:")
        for edge in buckets["special_event"]:
            for line in _emit_write_stmt(edge, owner_vars, constants):
                parts.append(f"\t{line}")
        parts.append("")

    parts.append("func process_round_start() -> void:")
    parts.append(f"\tfor {primary_owner} in {primary_pool}:")
    parts.append(f"\t\tif {primary_owner} == null:")
    parts.append("\t\t\tcontinue")
    if not buckets["round_start"]:
        parts.append("\t\tpass")
    else:
        for edge in buckets["round_start"]:
            for line in _emit_write_stmt(edge, owner_vars, constants):
                parts.append(f"\t\t{line}")
    parts.append("")

    parts.append("func on_config_init() -> void:")
    parts.append(f"\tfor {primary_owner} in {primary_pool}:")
    parts.append(f"\t\tif {primary_owner} == null:")
    parts.append("\t\t\tcontinue")
    if not buckets["config_init"]:
        parts.append("\t\tpass")
    else:
        for edge in buckets["config_init"]:
            for line in _emit_write_stmt(edge, owner_vars, constants):
                parts.append(f"\t\t{line}")
    parts.append("")
    return "\n".join(parts) + "\n"


def generate_custom_systems(
    impact_map_path: Path,
    mechanic_constants_path: Path | None,
    output_scripts_dir: Path,
) -> list[Path]:
    import json

    imap = ImpactMap.model_validate_json(impact_map_path.read_text())
    mechanic_systems: set[str] = set()
    for edge in imap.write_edges:
        if edge.declared_by.startswith("hlr_seed:") and ":hud" not in edge.declared_by:
            mechanic_systems.add(edge.declared_by.split(":", 1)[1])

    constants_by_system: dict[str, dict] = {}
    if mechanic_constants_path and mechanic_constants_path.exists():
        raw = json.loads(mechanic_constants_path.read_text())
        for system_name in raw:
            constants_by_system[system_name] = _flatten_constants_for_system(raw, system_name)

    output_scripts_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for system_name in sorted(mechanic_systems):
        gd = generate_system_gdscript(system_name, imap, constants_by_system.get(system_name, {}))
        out = output_scripts_dir / f"{system_name}.gd"
        out.write_text(gd, encoding="utf-8")
        written.append(out)
        _log.info("mechanic_gen: wrote %s (%d bytes)", out, len(gd))
    return written
