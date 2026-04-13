"""character_gen - emit req-owned runtime entity scripts from the impact map.

This module now handles both fighter character scripts and generic runtime-role
scripts (projectiles, HUD bars, etc.). The emitted scripts are deterministic and
pull their declarations, initial values, and locally-computable derived values
from the impact map instead of guessing from type alone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rayxi.spec.expr import BinOpExpr, CondExpr, Expr, FnCallExpr, LiteralExpr, RefExpr, expr_refs
from rayxi.spec.impact_map import Category, ImpactMap, PropertyNode

_log = logging.getLogger("rayxi.build.character_gen")

_GENERIC_NATIVE_MEMBERS = frozenset({
    "name",
    "owner",
    "script",
    "visible",
    "process_mode",
    "process_priority",
    "process_physics_priority",
    "unique_name_in_owner",
})
_NODE2D_NATIVE_MEMBERS = frozenset({
    "position",
    "global_position",
    "rotation",
    "rotation_degrees",
    "scale",
    "skew",
    "transform",
    "global_transform",
    "z_index",
    "modulate",
    "self_modulate",
})
_CHARACTER_BODY_NATIVE_MEMBERS = frozenset({
    "velocity",
    "floor_max_angle",
    "floor_snap_length",
    "floor_stop_on_slope",
    "floor_constant_speed",
    "floor_block_on_wall",
    "platform_on_leave",
    "platform_floor_layers",
    "platform_wall_layers",
    "wall_min_slide_angle",
    "slide_on_ceiling",
    "up_direction",
    "motion_mode",
    "collision_layer",
    "collision_mask",
    "collision_priority",
})
_CONTROL_NATIVE_MEMBERS = frozenset({
    "size",
    "position",
    "scale",
    "rotation",
    "layout_mode",
    "pivot_offset",
    "mouse_filter",
    "focus_mode",
    "custom_minimum_size",
    "tooltip_text",
})
_PROGRESS_BAR_NATIVE_MEMBERS = frozenset({
    "value",
    "min_value",
    "max_value",
    "show_percentage",
    "step",
})
_LABEL_NATIVE_MEMBERS = frozenset({
    "text",
    "horizontal_alignment",
    "vertical_alignment",
    "autowrap_mode",
})

_STRONG_TYPES = {
    "int": ("int", "0"),
    "integer": ("int", "0"),
    "float": ("float", "0.0"),
    "number": ("float", "0.0"),
    "bool": ("bool", "false"),
    "boolean": ("bool", "false"),
    "string": ("String", '""'),
    "str": ("String", '""'),
    "vector2": ("Vector2", "Vector2.ZERO"),
    "color": ("Color", "Color.WHITE"),
    "rect2": ("Rect2", "Rect2()"),
}
_COLLECTION_TYPES = {
    "dictionary": "{}",
    "dict": "{}",
    "array": "[]",
    "list": "[]",
    "object": "null",
    "variant": "null",
}
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
_ENUM_SENTINELS = ("none", "idle", "off", "inactive", "unset", "default")
_SCENE_EDITABLE_PROPS = frozenset({"is_ai_controlled", "is_cpu", "player_slot"})
_VEHICLE_ROLE_TOKENS = ("kart", "vehicle", "car", "racer", "driver", "bike", "ship")
_VEHICLE_RUNTIME_FALLBACKS = (
    ("speed", "float", "0.0"),
    ("facing_angle", "float", "0.0"),
    ("brake_input", "float", "0.0"),
    ("collision_radius", "float", "56.0"),
    ("is_offroad", "bool", "false"),
)


def _native_members_for(base_node: str) -> set[str]:
    members = set(_GENERIC_NATIVE_MEMBERS)
    if base_node in {"Node2D", "CharacterBody2D", "Area2D", "Sprite2D"}:
        members.update(_NODE2D_NATIVE_MEMBERS)
    if base_node == "CharacterBody2D":
        members.update(_CHARACTER_BODY_NATIVE_MEMBERS)
    if base_node in {"Control", "ProgressBar", "Label", "ColorRect", "TextureRect"}:
        members.update(_CONTROL_NATIVE_MEMBERS)
    if base_node == "ProgressBar":
        members.update(_PROGRESS_BAR_NATIVE_MEMBERS)
    if base_node == "Label":
        members.update(_LABEL_NATIVE_MEMBERS)
    return members


def _category_priority(node: PropertyNode) -> int:
    if node.category == Category.CONFIG:
        return 0
    if node.category == Category.STATE:
        return 1
    return 2


def _enum_default(node: PropertyNode) -> str | None:
    if not node.enum_values:
        return None
    sentinel = next((v for v in node.enum_values if v.lower() in _ENUM_SENTINELS), None)
    if sentinel is not None:
        return json.dumps(sentinel)
    return json.dumps(node.enum_values[0])


def _rect_dict_literal(expr: Expr | None) -> dict[str, Any] | None:
    if not isinstance(expr, LiteralExpr):
        return None
    value = expr.value
    if not isinstance(value, dict):
        return None
    lowered = {str(key).lower(): raw for key, raw in value.items()}
    if not {"x", "y", "w", "h"}.issubset(lowered.keys()):
        return None
    return lowered


def _rect_dict_to_gd(value: dict[str, Any]) -> str:
    x_expr = _python_value_to_gd(value.get("x", 0.0))
    y_expr = _python_value_to_gd(value.get("y", 0.0))
    w_expr = _python_value_to_gd(value.get("w", 0.0))
    h_expr = _python_value_to_gd(value.get("h", 0.0))
    return "Rect2((%s) - ((%s) * 0.5), (%s) - (%s), %s, %s)" % (
        x_expr,
        w_expr,
        y_expr,
        h_expr,
        w_expr,
        h_expr,
    )


def _rect_property_literal(node: PropertyNode) -> dict[str, Any] | None:
    return _rect_dict_literal(node.initial_value) or _rect_dict_literal(node.derivation)


def _owners_match_tokens(owners: list[str], tokens: tuple[str, ...]) -> bool:
    owner_text = " ".join(owners).lower()
    return any(token in owner_text for token in tokens)


def _compatibility_declarations(
    owners: list[str],
    godot_base_node: str,
    local_names: set[str],
    native_members: set[str],
) -> list[str]:
    declarations: list[str] = []
    if godot_base_node == "CharacterBody2D" and _owners_match_tokens(owners, _VEHICLE_ROLE_TOKENS):
        for prop_name, gd_type, default_expr in _VEHICLE_RUNTIME_FALLBACKS:
            if prop_name in local_names or prop_name in native_members:
                continue
            local_names.add(prop_name)
            declarations.append(f"var {prop_name}: {gd_type} = {default_expr}  # runtime compatibility")
    return declarations


def _declaration_line(node: PropertyNode, native_members: set[str]) -> str | None:
    if node.name in native_members:
        return None

    decl_keyword = "@export var" if (node.category == Category.CONFIG or node.name in _SCENE_EDITABLE_PROPS) else "var"
    type_name = (node.type or "").lower().strip()
    rect_literal = _rect_property_literal(node)
    if type_name in ("string", "str") and node.enum_values:
        default_literal = _enum_default(node) or '""'
        suffix = "  # enum"
        if node.category == Category.DERIVED:
            suffix = "  # derived enum"
        return f"{decl_keyword} {node.name}: String = {default_literal}{suffix}"

    if rect_literal is not None:
        suffix = "  # derived" if node.category == Category.DERIVED else ""
        return f"{decl_keyword} {node.name}: Rect2 = Rect2(){suffix}"

    if type_name in _STRONG_TYPES:
        gd_type, default = _STRONG_TYPES[type_name]
        suffix = "  # derived" if node.category == Category.DERIVED else ""
        return f"{decl_keyword} {node.name}: {gd_type} = {default}{suffix}"

    if type_name in _COLLECTION_TYPES:
        default = _COLLECTION_TYPES[type_name]
        suffix = "  # derived/untyped" if node.category == Category.DERIVED else "  # untyped (accepts null)"
        return f"{decl_keyword} {node.name} = {default}{suffix}"

    suffix = f"  # type={type_name or '?'}"
    if node.category == Category.DERIVED:
        suffix = f"  # derived type={type_name or '?'}"
    return f"{decl_keyword} {node.name}: Variant = null{suffix}"


def _literal_to_gd(type_name: str, value: Any) -> str:
    t = (type_name or "").lower()
    if t == "bool":
        return "true" if value else "false"
    if t == "string":
        return json.dumps("" if value is None else str(value))
    if t == "int":
        return str(int(value))
    if t == "float":
        return repr(float(value))
    if t == "vector2":
        if isinstance(value, list) and len(value) >= 2:
            return f"Vector2({value[0]}, {value[1]})"
        return "Vector2.ZERO"
    if t == "color":
        if isinstance(value, str):
            return f'Color("{value}")'
        return "Color.WHITE"
    if t == "rect2":
        if isinstance(value, list) and len(value) >= 4:
            return f"Rect2({value[0]}, {value[1]}, {value[2]}, {value[3]})"
        return "Rect2()"
    if t == "list":
        return "[" + ", ".join(_python_value_to_gd(v) for v in (value or [])) + "]"
    if t == "dict":
        if not isinstance(value, dict):
            return "{}"
        pairs = [f"{json.dumps(str(k))}: {_python_value_to_gd(v)}" for k, v in value.items()]
        return "{" + ", ".join(pairs) + "}"
    return _python_value_to_gd(value)


def _python_value_to_gd(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_python_value_to_gd(v) for v in value) + "]"
    if isinstance(value, dict):
        pairs = [f"{json.dumps(str(k))}: {_python_value_to_gd(v)}" for k, v in value.items()]
        return "{" + ", ".join(pairs) + "}"
    if value is None:
        return "null"
    return json.dumps(value, default=str)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _flatten_constant_values(constants: dict[str, Any] | None) -> dict[str, Any]:
    if not constants:
        return {}
    grouped: dict[str, list[Any]] = {}
    for bucket in constants.values():
        if not isinstance(bucket, dict):
            continue
        for name, value in bucket.items():
            grouped.setdefault(name, []).append(value)
    resolved: dict[str, Any] = {}
    for name, values in grouped.items():
        if not values:
            continue
        unique = {_stable_json(v) for v in values}
        if len(unique) == 1:
            resolved[name] = values[0]
    return resolved


def _owner_prefixes(owners: list[str]) -> list[str]:
    prefixes: set[str] = set()
    for owner in owners:
        if not owner:
            continue
        prefixes.add(owner)
        head = owner.split(".", 1)[0]
        prefixes.add(head)
    return sorted(prefixes, key=len, reverse=True)


def _local_ref_name(ref_path: str, owner_prefixes: list[str], local_names: set[str]) -> str | None:
    for prefix in owner_prefixes:
        dotted = prefix + "."
        if not ref_path.startswith(dotted):
            continue
        tail = ref_path[len(dotted):]
        if tail in local_names:
            return tail
    return None


def _expr_to_gd(
    expr: Expr | None,
    owners: list[str],
    local_names: set[str],
    const_values: dict[str, Any],
    expected_type: str | None = None,
) -> str | None:
    if expr is None:
        return None
    allowed_prefixes = _owner_prefixes(owners)
    if isinstance(expr, LiteralExpr):
        return _literal_to_gd(expr.type, expr.value)
    if isinstance(expr, RefExpr):
        if "." not in expr.path:
            return expr.path if expr.path in local_names else None
        head, tail = expr.path.split(".", 1)
        if head == "const":
            if tail in const_values:
                if expected_type:
                    return _literal_to_gd(expected_type, const_values[tail])
                return _python_value_to_gd(const_values[tail])
            if tail in local_names:
                return tail
            return None
        return _local_ref_name(expr.path, allowed_prefixes, local_names)
    if isinstance(expr, BinOpExpr):
        left = _expr_to_gd(expr.left, owners, local_names, const_values)
        right = _expr_to_gd(expr.right, owners, local_names, const_values)
        if left is None or right is None:
            return None
        return f"({left} {_BINOP_SYMBOLS.get(expr.op, expr.op)} {right})"
    if isinstance(expr, FnCallExpr):
        args = [_expr_to_gd(a, owners, local_names, const_values) for a in expr.args]
        if any(a is None for a in args):
            return None
        if expr.fn == "not":
            return f"(not {args[0]})"
        return f"{expr.fn}({', '.join(args)})"
    if isinstance(expr, CondExpr):
        condition = _expr_to_gd(expr.condition, owners, local_names, const_values)
        then_val = _expr_to_gd(expr.then_val, owners, local_names, const_values)
        else_val = _expr_to_gd(expr.else_val, owners, local_names, const_values)
        if condition is None or then_val is None or else_val is None:
            return None
        return f"({then_val} if {condition} else {else_val})"
    return None


def _local_dependencies(expr: Expr | None, owners: list[str], local_names: set[str]) -> set[str]:
    allowed_prefixes = _owner_prefixes(owners)
    deps: set[str] = set()
    for ref in expr_refs(expr):
        tail = _local_ref_name(ref, allowed_prefixes, local_names)
        if tail is not None:
            deps.add(tail)
    return deps


def _ordered_assignments(
    nodes: list[PropertyNode],
    owners: list[str],
    local_names: set[str],
    const_values: dict[str, Any],
    *,
    use_derivation: bool,
) -> tuple[list[tuple[PropertyNode, str]], list[str]]:
    node_map: dict[str, tuple[PropertyNode, str, set[str]]] = {}
    unresolved: list[str] = []
    for node in nodes:
        expr = node.derivation if use_derivation else node.initial_value
        if expr is None:
            continue
        rendered = _expr_to_gd(expr, owners, local_names, const_values, expected_type=node.type)
        if rendered is None:
            unresolved.append(node.name)
            continue
        deps = {d for d in _local_dependencies(expr, owners, local_names) if d != node.name}
        node_map[node.name] = (node, rendered, deps)

    ordered: list[tuple[PropertyNode, str]] = []
    remaining = {name: set(info[2]) for name, info in node_map.items()}
    while remaining:
        ready = [
            name for name, deps in remaining.items()
            if not deps or all(dep not in remaining for dep in deps)
        ]
        if not ready:
            ready = [sorted(remaining.keys(), key=lambda name: (_category_priority(node_map[name][0]), name))[0]]
        ready.sort(key=lambda name: (_category_priority(node_map[name][0]), name))
        for name in ready:
            node, rendered, _deps = node_map[name]
            ordered.append((node, rendered))
            remaining.pop(name, None)
            for deps in remaining.values():
                deps.discard(name)
    return ordered, unresolved


def _mode7_stage_block() -> str:
    return """
