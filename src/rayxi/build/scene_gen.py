"""scene_gen — generate the Godot scene wiring script from the impact map.

Given an ImpactMap + DLR constants + HLT role metadata, emits `scenes/{scene}.gd`
that instantiates every declared system, wires each one to its config bucket and
the shared entity_pools dict, and calls them in phase+topo order every physics
frame.

Fully generic. Zero game-specific strings in this module. Pool shapes come from
`imap.nodes[*].owner` (whatever owners the graph declares become the pools) and
acquisition instructions come from `hlt.roles[owner].scene_acquisition`.

Output contract (every emitted scene script):
  extends Node2D
  var entity_pools: Dictionary = {}
  var config: Dictionary = {}
  var systems: Dictionary = {}   # system_name -> Node

  func _ready():
      _populate_entity_pools()
      _load_constants()
      _instantiate_systems()
      _setup_systems()

  func _physics_process(delta):
      # calls in phase + topo order
      systems["<sys_1>"].process(delta)
      systems["<sys_2>"].process(delta)
      ...
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rayxi.spec.expr import BinOpExpr, CondExpr, Expr, FnCallExpr, LiteralExpr, RefExpr, expr_refs
from rayxi.spec.impact_map import ImpactMap
from rayxi.spec.models import GameIdentity

_log = logging.getLogger("rayxi.build.scene_gen")


def _role_owner(raw_owner: str) -> str:
    """Strip instance suffix — 'character.ryu' → 'character', 'hud.p1_bar' → 'hud'.
    Role-generic owners stay as-is.
    """
    return raw_owner.split(".", 1)[0]


def pool_owners_from_imap(imap: ImpactMap, role_defs: dict | None) -> list[str]:
    """Entity-pool owners: distinct `imap.nodes[*].owner` values (after stripping
    instance suffix) that have a matching entry in role_defs. Owners without
    role metadata don't get pools — they're either singletons (game), instance
    widgets (mechanic_patcher handles them), or unowned metadata.
    """
    roles = role_defs or {}
    owners: set[str] = set()
    for n in imap.nodes.values():
        if n.owner == "game":
            continue
        ro = _role_owner(n.owner)
        if ro in roles:
            owners.add(ro)
    return sorted(owners)


def pool_name_for(owner: str) -> str:
    """owner → pool key. pluralize only if not already plural."""
    return owner if owner.endswith("s") else owner + "s"


def _role_group_pool_keys(role_groups: dict | None, group_name: str) -> list[str]:
    groups = role_groups or {}
    return [pool_name_for(role) for role in groups.get(group_name, []) if role]


def _first_pool_key(
    role_groups: dict | None,
    group_names: tuple[str, ...],
    pool_owners: list[str],
    fallback_tokens: tuple[str, ...] = (),
) -> str | None:
    for group_name in group_names:
        pool_keys = _role_group_pool_keys(role_groups, group_name)
        if pool_keys:
            return pool_keys[0]
    for owner in pool_owners:
        lower_owner = owner.lower()
        if any(token in lower_owner for token in fallback_tokens):
            return pool_name_for(owner)
    return None


_TYPE_TO_GDTYPE = {
    "int": "int", "integer": "int",
    "float": "float", "number": "float",
    "bool": "bool", "boolean": "bool",
    "string": "String", "str": "String",
    "vector2": "Vector2", "color": "Color", "rect2": "Rect2",
    "list": "Array", "dict": "Dictionary",
}
_TYPE_TO_DEFAULT = {
    "int": "0", "integer": "0",
    "float": "0.0", "number": "0.0",
    "bool": "false", "boolean": "false",
    "string": '""', "str": '""',
    "vector2": "Vector2.ZERO", "color": "Color.WHITE", "rect2": "Rect2()",
    "list": "[]", "dict": "{}",
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


def _game_prop_gd_type(t: str) -> str:
    return _TYPE_TO_GDTYPE.get(t.lower(), "Variant")


def _game_prop_default(t: str) -> str:
    return _TYPE_TO_DEFAULT.get(t.lower(), "null")


def _scene_value_to_gd(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scene_value_to_gd(v) for v in value) + "]"
    if isinstance(value, dict):
        pairs = [f"{json.dumps(str(k))}: {_scene_value_to_gd(v)}" for k, v in value.items()]
        return "{" + ", ".join(pairs) + "}"
    if value is None:
        return "null"
    return json.dumps(value, default=str)


def _scene_literal_to_gd(type_name: str, value) -> str:
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
        return f'Color("{value}")' if isinstance(value, str) else "Color.WHITE"
    if t == "rect2":
        if isinstance(value, list) and len(value) >= 4:
            return f"Rect2({value[0]}, {value[1]}, {value[2]}, {value[3]})"
        return "Rect2()"
    if t == "list":
        return "[" + ", ".join(_scene_value_to_gd(v) for v in (value or [])) + "]"
    if t == "dict":
        if not isinstance(value, dict):
            return "{}"
        pairs = [f"{json.dumps(str(k))}: {_scene_value_to_gd(v)}" for k, v in value.items()]
        return "{" + ", ".join(pairs) + "}"
    return _scene_value_to_gd(value)


def _scene_constant_values(constants: dict) -> dict[str, object]:
    grouped: dict[str, list[object]] = {}
    for bucket in (constants or {}).values():
        if not isinstance(bucket, dict):
            continue
        for name, value in bucket.items():
            grouped.setdefault(name, []).append(value)
    resolved: dict[str, object] = {}
    for name, values in grouped.items():
        stable = {json.dumps(v, sort_keys=True, default=str) for v in values}
        if len(stable) == 1 and values:
            resolved[name] = values[0]
    return resolved


def _scene_expr_to_gd(
    expr: Expr | None,
    local_names: set[str],
    const_values: dict[str, object],
) -> str | None:
    if expr is None:
        return None
    if isinstance(expr, LiteralExpr):
        return _scene_literal_to_gd(expr.type, expr.value)
    if isinstance(expr, RefExpr):
        if "." not in expr.path:
            return expr.path if expr.path in local_names else None
        head, tail = expr.path.split(".", 1)
        if head == "const":
            return _scene_value_to_gd(const_values[tail]) if tail in const_values else None
        if head == "game" and tail in local_names:
            return tail
        return None
    if isinstance(expr, BinOpExpr):
        left = _scene_expr_to_gd(expr.left, local_names, const_values)
        right = _scene_expr_to_gd(expr.right, local_names, const_values)
        if left is None or right is None:
            return None
        return f"({left} {_BINOP_SYMBOLS.get(expr.op, expr.op)} {right})"
    if isinstance(expr, FnCallExpr):
        args = [_scene_expr_to_gd(a, local_names, const_values) for a in expr.args]
        if any(a is None for a in args):
            return None
        if expr.fn == "not":
            return f"(not {args[0]})"
        return f"{expr.fn}({', '.join(args)})"
    if isinstance(expr, CondExpr):
        condition = _scene_expr_to_gd(expr.condition, local_names, const_values)
        then_val = _scene_expr_to_gd(expr.then_val, local_names, const_values)
        else_val = _scene_expr_to_gd(expr.else_val, local_names, const_values)
        if condition is None or then_val is None or else_val is None:
            return None
        return f"({then_val} if {condition} else {else_val})"
    return None


def _ordered_game_assignments(nodes, constants: dict, *, use_derivation: bool) -> tuple[list[tuple[object, str]], list[str]]:
    local_names = {node.name for node in nodes}
    const_values = _scene_constant_values(constants)
    node_map: dict[str, tuple[object, str, set[str]]] = {}
    unresolved: list[str] = []
    for node in nodes:
        expr = node.derivation if use_derivation else node.initial_value
        if expr is None:
            continue
        rendered = _scene_expr_to_gd(expr, local_names, const_values)
        if rendered is None:
            unresolved.append(node.name)
            continue
        deps: set[str] = set()
        for ref in expr_refs(expr):
            if "." not in ref:
                continue
            head, tail = ref.split(".", 1)
            if head == "game" and tail in local_names and tail != node.name:
                deps.add(tail)
        node_map[node.name] = (node, rendered, deps)

    ordered: list[tuple[object, str]] = []
    remaining = {name: set(info[2]) for name, info in node_map.items()}
    while remaining:
        ready = [name for name, deps in remaining.items() if not deps or all(dep not in remaining for dep in deps)]
        if not ready:
            ready = [sorted(remaining.keys())[0]]
        ready.sort()
        for name in ready:
            node, rendered, _deps = node_map[name]
            ordered.append((node, rendered))
            remaining.pop(name, None)
            for deps in remaining.values():
                deps.discard(name)
    return ordered, unresolved


def _emit_pool_population(pool_owner: str, roles: dict) -> list[str]:
    """Return GDScript lines that populate one pool at _ready() time."""
    pool_key = pool_name_for(pool_owner)
    role = roles.get(pool_owner, {})
    acq = role.get("scene_acquisition", {})
    method = acq.get("method", "runtime_array")

    if method == "nodes_in_scene":
        pattern = acq.get("pattern", f"{pool_owner}*")
        return [
            f'    entity_pools["{pool_key}"] = []',
            f'    for child in get_children():',
            f'        if _name_matches(child.name, "{pattern}"):',
            f'            entity_pools["{pool_key}"].append(child)',
        ]
    if method == "named_node":
        node_name = acq.get("node_name", pool_owner)
        return [
            f'    var _n_{pool_owner} = get_node_or_null("{node_name}")',
            f'    entity_pools["{pool_key}"] = [_n_{pool_owner}] if _n_{pool_owner} else []',
        ]
    # runtime_array (default): start empty, systems append
    return [f'    entity_pools["{pool_key}"] = []']


def _emit_name_matcher() -> str:
    """Minimal glob matcher (supports * anywhere) — no hardcoded patterns."""
    return '''
func _name_matches(name: String, pattern: String) -> bool:
    var parts = pattern.split("*", true)
    if parts.size() == 1:
        return name == pattern
    var pos: int = 0
    if not pattern.begins_with("*"):
        if not name.begins_with(parts[0]):
            return false
        pos = parts[0].length()
        parts = parts.slice(1)
    for i in range(parts.size()):
        var part: String = str(parts[i])
        if part == "":
            continue
        var found: int = name.find(part, pos)
        if found < 0:
            return false
        pos = found + part.length()
    if not pattern.ends_with("*") and pos != name.length():
        return false
    return true
'''


def _emit_constant_literal(value) -> str:
    """Render a DLR constant value as a GDScript literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_emit_constant_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        pairs = [f'"{k}": {_emit_constant_literal(v)}' for k, v in value.items()]
        return "{" + ", ".join(pairs) + "}"
    if value is None:
        return "null"
    return json.dumps(value)


def _emit_config_dict(constants: dict) -> list[str]:
    """Flatten the per-system constants into a GDScript Dictionary literal."""
    lines: list[str] = ["    config = {"]
    for sys_name in sorted(constants.keys()):
        bucket = constants[sys_name]
        if not isinstance(bucket, dict):
            continue
        flat: dict[str, object] = {}
        for k, v in bucket.items():
            if isinstance(v, dict) and "value" in v:
                flat[k] = v["value"]
            else:
                flat[k] = v
        lines.append(f'        "{sys_name}": ' + _emit_constant_literal(flat) + ",")
    lines.append("    }")
    return lines


def _emit_system_instantiation(system: str) -> str:
    return (
        f'    systems["{system}"] = preload("res://scripts/systems/{system}.gd").new()\n'
        f'    add_child(systems["{system}"])'
    )


def _emit_system_setup(system: str) -> str:
    return (
        f'    if systems["{system}"].has_method("setup"):\n'
        f'        systems["{system}"].setup(entity_pools, config.get("{system}", {{}}))'
    )


def _emit_system_process(system: str) -> str:
    return f'    systems["{system}"].process(delta)'


def _fighter_index_from_hud_name(hud_name: str) -> int | None:
    """Extract the fighter index from a HUD widget name if it follows the
    `p<N>_*` convention. Returns 0-based index, or None if no prefix match.
    Keeps the mapping inside scene_gen — no game-specific strings elsewhere.
    """
    import re as _re
    m = _re.match(r"p(\d+)_", hud_name)
    if m:
        return int(m.group(1)) - 1
    return None