var _track_samples: Array = []
var _cached_checkpoint_signature: String = ""
var _mode7_announced: bool = false

func _scene_root() -> Node:
    return get_parent()

func _scene_pools() -> Dictionary:
    var root: Node = _scene_root()
    if root == null:
        return {}
    var pools: Variant = root.get("entity_pools")
    return pools if pools is Dictionary else {}

func _is_mode7_scene() -> bool:
    var pools: Dictionary = _scene_pools()
    var karts: Array = pools.get("karts", []) as Array
    return _checkpoint_points().size() >= 2 and not karts.is_empty()

func _checkpoint_points() -> Array[Vector2]:
    var points: Array[Vector2] = []
    var raw: Variant = get("checkpoint_positions")
    if raw is Array:
        for entry in raw:
            if entry is Vector2:
                points.append(entry)
            elif entry is Array and entry.size() >= 2:
                points.append(Vector2(float(entry[0]), float(entry[1])))
    return points

func _catmull_rom_point(p0: Vector2, p1: Vector2, p2: Vector2, p3: Vector2, t: float) -> Vector2:
    var t2: float = t * t
    var t3: float = t2 * t
    return 0.5 * (
        (2.0 * p1)
        + (-p0 + p2) * t
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
    )

func _rebuild_track_cache() -> void:
    var points: Array[Vector2] = _checkpoint_points()
    var signature: String = str(points)
    if signature == _cached_checkpoint_signature:
        return
    _cached_checkpoint_signature = signature
    _track_samples.clear()
    if points.size() < 2:
        return
    var samples_per_segment: int = 18
    for i in range(points.size()):
        var p0: Vector2 = points[(i - 1 + points.size()) % points.size()]
        var p1: Vector2 = points[i]
        var p2: Vector2 = points[(i + 1) % points.size()]
        var p3: Vector2 = points[(i + 2) % points.size()]
        for step in range(samples_per_segment):
            var t: float = float(step) / float(samples_per_segment)
            var pos: Vector2 = _catmull_rom_point(p0, p1, p2, p3, t)
            var next_t: float = min(t + (1.0 / float(samples_per_segment)), 1.0)
            var next_pos: Vector2 = _catmull_rom_point(p0, p1, p2, p3, next_t)
            var tangent: Vector2 = (next_pos - pos).normalized()
            if tangent.length() <= 0.001:
                tangent = (p2 - p1).normalized()
            if tangent.length() <= 0.001:
                tangent = Vector2.RIGHT
            _track_samples.append({
                "pos": pos,
                "tangent": tangent,
            })

func _forward(angle_deg: float) -> Vector2:
    var radians: float = deg_to_rad(angle_deg)
    return Vector2(cos(radians), sin(radians))

func _project_ground_point(world: Vector2, camera_pos: Vector2, camera_angle: float) -> Dictionary:
    var view_size: Vector2 = get_viewport_rect().size
    var forward: Vector2 = _forward(camera_angle)
    var right: Vector2 = Vector2(-forward.y, forward.x)
    var relative: Vector2 = world - camera_pos
    var local_z: float = relative.dot(forward)
    var local_x: float = relative.dot(right)
    if local_z <= 14.0 or local_z > 2200.0:
        return {"visible": false}
    var horizon_y: float = view_size.y * 0.30
    var ground_scale: float = view_size.y * 52.0
    var screen_x: float = view_size.x * 0.5 + (local_x / local_z) * view_size.x * 1.12
    var screen_y: float = horizon_y + ground_scale / local_z
    var scale: float = clampf((ground_scale / local_z) / 240.0, 0.05, 4.0)
    return {
        "visible": screen_y >= horizon_y - 24.0 and screen_y <= view_size.y + 140.0,
        "x": screen_x,
        "y": screen_y,
        "scale": scale,
        "depth": local_z,
        "lateral": local_x,
    }

func _quad(a: Vector2, b: Vector2, c: Vector2, d: Vector2) -> PackedVector2Array:
    return PackedVector2Array([a, b, c, d])

func _solid_colors(color: Color, count: int) -> PackedColorArray:
    var colors: PackedColorArray = PackedColorArray()
    for _i in range(count):
        colors.append(color)
    return colors

func _triangle_valid(a: Vector2, b: Vector2, c: Vector2) -> bool:
    return abs((b - a).cross(c - a)) > 1.0

func _draw_quad_fill(points: PackedVector2Array, color: Color) -> void:
    if points.size() != 4:
        return
    var a: Vector2 = points[0]
    var b: Vector2 = points[1]
    var c: Vector2 = points[2]
    var d: Vector2 = points[3]
    if _triangle_valid(a, b, c):
        draw_colored_polygon(PackedVector2Array([a, b, c]), color)
    if _triangle_valid(a, c, d):
        draw_colored_polygon(PackedVector2Array([a, c, d]), color)

func _draw_flat_track(points: Array[Vector2]) -> void:
    var lane_outer: Color = Color(0.18, 0.2, 0.24, 0.86)
    var lane_inner: Color = Color(0.08, 0.1, 0.12, 0.95)
    var center_line: Color = Color(0.92, 0.92, 0.82, 0.88)
    var checkpoint_color: Color = Color(0.25, 0.95, 0.45, 0.9)
    for i in range(points.size()):
        var a: Vector2 = points[i]
        var b: Vector2 = points[(i + 1) % points.size()]
        draw_line(a, b, lane_outer, 168.0)
        draw_line(a, b, lane_inner, 118.0)
        draw_line(a, b, center_line, 6.0)
    for checkpoint in points:
        draw_circle(checkpoint, 22.0, checkpoint_color)
        draw_circle(checkpoint, 10.0, Color(0.04, 0.05, 0.06, 0.9))

func _sync_mode7_visibility() -> void:
    if not _is_mode7_scene():
        return
    var root: Node = _scene_root()
    if root != null:
        root.set_meta("rayxi_render_mode", "mode7_kart")
        var glow: Node = root.get_node_or_null("BackdropGlow")
        if glow is CanvasItem:
            (glow as CanvasItem).visible = false
    var pools: Dictionary = _scene_pools()
    for pool_name in ["karts", "item_boxs", "green_shells", "banana_peels", "game_objects"]:
        var entities: Array = pools.get(pool_name, []) as Array
        for entity in entities:
            if entity is CanvasItem:
                (entity as CanvasItem).visible = false
    if not _mode7_announced:
        _mode7_announced = true
        print("[trace] render.mode7 enabled=true samples=%d" % _track_samples.size())

func _camera_state() -> Dictionary:
    var pools: Dictionary = _scene_pools()
    var cameras: Array = pools.get("cameras", []) as Array
    if not cameras.is_empty():
        var camera = cameras[0]
        if camera != null:
            return {
                "position": Vector2(float(camera.position.x), float(camera.position.y)),
                "angle": float(camera.get("angle") if camera.get("angle") != null else 0.0),
            }
    var karts: Array = pools.get("karts", []) as Array
    if not karts.is_empty():
        var kart = karts[0]
        if kart != null:
            return {
                "position": Vector2(float(kart.position.x), float(kart.position.y)),
                "angle": float(kart.get("facing_angle") if kart.get("facing_angle") != null else 0.0),
            }
    return {"position": Vector2.ZERO, "angle": 0.0}

func _draw_mode7_surface(view_size: Vector2, horizon_y: float) -> void:
    draw_rect(Rect2(Vector2.ZERO, Vector2(view_size.x, horizon_y)), Color(0.40, 0.68, 0.94, 1.0), true)
    draw_rect(Rect2(Vector2(0.0, horizon_y), Vector2(view_size.x, view_size.y - horizon_y)), Color(0.22, 0.18, 0.12, 1.0), true)
    draw_circle(Vector2(view_size.x * 0.82, horizon_y * 0.42), 54.0, Color(1.0, 0.82, 0.46, 0.28))
    var hills: PackedVector2Array = PackedVector2Array([
        Vector2(0.0, horizon_y + 40.0),
        Vector2(view_size.x * 0.18, horizon_y - 16.0),
        Vector2(view_size.x * 0.36, horizon_y + 28.0),
        Vector2(view_size.x * 0.54, horizon_y - 34.0),
        Vector2(view_size.x * 0.72, horizon_y + 22.0),
        Vector2(view_size.x, horizon_y - 6.0),
        Vector2(view_size.x, horizon_y + 140.0),
        Vector2(0.0, horizon_y + 140.0),
    ])
    draw_colored_polygon(hills, Color(0.17, 0.13, 0.10, 0.92))
    draw_line(Vector2(0.0, horizon_y), Vector2(view_size.x, horizon_y), Color(0.96, 0.79, 0.52, 0.25), 2.0)

func _draw_mode7_track(camera_pos: Vector2, camera_angle: float) -> void:
    var segments: Array = []
    for i in range(_track_samples.size()):
        var current: Dictionary = _track_samples[i] as Dictionary
        var nxt: Dictionary = _track_samples[(i + 1) % _track_samples.size()] as Dictionary
        var current_pos: Vector2 = current.get("pos", Vector2.ZERO)
        var next_pos: Vector2 = nxt.get("pos", Vector2.ZERO)
        var current_tangent: Vector2 = current.get("tangent", Vector2.RIGHT)
        var next_tangent: Vector2 = nxt.get("tangent", Vector2.RIGHT)
        var current_normal: Vector2 = Vector2(-current_tangent.y, current_tangent.x)
        var next_normal: Vector2 = Vector2(-next_tangent.y, next_tangent.x)
        var shoulder_half_width: float = 198.0
        var road_half_width: float = 148.0
        var outer_left_a: Dictionary = _project_ground_point(current_pos - current_normal * shoulder_half_width, camera_pos, camera_angle)
        var outer_right_a: Dictionary = _project_ground_point(current_pos + current_normal * shoulder_half_width, camera_pos, camera_angle)
        var outer_left_b: Dictionary = _project_ground_point(next_pos - next_normal * shoulder_half_width, camera_pos, camera_angle)
        var outer_right_b: Dictionary = _project_ground_point(next_pos + next_normal * shoulder_half_width, camera_pos, camera_angle)
        var road_left_a: Dictionary = _project_ground_point(current_pos - current_normal * road_half_width, camera_pos, camera_angle)
        var road_right_a: Dictionary = _project_ground_point(current_pos + current_normal * road_half_width, camera_pos, camera_angle)
        var road_left_b: Dictionary = _project_ground_point(next_pos - next_normal * road_half_width, camera_pos, camera_angle)
        var road_right_b: Dictionary = _project_ground_point(next_pos + next_normal * road_half_width, camera_pos, camera_angle)
        var center_left_a: Dictionary = _project_ground_point(current_pos - current_normal * 10.0, camera_pos, camera_angle)
        var center_right_a: Dictionary = _project_ground_point(current_pos + current_normal * 10.0, camera_pos, camera_angle)
        var center_left_b: Dictionary = _project_ground_point(next_pos - next_normal * 10.0, camera_pos, camera_angle)
        var center_right_b: Dictionary = _project_ground_point(next_pos + next_normal * 10.0, camera_pos, camera_angle)
        if not bool(road_left_a.get("visible")) or not bool(road_right_a.get("visible")) or not bool(road_left_b.get("visible")) or not bool(road_right_b.get("visible")):
            continue
        var center_visible: bool = bool(center_left_a.get("visible")) and bool(center_right_a.get("visible")) and bool(center_left_b.get("visible")) and bool(center_right_b.get("visible"))
        segments.append({
            "depth": (
                float(road_left_a.get("depth", 0.0))
                + float(road_right_a.get("depth", 0.0))
                + float(road_left_b.get("depth", 0.0))
                + float(road_right_b.get("depth", 0.0))
            ) / 4.0,
            "index": i,
            "outer": _quad(
                Vector2(float(outer_left_a.get("x", 0.0)), float(outer_left_a.get("y", 0.0))),
                Vector2(float(outer_right_a.get("x", 0.0)), float(outer_right_a.get("y", 0.0))),
                Vector2(float(outer_right_b.get("x", 0.0)), float(outer_right_b.get("y", 0.0))),
                Vector2(float(outer_left_b.get("x", 0.0)), float(outer_left_b.get("y", 0.0)))
            ),
            "road": _quad(
                Vector2(float(road_left_a.get("x", 0.0)), float(road_left_a.get("y", 0.0))),
                Vector2(float(road_right_a.get("x", 0.0)), float(road_right_a.get("y", 0.0))),
                Vector2(float(road_right_b.get("x", 0.0)), float(road_right_b.get("y", 0.0))),
                Vector2(float(road_left_b.get("x", 0.0)), float(road_left_b.get("y", 0.0)))
            ),
            "center_visible": center_visible,
            "center": _quad(
                Vector2(float(center_left_a.get("x", 0.0)), float(center_left_a.get("y", 0.0))),
                Vector2(float(center_right_a.get("x", 0.0)), float(center_right_a.get("y", 0.0))),
                Vector2(float(center_right_b.get("x", 0.0)), float(center_right_b.get("y", 0.0))),
                Vector2(float(center_left_b.get("x", 0.0)), float(center_left_b.get("y", 0.0)))
            ) if center_visible else PackedVector2Array(),
        })
    segments.sort_custom(func(a, b): return float(a.get("depth", 0.0)) > float(b.get("depth", 0.0)))
    for segment in segments:
        var segment_index: int = int(segment.get("index", 0))
        var shoulder_color: Color = Color(0.36, 0.10, 0.11, 0.92) if segment_index % 2 == 0 else Color(0.92, 0.90, 0.78, 0.92)
        var road_color: Color = Color(0.24, 0.25, 0.30, 0.98) if segment_index % 2 == 0 else Color(0.20, 0.21, 0.26, 0.98)
        _draw_quad_fill(segment.get("outer"), shoulder_color)
        _draw_quad_fill(segment.get("road"), road_color)
        if segment_index % 3 == 0 and bool(segment.get("center_visible", false)):
            _draw_quad_fill(segment.get("center"), Color(0.98, 0.94, 0.58, 0.92))