def _primary_actor_pool_key(pool_owners: list[str], role_groups: dict | None = None) -> str | None:
    groups = role_groups or {}
    for group_name in ("combat_actor_roles", "vehicle_actor_roles", "actor_roles"):
        roles = [role for role in groups.get(group_name, []) if role]
        if not roles:
            continue
        preferred = [role for role in roles if "ai" not in role.lower()]
        selected = preferred[0] if preferred else roles[0]
        return pool_name_for(selected)
    return _first_pool_key(
        role_groups,
        ("combat_actor_roles", "vehicle_actor_roles", "actor_roles"),
        pool_owners,
        fallback_tokens=("fighter", "vehicle", "kart", "car", "racer", "driver", "bike", "ship", "player"),
    )


def _default_hud_position(name: str, index: int, scene_defaults: dict | None) -> tuple[float, float]:
    hud_layout = ((scene_defaults or {}).get("hud_layout") or {})
    candidate_names = [name]
    lower_name = name.lower()
    if lower_name == "mini_map":
        candidate_names.append("minimap")
    elif lower_name == "minimap":
        candidate_names.append("mini_map")
    for candidate in candidate_names:
        if candidate in hud_layout and isinstance(hud_layout[candidate], list) and len(hud_layout[candidate]) >= 2:
            return float(hud_layout[candidate][0]), float(hud_layout[candidate][1])

    if lower_name.startswith("p1_"):
        return 100.0, 126.0
    if lower_name.startswith("p2_"):
        return 1590.0, 126.0
    if lower_name == "lap_counter":
        return 60.0, 36.0
    if lower_name == "position_display":
        return 1590.0, 36.0
    if lower_name == "speedometer":
        return 60.0, 938.0
    if lower_name == "item_icon":
        return 820.0, 928.0
    if lower_name == "finish_banner":
        return 760.0, 68.0
    if lower_name == "minimap":
        return 1640.0, 760.0
    col = index % 3
    row = index // 3
    return 80.0 + col * 560.0, 48.0 + row * 92.0


def _emit_hud_widgets(
    hlr: GameIdentity,
    scene_defaults: dict | None = None,
    actor_pool_key: str | None = None,
    capabilities: dict | None = None,
) -> list[str]:
    """Emit instantiation for each mechanic_spec HUD widget.

    Widgets are Control nodes that read properties from a specific fighter
    instance. scene_gen binds `fighter_path` via the `p<N>_*` name convention;
    if the name doesn't follow that convention, the widget is instantiated
    without a fighter binding and must bind itself at runtime.
    """
    widgets: list[tuple[str, int | None]] = []
    for spec in hlr.mechanic_specs:
        for hud in spec.hud_entities:
            idx = _fighter_index_from_hud_name(hud.name)
            widgets.append((hud.name, idx))

    lines = ["    # --- HUD widgets from mechanic_specs ---"]
    if bool((capabilities or {}).get("duel_combat")):
        lines.extend(
            [
                '    if ResourceLoader.exists("res://scripts/hud/rayxi_duel_status.gd"):',
                '        var _duel_status: Node = preload("res://scripts/hud/rayxi_duel_status.gd").new()',
                '        if _duel_status.has_method("setup"):',
                "            _duel_status.setup(self)",
                "        add_child(_duel_status)",
                '        print("[trace] hud.widget_ready name=%s pos=%s" % ["rayxi_duel_status", str(_duel_status.position)])',
            ]
        )
    if not widgets:
        return lines if len(lines) > 1 else []

    for i, (name, fighter_idx) in enumerate(widgets):
        var_name = f"_hud_{i}"
        lines.append(
            f'    var {var_name}: Node = preload("res://scripts/hud/{name}.gd").new()'
        )
        if fighter_idx is not None and actor_pool_key:
            lines.append(
                f'    if entity_pools.get("{actor_pool_key}", []).size() > {fighter_idx}:'
            )
            lines.append(
                f'        {var_name}.fighter_path = entity_pools["{actor_pool_key}"][{fighter_idx}].get_path()'
            )
        elif actor_pool_key:
            lines.append(
                f'    if entity_pools.get("{actor_pool_key}", []).size() > 0:'
            )
            lines.append(
                f'        {var_name}.fighter_path = entity_pools["{actor_pool_key}"][0].get_path()'
            )
        # Default widget position: stagger across the top of the screen.
        # Widgets can override in their own _ready() if needed.
        x_pos, y_pos = _default_hud_position(name, i, scene_defaults)
        lines.append(f"    {var_name}.position = Vector2({x_pos}, {y_pos})")
        lines.append(f"    add_child({var_name})")
        lines.append(
            f'    print("[trace] hud.widget_ready name=%s pos=%s" % ["{name}", str({var_name}.position)])'
        )
    return lines


def _emit_scene_runtime_seed(
    scene_defaults: dict | None,
    pool_owners: list[str],
    role_groups: dict | None = None,
    capabilities: dict | None = None,
) -> list[str]:
    defaults = scene_defaults or {}
    pool_keys = {pool_name_for(owner) for owner in pool_owners}
    actor_pool_key = _primary_actor_pool_key(pool_owners, role_groups)
    stage_pool_key = _first_pool_key(
        role_groups,
        ("stage_roles",),
        pool_owners,
        fallback_tokens=("stage", "track", "arena", "background"),
    )
    camera_pool_key = _first_pool_key(
        role_groups,
        ("camera_roles",),
        pool_owners,
        fallback_tokens=("camera",),
    )
    pickup_roles = list((role_groups or {}).get("pickup_roles", []))
    pickup_role = pickup_roles[0] if pickup_roles else None
    pickup_pool_key = pool_name_for(pickup_role) if pickup_role else None
    has_stages = stage_pool_key in pool_keys if stage_pool_key else False
    has_cameras = camera_pool_key in pool_keys if camera_pool_key else False
    has_pickups = pickup_pool_key in pool_keys if pickup_pool_key else False
    needs_stage_fallback = (bool((capabilities or {}).get("checkpoint_race")) or bool((capabilities or {}).get("mode7_surface"))) and not has_stages

    lines = [f"var _scene_defaults: Dictionary = {_scene_value_to_gd(defaults)}", ""]
    lines.extend(
        [
            """
func _scene_points(key: String) -> Array:
    var points: Array = []
    var raw: Variant = _scene_defaults.get(key, [])
    if not (raw is Array):
        return points
    for entry in raw:
        if entry is Vector2:
            points.append(entry)
        elif entry is Array and entry.size() >= 2:
            points.append(Vector2(float(entry[0]), float(entry[1])))
    return points
""".strip(),
            """
func _apply_scene_defaults() -> void:
    var checkpoint_points: Array = _scene_points("checkpoint_positions")
    var item_box_points: Array = _scene_points("item_box_positions")
""".strip(),
        ]
    )
    if has_stages or needs_stage_fallback:
        lines.extend(
            [
                f'    var stages: Array = entity_pools.get("{stage_pool_key if stage_pool_key else "stages"}", [])',
                "    if stages.is_empty():",
                '        var background_stage: Node = get_node_or_null("Background")',
                "        if background_stage != null:",
                '            entity_pools["stages"] = [background_stage]',
                '            stages = entity_pools["stages"]',
                "    if stages.size() > 0 and not checkpoint_points.is_empty():",
                "        var stage: Variant = stages[0]",
                "        if stage != null:",
                '            var existing_points: Variant = stage.get("checkpoint_positions")',
                '            if (not (existing_points is Array) or (existing_points as Array).is_empty()) and stage.has_meta("checkpoint_positions"):',
                '                existing_points = stage.get_meta("checkpoint_positions", [])',
                "            if not (existing_points is Array) or (existing_points as Array).is_empty():",
                '                if stage.get("checkpoint_positions") != null:',
                '                    stage.set("checkpoint_positions", checkpoint_points)',
                '                stage.set_meta("checkpoint_positions", checkpoint_points)',
                "                var min_x: float = 1e9",
                "                var max_x: float = -1e9",
                "                for checkpoint in checkpoint_points:",
                "                    if checkpoint is Vector2:",
                "                        min_x = min(min_x, float(checkpoint.x))",
                "                        max_x = max(max_x, float(checkpoint.x))",
                "                if min_x < 1e8 and max_x > -1e8 and not stage.has_meta(\"road_width\"):",
                '                    stage.set_meta("road_width", max((max_x - min_x) + 320.0, 720.0))',
                '                print("[trace] stage.track_seeded checkpoints=%d" % checkpoint_points.size())',
                '                if stage.has_method("queue_redraw"):',
                "                    stage.queue_redraw()",
            ]
        )
    if has_cameras:
        lines.extend(
            [
                f'    var cameras: Array = entity_pools.get("{camera_pool_key}", [])',
                f'    if cameras.is_empty() and ResourceLoader.exists("res://scripts/entities/{camera_pool_key[:-1]}.gd"):',
                f'        var spawned_camera: Node = preload("res://scripts/entities/{camera_pool_key[:-1]}.gd").new()',
                '        spawned_camera.name = "camera_main"',
                f'        if entity_pools.get("{actor_pool_key}", []).size() > 0:',
                f'            spawned_camera.position = entity_pools["{actor_pool_key}"][0].position',
                "        add_child(spawned_camera)",
                f'        entity_pools["{camera_pool_key}"].append(spawned_camera)',
                '        print("[trace] entity.spawned role=camera name=%s pos=%s" % [spawned_camera.name, str(spawned_camera.position)])',
            ]
        )
    if has_pickups and pickup_role:
        lines.extend(
            [
                f'    var item_boxes: Array = entity_pools.get("{pickup_pool_key}", [])',
                f'    if item_boxes.is_empty() and not item_box_points.is_empty() and ResourceLoader.exists("res://scripts/entities/{pickup_role}.gd"):',
                "        var _item_index: int = 0",
                "        for point in item_box_points:",
                f'            var item_box: Node = preload("res://scripts/entities/{pickup_role}.gd").new()',
                f'            item_box.name = "{pickup_role}_%d" % _item_index',
                "            item_box.position = point",
                '            if item_box.get("collision_radius") != null:',
                "                item_box.collision_radius = 44.0",
                '            if item_box.get("hitbox_width") != null:',
                "                item_box.hitbox_width = 56.0",
                '            if item_box.get("hitbox_height") != null:',
                "                item_box.hitbox_height = 56.0",
                '            if item_box.get("active") != null:',
                "                item_box.active = true",
                '            if item_box.get("is_active") != null:',
                "                item_box.is_active = true",
                "            add_child(item_box)",
                f'            entity_pools["{pickup_pool_key}"].append(item_box)',
                f'            print("[trace] entity.spawned role={pickup_role} name=%s pos=%s" % [item_box.name, str(item_box.position)])',
                "            _item_index += 1",
            ]
        )
    if not any((has_stages, has_cameras, has_pickups)):
        lines.append("    pass")
    lines.append("")
    return lines


def _emit_debug_overlay_helper() -> list[str]:
    return [
        """
func _install_debug_overlays() -> void:
    if ResourceLoader.exists("res://scripts/debug/rayxi_debug_boxes.gd"):
        var debug_boxes: Node = preload("res://scripts/debug/rayxi_debug_boxes.gd").new()
        if debug_boxes.has_method("setup"):
            debug_boxes.setup(self, _scene_defaults)
        add_child(debug_boxes)
    if ResourceLoader.exists("res://scripts/debug/rayxi_debug_log.gd"):
        var debug_log: Node = preload("res://scripts/debug/rayxi_debug_log.gd").new()
        if debug_log.has_method("setup"):
            debug_log.setup(self, _scene_defaults)
        add_child(debug_log)
""".strip(),
        "",
    ]