func _draw_billboard(center_x: float, bottom_y: float, width: float, height: float, body_color: Color, accent_color: Color) -> void:
    draw_circle(Vector2(center_x, bottom_y + height * 0.08), max(width * 0.32, 6.0), Color(0.0, 0.0, 0.0, 0.22))
    var body: PackedVector2Array = PackedVector2Array([
        Vector2(center_x - width * 0.54, bottom_y),
        Vector2(center_x + width * 0.54, bottom_y),
        Vector2(center_x + width * 0.38, bottom_y - height),
        Vector2(center_x - width * 0.38, bottom_y - height),
    ])
    _draw_quad_fill(body, body_color)
    draw_rect(Rect2(Vector2(center_x - width * 0.18, bottom_y - height * 0.72), Vector2(width * 0.36, height * 0.34)), accent_color, true)
    draw_rect(Rect2(Vector2(center_x - width * 0.50, bottom_y - height * 0.20), Vector2(width * 0.18, height * 0.20)), Color(0.06, 0.06, 0.08, 0.98), true)
    draw_rect(Rect2(Vector2(center_x + width * 0.32, bottom_y - height * 0.20), Vector2(width * 0.18, height * 0.20)), Color(0.06, 0.06, 0.08, 0.98), true)

func _draw_world_billboards(camera_pos: Vector2, camera_angle: float) -> void:
    var pools: Dictionary = _scene_pools()
    for checkpoint in _checkpoint_points():
        var marker_projection: Dictionary = _project_ground_point(checkpoint, camera_pos, camera_angle)
        if not bool(marker_projection.get("visible")):
            continue
        var marker_scale: float = max(float(marker_projection.get("scale", 0.0)), 0.16)
        var marker_width: float = max(12.0, 42.0 * marker_scale)
        var marker_height: float = max(22.0, 112.0 * marker_scale)
        draw_rect(
            Rect2(
                Vector2(float(marker_projection.get("x", 0.0)) - marker_width * 0.5, float(marker_projection.get("y", 0.0)) - marker_height),
                Vector2(marker_width, marker_height)
            ),
            Color(0.22, 0.98, 0.56, 0.90),
            true
        )
    for item_box in pools.get("item_boxs", []):
        if item_box == null or not bool(item_box.get("active")):
            continue
        var item_projection: Dictionary = _project_ground_point(item_box.position, camera_pos, camera_angle)
        if not bool(item_projection.get("visible")):
            continue
        var item_scale: float = max(float(item_projection.get("scale", 0.0)), 0.14)
        var item_size: float = max(14.0, 64.0 * item_scale)
        draw_rect(
            Rect2(
                Vector2(float(item_projection.get("x", 0.0)) - item_size * 0.5, float(item_projection.get("y", 0.0)) - item_size),
                Vector2(item_size, item_size)
            ),
            Color(0.96, 0.62, 0.20, 0.94),
            true
        )
        draw_rect(
            Rect2(
                Vector2(float(item_projection.get("x", 0.0)) - item_size * 0.18, float(item_projection.get("y", 0.0)) - item_size * 0.76),
                Vector2(item_size * 0.36, item_size * 0.36)
            ),
            Color(0.98, 0.92, 0.78, 0.94),
            true
        )
    var opponent_draws: Array = []
    var karts: Array = pools.get("karts", []) as Array
    for i in range(1, karts.size()):
        var kart = karts[i]
        if kart == null:
            continue
        var projection: Dictionary = _project_ground_point(kart.position, camera_pos, camera_angle)
        if not bool(projection.get("visible")):
            continue
        opponent_draws.append({
            "depth": float(projection.get("depth", 0.0)),
            "projection": projection,
            "slot": int(kart.get("player_slot") if kart.get("player_slot") != null else i),
        })
    opponent_draws.sort_custom(func(a, b): return float(a.get("depth", 0.0)) > float(b.get("depth", 0.0)))
    for entry in opponent_draws:
        var projection: Dictionary = entry.get("projection", {})
        var scale: float = max(float(projection.get("scale", 0.0)), 0.10)
        var body_color: Color = Color(0.78, 0.22, 0.18, 0.96)
        var accent_color: Color = Color(0.96, 0.88, 0.84, 0.96)
        _draw_billboard(
            float(projection.get("x", 0.0)),
            float(projection.get("y", 0.0)),
            max(22.0, 140.0 * scale),
            max(20.0, 92.0 * scale),
            body_color,
            accent_color
        )
    for shell in pools.get("green_shells", []):
        if shell == null or not bool(shell.get("active")):
            continue
        var shell_projection: Dictionary = _project_ground_point(shell.position, camera_pos, camera_angle)
        if not bool(shell_projection.get("visible")):
            continue
        draw_circle(
            Vector2(float(shell_projection.get("x", 0.0)), float(shell_projection.get("y", 0.0)) - 8.0),
            max(7.0, 18.0 * float(shell_projection.get("scale", 0.0))),
            Color(0.30, 0.94, 0.38, 0.96)
        )
    for peel in pools.get("banana_peels", []):
        if peel == null or not bool(peel.get("active")):
            continue
        var peel_projection: Dictionary = _project_ground_point(peel.position, camera_pos, camera_angle)
        if not bool(peel_projection.get("visible")):
            continue
        draw_rect(
            Rect2(
                Vector2(float(peel_projection.get("x", 0.0)) - 10.0, float(peel_projection.get("y", 0.0)) - 10.0),
                Vector2(20.0, 12.0)
            ),
            Color(0.98, 0.90, 0.18, 0.94),
            true
        )

func _draw_player_kart() -> void:
    var pools: Dictionary = _scene_pools()
    var karts: Array = pools.get("karts", []) as Array
    if karts.is_empty():
        return
    var player = karts[0]
    if player == null:
        return
    var view_size: Vector2 = get_viewport_rect().size
    var steer_input: float = clampf(float(player.get("steer_input") if player.get("steer_input") != null else 0.0), -1.0, 1.0)
    var boosting: bool = bool(player.get("is_boosting")) or bool(player.get("item_boost_active"))
    var center_x: float = view_size.x * 0.5 + steer_input * 34.0
    var bottom_y: float = view_size.y * 0.90
    var width: float = 168.0
    var height: float = 116.0
    _draw_billboard(center_x, bottom_y, width, height, Color(0.86, 0.86, 0.92, 0.98), Color(0.94, 0.32, 0.18, 0.94))
    if boosting:
        draw_circle(Vector2(center_x - 34.0, bottom_y + 8.0), 16.0, Color(1.0, 0.56, 0.12, 0.72))
        draw_circle(Vector2(center_x + 34.0, bottom_y + 8.0), 16.0, Color(1.0, 0.56, 0.12, 0.72))

func _draw() -> void:
    var points: Array[Vector2] = _checkpoint_points()
    if points.size() < 2:
        return
    if not _is_mode7_scene():
        _draw_flat_track(points)
        return
    _rebuild_track_cache()
    var view_size: Vector2 = get_viewport_rect().size
    var horizon_y: float = view_size.y * 0.30
    _draw_mode7_surface(view_size, horizon_y)
    var camera_state: Dictionary = _camera_state()
    var camera_pos: Vector2 = camera_state.get("position", Vector2.ZERO)
    var camera_angle: float = float(camera_state.get("angle", 0.0))
    _draw_mode7_track(camera_pos, camera_angle)
    _draw_world_billboards(camera_pos, camera_angle)
    _draw_player_kart()
""".strip()


def _gd_string_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def _mode7_stage_block_for_context(role_context: dict[str, Any] | None = None) -> str:
    role_context = role_context or {}
    actor_roles = list(role_context.get("vehicle_actor_roles") or role_context.get("actor_roles") or [])
    camera_roles = list(role_context.get("camera_roles") or [])
    pickup_roles = list(role_context.get("pickup_roles") or [])
    projectile_roles = list(role_context.get("projectile_roles") or [])
    hazard_roles = list(role_context.get("hazard_roles") or [])

    def _to_pool(role_name: str) -> str:
        return role_name if role_name.endswith("s") else role_name + "s"

    actor_pools = [_to_pool(role_name) for role_name in actor_roles]
    camera_pools = [_to_pool(role_name) for role_name in camera_roles]
    pickup_pools = [_to_pool(role_name) for role_name in pickup_roles]
    projectile_pools = [_to_pool(role_name) for role_name in projectile_roles]
    hazard_pools = [_to_pool(role_name) for role_name in hazard_roles]
    hidden_pools: list[str] = []
    for pool_name in actor_pools + pickup_pools + projectile_pools + hazard_pools:
        if pool_name not in hidden_pools:
            hidden_pools.append(pool_name)

    return f"""
const MODE7_ACTOR_POOLS: Array[String] = {_gd_string_list(actor_pools)}
const MODE7_CAMERA_POOLS: Array[String] = {_gd_string_list(camera_pools)}
const MODE7_PICKUP_POOLS: Array[String] = {_gd_string_list(pickup_pools)}
const MODE7_PROJECTILE_POOLS: Array[String] = {_gd_string_list(projectile_pools)}
const MODE7_HAZARD_POOLS: Array[String] = {_gd_string_list(hazard_pools)}
const MODE7_HIDDEN_POOLS: Array[String] = {_gd_string_list(hidden_pools)}

var _track_samples: Array = []
var _cached_checkpoint_signature: String = ""
var _mode7_announced: bool = false

func _scene_root() -> Node:
    return get_parent()

func _scene_pools() -> Dictionary:
    var root: Node = _scene_root()
    if root == null:
        return {{}}
    var pools: Variant = root.get("entity_pools")
    return pools if pools is Dictionary else {{}}

func _pool_entities(pool_names: Array[String]) -> Array:
    var entities: Array = []
    var pools: Dictionary = _scene_pools()
    for pool_name in pool_names:
        var raw: Variant = pools.get(pool_name, [])
        if raw is Array:
            for entity in raw:
                entities.append(entity)
    return entities

func _primary_mode7_actor():
    var actors: Array = _pool_entities(MODE7_ACTOR_POOLS)
    return actors[0] if not actors.is_empty() else null

func _actor_angle(actor) -> float:
    if actor == null:
        return 0.0
    for prop_name in ["facing_angle", "angle", "heading_degrees"]:
        var value: Variant = actor.get(prop_name)
        if value != null:
            return float(value)
    return 0.0

func _checkpoint_points() -> Array[Vector2]:
    var points: Array[Vector2] = []
    var raw: Variant = get("checkpoint_positions")
    if raw is Array:
        for entry in raw:
            if entry is Vector2:
                points.append(entry)
            elif entry is Array and entry.size() >= 2:
                points.append(Vector2(float(entry[0]), float(entry[1])))
    return points

func _is_mode7_scene() -> bool:
    return _checkpoint_points().size() >= 2 and _primary_mode7_actor() != null

func _catmull_rom_point(p0: Vector2, p1: Vector2, p2: Vector2, p3: Vector2, t: float) -> Vector2:
    var t2: float = t * t
    var t3: float = t2 * t
    return 0.5 * (
        (2.0 * p1)
        + (-p0 + p2) * t
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
    )

func _rebuild_track_cache() -> void:
    var points: Array[Vector2] = _checkpoint_points()
    var signature: String = str(points)
    if signature == _cached_checkpoint_signature:
        return
    _cached_checkpoint_signature = signature
    _track_samples.clear()
    if points.size() < 2:
        return
    for i in range(points.size()):
        var p0: Vector2 = points[(i - 1 + points.size()) % points.size()]
        var p1: Vector2 = points[i]
        var p2: Vector2 = points[(i + 1) % points.size()]
        var p3: Vector2 = points[(i + 2) % points.size()]
        for step in range(18):
            var t: float = float(step) / 18.0
            var pos: Vector2 = _catmull_rom_point(p0, p1, p2, p3, t)
            var next_pos: Vector2 = _catmull_rom_point(p0, p1, p2, p3, min(t + 0.08, 1.0))
            var tangent: Vector2 = (next_pos - pos).normalized()
            if tangent.length() <= 0.001:
                tangent = Vector2.RIGHT
            _track_samples.append({{"pos": pos, "tangent": tangent}})

func _forward(angle_deg: float) -> Vector2:
    var radians: float = deg_to_rad(angle_deg)
    return Vector2(cos(radians), sin(radians))

func _project_ground_point(world: Vector2, camera_pos: Vector2, camera_angle: float) -> Dictionary:
    var view_size: Vector2 = get_viewport_rect().size
    var forward: Vector2 = _forward(camera_angle)
    var right: Vector2 = Vector2(-forward.y, forward.x)
    var relative: Vector2 = world - camera_pos
    var local_z: float = relative.dot(forward)
    var local_x: float = relative.dot(right)
    if local_z <= 14.0 or local_z > 2200.0:
        return {{"visible": false}}
    var horizon_y: float = view_size.y * 0.30
    var ground_scale: float = view_size.y * 52.0
    return {{
        "visible": true,
        "x": view_size.x * 0.5 + (local_x / local_z) * view_size.x * 1.12,
        "y": horizon_y + ground_scale / local_z,
        "scale": clampf((ground_scale / local_z) / 240.0, 0.05, 4.0),
        "depth": local_z,
    }}

func _quad(a: Vector2, b: Vector2, c: Vector2, d: Vector2) -> PackedVector2Array:
    return PackedVector2Array([a, b, c, d])

func _triangle_valid(a: Vector2, b: Vector2, c: Vector2) -> bool:
    return abs((b - a).cross(c - a)) > 1.0

func _draw_quad_fill(points: PackedVector2Array, color: Color) -> void:
    if points.size() != 4:
        return
    var a: Vector2 = points[0]
    var b: Vector2 = points[1]
    var c: Vector2 = points[2]
    var d: Vector2 = points[3]
    if _triangle_valid(a, b, c):
        draw_colored_polygon(PackedVector2Array([a, b, c]), color)
    if _triangle_valid(a, c, d):
        draw_colored_polygon(PackedVector2Array([a, c, d]), color)

func _draw_billboard(center_x: float, bottom_y: float, width: float, height: float, body_color: Color, accent_color: Color) -> void:
    draw_circle(Vector2(center_x, bottom_y + height * 0.08), max(width * 0.32, 6.0), Color(0.0, 0.0, 0.0, 0.22))
    var body: PackedVector2Array = PackedVector2Array([
        Vector2(center_x - width * 0.54, bottom_y),
        Vector2(center_x + width * 0.54, bottom_y),
        Vector2(center_x + width * 0.38, bottom_y - height),
        Vector2(center_x - width * 0.38, bottom_y - height),
    ])
    _draw_quad_fill(body, body_color)
    draw_rect(Rect2(Vector2(center_x - width * 0.18, bottom_y - height * 0.72), Vector2(width * 0.36, height * 0.34)), accent_color, true)
    draw_rect(Rect2(Vector2(center_x - width * 0.50, bottom_y - height * 0.20), Vector2(width * 0.18, height * 0.20)), Color(0.06, 0.06, 0.08, 0.98), true)
    draw_rect(Rect2(Vector2(center_x + width * 0.32, bottom_y - height * 0.20), Vector2(width * 0.18, height * 0.20)), Color(0.06, 0.06, 0.08, 0.98), true)

func _sync_mode7_visibility() -> void:
    if not _is_mode7_scene():
        return
    var root: Node = _scene_root()
    if root != null:
        if str(root.get_meta("rayxi_render_mode", "")) == "race_3d_surface":
            return
        root.set_meta("rayxi_render_mode", "mode7_surface")
        var glow: Node = root.get_node_or_null("BackdropGlow")
        if glow is CanvasItem:
            (glow as CanvasItem).visible = false
    for pool_name in MODE7_HIDDEN_POOLS:
        for entity in _scene_pools().get(pool_name, []):
            if entity is CanvasItem:
                (entity as CanvasItem).visible = false
    if not _mode7_announced:
        _mode7_announced = true
        print("[trace] render.mode7 enabled=true samples=%d" % _track_samples.size())

func _camera_state() -> Dictionary:
    var cameras: Array = _pool_entities(MODE7_CAMERA_POOLS)
    if not cameras.is_empty():
        var camera = cameras[0]
        if camera != null:
            return {{
                "position": Vector2(float(camera.position.x), float(camera.position.y)),
                "angle": float(camera.get("angle") if camera.get("angle") != null else 0.0),
            }}
    var actor = _primary_mode7_actor()
    if actor != null:
        return {{
            "position": Vector2(float(actor.position.x), float(actor.position.y)),
            "angle": _actor_angle(actor),
        }}
    return {{"position": Vector2.ZERO, "angle": 0.0}}

func _draw_mode7_surface(view_size: Vector2, horizon_y: float) -> void:
    draw_rect(Rect2(Vector2.ZERO, Vector2(view_size.x, horizon_y)), Color(0.40, 0.68, 0.94, 1.0), true)
    draw_rect(Rect2(Vector2(0.0, horizon_y), Vector2(view_size.x, view_size.y - horizon_y)), Color(0.22, 0.18, 0.12, 1.0), true)
    draw_circle(Vector2(view_size.x * 0.82, horizon_y * 0.42), 54.0, Color(1.0, 0.82, 0.46, 0.28))

func _draw_mode7_track(camera_pos: Vector2, camera_angle: float) -> void:
    for i in range(_track_samples.size()):
        var current: Dictionary = _track_samples[i] as Dictionary
        var nxt: Dictionary = _track_samples[(i + 1) % _track_samples.size()] as Dictionary
        var current_pos: Vector2 = current.get("pos", Vector2.ZERO)
        var next_pos: Vector2 = nxt.get("pos", Vector2.ZERO)
        var current_tangent: Vector2 = current.get("tangent", Vector2.RIGHT)
        var next_tangent: Vector2 = nxt.get("tangent", Vector2.RIGHT)
        var current_normal: Vector2 = Vector2(-current_tangent.y, current_tangent.x)
        var next_normal: Vector2 = Vector2(-next_tangent.y, next_tangent.x)
        var left_a: Dictionary = _project_ground_point(current_pos - current_normal * 148.0, camera_pos, camera_angle)
        var right_a: Dictionary = _project_ground_point(current_pos + current_normal * 148.0, camera_pos, camera_angle)
        var left_b: Dictionary = _project_ground_point(next_pos - next_normal * 148.0, camera_pos, camera_angle)
        var right_b: Dictionary = _project_ground_point(next_pos + next_normal * 148.0, camera_pos, camera_angle)
        if not bool(left_a.get("visible")) or not bool(right_a.get("visible")) or not bool(left_b.get("visible")) or not bool(right_b.get("visible")):
            continue
        _draw_quad_fill(
            _quad(
                Vector2(float(left_a.get("x", 0.0)), float(left_a.get("y", 0.0))),
                Vector2(float(right_a.get("x", 0.0)), float(right_a.get("y", 0.0))),
                Vector2(float(right_b.get("x", 0.0)), float(right_b.get("y", 0.0))),
                Vector2(float(left_b.get("x", 0.0)), float(left_b.get("y", 0.0)))
            ),
            Color(0.24, 0.25, 0.30, 0.98) if i % 2 == 0 else Color(0.20, 0.21, 0.26, 0.98)
        )