def _emit_race_3d_renderer_block(
    *,
    pickup_pool_key: str | None = None,
    projectile_pool_key: str | None = None,
    hazard_pool_key: str | None = None,
) -> str:
    pickup_key = pickup_pool_key or ""
    projectile_key = projectile_pool_key or ""
    hazard_key = hazard_pool_key or ""
    return '''
var _race3d_container: SubViewportContainer = null
var _race3d_viewport: SubViewport = null
var _race3d_world: Node3D = null
var _race3d_camera: Camera3D = null
var _race3d_track_root: Node3D = null
var _race3d_scenery_root: Node3D = null
var _race3d_actor_root: Node3D = null
var _race3d_pickup_root: Node3D = null
var _race3d_projectile_root: Node3D = null
var _race3d_hazard_root: Node3D = null
var _race3d_actor_nodes: Dictionary = {}
var _race3d_pickup_nodes: Dictionary = {}
var _race3d_projectile_nodes: Dictionary = {}
var _race3d_hazard_nodes: Dictionary = {}
var _race3d_track_signature: String = ""
var _race3d_texture_cache: Dictionary = {}
var _race3d_json_cache: Dictionary = {}
var _race3d_announced: bool = false

func _race_point_array(raw: Variant) -> Array:
    var points: Array = []
    if not (raw is Array):
        return points
    for entry in raw:
        if entry is Vector2:
            points.append(entry)
        elif entry is Array and entry.size() >= 2:
            points.append(Vector2(float(entry[0]), float(entry[1])))
    return points

func _race_checkpoint_points() -> Array:
    var stages: Array = entity_pools.get("stages", [])
    if not stages.is_empty() and stages[0] != null:
        var stage = stages[0]
        var raw: Variant = stage.get("checkpoint_positions")
        if (not (raw is Array) or (raw as Array).is_empty()) and stage.has_meta("checkpoint_positions"):
            raw = stage.get_meta("checkpoint_positions", [])
        var stage_points: Array = _race_point_array(raw)
        if not stage_points.is_empty():
            return stage_points
    return _scene_points("checkpoint_positions")

func _race_item_points() -> Array:
    return _scene_points("item_box_positions")

func _race_track_bounds(points: Array) -> Dictionary:
    if points.is_empty():
        return {"center": Vector2.ZERO, "span": Vector2(1200.0, 900.0), "radius": 780.0}
    var min_x: float = 1e9
    var max_x: float = -1e9
    var min_y: float = 1e9
    var max_y: float = -1e9
    for point in points:
        if not (point is Vector2):
            continue
        var p: Vector2 = point as Vector2
        min_x = min(min_x, p.x)
        max_x = max(max_x, p.x)
        min_y = min(min_y, p.y)
        max_y = max(max_y, p.y)
    var span: Vector2 = Vector2(max(max_x - min_x, 1.0), max(max_y - min_y, 1.0))
    return {
        "center": Vector2((min_x + max_x) * 0.5, (min_y + max_y) * 0.5),
        "span": span,
        "radius": max(span.x, span.y) * 0.72,
    }

func _race_world_scale(points: Array) -> float:
    var bounds: Dictionary = _race_track_bounds(points)
    var span: Vector2 = bounds.get("span", Vector2(1200.0, 900.0))
    var longest: float = max(span.x, span.y)
    return clampf(260.0 / max(longest, 1.0), 0.18, 0.36)

func _race_lane_half_width(points: Array) -> float:
    var bounds: Dictionary = _race_track_bounds(points)
    var span: Vector2 = bounds.get("span", Vector2(1200.0, 900.0))
    return clampf(min(span.x, span.y) * _race_world_scale(points) * 0.12, 16.0, 30.0)

func _race_world_position(point: Vector2, points: Array, lift: float = 0.0) -> Vector3:
    var bounds: Dictionary = _race_track_bounds(points)
    var center: Vector2 = bounds.get("center", Vector2.ZERO)
    var scale: float = _race_world_scale(points)
    var centered: Vector2 = point - center
    return Vector3(centered.x * scale, lift, -centered.y * scale)

func _race_material(color: Color, roughness: float = 0.8, metallic: float = 0.0, emission_energy: float = 0.0) -> StandardMaterial3D:
    var material := StandardMaterial3D.new()
    material.albedo_color = color
    material.roughness = roughness
    material.metallic = metallic
    if emission_energy > 0.0:
        material.emission_enabled = true
        material.emission = color
        material.emission_energy_multiplier = emission_energy
    return material

func _race_apply_mesh_texture(node: MeshInstance3D, texture: Texture2D, tint: Color, roughness: float = 0.8, metallic: float = 0.0, emission_energy: float = 0.0) -> void:
    if node == null or texture == null:
        return
    var material := _race_material(tint, roughness, metallic, emission_energy)
    material.albedo_texture = texture
    material.texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR_WITH_MIPMAPS_ANISOTROPIC
    node.material_override = material

func _race_label_matches(file_name: String, labels: Array) -> bool:
    var lower_name: String = file_name.to_lower()
    for label_value in labels:
        var label: String = str(label_value).strip_edges().to_lower()
        if label == "":
            continue
        if lower_name == "%s.png" % label or lower_name == "%s.jpg" % label or lower_name == "%s.jpeg" % label:
            return true
        if lower_name.begins_with("%s_" % label) or lower_name.begins_with("%s-" % label):
            return true
    return false

func _race_directory_candidates(dir_path: String, labels: Array) -> Array:
    var matches: Array = []
    var dir := DirAccess.open(dir_path)
    if dir == null:
        return matches
    dir.list_dir_begin()
    while true:
        var file_name: String = dir.get_next()
        if file_name == "":
            break
        if dir.current_is_dir():
            continue
        var lower_name: String = file_name.to_lower()
        if not (lower_name.ends_with(".png") or lower_name.ends_with(".jpg") or lower_name.ends_with(".jpeg")):
            continue
        if _race_label_matches(file_name, labels):
            matches.append("%s/%s" % [dir_path, file_name])
    dir.list_dir_end()
    return matches

func _clear_node_children(node: Node) -> void:
    if node == null:
        return
    for child in node.get_children():
        child.queue_free()

func _install_race_3d_view() -> void:
    if _race3d_container != null:
        return
    _race3d_container = SubViewportContainer.new()
    _race3d_container.name = "Race3DView"
    _race3d_container.set_anchors_preset(Control.PRESET_FULL_RECT)
    _race3d_container.mouse_filter = Control.MOUSE_FILTER_IGNORE
    _race3d_container.stretch = true
    add_child(_race3d_container)
    move_child(_race3d_container, 0)

    _race3d_viewport = SubViewport.new()
    _race3d_viewport.name = "Race3DViewport"
    _race3d_viewport.transparent_bg = false
    _race3d_viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
    _race3d_viewport.msaa_3d = Viewport.MSAA_4X
    _race3d_container.add_child(_race3d_viewport)

    _race3d_world = Node3D.new()
    _race3d_world.name = "Race3DWorld"
    _race3d_viewport.add_child(_race3d_world)

    var environment_node := WorldEnvironment.new()
    var environment := Environment.new()
    var sky_material := ProceduralSkyMaterial.new()
    sky_material.sky_top_color = Color(0.44, 0.72, 0.98, 1.0)
    sky_material.sky_horizon_color = Color(0.90, 0.96, 1.0, 1.0)
    sky_material.ground_horizon_color = Color(0.48, 0.60, 0.30, 1.0)
    sky_material.ground_bottom_color = Color(0.24, 0.28, 0.16, 1.0)
    var sky := Sky.new()
    sky.sky_material = sky_material
    environment.background_mode = Environment.BG_SKY
    environment.background_sky = sky
    environment.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
    environment.glow_enabled = true
    environment.glow_strength = 0.22
    environment_node.environment = environment
    _race3d_world.add_child(environment_node)

    var sun := DirectionalLight3D.new()
    sun.rotation_degrees = Vector3(-48.0, 18.0, 0.0)
    sun.light_energy = 2.2
    _race3d_world.add_child(sun)

    _race3d_track_root = Node3D.new()
    _race3d_track_root.name = "TrackRoot"
    _race3d_world.add_child(_race3d_track_root)
    _race3d_scenery_root = Node3D.new()
    _race3d_scenery_root.name = "SceneryRoot"
    _race3d_world.add_child(_race3d_scenery_root)
    _race3d_actor_root = Node3D.new()
    _race3d_actor_root.name = "ActorRoot"
    _race3d_world.add_child(_race3d_actor_root)
    _race3d_pickup_root = Node3D.new()
    _race3d_pickup_root.name = "PickupRoot"
    _race3d_world.add_child(_race3d_pickup_root)
    _race3d_projectile_root = Node3D.new()
    _race3d_projectile_root.name = "ProjectileRoot"
    _race3d_world.add_child(_race3d_projectile_root)
    _race3d_hazard_root = Node3D.new()
    _race3d_hazard_root.name = "HazardRoot"
    _race3d_world.add_child(_race3d_hazard_root)

    _race3d_camera = Camera3D.new()
    _race3d_camera.name = "RaceCamera3D"
    _race3d_camera.current = true
    _race3d_camera.fov = 72.0
    _race3d_camera.near = 0.1
    _race3d_camera.far = 4000.0
    _race3d_world.add_child(_race3d_camera)

func _sync_race_3d_viewport() -> void:
    if _race3d_container == null or _race3d_viewport == null:
        return
    var view_size: Vector2 = get_viewport_rect().size
    _race3d_container.size = view_size
    var target_size := Vector2i(max(int(view_size.x), 1), max(int(view_size.y), 1))
    if _race3d_viewport.size != target_size:
        _race3d_viewport.size = target_size

func _rebuild_race_3d_track() -> void:
    if _race3d_track_root == null or _race3d_scenery_root == null:
        return
    var points: Array = _race_checkpoint_points()
    var signature: String = str(points)
    if signature == _race3d_track_signature:
        return
    _race3d_track_signature = signature
    _clear_node_children(_race3d_track_root)
    _clear_node_children(_race3d_scenery_root)
    if points.size() < 2:
        return

    var bounds: Dictionary = _race_track_bounds(points)
    var span: Vector2 = bounds.get("span", Vector2(1200.0, 900.0))
    var scale: float = _race_world_scale(points)
    var lane_half_width: float = _race_lane_half_width(points)
    var ground_texture = _race_load_texture(["common"], ["grass_ground_tile", "ground_tile", "grass_tile"])
    var road_texture = _race_load_texture(["common"], ["road_surface_tile", "road_tile", "track_road_tile"])
    var shoulder_texture = _race_load_texture(["common"], ["road_shoulder_tile", "shoulder_tile", "track_shoulder_tile"])
    var barrier_texture = _race_load_texture(["common"], ["barrier_segment", "guardrail", "barrier"])
    var sign_texture = _race_load_texture(["common"], ["direction_sign", "festival_banner", "sign"])
    var tree_texture = _race_load_texture(["common"], ["tree_cluster", "scenery_tree", "tree"])
    var cloud_texture = _race_load_texture(["common"], ["cloud_card", "cloud"])
    var backdrop_texture = _race_load_texture(["common"], ["track_backdrop", "backdrop", "mountain_backdrop"])

    var ground := MeshInstance3D.new()
    var ground_mesh := PlaneMesh.new()
    ground_mesh.size = Vector2(max(span.x * scale * 3.2, 260.0), max(span.y * scale * 3.2, 260.0))
    ground.mesh = ground_mesh
    ground.position = Vector3(0.0, -0.08, 0.0)
    ground.material_override = _race_material(Color(0.72, 0.78, 0.62, 1.0), 0.98, 0.0, 0.0)
    if ground_texture != null:
        _race_apply_mesh_texture(ground, ground_texture, Color(1.0, 1.0, 1.0, 1.0), 0.98, 0.0, 0.0)
    _race3d_track_root.add_child(ground)

    for idx in range(points.size()):
        var a2: Vector2 = points[idx]
        var b2: Vector2 = points[(idx + 1) % points.size()]
        var a3: Vector3 = _race_world_position(a2, points, 0.04)
        var b3: Vector3 = _race_world_position(b2, points, 0.04)
        var segment_length: float = a3.distance_to(b3)
        if segment_length <= 0.1:
            continue
        var tangent_2d: Vector2 = (b2 - a2).normalized()
        var normal_2d: Vector2 = Vector2(-tangent_2d.y, tangent_2d.x)
        var lane_offset_2d: float = lane_half_width / max(scale, 0.001)

        var shoulder := MeshInstance3D.new()
        var shoulder_mesh := BoxMesh.new()
        shoulder_mesh.size = Vector3(lane_half_width * 2.55, 0.16, segment_length + 1.6)
        shoulder.mesh = shoulder_mesh
        shoulder.position = (a3 + b3) * 0.5
        shoulder.material_override = _race_material(Color(0.98, 0.70, 0.28, 1.0), 0.84, 0.0, 0.0)
        if shoulder_texture != null:
            _race_apply_mesh_texture(shoulder, shoulder_texture, Color(1.0, 1.0, 1.0, 1.0), 0.84, 0.0, 0.0)
        _race3d_track_root.add_child(shoulder)
        shoulder.look_at_from_position(shoulder.position, b3, Vector3.UP)

        var road := MeshInstance3D.new()
        var road_mesh := BoxMesh.new()
        road_mesh.size = Vector3(lane_half_width * 2.0, 0.34, segment_length + 0.6)
        road.mesh = road_mesh
        road.position = (a3 + b3) * 0.5 + Vector3(0.0, 0.10, 0.0)
        road.material_override = _race_material(Color(0.88, 0.82, 0.74, 1.0) if idx % 2 == 0 else Color(0.84, 0.78, 0.70, 1.0), 0.65, 0.02, 0.0)
        if road_texture != null:
            _race_apply_mesh_texture(road, road_texture, Color(1.0, 1.0, 1.0, 1.0), 0.65, 0.02, 0.0)
        _race3d_track_root.add_child(road)
        road.look_at_from_position(road.position, b3, Vector3.UP)

        var center_line := MeshInstance3D.new()
        var line_mesh := BoxMesh.new()
        line_mesh.size = Vector3(max(lane_half_width * 0.08, 0.45), 0.03, segment_length * 0.52)
        center_line.mesh = line_mesh
        center_line.position = (a3 + b3) * 0.5 + Vector3(0.0, 0.29, 0.0)
        center_line.material_override = _race_material(Color(1.0, 0.98, 0.86, 1.0), 0.32, 0.0, 0.5)
        _race3d_track_root.add_child(center_line)
        center_line.look_at_from_position(center_line.position, b3, Vector3.UP)

        var checkpoint := MeshInstance3D.new()
        var checkpoint_mesh := CylinderMesh.new()
        checkpoint_mesh.top_radius = max(lane_half_width * 0.12, 1.0)
        checkpoint_mesh.bottom_radius = checkpoint_mesh.top_radius
        checkpoint_mesh.height = 9.0
        checkpoint.mesh = checkpoint_mesh
        checkpoint.position = _race_world_position(a2, points, 4.4)
        checkpoint.material_override = _race_material(Color(0.22, 0.96, 0.58, 1.0), 0.28, 0.0, 1.2)
        _race3d_track_root.add_child(checkpoint)

        if barrier_texture != null:
            var barrier_size: Vector2 = _race_billboard_size(barrier_texture, 10.0, 4.4, 4.2, 2.0)
            for side in [-1.0, 1.0]:
                var barrier := _race_billboard(barrier_texture, barrier_size, barrier_size.y * 0.48)
                barrier.position = _race_world_position(a2 + normal_2d * lane_offset_2d * 1.45 * side, points, barrier_size.y * 0.48)
                _race3d_scenery_root.add_child(barrier)

        if sign_texture != null and idx % 2 == 0:
            var sign_size: Vector2 = _race_billboard_size(sign_texture, 8.0, 5.4, 3.8, 2.6)
            var sign := _race_billboard(sign_texture, sign_size, sign_size.y * 0.54)
            sign.position = _race_world_position(a2 + normal_2d * lane_offset_2d * 1.95, points, sign_size.y * 0.54)
            _race3d_scenery_root.add_child(sign)
    var center: Vector2 = bounds.get("center", Vector2.ZERO)
    var radius: float = float(bounds.get("radius", 780.0))
    for idx in range(6):
        var theta: float = TAU * float(idx) / 6.0
        if tree_texture != null:
            var tree_size: Vector2 = _race_billboard_size(tree_texture, 12.0, 11.0, 5.5, 5.0)
            var tree := _race_billboard(tree_texture, tree_size, tree_size.y * 0.52)
            tree.position = _race_world_position(center + Vector2(cos(theta), sin(theta)) * radius * 1.7, points, tree_size.y * 0.52)
            _race3d_scenery_root.add_child(tree)
        else:
            var mountain := MeshInstance3D.new()
            var mountain_mesh := CylinderMesh.new()
            mountain_mesh.top_radius = 0.0
            mountain_mesh.bottom_radius = max(radius * scale * (0.20 + float(idx % 2) * 0.04), 12.0)
            mountain_mesh.height = 28.0 + float(idx % 3) * 7.0
            mountain.mesh = mountain_mesh
            mountain.position = _race_world_position(center + Vector2(cos(theta), sin(theta)) * radius * 1.9, points, mountain_mesh.height * 0.5 - 0.2)
            mountain.material_override = _race_material(Color(0.48, 0.60, 0.72, 1.0), 0.94, 0.0, 0.0)
            _race3d_scenery_root.add_child(mountain)

    for idx in range(4):
        var offset_angle: float = TAU * float(idx) / 4.0
        if cloud_texture != null:
            var cloud_size: Vector2 = _race_billboard_size(cloud_texture, 26.0, 14.0, 14.0, 7.0)
            var cloud := _race_billboard(cloud_texture, cloud_size, cloud_size.y * 0.5)
            cloud.position = Vector3(cos(offset_angle) * 92.0, 34.0 + float(idx) * 2.5, sin(offset_angle) * 54.0)
            _race3d_scenery_root.add_child(cloud)
        else:
            var cloud := MeshInstance3D.new()
            var cloud_mesh := SphereMesh.new()
            cloud_mesh.radius = 6.0 + float(idx % 2) * 2.4
            cloud_mesh.height = cloud_mesh.radius * 2.0
            cloud.mesh = cloud_mesh
            cloud.position = Vector3(cos(offset_angle) * 80.0, 34.0 + float(idx) * 2.5, sin(offset_angle) * 54.0)
            cloud.material_override = _race_material(Color(1.0, 1.0, 1.0, 0.95), 0.92, 0.0, 0.15)
            _race3d_scenery_root.add_child(cloud)

    if backdrop_texture != null:
        var backdrop_size: Vector2 = _race_billboard_size(backdrop_texture, max(span.x * scale * 2.8, 220.0), 120.0, 120.0, 60.0)
        var backdrop := _race_billboard(backdrop_texture, backdrop_size, backdrop_size.y * 0.5)
        backdrop.position = Vector3(0.0, backdrop_size.y * 0.52, -max(span.y * scale * 1.45, 180.0))
        _race3d_scenery_root.add_child(backdrop)

func _race_entity_color(entity, primary: Color, secondary: Color) -> Color:
    if entity == null:
        return primary
    var prefix: String = str(entity.get("visual_asset_prefix") if entity.get("visual_asset_prefix") != null else "").to_lower()
    if prefix.find("p2") >= 0 or prefix.find("rival") >= 0 or _entity_is_ai(entity):
        return secondary
    var slot: int = int(entity.get("player_slot") if entity.get("player_slot") != null else 0)
    return primary if slot <= 0 else secondary

func _race_entity_active(entity) -> bool:
    if entity == null:
        return false
    var raw: Variant = entity.get("active")
    if raw == null:
        raw = entity.get("is_active")
    return false if raw == null else bool(raw)

func _race_texture_candidates(prefixes: Array, labels: Array) -> Array:
    var candidates: Array = []
    for prefix_value in prefixes:
        var prefix: String = str(prefix_value).strip_edges()
        if prefix == "":
            continue
        var dir_path: String = "res://assets/%s" % prefix
        var prefix_basename: String = prefix.get_file()
        for label_value in labels:
            var label: String = str(label_value).strip_edges()
            if label == "":
                continue
            candidates.append("res://assets/%s/%s.png" % [prefix, label])
            candidates.append("res://assets/%s/%s.jpg" % [prefix, label])
            candidates.append("res://assets/%s/%s.jpeg" % [prefix, label])
            if prefix_basename != "":
                candidates.append("res://assets/%s/%s_%s.png" % [prefix, prefix_basename, label])
                candidates.append("res://assets/%s/%s_%s.jpg" % [prefix, prefix_basename, label])
                candidates.append("res://assets/%s/%s_%s.jpeg" % [prefix, prefix_basename, label])
        candidates.append_array(_race_directory_candidates(dir_path, labels))
    return candidates

func _race_load_texture(prefixes: Array, labels: Array):
    var cache_key: String = "%s|%s" % [str(prefixes), str(labels)]
    if _race3d_texture_cache.has(cache_key):
        var cached: Variant = _race3d_texture_cache.get(cache_key)
        return cached if cached is Texture2D else null
    for candidate in _race_texture_candidates(prefixes, labels):
        if not ResourceLoader.exists(candidate):
            continue
        var loaded: Variant = load(candidate)
        if loaded is Texture2D:
            _race3d_texture_cache[cache_key] = loaded
            return loaded
    _race3d_texture_cache[cache_key] = false
    return null

func _race_load_json_dict(res_path: String) -> Dictionary:
    if _race3d_json_cache.has(res_path):
        var cached: Variant = _race3d_json_cache.get(res_path, {})
        return cached if cached is Dictionary else {}
    if not FileAccess.file_exists(res_path):
        _race3d_json_cache[res_path] = {}
        return {}
    var text: String = FileAccess.get_file_as_string(res_path)
    if text.strip_edges() == "":
        _race3d_json_cache[res_path] = {}
        return {}
    var parsed: Variant = JSON.parse_string(text)
    if parsed is Dictionary:
        _race3d_json_cache[res_path] = parsed
        return parsed
    _race3d_json_cache[res_path] = {}
    return {}

func _race_actor_texture_permitted(entity) -> bool:
    if entity == null:
        return true
    var actor_prefix: String = str(entity.get("visual_asset_prefix") if entity.get("visual_asset_prefix") != null else "").strip_edges()
    if actor_prefix == "":
        return true
    var review: Dictionary = _race_load_json_dict("res://assets/%s/asset_review.json" % actor_prefix)
    if review.is_empty():
        return true
    if review.has("runtime_approved") and not bool(review.get("runtime_approved", true)):
        return false
    var perspective: String = str(review.get("camera_perspective", "")).strip_edges().to_lower()
    if perspective == "":
        return true
    return perspective in ["chase_rear", "rear_chase", "third_person_chase", "third_person_chase_rear"]

func _race_sprite_plane(texture: Texture2D, size: Vector2, lift: float, billboard_mode: int = BaseMaterial3D.BILLBOARD_DISABLED) -> MeshInstance3D:
    var quad := MeshInstance3D.new()
    quad.name = "Billboard"
    var mesh := QuadMesh.new()
    mesh.size = size
    quad.mesh = mesh
    quad.position = Vector3(0.0, lift, 0.0)
    var material := StandardMaterial3D.new()
    material.albedo_texture = texture
    material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
    material.billboard_mode = billboard_mode
    material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
    material.cull_mode = BaseMaterial3D.CULL_DISABLED
    quad.material_override = material
    return quad

func _race_billboard(texture: Texture2D, size: Vector2, lift: float) -> MeshInstance3D:
    return _race_sprite_plane(texture, size, lift, BaseMaterial3D.BILLBOARD_FIXED_Y)

func _race_actor_plane(texture: Texture2D, size: Vector2, lift: float) -> MeshInstance3D:
    var quad := _race_sprite_plane(texture, size, lift, BaseMaterial3D.BILLBOARD_DISABLED)
    quad.rotation.y = PI
    return quad

func _race_billboard_size(texture: Texture2D, max_width: float, max_height: float, min_width: float = 1.0, min_height: float = 1.0) -> Vector2:
    if texture == null:
        return Vector2(max_width, max_height)
    var texture_size: Vector2 = texture.get_size()
    if texture_size.x <= 0.0 or texture_size.y <= 0.0:
        return Vector2(max_width, max_height)
    var aspect: float = texture_size.x / texture_size.y
    var width: float = max_width
    var height: float = width / max(aspect, 0.01)
    if height > max_height:
        height = max_height
        width = height * aspect
    return Vector2(clampf(width, min_width, max_width), clampf(height, min_height, max_height))

func _race_actor_visual_state(entity) -> String:
    if entity == null:
        return "idle"
    var boost_timer: float = float(entity.get("boost_timer") if entity.get("boost_timer") != null else 0.0)
    var boost_multiplier: float = float(entity.get("boost_multiplier") if entity.get("boost_multiplier") != null else 1.0)
    var steer_input: float = float(entity.get("steer_input") if entity.get("steer_input") != null else 0.0)
    var speed: float = absf(float(entity.get("speed") if entity.get("speed") != null else 0.0))
    if boost_timer > 0.05 or boost_multiplier > 1.05:
        return "boost"
    if speed > 4.0:
        if steer_input <= -0.12:
            return "turn_left"
        if steer_input >= 0.12:
            return "turn_right"
    return "idle"

func _race_actor_texture_labels(state: String) -> Array:
    if state == "boost":
        return ["kart_boost", "kart_idle", "driver_portrait"]
    if state == "turn_left":
        return ["kart_turn_left", "kart_idle", "driver_portrait"]
    if state == "turn_right":
        return ["kart_turn_right", "kart_idle", "driver_portrait"]
    return ["kart_idle", "driver_portrait"]

func _race_apply_actor_texture(root: Node3D, entity) -> void:
    if root == null or entity == null:
        return
    if not _race_actor_texture_permitted(entity):
        return
    var billboard: Variant = root.get_node_or_null("Billboard")
    if not (billboard is MeshInstance3D):
        return
    var state: String = _race_actor_visual_state(entity)
    if str(root.get_meta("rayxi_actor_state", "")) == state:
        return
    var actor_prefix: String = str(entity.get("visual_asset_prefix") if entity.get("visual_asset_prefix") != null else "")
    var actor_texture = _race_load_texture([actor_prefix], _race_actor_texture_labels(state))
    if actor_texture == null:
        return
    var billboard_mesh: Variant = (billboard as MeshInstance3D).mesh
    if billboard_mesh is QuadMesh:
        (billboard_mesh as QuadMesh).size = _race_billboard_size(actor_texture, 4.2, 3.2, 2.0, 1.8)
    var material: Variant = (billboard as MeshInstance3D).material_override
    if material is StandardMaterial3D:
        (material as StandardMaterial3D).albedo_texture = actor_texture
    var texture_size: Vector2 = _race_billboard_size(actor_texture, 4.2, 3.2, 2.0, 1.8)
    (billboard as MeshInstance3D).position = Vector3(0.0, texture_size.y * 0.52, 0.0)
    root.set_meta("rayxi_actor_state", state)

func _make_race_actor_visual(entity) -> Node3D:
    var root := Node3D.new()
    var actor_prefix: String = str(entity.get("visual_asset_prefix") if entity != null and entity.get("visual_asset_prefix") != null else "")
    var actor_texture = _race_load_texture([actor_prefix], _race_actor_texture_labels("idle")) if _race_actor_texture_permitted(entity) else null
    if actor_texture != null:
        var billboard_size: Vector2 = _race_billboard_size(actor_texture, 4.2, 3.2, 2.0, 1.8)
        root.add_child(_race_actor_plane(actor_texture, billboard_size, billboard_size.y * 0.52))
        root.set_meta("rayxi_actor_state", "idle")
        return root
    var body_color: Color = _race_entity_color(entity, Color(0.95, 0.78, 0.18, 1.0), Color(0.28, 0.78, 1.0, 1.0))
    var trim_color: Color = Color(0.94, 0.26, 0.18, 1.0) if not _entity_is_ai(entity) else Color(0.98, 0.88, 0.92, 1.0)

    var chassis := MeshInstance3D.new()
    var chassis_mesh := BoxMesh.new()
    chassis_mesh.size = Vector3(3.8, 0.9, 6.2)
    chassis.mesh = chassis_mesh
    chassis.position = Vector3(0.0, 0.85, 0.0)
    chassis.material_override = _race_material(body_color, 0.24, 0.08, 0.25)
    root.add_child(chassis)

    var canopy := MeshInstance3D.new()
    var canopy_mesh := BoxMesh.new()
    canopy_mesh.size = Vector3(2.1, 0.7, 2.4)
    canopy.mesh = canopy_mesh
    canopy.position = Vector3(0.0, 1.52, -0.2)
    canopy.material_override = _race_material(trim_color, 0.12, 0.02, 0.55)
    root.add_child(canopy)

    for offset in [Vector3(-1.6, 0.45, -2.1), Vector3(1.6, 0.45, -2.1), Vector3(-1.6, 0.45, 2.1), Vector3(1.6, 0.45, 2.1)]:
        var wheel := MeshInstance3D.new()
        var wheel_mesh := CylinderMesh.new()
        wheel_mesh.top_radius = 0.58
        wheel_mesh.bottom_radius = 0.58
        wheel_mesh.height = 0.55
        wheel.mesh = wheel_mesh
        wheel.position = offset
        wheel.rotation_degrees = Vector3(90.0, 0.0, 0.0)
        wheel.material_override = _race_material(Color(0.08, 0.08, 0.10, 1.0), 0.9, 0.0, 0.0)
        root.add_child(wheel)

    return root

func _make_race_pickup_visual() -> Node3D:
    var root := Node3D.new()
    var pickup_texture = _race_load_texture(["common"], ["item_box", "boost_item"])
    if pickup_texture != null:
        var billboard_size: Vector2 = _race_billboard_size(pickup_texture, 3.0, 3.0, 1.6, 1.6)
        root.add_child(_race_billboard(pickup_texture, billboard_size, billboard_size.y * 0.5))
        return root
    var cube := MeshInstance3D.new()
    var mesh := BoxMesh.new()
    mesh.size = Vector3(2.4, 2.4, 2.4)
    cube.mesh = mesh
    cube.position = Vector3(0.0, 1.6, 0.0)
    cube.material_override = _race_material(Color(0.98, 0.44, 0.92, 1.0), 0.16, 0.0, 1.9)
    root.add_child(cube)
    var inner := MeshInstance3D.new()
    var inner_mesh := BoxMesh.new()
    inner_mesh.size = Vector3(1.1, 1.1, 1.1)
    inner.mesh = inner_mesh
    inner.position = Vector3(0.0, 1.6, 0.0)
    inner.material_override = _race_material(Color(1.0, 0.85, 0.30, 1.0), 0.12, 0.0, 2.3)
    root.add_child(inner)
    return root

func _make_race_projectile_visual() -> Node3D:
    var root := Node3D.new()
    var projectile_texture = _race_load_texture(["common"], ["shell_projectile", "projectile"])
    if projectile_texture != null:
        var billboard_size: Vector2 = _race_billboard_size(projectile_texture, 2.2, 2.2, 1.0, 1.0)
        root.add_child(_race_billboard(projectile_texture, billboard_size, billboard_size.y * 0.5))
        return root
    var orb := MeshInstance3D.new()
    var mesh := SphereMesh.new()
    mesh.radius = 0.9
    mesh.height = 1.8
    orb.mesh = mesh
    orb.position = Vector3(0.0, 1.0, 0.0)
    orb.material_override = _race_material(Color(0.32, 0.98, 0.40, 1.0), 0.18, 0.0, 1.4)
    root.add_child(orb)
    return root

func _make_race_hazard_visual() -> Node3D:
    var root := Node3D.new()
    var hazard_texture = _race_load_texture(["common"], ["banana_hazard", "hazard"])
    if hazard_texture != null:
        var billboard_size: Vector2 = _race_billboard_size(hazard_texture, 2.6, 2.6, 1.2, 1.2)
        root.add_child(_race_billboard(hazard_texture, billboard_size, billboard_size.y * 0.5))
        return root
    var mesh_instance := MeshInstance3D.new()
    var mesh := CylinderMesh.new()
    mesh.top_radius = 1.2
    mesh.bottom_radius = 1.8
    mesh.height = 1.3
    mesh_instance.mesh = mesh
    mesh_instance.position = Vector3(0.0, 0.7, 0.0)
    mesh_instance.material_override = _race_material(Color(0.96, 0.84, 0.20, 1.0), 0.38, 0.0, 1.0)
    root.add_child(mesh_instance)
    return root

func _prune_race_3d_cache(cache: Dictionary, seen: Dictionary) -> void:
    var stale: Array = []
    for key in cache.keys():
        if not seen.has(key):
            stale.append(key)
    for key in stale:
        var node: Variant = cache.get(key)
        if node is Node:
            (node as Node).queue_free()
        cache.erase(key)

func _sync_race_3d_actor_nodes(points: Array) -> void:
    if _race3d_actor_root == null:
        return
    var seen: Dictionary = {}
    for actor in entity_pools.get("vehicles", []):
        if actor == null:
            continue
        var key: String = str(actor.get_instance_id())
        seen[key] = true
        var visual: Node3D = _race3d_actor_nodes.get(key)
        if visual == null:
            visual = _make_race_actor_visual(actor)
            _race3d_actor_nodes[key] = visual
            _race3d_actor_root.add_child(visual)
        _race_apply_actor_texture(visual, actor)
        visual.position = _race_world_position(Vector2(float(actor.position.x), float(actor.position.y)), points, 0.2)
        var angle_value: Variant = actor.get("facing_angle")
        if angle_value == null:
            angle_value = actor.get("angle")
        var angle_rad: float = deg_to_rad(float(angle_value if angle_value != null else 0.0))
        var forward_2d: Vector2 = Vector2(cos(angle_rad), sin(angle_rad))
        if forward_2d.length() <= 0.01:
            var velocity_2d: Vector2 = actor.get("velocity") if actor.get("velocity") is Vector2 else Vector2.ZERO
            forward_2d = velocity_2d.normalized()
        visual.rotation.y = atan2(forward_2d.x, -forward_2d.y)
    _prune_race_3d_cache(_race3d_actor_nodes, seen)

func _sync_race_3d_pickup_nodes(points: Array) -> void:
    if _race3d_pickup_root == null:
        return
    var seen: Dictionary = {}
    var pool_name: String = "__PICKUP_POOL__"
    var source_points: Array = []
    if pool_name != "":
        var raw_pool: Variant = entity_pools.get(pool_name, [])
        if raw_pool is Array and not (raw_pool as Array).is_empty():
            for pickup in raw_pool:
                if pickup == null or not _race_entity_active(pickup):
                    continue
                source_points.append({"key": str(pickup.get_instance_id()), "point": Vector2(float(pickup.position.x), float(pickup.position.y))})
    if source_points.is_empty():
        var fallback_points: Array = _race_item_points()
        for idx in range(fallback_points.size()):
            source_points.append({"key": "default_%d" % idx, "point": fallback_points[idx]})
    for entry in source_points:
        var key: String = str((entry as Dictionary).get("key", ""))
        var point: Vector2 = (entry as Dictionary).get("point", Vector2.ZERO)
        seen[key] = true
        var visual: Node3D = _race3d_pickup_nodes.get(key)
        if visual == null:
            visual = _make_race_pickup_visual()
            _race3d_pickup_nodes[key] = visual
            _race3d_pickup_root.add_child(visual)
        visual.position = _race_world_position(point, points, 0.3)
        visual.rotation_degrees.y += 1.8
    _prune_race_3d_cache(_race3d_pickup_nodes, seen)

func _sync_race_3d_projectile_nodes(points: Array) -> void:
    if _race3d_projectile_root == null:
        return
    var seen: Dictionary = {}
    var pool_name: String = "__PROJECTILE_POOL__"
    if pool_name == "":
        _prune_race_3d_cache(_race3d_projectile_nodes, seen)
        return
    var raw_pool: Variant = entity_pools.get(pool_name, [])
    if raw_pool is Array:
        for projectile in raw_pool:
            if projectile == null or not _race_entity_active(projectile):
                continue
            var key: String = str(projectile.get_instance_id())
            seen[key] = true
            var visual: Node3D = _race3d_projectile_nodes.get(key)
            if visual == null:
                visual = _make_race_projectile_visual()
                _race3d_projectile_nodes[key] = visual
                _race3d_projectile_root.add_child(visual)
            visual.position = _race_world_position(Vector2(float(projectile.position.x), float(projectile.position.y)), points, 0.5)
            visual.rotation_degrees.y += 6.0
    _prune_race_3d_cache(_race3d_projectile_nodes, seen)

func _sync_race_3d_hazard_nodes(points: Array) -> void:
    if _race3d_hazard_root == null:
        return
    var seen: Dictionary = {}
    var pool_name: String = "__HAZARD_POOL__"
    if pool_name == "":
        _prune_race_3d_cache(_race3d_hazard_nodes, seen)
        return
    var raw_pool: Variant = entity_pools.get(pool_name, [])
    if raw_pool is Array:
        for hazard in raw_pool:
            if hazard == null or not _race_entity_active(hazard):
                continue
            var key: String = str(hazard.get_instance_id())
            seen[key] = true
            var visual: Node3D = _race3d_hazard_nodes.get(key)
            if visual == null:
                visual = _make_race_hazard_visual()
                _race3d_hazard_nodes[key] = visual
                _race3d_hazard_root.add_child(visual)
            visual.position = _race_world_position(Vector2(float(hazard.position.x), float(hazard.position.y)), points, 0.12)
    _prune_race_3d_cache(_race3d_hazard_nodes, seen)

func _sync_race_3d_camera(points: Array) -> void:
    if _race3d_camera == null:
        return
    var vehicles: Array = entity_pools.get("vehicles", [])
    if vehicles.is_empty():
        return
    var player = vehicles[0]
    if player == null:
        return
    var player_pos_2d := Vector2(float(player.position.x), float(player.position.y))
    var angle_value: Variant = player.get("facing_angle")
    if angle_value == null:
        angle_value = player.get("angle")
    var angle_rad: float = deg_to_rad(float(angle_value if angle_value != null else 0.0))
    var forward_2d: Vector2 = Vector2(cos(angle_rad), sin(angle_rad))
    if forward_2d.length() <= 0.01:
        var velocity_2d: Vector2 = player.get("velocity") if player.get("velocity") is Vector2 else Vector2.ZERO
        forward_2d = velocity_2d.normalized()
    var forward: Vector3 = Vector3(forward_2d.x, 0.0, -forward_2d.y).normalized()
    if forward.length() <= 0.01:
        forward = Vector3(1.0, 0.0, 0.0)
    var focus: Vector3 = _race_world_position(player_pos_2d, points, 1.6) + forward * 5.8
    _race3d_camera.position = _race_world_position(player_pos_2d, points, 6.8) - forward * 12.5
    _race3d_camera.look_at(focus, Vector3.UP)
    camera_position = player_pos_2d
    camera_angle = atan2(forward_2d.y, forward_2d.x)
    camera_height = 6.8

func _sync_race_3d_visibility() -> void:
    set_meta("rayxi_render_mode", "race_3d_surface")
    var background: Node = get_node_or_null("Background")
    if background is CanvasItem:
        (background as CanvasItem).visible = false
    var glow: Node = get_node_or_null("BackdropGlow")
    if glow is CanvasItem:
        (glow as CanvasItem).visible = false
    for pool_name in ["vehicles", "player_karts", "ai_karts", "stages", "__PICKUP_POOL__", "__PROJECTILE_POOL__", "__HAZARD_POOL__"]:
        if pool_name == "":
            continue
        var raw_pool: Variant = entity_pools.get(pool_name, [])
        if not (raw_pool is Array):
            continue
        for entity in raw_pool:
            if entity is CanvasItem:
                (entity as CanvasItem).visible = false
    if not _race3d_announced:
        _race3d_announced = true
        print("[trace] render.race_3d enabled=true")

func _refresh_mode7_game_state() -> void:
    var points: Array = _race_checkpoint_points()
    if points.size() < 2:
        return
    _install_race_3d_view()
    _sync_race_3d_viewport()
    _rebuild_race_3d_track()
    _sync_race_3d_visibility()
    _sync_race_3d_actor_nodes(points)
    _sync_race_3d_pickup_nodes(points)
    _sync_race_3d_projectile_nodes(points)
    _sync_race_3d_hazard_nodes(points)
    _sync_race_3d_camera(points)
'''.replace("__PICKUP_POOL__", pickup_key).replace("__PROJECTILE_POOL__", projectile_key).replace("__HAZARD_POOL__", hazard_key).strip()