func _draw_world_billboards(camera_pos: Vector2, camera_angle: float) -> void:
    for checkpoint in _checkpoint_points():
        var marker_projection: Dictionary = _project_ground_point(checkpoint, camera_pos, camera_angle)
        if not bool(marker_projection.get("visible")):
            continue
        var marker_scale: float = max(float(marker_projection.get("scale", 0.0)), 0.16)
        var marker_width: float = max(12.0, 42.0 * marker_scale)
        var marker_height: float = max(22.0, 112.0 * marker_scale)
        draw_rect(Rect2(Vector2(float(marker_projection.get("x", 0.0)) - marker_width * 0.5, float(marker_projection.get("y", 0.0)) - marker_height), Vector2(marker_width, marker_height)), Color(0.22, 0.98, 0.56, 0.90), true)
    for pickup in _pool_entities(MODE7_PICKUP_POOLS):
        if pickup == null or not bool(pickup.get("active")):
            continue
        var pickup_projection: Dictionary = _project_ground_point(pickup.position, camera_pos, camera_angle)
        if not bool(pickup_projection.get("visible")):
            continue
        var pickup_scale: float = max(float(pickup_projection.get("scale", 0.0)), 0.14)
        var pickup_size: float = max(14.0, 64.0 * pickup_scale)
        draw_rect(Rect2(Vector2(float(pickup_projection.get("x", 0.0)) - pickup_size * 0.5, float(pickup_projection.get("y", 0.0)) - pickup_size), Vector2(pickup_size, pickup_size)), Color(0.96, 0.62, 0.20, 0.94), true)
    var actors: Array = _pool_entities(MODE7_ACTOR_POOLS)
    for i in range(1, actors.size()):
        var actor = actors[i]
        if actor == null:
            continue
        var projection: Dictionary = _project_ground_point(actor.position, camera_pos, camera_angle)
        if not bool(projection.get("visible")):
            continue
        var scale: float = max(float(projection.get("scale", 0.0)), 0.10)
        _draw_billboard(float(projection.get("x", 0.0)), float(projection.get("y", 0.0)), max(22.0, 140.0 * scale), max(20.0, 92.0 * scale), Color(0.78, 0.22, 0.18, 0.96), Color(0.96, 0.88, 0.84, 0.96))
    for projectile in _pool_entities(MODE7_PROJECTILE_POOLS):
        if projectile == null or not bool(projectile.get("active")):
            continue
        var projectile_projection: Dictionary = _project_ground_point(projectile.position, camera_pos, camera_angle)
        if not bool(projectile_projection.get("visible")):
            continue
        draw_circle(Vector2(float(projectile_projection.get("x", 0.0)), float(projectile_projection.get("y", 0.0)) - 8.0), max(7.0, 18.0 * float(projectile_projection.get("scale", 0.0))), Color(0.30, 0.94, 0.38, 0.96))
    for hazard in _pool_entities(MODE7_HAZARD_POOLS):
        if hazard == null or not bool(hazard.get("active")):
            continue
        var hazard_projection: Dictionary = _project_ground_point(hazard.position, camera_pos, camera_angle)
        if not bool(hazard_projection.get("visible")):
            continue
        draw_rect(Rect2(Vector2(float(hazard_projection.get("x", 0.0)) - 10.0, float(hazard_projection.get("y", 0.0)) - 10.0), Vector2(20.0, 12.0)), Color(0.98, 0.90, 0.18, 0.94), true)

func _draw_player_actor() -> void:
    var player = _primary_mode7_actor()
    if player == null:
        return
    var view_size: Vector2 = get_viewport_rect().size
    var steer_input: float = clampf(float(player.get("steer_input") if player.get("steer_input") != null else 0.0), -1.0, 1.0)
    var boosting: bool = bool(player.get("is_boosting")) or bool(player.get("item_boost_active"))
    var center_x: float = view_size.x * 0.5 + steer_input * 34.0
    var bottom_y: float = view_size.y * 0.90
    _draw_billboard(center_x, bottom_y, 168.0, 116.0, Color(0.86, 0.86, 0.92, 0.98), Color(0.94, 0.32, 0.18, 0.94))
    if boosting:
        draw_circle(Vector2(center_x - 34.0, bottom_y + 8.0), 16.0, Color(1.0, 0.56, 0.12, 0.72))
        draw_circle(Vector2(center_x + 34.0, bottom_y + 8.0), 16.0, Color(1.0, 0.56, 0.12, 0.72))

func _draw() -> void:
    var points: Array[Vector2] = _checkpoint_points()
    if points.size() < 2:
        return
    if not _is_mode7_scene():
        for i in range(points.size()):
            draw_line(points[i], points[(i + 1) % points.size()], Color(0.24, 0.25, 0.30, 0.98), 120.0)
            draw_circle(points[i], 18.0, Color(0.22, 0.98, 0.56, 0.90))
        return
    _rebuild_track_cache()
    var view_size: Vector2 = get_viewport_rect().size
    var horizon_y: float = view_size.y * 0.30
    _draw_mode7_surface(view_size, horizon_y)
    var camera_state: Dictionary = _camera_state()
    _draw_mode7_track(camera_state.get("position", Vector2.ZERO), float(camera_state.get("angle", 0.0)))
    _draw_world_billboards(camera_state.get("position", Vector2.ZERO), float(camera_state.get("angle", 0.0)))
    _draw_player_actor()
""".strip()


def _visual_bounds_helper_block() -> str:
    return """
func _rayxi_rect_like(value: Variant, fallback: Rect2 = Rect2()) -> Rect2:
    if value is Rect2:
        return value as Rect2
    if value is Dictionary:
        var box: Dictionary = value as Dictionary
        if box.has("x") and box.has("y") and box.has("w") and box.has("h"):
            return Rect2(
                float(box.get("x", 0.0)) - (float(box.get("w", 0.0)) * 0.5),
                float(box.get("y", 0.0)) - float(box.get("h", 0.0)),
                float(box.get("w", 0.0)),
                float(box.get("h", 0.0))
            )
    return fallback

func _rayxi_rect_union(rects: Array) -> Rect2:
    var merged: Rect2 = Rect2()
    var has_rect: bool = false
    for entry in rects:
        if not (entry is Rect2):
            continue
        var rect: Rect2 = entry as Rect2
        if rect.size.x <= 0.0 or rect.size.y <= 0.0:
            continue
        if not has_rect:
            merged = rect
            has_rect = true
        else:
            merged = merged.merge(rect)
    return merged if has_rect else Rect2()

func _rayxi_sprite_source_rect(sprite: Sprite2D) -> Rect2:
    if sprite.texture == null:
        return Rect2()
    var size: Vector2 = sprite.texture.get_size()
    var origin: Vector2 = sprite.offset
    if sprite.centered:
        origin -= size * 0.5
    return Rect2(origin, size)

func _rayxi_animated_source_rect(sprite: AnimatedSprite2D) -> Rect2:
    if sprite.sprite_frames == null:
        return Rect2()
    var current_animation: StringName = sprite.animation
    if current_animation == StringName():
        return Rect2()
    var frame_texture: Texture2D = sprite.sprite_frames.get_frame_texture(current_animation, sprite.frame)
    if frame_texture == null:
        return Rect2()
    var size: Vector2 = frame_texture.get_size()
    var origin: Vector2 = sprite.offset
    if sprite.centered:
        origin -= size * 0.5
    return Rect2(origin, size)

func _rayxi_collect_visual_rects(node: Node, parent_position: Vector2, parent_scale: Vector2, out_rects: Array) -> void:
    var node_position: Vector2 = parent_position
    var node_scale: Vector2 = parent_scale
    if node is Node2D:
        var node2d: Node2D = node as Node2D
        node_position += Vector2(node2d.position.x * parent_scale.x, node2d.position.y * parent_scale.y)
        var scale_x: float = node2d.scale.x if absf(node2d.scale.x) > 0.001 else 1.0
        var scale_y: float = node2d.scale.y if absf(node2d.scale.y) > 0.001 else 1.0
        node_scale = Vector2(parent_scale.x * scale_x, parent_scale.y * scale_y)

    var local_rect: Rect2 = Rect2()
    if node is Sprite2D:
        local_rect = _rayxi_sprite_source_rect(node as Sprite2D)
    elif node is AnimatedSprite2D:
        local_rect = _rayxi_animated_source_rect(node as AnimatedSprite2D)
    elif node is TextureRect:
        var texture_rect: TextureRect = node as TextureRect
        var texture_size: Vector2 = texture_rect.size
        if texture_size.length() <= 0.01 and texture_rect.texture != null:
            texture_size = texture_rect.texture.get_size()
        local_rect = Rect2(texture_rect.position, texture_size)
    elif node is ColorRect:
        var color_rect: ColorRect = node as ColorRect
        local_rect = Rect2(
            Vector2(color_rect.offset_left, color_rect.offset_top),
            Vector2(
                max(color_rect.offset_right - color_rect.offset_left, 0.0),
                max(color_rect.offset_bottom - color_rect.offset_top, 0.0)
            )
        )

    if local_rect.size.x > 0.0 and local_rect.size.y > 0.0:
        var scaled_pos: Vector2 = node_position + Vector2(local_rect.position.x * node_scale.x, local_rect.position.y * node_scale.y)
        var scaled_size: Vector2 = Vector2(absf(local_rect.size.x * node_scale.x), absf(local_rect.size.y * node_scale.y))
        if node_scale.x < 0.0:
            scaled_pos.x -= scaled_size.x
        if node_scale.y < 0.0:
            scaled_pos.y -= scaled_size.y
        out_rects.append(Rect2(scaled_pos, scaled_size))

    for child in node.get_children():
        if child is Node:
            _rayxi_collect_visual_rects(child, node_position, node_scale, out_rects)

func rayxi_visual_bounds_local() -> Rect2:
    var rects: Array = []
    for child in get_children():
        if child is Node:
            _rayxi_collect_visual_rects(child, Vector2.ZERO, Vector2.ONE, rects)
    var merged: Rect2 = _rayxi_rect_union(rects)
    if merged.size.x > 0.0 and merged.size.y > 0.0:
        return merged
    var fallback: Rect2 = _rayxi_rect_like(get("stand_hurtbox"), Rect2(-60.0, -180.0, 120.0, 180.0))
    if bool(get("is_airborne")):
        fallback = _rayxi_rect_like(get("air_hurtbox"), fallback)
    elif bool(get("is_crouching")):
        fallback = _rayxi_rect_like(get("crouch_hurtbox"), fallback)
    return fallback

func _rayxi_center_box(center: Vector2, size: Vector2) -> Rect2:
    return Rect2(center - (size * 0.5), size)

func rayxi_hurtbox_rects_local() -> Array:
    var visual: Rect2 = rayxi_visual_bounds_local()
    if visual.size.x <= 0.0 or visual.size.y <= 0.0:
        return [Rect2(-60.0, -180.0, 120.0, 180.0)]

    var auto_boxes: Array = []
    if bool(get("is_crouching")):
        var crouch_core: Rect2 = Rect2(
            Vector2(visual.position.x + visual.size.x * 0.18, visual.position.y + visual.size.y * 0.34),
            Vector2(visual.size.x * 0.64, visual.size.y * 0.50)
        )
        auto_boxes.append(
            Rect2(
                Vector2(crouch_core.position.x + crouch_core.size.x * 0.12, crouch_core.position.y),
                Vector2(crouch_core.size.x * 0.76, crouch_core.size.y * 0.42)
            )
        )
        auto_boxes.append(
            Rect2(
                Vector2(crouch_core.position.x, crouch_core.position.y + crouch_core.size.y * 0.38),
                Vector2(crouch_core.size.x, crouch_core.size.y * 0.62)
            )
        )
    else:
        var stand_core: Rect2 = Rect2(
            Vector2(visual.position.x + visual.size.x * 0.16, visual.position.y + visual.size.y * 0.05),
            Vector2(visual.size.x * 0.68, visual.size.y * 0.90)
        )
        if bool(get("is_airborne")):
            stand_core.position.y += visual.size.y * 0.04
            stand_core.size.y *= 0.86
        auto_boxes.append(
            Rect2(
                Vector2(stand_core.position.x + stand_core.size.x * 0.20, stand_core.position.y),
                Vector2(stand_core.size.x * 0.60, stand_core.size.y * 0.22)
            )
        )
        auto_boxes.append(
            Rect2(
                Vector2(stand_core.position.x + stand_core.size.x * 0.10, stand_core.position.y + stand_core.size.y * 0.21),
                Vector2(stand_core.size.x * 0.80, stand_core.size.y * 0.37)
            )
        )
        auto_boxes.append(
            Rect2(
                Vector2(stand_core.position.x + stand_core.size.x * 0.18, stand_core.position.y + stand_core.size.y * 0.56),
                Vector2(stand_core.size.x * 0.64, stand_core.size.y * 0.32)
            )
        )

    return auto_boxes

func rayxi_active_hitbox_rects_local(hitbox: Dictionary = {}) -> Array:
    if hitbox.is_empty():
        return []
    var visual: Rect2 = rayxi_visual_bounds_local()
    var facing: float = 1.0 if float(get("facing_direction") if get("facing_direction") != null else 1.0) >= 0.0 else -1.0
    var min_width: float = max(visual.size.x * 0.22, 34.0)
    var min_height: float = max(visual.size.y * 0.18, 30.0)
    var width: float = max(float(hitbox.get("width", 0.0)), min_width)
    var height: float = max(float(hitbox.get("height", 0.0)), min_height)
    var explicit_center_x: float = float(hitbox.get("offset_x", visual.size.x * 0.24 * facing))
    var explicit_bottom_y: float = float(hitbox.get("offset_y", visual.position.y + visual.size.y * 0.58))
    var center: Vector2 = Vector2(explicit_center_x, explicit_bottom_y - height * 0.5)
    var boxes: Array = [_rayxi_center_box(center, Vector2(width, height))]

    var shoulder_center: Vector2 = Vector2(
        lerpf(0.0, center.x, 0.48),
        lerpf(visual.position.y + visual.size.y * 0.44, center.y, 0.52)
    )
    var bridge_size: Vector2 = Vector2(
        max(width * 0.62, visual.size.x * 0.16),
        max(height * 0.72, visual.size.y * 0.16)
    )
    if absf(center.x) > visual.size.x * 0.18 or width > visual.size.x * 0.28:
        boxes.append(_rayxi_center_box(shoulder_center, bridge_size))

    if center.y < visual.position.y + visual.size.y * 0.26:
        var upper_center: Vector2 = Vector2(
            lerpf(0.0, center.x, 0.36),
            lerpf(visual.position.y + visual.size.y * 0.26, center.y, 0.50)
        )
        boxes.append(
            _rayxi_center_box(
                upper_center,
                Vector2(max(width * 0.56, visual.size.x * 0.18), max(height * 0.82, visual.size.y * 0.22))
            )
        )

    return boxes
""".strip()


def _role_helpers(
    role_name: str,
    base_node: str,
    local_names: set[str],
    role_context: dict[str, Any] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    ready_lines: list[str] = []
    process_lines: list[str] = []
    extra_blocks: list[str] = []

    if base_node == "CharacterBody2D":
        extra_blocks.append(_visual_bounds_helper_block())

    if role_name == "hud_bar" or base_node == "ProgressBar":
        ready_lines.extend(
            [
                "min_value = 0.0",
                "max_value = 1.0",
                "show_percentage = false",
                "step = 0.01",
            ]
        )
        if "display_value" in local_names:
            ready_lines.append("value = clamp(float(display_value), min_value, max_value)")
            ready_lines.append("_refresh_bar_theme()")
            process_lines.append("value = clamp(float(display_value), min_value, max_value)")
            process_lines.append("_refresh_bar_theme()")
            extra_blocks.append(
                """
func _refresh_bar_theme() -> void:
    var ratio: float = clamp(float(display_value), min_value, max_value)
    var fill_color: Color = Color(0.14, 0.86, 0.38, 0.98)
    if ratio <= 0.5:
        fill_color = Color(0.96, 0.74, 0.18, 0.98)
    if ratio <= 0.25:
        fill_color = Color(0.94, 0.22, 0.18, 0.98)

    var background_box: StyleBoxFlat = StyleBoxFlat.new()
    background_box.bg_color = Color(0.08, 0.09, 0.12, 0.86)
    background_box.corner_radius_top_left = 6
    background_box.corner_radius_top_right = 6
    background_box.corner_radius_bottom_left = 6
    background_box.corner_radius_bottom_right = 6
    background_box.border_width_left = 2
    background_box.border_width_top = 2
    background_box.border_width_right = 2
    background_box.border_width_bottom = 2
    background_box.border_color = Color(0.34, 0.38, 0.45, 0.92)
    add_theme_stylebox_override("background", background_box)

    var fill_box: StyleBoxFlat = StyleBoxFlat.new()
    fill_box.bg_color = fill_color
    fill_box.corner_radius_top_left = 6
    fill_box.corner_radius_top_right = 6
    fill_box.corner_radius_bottom_left = 6
    fill_box.corner_radius_bottom_right = 6
    add_theme_stylebox_override("fill", fill_box)
""".strip()
            )

    if role_name == "stage" or (base_node == "ColorRect" and "checkpoint_positions" in local_names):
        ready_lines.extend([
            "_rebuild_track_cache()",
            "_sync_mode7_visibility()",
            "queue_redraw()",
        ])
        process_lines.extend([
            "_rebuild_track_cache()",
            "_sync_mode7_visibility()",
            "queue_redraw()",
        ])
        extra_blocks.append(_mode7_stage_block_for_context(role_context))

    if role_name == "projectile" or base_node == "Area2D":
        ready_lines.extend([
            "z_as_relative = false",
            "z_index = 120",
            "queue_redraw()",
        ])
        process_lines.append("queue_redraw()")
        extra_blocks.append(
            """
func _contract_prop(name: String, fallback):
    var raw = get(name)
    return fallback if raw == null else raw

var _cached_projectile_texture = null
var _cached_projectile_texture_powered: bool = false

func _projectile_texture_candidates(powered: bool) -> Array:
    var candidates: Array = []
    var prefix: String = str(_contract_prop("visual_asset_prefix", "")).strip_edges()
    var basenames: Array = []
    if powered:
        basenames.append("powered_projectile.png")
        basenames.append("hadouken_powered.png")
    basenames.append("projectile.png")
    basenames.append("hadouken_projectile.png")
    if prefix != "":
        for basename in basenames:
            candidates.append("res://assets/%s/%s" % [prefix, basename])
    for basename in basenames:
        candidates.append("res://assets/ryu/%s" % basename)
        candidates.append("res://assets/common/%s" % basename)
    return candidates