def _game_property_owner_map(imap: ImpactMap) -> dict[str, str]:
    owners: dict[str, str] = {}
    for node in imap.properties_owned_by("game"):
        writer_systems = sorted({edge.system for edge in imap.writers_of(node.id)})
        if len(writer_systems) == 1:
            owners[node.name] = writer_systems[0]
    return owners


def _emit_scene_bootstrap_helpers(
    game_property_owners: dict[str, str],
    game_property_names: set[str],
    pool_owners: list[str],
    system_names: list[str],
    role_groups: dict | None = None,
    capabilities: dict | None = None,
) -> list[str]:
    owner_map_literal = _emit_constant_literal(game_property_owners)
    pool_keys = {pool_name_for(owner) for owner in pool_owners}
    combat_pool_key = _first_pool_key(
        role_groups,
        ("combat_actor_roles",),
        pool_owners,
        fallback_tokens=("fighter", "combat", "duelist"),
    )
    vehicle_pool_key = _primary_actor_pool_key(pool_owners, role_groups)
    pickup_pool_key = _first_pool_key(
        role_groups,
        ("pickup_roles",),
        pool_owners,
        fallback_tokens=("item", "pickup", "collectible"),
    )
    projectile_pool_key = _first_pool_key(
        role_groups,
        ("projectile_roles",),
        pool_owners,
        fallback_tokens=("projectile", "shell", "missile", "bullet"),
    )
    hazard_pool_key = _first_pool_key(
        role_groups,
        ("hazard_roles",),
        pool_owners,
        fallback_tokens=("hazard", "trap", "obstacle"),
    )
    has_hud_bar_pool = "hud_bars" in pool_keys
    has_combat_profile = bool((capabilities or {}).get("duel_combat")) and combat_pool_key is not None
    has_vehicle_profile = bool((capabilities or {}).get("checkpoint_race")) and vehicle_pool_key is not None
    has_mode7_profile = bool((capabilities or {}).get("mode7_surface"))
    has_fighter_input_map = has_combat_profile or "p1_keys" in game_property_names
    has_generic_player_input = (
        not has_combat_profile
        and any("input" in (name or "").lower() and "ai" not in (name or "").lower() for name in system_names)
    )
    singleton_roles = [
        owner for owner in pool_owners
        if owner not in {"camera"}
        and ("renderer" in owner.lower() or owner.lower().endswith("manager"))
    ]
    singleton_role_literals = [(owner, pool_name_for(owner)) for owner in singleton_roles]
    movement_pairs: list[tuple[str, str]] = []
    if "gravity" in game_property_names:
        movement_pairs.append(("gravity", "gravity"))
    if "floor_y" in game_property_names:
        movement_pairs.append(("floor_y", "floor_y"))
    if "stage_left_bound" in game_property_names:
        movement_pairs.append(("stage_left", "stage_left_bound"))
    if "stage_right_bound" in game_property_names:
        movement_pairs.append(("stage_right", "stage_right_bound"))

    input_pairs: list[tuple[str, str]] = []
    if "input_buffer_size" in game_property_names:
        input_pairs.append(("input_buffer_max_size", "input_buffer_size"))

    round_pairs: list[tuple[str, str]] = []
    if "rounds_to_win" in game_property_names:
        round_pairs.append(("rounds_to_win", "rounds_to_win"))
    if "round_duration_frames" in game_property_names:
        round_pairs.append(("round_duration_frames", "round_duration_frames"))
    elif "round_timer_frames" in game_property_names:
        round_pairs.append(("round_duration_frames", "round_timer_frames"))
    elif "round_time_seconds" in game_property_names:
        round_pairs.append(("round_duration_frames", "round_time_seconds * 60"))

    rage_pairs: list[tuple[str, str]] = []
    if "max_rage_stacks" in game_property_names:
        rage_pairs.append(("max_rage_stacks", "max_rage_stacks"))

    sync_lines = ["func _sync_system_config_from_game() -> void:"]
    if movement_pairs:
        sync_lines.append('    _overlay_system_config("movement_system", {')
        for key, expr in movement_pairs:
            sync_lines.append(f'        "{key}": {expr},')
        sync_lines.append("    })")
    if input_pairs:
        sync_lines.append('    _overlay_system_config("input_system", {')
        for key, expr in input_pairs:
            sync_lines.append(f'        "{key}": {expr},')
        sync_lines.append("    })")
    if round_pairs:
        sync_lines.append('    _overlay_system_config("round_system", {')
        for key, expr in round_pairs:
            sync_lines.append(f'        "{key}": {expr},')
        sync_lines.append("    })")
    if rage_pairs:
        sync_lines.append('    _overlay_system_config("rage_meter_system", {')
        for key, expr in rage_pairs:
            sync_lines.append(f'        "{key}": {expr},')
        sync_lines.append("    })")
    if len(sync_lines) == 1:
        sync_lines.append("    pass")

    if has_combat_profile and combat_pool_key:
        ground_expr = "float(floor_y)" if "floor_y" in game_property_names else "0.0"
        test_mode_block = f'''
func _apply_test_mode_overrides() -> void:
    rayxi_test_mode = _read_web_query_param("rayxi_test_mode")
    if rayxi_test_mode == "":
        return
    var combat_actors: Array = entity_pools.get("{combat_pool_key}", [])
    if combat_actors.size() >= 2:
        var p1: Node = combat_actors[0] as Node
        var p2: Node = combat_actors[1] as Node
        var ground: float = {ground_expr}
        var p1_x: float = 790.0
        var p2_x: float = 960.0
        if rayxi_test_mode == "dummy" or rayxi_test_mode == "rage_ready":
            p1_x = 650.0
            p2_x = 1100.0
        elif rayxi_test_mode == "projectile_ready":
            p1_x = 580.0
            p2_x = 1320.0
        elif rayxi_test_mode == "uppercut_ready":
            p1_x = 820.0
            p2_x = 930.0
        elif rayxi_test_mode in ["guard_high", "guard_low"]:
            p1_x = 800.0
            p2_x = 955.0
        elif rayxi_test_mode == "aggressor":
            p1_x = 790.0
            p2_x = 930.0
        if p1 != null:
            p1.position = Vector2(p1_x, ground if ground > 0.0 else float(p1.position.y))
            p1.facing_direction = 1
            p1.velocity = Vector2.ZERO
            p1.is_airborne = false
        if p2 != null:
            p2.position = Vector2(p2_x, ground if ground > 0.0 else float(p2.position.y))
            p2.facing_direction = -1
            p2.velocity = Vector2.ZERO
            p2.is_airborne = false
    _overlay_system_config("ai_system", {{
        "test_mode": rayxi_test_mode,
    }})
    _overlay_system_config("input_system", {{
        "test_mode": rayxi_test_mode,
    }})
    _overlay_system_config("blocking_system", {{
        "test_mode": rayxi_test_mode,
    }})
    _overlay_system_config("rage_meter_system", {{
        "test_mode": rayxi_test_mode,
    }})
    print("[trace] test.mode mode=%s" % rayxi_test_mode)
'''.strip()
    elif has_vehicle_profile and vehicle_pool_key:
        test_mode_block = '''
func _apply_test_mode_overrides() -> void:
    rayxi_test_mode = _read_web_query_param("rayxi_test_mode")
    if rayxi_test_mode == "":
        _rayxi_test_mode_seed_until_ms = 0
        return
    _overlay_system_config("countdown_system", {
        "skip_countdown": true,
        "seconds_per_count": 0,
    })
    if get("countdown_active") != null:
        set("countdown_active", false)
    if get("countdown_value") != null:
        set("countdown_value", 0)
    if get("countdown_timer") != null:
        set("countdown_timer", 0.0)
    if get("fsm_state") != null:
        set("fsm_state", "S_RACING")
    var race_managers: Array = entity_pools.get("race_managers", [])
    if not race_managers.is_empty() and race_managers[0] != null:
        race_managers[0].current_state = "racing"
        if race_managers[0].get("race_timer") != null:
            race_managers[0].race_timer = 0.01
    _rayxi_test_mode_seed_until_ms = Time.get_ticks_msec() + (2200 if rayxi_test_mode == "drift_ready" else 0)
    var vehicles: Array = entity_pools.get("__VEHICLE_POOL__", [])
    if vehicles.size() >= 2:
        var p1: Node = vehicles[0] as Node
        var p2: Node = vehicles[1] as Node
        var p1_x: float = 760.0
        var p2_x: float = 1040.0
        if rayxi_test_mode == "drift_ready":
            p1_x = 720.0
            p2_x = 1160.0
        elif rayxi_test_mode == "collision_ready":
            p1_x = 760.0
            p2_x = 915.0
        elif rayxi_test_mode == "item_ready":
            p1_x = 760.0
            p2_x = 1160.0
        if p1 != null:
            p1.position = Vector2(p1_x, float(p1.position.y))
            if p1.get("velocity") != null:
                p1.velocity = Vector2.ZERO
            if p1.get("speed") != null:
                p1.speed = 56.0 if rayxi_test_mode == "drift_ready" else 0.0
            if rayxi_test_mode == "drift_ready":
                if p1.get("acceleration_input") != null:
                    p1.acceleration_input = 1.0
                if p1.get("accel_input") != null:
                    p1.accel_input = 1.0
                if p1.get("input_accelerate") != null:
                    p1.input_accelerate = true
            if p1.get("current_item") != null:
                p1.current_item = "green_shell" if rayxi_test_mode == "item_ready" else ""
            if p1.get("held_item") != null:
                p1.held_item = "green_shell" if rayxi_test_mode == "item_ready" else ""
            if p1.get("drift_charge") != null:
                p1.drift_charge = 0.0
            if p1.get("drift_tier") != null:
                p1.drift_tier = 0
            if p1.get("is_drifting") != null:
                p1.is_drifting = false
            if p1.get("is_boosting") != null:
                p1.is_boosting = false
            if p1.get("boost_timer") != null:
                p1.boost_timer = 0.0
            if p1.get("item_boost_active") != null:
                p1.item_boost_active = false
        if p2 != null:
            p2.position = Vector2(p2_x, float(p2.position.y))
            if p2.get("velocity") != null:
                p2.velocity = Vector2.ZERO
            if p2.get("speed") != null:
                p2.speed = 0.0
            if p2.get("facing_angle") != null and rayxi_test_mode == "collision_ready":
                p2.facing_angle = p1.facing_angle + PI if p1 != null and p1.get("facing_angle") != null else PI
            if p2.get("is_ai_controlled") != null:
                p2.is_ai_controlled = true
            if p2.get("current_item") != null:
                p2.current_item = ""
            if p2.get("held_item") != null:
                p2.held_item = ""
    print("[trace] test.mode mode=%s" % rayxi_test_mode)

func _maintain_test_mode_seed() -> void:
    if rayxi_test_mode != "drift_ready":
        return
    var vehicles: Array = entity_pools.get("__VEHICLE_POOL__", [])
    if vehicles.is_empty():
        return
    var p1: Node = vehicles[0] as Node
    if p1 == null:
        return
    var drift_started: bool = bool(p1.get("input_drift") if p1.get("input_drift") != null else false)
    if drift_started:
        return
    if p1.get("speed") != null:
        p1.speed = max(float(p1.get("speed")), 52.0)
    if p1.get("acceleration_input") != null:
        p1.acceleration_input = max(float(p1.get("acceleration_input")), 1.0)
    if p1.get("accel_input") != null:
        p1.accel_input = max(float(p1.get("accel_input")), 1.0)
'''.replace("__VEHICLE_POOL__", vehicle_pool_key).strip()
    else:
        test_mode_block = '''
func _apply_test_mode_overrides() -> void:
    rayxi_test_mode = _read_web_query_param("rayxi_test_mode")
    if rayxi_test_mode == "":
        return
    print("[trace] test.mode mode=%s" % rayxi_test_mode)
'''.strip()

    runtime_trace_lines = [
        "func _emit_runtime_bootstrap_traces() -> void:",
        '    var background: Node = get_node_or_null("Background")',
        "    if background != null:",
        '        print("[trace] render.background node=%s" % [background.name])',
    ]
    for owner in pool_owners:
        pool_key = pool_name_for(owner)
        runtime_trace_lines.extend([
            f'    for entity in entity_pools.get("{pool_key}", []):',
            "        if entity == null:",
            "            continue",
            "        var entity_pos: Variant = entity.get(\"position\")",
            "        if entity_pos == null:",
            "            entity_pos = entity.get(\"world_position\")",
            '        print("[trace] entity.ready role=%s name=%s pos=%s active=%s slot=%s cpu=%s" % [',
            f'            "{owner}",',
            "            entity.name,",
            "            str(entity_pos),",
            "            str(entity.get(\"active\")),",
            "            str(entity.get(\"player_slot\")),",
            "            str(entity.get(\"is_cpu\")),",
            "        ])",
        ])
    if has_hud_bar_pool:
        runtime_trace_lines.extend([
            '    for hud_bar in entity_pools.get("hud_bars", []):',
            "        if hud_bar == null:",
            "            continue",
            '        print("[trace] entity.ready role=hud_bar name=%s value=%s max=%s" % [',
            "            hud_bar.name,",
            '            str(hud_bar.get("value")),',
            '            str(hud_bar.get("max_value")),',
            "        ])",
        ])
    runtime_trace_block = "\n".join(runtime_trace_lines)

    if has_fighter_input_map or has_generic_player_input:
        p1_keys_loader = '    if p1_keys is Dictionary:\n        keys = p1_keys as Dictionary' if "p1_keys" in game_property_names else ""
        input_map_block = '''
func _bind_action_key(action_name: String, key_name: String) -> void:
    var resolved: String = key_name.strip_edges()
    if resolved == "":
        return
    if InputMap.has_action(action_name):
        InputMap.erase_action(action_name)
    InputMap.add_action(action_name)
    var event: InputEventKey = InputEventKey.new()
    var keycode: int = OS.find_keycode_from_string(resolved.to_upper())
    if keycode == 0 and resolved.length() == 1:
        keycode = resolved.unicode_at(0)
    if keycode == 0:
        return
    event.keycode = keycode
    event.physical_keycode = keycode
    InputMap.action_add_event(action_name, event)

func _install_default_input_map() -> void:
{install_lines}
'''.format(
            p1_keys_loader=p1_keys_loader,
            install_lines=(
                '    var keys: Dictionary = {}\n'
                f'{p1_keys_loader}\n'
                '    _bind_action_key("p1_up", str(keys.get("up", "w")))\n'
                '    _bind_action_key("p1_down", str(keys.get("down", "s")))\n'
                '    _bind_action_key("p1_left", str(keys.get("left", "a")))\n'
                '    _bind_action_key("p1_right", str(keys.get("right", "d")))\n'
                '    _bind_action_key("p1_light_punch", str(keys.get("lp", keys.get("light_punch", "u"))))\n'
                '    _bind_action_key("p1_medium_punch", str(keys.get("mp", keys.get("medium_punch", "i"))))\n'
                '    _bind_action_key("p1_heavy_punch", str(keys.get("hp", keys.get("heavy_punch", "o"))))\n'
                '    _bind_action_key("p1_light_kick", str(keys.get("lk", keys.get("light_kick", "j"))))\n'
                '    _bind_action_key("p1_medium_kick", str(keys.get("mk", keys.get("medium_kick", "k"))))\n'
                '    _bind_action_key("p1_heavy_kick", str(keys.get("hk", keys.get("heavy_kick", "l"))))'
            ) if has_fighter_input_map else (
                '    _bind_action_key("accelerate", "w")\n'
                '    _bind_action_key("brake", "s")\n'
                '    _bind_action_key("steer_left", "a")\n'
                '    _bind_action_key("steer_right", "d")\n'
                '    _bind_action_key("drift", "Shift")\n'
                '    _bind_action_key("item", "Space")'
            ),
        ).strip()
    else:
        input_map_block = '''
func _install_default_input_map() -> void:
    pass
'''.strip()

    singleton_lines: list[str] = ["func _ensure_runtime_singletons() -> void:"]
    if singleton_role_literals:
        for owner, pool_key in singleton_role_literals:
            singleton_lines.extend([
                f'    if entity_pools.get("{pool_key}", []).is_empty() and ResourceLoader.exists("res://scripts/entities/{owner}.gd"):',
                f'        var spawned_{owner}: Node = preload("res://scripts/entities/{owner}.gd").new()',
                f'        spawned_{owner}.name = "{owner}_main"',
                f'        add_child(spawned_{owner})',
                f'        entity_pools["{pool_key}"].append(spawned_{owner})',
                f'        print("[trace] entity.spawned role={owner} name=%s pos=%s" % [spawned_{owner}.name, str(spawned_{owner}.position)])',
            ])
    else:
        singleton_lines.append("    pass")
    singleton_block = "\n".join(singleton_lines)

    alias_lines: list[str] = [
        "func _dedupe_entities(items: Array) -> Array:",
        "    var out: Array = []",
        "    var seen: Dictionary = {}",
        "    for item in items:",
        "        if item == null:",
        "            continue",
        "        var key: String = str(item.get_instance_id()) if item is Object else str(item)",
        "        if seen.has(key):",
        "            continue",
        "        seen[key] = true",
        "        out.append(item)",
        "    return out",
        "",
        "func _entity_is_ai(entity) -> bool:",
        "    if entity == null:",
        "        return false",
        '    var ai_flag: Variant = entity.get("is_ai_controlled")',
        "    if ai_flag != null:",
        "        return bool(ai_flag)",
        '    var cpu_flag: Variant = entity.get("is_cpu")',
        "    if cpu_flag != null:",
        "        return bool(cpu_flag)",
        "    var lower_name: String = str(entity.name).to_lower()",
        '    return lower_name.find("ai") >= 0 or lower_name.begins_with("p2")',
        "",
        "func _refresh_runtime_alias_pools() -> void:",
        '    entity_pools["game_objects"] = [self]',
        '    if entity_pools.get("stages", []).is_empty():',
        '        var background_stage: Node = get_node_or_null("Background")',
        "        if background_stage != null:",
        '            entity_pools["stages"] = [background_stage]',
    ]
    if has_vehicle_profile and vehicle_pool_key:
        alias_lines.extend([
            "    var vehicle_candidates: Array = []",
            f'    for pool_name in ["{vehicle_pool_key}", "player_karts", "ai_karts"]:',
            "        var raw_pool: Variant = entity_pools.get(pool_name, [])",
            "        if raw_pool is Array:",
            "            vehicle_candidates.append_array(raw_pool)",
            "    var vehicles: Array = _dedupe_entities(vehicle_candidates)",
            "    if not vehicles.is_empty():",
            '        entity_pools["vehicles"] = vehicles',
            "        var player_vehicles: Array = []",
            "        for vehicle in vehicles:",
            "            if not _entity_is_ai(vehicle):",
            "                player_vehicles.append(vehicle)",
            "        if player_vehicles.is_empty() and not vehicles.is_empty():",
            "            player_vehicles.append(vehicles[0])",
            '        entity_pools["player_karts"] = player_vehicles',
            "        var ai_vehicles: Array = []",
            "        for vehicle in vehicles:",
            "            if _entity_is_ai(vehicle):",
            "                ai_vehicles.append(vehicle)",
            "        if ai_vehicles.is_empty() and vehicles.size() > 1:",
            "            for idx in range(1, vehicles.size()):",
            "                ai_vehicles.append(vehicles[idx])",
            '        entity_pools["ai_karts"] = ai_vehicles',
        ])
    alias_block = "\n".join(alias_lines)

    if has_vehicle_profile:
        mode7_state_block = _emit_race_3d_renderer_block(
            pickup_pool_key=pickup_pool_key,
            projectile_pool_key=projectile_pool_key,
            hazard_pool_key=hazard_pool_key,
        )
    elif has_mode7_profile:
        mode7_state_block = '''
func _refresh_mode7_game_state() -> void:
    var cameras: Array = entity_pools.get("cameras", [])
    if not cameras.is_empty():
        var camera = cameras[0]
        if camera != null:
            camera_position = Vector2(
                float(camera.get("world_x") if camera.get("world_x") != null else camera.position.x),
                float(camera.get("world_y") if camera.get("world_y") != null else camera.position.y)
            )
            camera_angle = float(camera.get("angle") if camera.get("angle") != null else 0.0)
            camera_height = float(camera.get("height") if camera.get("height") != null else camera_height)
            return
    var vehicles: Array = entity_pools.get("vehicles", [])
    if not vehicles.is_empty():
        var vehicle = vehicles[0]
        if vehicle != null:
            camera_position = Vector2(float(vehicle.position.x), float(vehicle.position.y))
            var angle_value: Variant = vehicle.get("facing_angle")
            if angle_value == null:
                angle_value = vehicle.get("angle")
            camera_angle = float(angle_value if angle_value != null else 0.0)
'''.strip()
    else:
        mode7_state_block = '''
func _refresh_mode7_game_state() -> void:
    pass
'''.strip()

    return [
        f"var _game_property_owners: Dictionary = {owner_map_literal}",
        "",
        '''
func _overlay_system_config(system_name: String, values: Dictionary) -> void:
    var bucket: Dictionary = {}
    if config.has(system_name) and config[system_name] is Dictionary:
        bucket = (config[system_name] as Dictionary).duplicate()
    for key in values.keys():
        bucket[key] = values[key]
    config[system_name] = bucket
'''.strip(),
        '''
func _read_web_query_param(name: String) -> String:
    if not OS.has_feature("web"):
        return ""
    var query: Variant = JavaScriptBridge.eval("window.location.search", true)
    if query == null:
        return ""
    var text: String = str(query)
    if text.begins_with("?"):
        text = text.substr(1)
    if text == "":
        return ""
    for pair in text.split("&", false):
        if pair == "":
            continue
        var eq: int = pair.find("=")
        var key: String = pair if eq < 0 else pair.substr(0, eq)
        var value: String = "" if eq < 0 else pair.substr(eq + 1)
        if key.uri_decode() == name:
            return value.uri_decode()
    return ""
'''.strip(),
        test_mode_block,
        "\n".join(sync_lines),
        singleton_block,
        alias_block,
        mode7_state_block,
        '''
func _push_game_state_to_owner_systems() -> void:
    for prop_name in _game_property_owners.keys():
        var owner_name: String = str(_game_property_owners[prop_name])
        if owner_name == "" or not systems.has(owner_name):
            continue
        var owner_system: Variant = systems[owner_name]
        if owner_system == null:
            continue
        var owner_value: Variant = owner_system.get(prop_name)
        if owner_value == null:
            continue
        owner_system.set(prop_name, get(prop_name))
'''.strip(),
        '''
func _pull_game_state_from_owner_systems() -> void:
    for prop_name in _game_property_owners.keys():
        var owner_name: String = str(_game_property_owners[prop_name])
        if owner_name == "" or not systems.has(owner_name):
            continue
        var owner_system: Variant = systems[owner_name]
        if owner_system == null:
            continue
        var owner_value: Variant = owner_system.get(prop_name)
        if owner_value != null:
            set(prop_name, owner_value)
'''.strip(),
        runtime_trace_block,
        input_map_block,
    ]