func _projectile_texture(powered: bool):
    if _cached_projectile_texture != null and _cached_projectile_texture_powered == powered:
        return _cached_projectile_texture
    for candidate in _projectile_texture_candidates(powered):
        if not ResourceLoader.exists(candidate):
            continue
        var loaded = load(candidate)
        if loaded is Texture2D:
            _cached_projectile_texture = loaded
            _cached_projectile_texture_powered = powered
            return loaded
    return null

func _draw() -> void:
    var width: float = max(float(_contract_prop("hitbox_width", 48)), 84.0)
    var height: float = max(float(_contract_prop("hitbox_height", 24)), 52.0)
    var powered: bool = bool(_contract_prop("is_powered", _contract_prop("is_powered_up", false)))
    var tint: Color = Color(1.0, 0.55, 0.2, 0.95)
    if powered:
        tint = Color(1.0, 0.8, 0.2, 0.98)
    var projectile_texture = _projectile_texture(powered)
    if projectile_texture != null:
        var texture_size = projectile_texture.get_size()
        var draw_scale: float = max(width / max(texture_size.x, 1.0), height / max(texture_size.y, 1.0))
        var draw_size = texture_size * draw_scale
        draw_texture_rect(
            projectile_texture,
            Rect2(Vector2(-draw_size.x * 0.5, -draw_size.y * 0.62), draw_size),
            false
        )
        return
    draw_circle(Vector2.ZERO, max(width, height) * 0.34, Color(tint.r, tint.g, tint.b, 0.28))
    draw_rect(
        Rect2(Vector2(-width * 0.5, -height * 0.5), Vector2(width, height)),
        Color(tint.r, tint.g, tint.b, 0.82)
    )
    draw_rect(
        Rect2(Vector2(-width * 0.2, -height * 0.22), Vector2(width * 0.4, height * 0.44)),
        Color(1.0, 0.96, 0.88, 0.86)
    )
""".strip()
        )

    if base_node == "Label":
        for candidate in ("display_text", "text_value", "current_text"):
            if candidate in local_names:
                process_lines.append(f"text = str({candidate})")
                break

    return ready_lines, process_lines, extra_blocks


def _normalize_init_assignment_expr(
    owners: list[str],
    godot_base_node: str,
    node: PropertyNode,
    expr: str,
) -> str:
    rect_literal = _rect_property_literal(node)
    if rect_literal is not None:
        return _rect_dict_to_gd(rect_literal)
    if node.name != "collision_radius" or godot_base_node != "CharacterBody2D":
        return expr
    owner_text = " ".join(owners).lower()
    if not any(token in owner_text for token in _VEHICLE_ROLE_TOKENS):
        return expr
    try:
        value = float(expr)
    except Exception:
        return expr
    if 0.0 < value < 8.0:
        # Synthesized racing scenes use pixel-space positions and 120px-wide
        # fallback sprites, so tiny unit-scale radii tunnel through each other.
        return "56.0"
    return expr


def _has_executable_lines(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def _merged_owner_nodes(imap: ImpactMap, owners: list[str]) -> list[PropertyNode]:
    merged: dict[str, PropertyNode] = {}
    for owner in owners:
        for node in imap.properties_owned_by(owner):
            merged[node.name] = node
    return list(merged.values())


def _emit_entity_source(
    script_name: str,
    owners: list[str],
    nodes: list[PropertyNode],
    godot_base_node: str,
    constants: dict[str, Any] | None = None,
    role_context: dict[str, Any] | None = None,
) -> str:
    native_members = _native_members_for(godot_base_node)
    sorted_nodes = sorted(nodes, key=lambda n: (_category_priority(n), n.name))
    local_names = {node.name for node in sorted_nodes if node.name not in native_members}
    compatibility_declarations = _compatibility_declarations(owners, godot_base_node, local_names, native_members)
    const_values = _flatten_constant_values(constants)

    lines: list[str] = [
        f"extends {godot_base_node}",
        f"## {script_name} - generated from impact map owner(s) {owners}.",
        f"## Base node: {godot_base_node} | Properties: {len(sorted_nodes)}",
        "## DO NOT HAND-EDIT. Re-run scripts/build_game.py to refresh.",
        "",
    ]

    if sorted_nodes:
        lines.append("# --- Contract properties ---")
        for node in sorted_nodes:
            declaration = _declaration_line(node, native_members)
            if declaration:
                lines.append(declaration)
        lines.append("")

    if compatibility_declarations:
        lines.append("# --- Runtime compatibility properties ---")
        lines.extend(compatibility_declarations)
        lines.append("")

    if godot_base_node in {"CharacterBody2D", "Area2D"}:
        lines.append('@export var visual_asset_prefix: String = ""  # visual asset override')
        lines.append("")

    init_nodes = [node for node in sorted_nodes if node.category != Category.DERIVED and node.name not in native_members]
    init_assignments, unresolved_init = _ordered_assignments(
        init_nodes,
        owners,
        local_names,
        const_values,
        use_derivation=False,
    )
    if init_assignments or unresolved_init:
        lines.append("func _init() -> void:")
        if init_assignments:
            for node, expr in init_assignments:
                normalized_expr = _normalize_init_assignment_expr(owners, godot_base_node, node, expr)
                lines.append(f"    {node.name} = {normalized_expr}")
        if unresolved_init:
            lines.append(
                "    # External/default-sensitive contract values stay at declaration defaults: "
                + ", ".join(sorted(unresolved_init))
            )
        lines.append("")

    derived_nodes = [node for node in sorted_nodes if node.category == Category.DERIVED and node.name not in native_members]
    derived_assignments, unresolved_derived = _ordered_assignments(
        derived_nodes,
        owners,
        local_names,
        const_values,
        use_derivation=True,
    )
    ready_lines, process_lines, extra_blocks = _role_helpers(
        script_name,
        godot_base_node,
        local_names,
        role_context=role_context,
    )
    if derived_assignments:
        process_lines.extend(f"{node.name} = {expr}" for node, expr in derived_assignments)
    if unresolved_derived:
        process_lines.append(
            "# Derived values with external dependencies were left to systems: "
            + ", ".join(sorted(unresolved_derived))
        )

    if ready_lines:
        lines.append("func _ready() -> void:")
        if not _has_executable_lines(ready_lines):
            lines.append("    pass")
        for line in ready_lines:
            lines.append(f"    {line}")
        lines.append("")

    if process_lines:
        lines.append("func _process(_delta: float) -> void:")
        if not _has_executable_lines(process_lines):
            lines.append("    pass")
        for line in process_lines:
            lines.append(f"    {line}")
        lines.append("")

    for block in extra_blocks:
        lines.append(block)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def emit_character(
    character_name: str,
    imap: ImpactMap,
    role: str = "fighter",
    godot_base_node: str = "CharacterBody2D",
    constants: dict[str, Any] | None = None,
    role_context: dict[str, Any] | None = None,
) -> str:
    owners = [role]
    groups = role_context or {}
    for group_name in ("vehicle_actor_roles", "combat_actor_roles", "actor_roles"):
        group_roles = [name for name in groups.get(group_name, []) if name]
        if role not in group_roles:
            continue
        for sibling_role in group_roles:
            if sibling_role != role and imap.properties_owned_by(sibling_role):
                owners.append(sibling_role)
        break
    character_owner = f"character.{character_name}"
    if imap.properties_owned_by(character_owner):
        owners.append(character_owner)
    nodes = _merged_owner_nodes(imap, owners)
    return _emit_entity_source(character_name, owners, nodes, godot_base_node, constants)


def emit_all_characters(
    imap: ImpactMap,
    characters: list[str],
    output_dir: Path,
    role: str = "fighter",
    godot_base_node: str = "CharacterBody2D",
    constants: dict[str, Any] | None = None,
    role_context: dict[str, Any] | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for char in characters:
        source = emit_character(
            char,
            imap,
            role=role,
            godot_base_node=godot_base_node,
            constants=constants,
            role_context=role_context,
        )
        out_path = output_dir / f"{char}.gd"
        out_path.write_text(source, encoding="utf-8")
        written[char] = out_path
        _log.info(
            "character_gen: wrote %s (%d bytes, %d lines)",
            out_path.name,
            len(source),
            source.count("\n"),
        )
    return written


def emit_runtime_role_scripts(
    imap: ImpactMap,
    role_defs: dict[str, Any],
    output_dir: Path,
    constants: dict[str, Any] | None = None,
    skip_roles: set[str] | None = None,
    role_context: dict[str, Any] | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    skipped = skip_roles or {"fighter"}
    written: dict[str, Path] = {}
    for role_name in sorted(role_defs.keys()):
        if role_name in skipped:
            continue
        nodes = imap.properties_owned_by(role_name)
        if not nodes:
            continue
        role_meta = role_defs.get(role_name) or {}
        if hasattr(role_meta, "model_dump"):
            role_meta = role_meta.model_dump(exclude_none=True)
        base_node = role_meta.get("godot_base_node") or "Node2D"
        source = _emit_entity_source(
            role_name,
            [role_name],
            nodes,
            base_node,
            constants,
            role_context=role_context,
        )
        out_path = output_dir / f"{role_name}.gd"
        out_path.write_text(source, encoding="utf-8")
        written[role_name] = out_path
        _log.info(
            "character_gen: wrote runtime role %s (%d bytes, %d lines)",
            out_path.name,
            len(source),
            source.count("\n"),
        )
    return written