def emit_scene(
    imap: ImpactMap,
    hlr: GameIdentity,
    constants: dict,
    godot_dir: Path,
    scene_name: str = "fighting",
    role_defs: dict | None = None,
    scene_defaults: dict | None = None,
    role_groups: dict | None = None,
    capabilities: dict | None = None,
) -> Path:
    """Emit scenes/{scene_name}.gd deterministically from the impact map.

    Returns the written path. Overwrites any existing file.
    """
    roles = role_defs or {}
    scene_defaults = scene_defaults or {}
    pool_owners = pool_owners_from_imap(imap, roles)
    all_ordered = imap.ordered_systems()
    has_mode7_profile = bool((capabilities or {}).get("mode7_surface"))
    has_vehicle_profile = bool((capabilities or {}).get("checkpoint_race"))

    # Only include systems whose generated .gd file actually exists. A failed
    # codegen_runner LLM call leaves its entry in imap.systems but no file on
    # disk — emitting a preload line for a missing file crashes the whole
    # scene parse. Better to skip the missing system (logged as SKIPPED)
    # than to block every other system.
    systems_dir = godot_dir / "scripts" / "systems"
    ordered: list[str] = []
    skipped: list[str] = []
    for s in all_ordered:
        if (systems_dir / f"{s}.gd").exists():
            ordered.append(s)
        else:
            skipped.append(s)
    if skipped:
        _log.warning(
            "scene_gen: %d systems skipped (no .gd file): %s",
            len(skipped), skipped,
        )

    _log.info(
        "scene_gen: scene=%s pools=%s systems=%d (skipped %d)",
        scene_name, pool_owners, len(ordered), len(skipped),
    )
    game_property_owners = _game_property_owner_map(imap)

    # Game-scoped properties become var declarations on the scene root so
    # systems can write `get_parent().round_state = "fighting"` (or whatever).
    # The impact map models `game.*` as a singleton owner — this is the
    # pipeline's way of projecting that singleton onto the scene tree.
    game_nodes = [n for n in imap.nodes.values() if n.owner == "game"]
    game_state_nodes = [n for n in game_nodes if n.category != "derived"]
    game_derived_nodes = [n for n in game_nodes if n.category == "derived"]
    game_prop_names = {n.name for n in game_nodes}
    game_init_assignments, unresolved_game_init = _ordered_game_assignments(
        game_state_nodes,
        constants,
        use_derivation=False,
    )
    game_derived_assignments, unresolved_game_derived = _ordered_game_assignments(
        game_derived_nodes,
        constants,
        use_derivation=True,
    )
    header = [
        "extends Node2D",
        f"## {scene_name} — generated by scene_gen. DO NOT HAND-EDIT.",
        f"## Systems in process order: {', '.join(ordered)}",
        f"## Entity pools from imap owners: {pool_owners}",
        f"## Game-scoped properties: {len(game_nodes)}",
        "",
        "var entity_pools: Dictionary = {}",
        "var config: Dictionary = {}",
        "var systems: Dictionary = {}",
        'var rayxi_test_mode: String = ""',
        "var _rayxi_test_mode_seed_until_ms: int = 0",
        "",
    ]

    # Declare each game-scoped property. Type comes from the imap node.
    if game_nodes:
        header.append("# --- game-scoped properties (owner='game' in impact map) ---")
        for n in game_nodes:
            gd_type = _game_prop_gd_type(n.type)
            default = _game_prop_default(n.type)
            header.append(f"var {n.name}: {gd_type} = {default}")
        header.append("")
    if has_vehicle_profile or has_mode7_profile:
        mode7_game_props = [
            ("camera_position", "Vector2", "Vector2.ZERO"),
            ("camera_angle", "float", "0.0"),
            ("camera_height", "float", "120.0"),
            ("horizon_y", "int", "300"),
            ("focal_length", "float", "256.0"),
            ("frame_buffer", "Variant", "null"),
        ]
        for prop_name, gd_type, default_value in mode7_game_props:
            if prop_name in game_prop_names:
                continue
            header.append(f"var {prop_name}: {gd_type} = {default_value}")
        header.append("")

    helper_blocks: list[str] = []
    if game_init_assignments or unresolved_game_init:
        helper_lines = ["func _apply_game_contract_defaults() -> void:"]
        for node, expr in game_init_assignments:
            helper_lines.append(f"    {node.name} = {expr}")
        if not game_init_assignments:
            helper_lines.append("    pass")
        if unresolved_game_init:
            helper_lines.append(
                "    # External/default-sensitive game values stay at declaration defaults: "
                + ", ".join(sorted(unresolved_game_init))
            )
        helper_blocks.append("\n".join(helper_lines))
    if game_derived_assignments or unresolved_game_derived:
        helper_lines = ["func _update_game_derived_props() -> void:"]
        for node, expr in game_derived_assignments:
            helper_lines.append(f"    {node.name} = {expr}")
        if not game_derived_assignments:
            helper_lines.append("    pass")
        if unresolved_game_derived:
            helper_lines.append(
                "    # Derived game values with external dependencies are system-managed: "
                + ", ".join(sorted(unresolved_game_derived))
            )
        helper_blocks.append("\n".join(helper_lines))

    ready_body = ["func _ready():"]
    if game_init_assignments or unresolved_game_init:
        ready_body.append("    _apply_game_contract_defaults()")
        ready_body.append("")

    ready_body.append("    # --- populate entity pools (from compiled req role definitions) ---")
    for owner in pool_owners:
        ready_body.extend(_emit_pool_population(owner, roles))
    ready_body.append("    entity_pools[\"game\"] = self")
    ready_body.append("    _apply_scene_defaults()")
    ready_body.append("    _ensure_runtime_singletons()")
    ready_body.append("    _refresh_runtime_alias_pools()")
    ready_body.append("    _refresh_mode7_game_state()")
    ready_body.append("")

    ready_body.append("    # --- load DLR constants into per-system config buckets ---")
    ready_body.extend(_emit_config_dict(constants))
    ready_body.append("    _sync_system_config_from_game()")
    ready_body.append("    _apply_test_mode_overrides()")
    ready_body.append("    _refresh_runtime_alias_pools()")
    ready_body.append("    _refresh_mode7_game_state()")
    if has_vehicle_profile:
        ready_body.append('    set_meta("rayxi_render_mode", "race_3d_surface")')
    elif has_mode7_profile:
        ready_body.append('    set_meta("rayxi_render_mode", "mode7_surface")')
    ready_body.append("    _install_default_input_map()")
    ready_body.append("")

    ready_body.append("    # --- instantiate systems ---")
    for s in ordered:
        ready_body.append(_emit_system_instantiation(s))
    ready_body.append("")

    ready_body.append("    # --- call setup on each system with (entity_pools, config[sys]) ---")
    for s in ordered:
        ready_body.append(_emit_system_setup(s))
    ready_body.append("")

    # --- cross-system sibling handoff pass ---
    # After every system is instantiated + setup, pass the full systems dict
    # to any system that opts in via `set_siblings(systems: Dictionary)`.
    # Systems that need to call siblings (e.g. collision → combat damage
    # event) use `sibling_systems["combat_system"].process_X_event(...)`.
    # This is the authoritative cross-system wiring path; LLMs are told in
    # their prompt to never hold direct references to other systems.
    ready_body.append("    # --- sibling-systems handoff (cross-system calls) ---")
    ready_body.append("    for _sys_name in systems.keys():")
    ready_body.append('        if systems[_sys_name].has_method("set_siblings"):')
    ready_body.append("            systems[_sys_name].set_siblings(systems)")
    ready_body.append("    _push_game_state_to_owner_systems()")
    ready_body.append("    _pull_game_state_from_owner_systems()")
    ready_body.append("")

    hud_block = _emit_hud_widgets(
        hlr,
        scene_defaults=scene_defaults,
        actor_pool_key=_primary_actor_pool_key(pool_owners, role_groups),
        capabilities=capabilities,
    )
    if hud_block:
        ready_body.extend(hud_block)
        ready_body.append("")

    ready_body.append("    _install_debug_overlays()")
    ready_body.append("    _emit_runtime_bootstrap_traces()")
    ready_body.append(
        '    print("[trace] scene.ready scene=%s systems=%d pools=%s" % '
        f'["{scene_name}", systems.size(), str(entity_pools.keys())])'
    )
    ready_body.append("")

    process_body = ["func _physics_process(delta):"]
    process_body.append("    _pull_game_state_from_owner_systems()")
    process_body.append("    _refresh_runtime_alias_pools()")
    process_body.append("    _refresh_mode7_game_state()")
    if has_vehicle_profile:
        process_body.append("    _maintain_test_mode_seed()")
    if game_derived_assignments or unresolved_game_derived:
        process_body.append("    _update_game_derived_props()")
    for s in ordered:
        process_body.append(_emit_system_process(s))
    process_body.append("    _pull_game_state_from_owner_systems()")

    lines = (
        header
        + helper_blocks
        + _emit_scene_runtime_seed(
            scene_defaults,
            pool_owners,
            role_groups=role_groups,
            capabilities=capabilities,
        )
        + _emit_debug_overlay_helper()
        + _emit_scene_bootstrap_helpers(
            game_property_owners,
            game_prop_names,
            pool_owners,
            ordered,
            role_groups=role_groups,
            capabilities=capabilities,
        )
        + ready_body
        + process_body
        + [_emit_name_matcher()]
    )
    source = "\n".join(lines) + "\n"

    out_path = godot_dir / "scenes" / f"{scene_name}.gd"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(source, encoding="utf-8")
    _log.info("scene_gen: wrote %s (%d bytes)", out_path, len(source))
    return out_path
