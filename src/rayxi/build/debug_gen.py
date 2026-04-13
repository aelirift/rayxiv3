from __future__ import annotations

from pathlib import Path


_DEBUG_BOXES_GD = """extends Node2D

var scene_root: Node
var scene_defaults: Dictionary = {}

@export var hurtbox_fill: Color = Color(0.15, 0.85, 1.0, 0.26)
@export var hurtbox_outline: Color = Color(0.15, 0.95, 1.0, 0.95)
@export var hitbox_fill: Color = Color(1.0, 0.2, 0.12, 0.28)
@export var hitbox_outline: Color = Color(1.0, 0.35, 0.2, 0.95)
@export var projectile_fill: Color = Color(1.0, 0.86, 0.12, 0.32)
@export var projectile_outline: Color = Color(1.0, 0.92, 0.38, 0.98)
@export var collision_color: Color = Color(0.45, 1.0, 0.45, 0.8)
@export var checkpoint_color: Color = Color(0.24, 1.0, 0.55, 0.82)
@export var origin_color: Color = Color(1.0, 1.0, 1.0, 0.95)

var _announced_ready: bool = false
var _recent_hitboxes: Dictionary = {}

func setup(root: Node, defaults: Dictionary = {}) -> void:
    scene_root = root
    scene_defaults = defaults.duplicate(true)

func _ready() -> void:
    top_level = true
    z_as_relative = false
    z_index = 4096
    queue_redraw()
    print("[trace] debug.overlay_ready kind=boxes render_mode=%s" % _render_mode())

func _process(_delta: float) -> void:
    _remember_recent_hitboxes(_pools().get("fighters", []))
    queue_redraw()

func _pools() -> Dictionary:
    if scene_root == null:
        return {}
    var pools: Variant = scene_root.get("entity_pools")
    return pools if pools is Dictionary else {}

func _render_mode() -> String:
    if scene_root == null:
        return ""
    return str(scene_root.get_meta("rayxi_render_mode", ""))

func _draw_rect_overlay(rect: Rect2, fill_color: Color, outline_color: Color) -> void:
    draw_rect(rect, fill_color, true)
    draw_rect(rect, outline_color, false, 2.0)

func _rect_like(value: Variant, fallback: Rect2) -> Rect2:
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

func _fighter_hurtbox_rect(fighter) -> Rect2:
    var fallback: Rect2 = Rect2(-60.0, -180.0, 120.0, 180.0)
    var hurtbox: Rect2 = _rect_like(fighter.get("stand_hurtbox"), fallback)
    if bool(fighter.get("is_airborne")):
        hurtbox = _rect_like(fighter.get("air_hurtbox"), hurtbox)
    elif bool(fighter.get("is_crouching")):
        hurtbox = _rect_like(fighter.get("crouch_hurtbox"), hurtbox)
    return Rect2(
        Vector2(float(fighter.position.x) + hurtbox.position.x, float(fighter.position.y) + hurtbox.position.y),
        hurtbox.size
    )

func _rect_union(rects: Array) -> Rect2:
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

func _local_rects_to_world(entity, rects: Array) -> Array:
    var world_rects: Array = []
    for entry in rects:
        if not (entry is Rect2):
            continue
        var rect: Rect2 = entry as Rect2
        world_rects.append(
            Rect2(
                Vector2(float(entity.position.x) + rect.position.x, float(entity.position.y) + rect.position.y),
                rect.size
            )
        )
    return world_rects

func _fighter_hurtbox_rects(fighter) -> Array:
    if fighter != null and fighter.has_method("rayxi_hurtbox_rects_local"):
        var local_rects: Variant = fighter.call("rayxi_hurtbox_rects_local")
        if local_rects is Array and not (local_rects as Array).is_empty():
            return _local_rects_to_world(fighter, local_rects as Array)
    return [_fighter_hurtbox_rect(fighter)]

func _fighter_hitbox_rect(fighter) -> Rect2:
    var hitbox: Variant = fighter.get("active_hitbox")
    if not (hitbox is Dictionary) or (hitbox as Dictionary).is_empty():
        return Rect2()
    var hitbox_dict: Dictionary = hitbox as Dictionary
    var facing: float = 1.0 if float(fighter.get("facing_direction")) >= 0.0 else -1.0
    var width: float = max(float(hitbox_dict.get("width", 80.0)), 8.0)
    var height: float = max(float(hitbox_dict.get("height", 80.0)), 8.0)
    var offset_x: float = float(hitbox_dict.get("offset_x", 0.0)) * facing
    var offset_y: float = float(hitbox_dict.get("offset_y", 0.0))
    return Rect2(
        Vector2(float(fighter.position.x) + offset_x - width * 0.5, float(fighter.position.y) + offset_y - height),
        Vector2(width, height)
    )

func _fighter_hitbox_rects(fighter) -> Array:
    var hitbox: Variant = fighter.get("active_hitbox")
    if not (hitbox is Dictionary) or (hitbox as Dictionary).is_empty():
        return []
    if fighter != null and fighter.has_method("rayxi_active_hitbox_rects_local"):
        var local_rects: Variant = fighter.call("rayxi_active_hitbox_rects_local", hitbox as Dictionary)
        if local_rects is Array and not (local_rects as Array).is_empty():
            return _local_rects_to_world(fighter, local_rects as Array)
    var fallback: Rect2 = _fighter_hitbox_rect(fighter)
    return [fallback] if fallback.size.x > 0.0 and fallback.size.y > 0.0 else []

func _remember_recent_hitboxes(fighters: Array) -> void:
    var expired: Array = []
    for key in _recent_hitboxes.keys():
        var entry: Variant = _recent_hitboxes.get(key)
        if not (entry is Dictionary):
            expired.append(key)
            continue
        var ttl: int = int((entry as Dictionary).get("ttl", 0)) - 1
        if ttl <= 0:
            expired.append(key)
            continue
        var next_entry: Dictionary = (entry as Dictionary).duplicate(true)
        next_entry["ttl"] = ttl
        _recent_hitboxes[key] = next_entry
    for key in expired:
        _recent_hitboxes.erase(key)
    for fighter in fighters:
        if fighter == null:
            continue
        var hit_rect: Rect2 = _rect_union(_fighter_hitbox_rects(fighter))
        if hit_rect.size.x <= 0.0 or hit_rect.size.y <= 0.0:
            continue
        _recent_hitboxes[str(fighter.name)] = {
            "rect": hit_rect,
            "ttl": 8,
        }

func _projectile_rect(projectile) -> Rect2:
    var width: float = float(projectile.get("hitbox_width") if projectile.get("hitbox_width") != null else 36.0)
    var height: float = float(projectile.get("hitbox_height") if projectile.get("hitbox_height") != null else 24.0)
    return Rect2(
        Vector2(float(projectile.position.x) - width * 0.5, float(projectile.position.y) - height * 0.5),
        Vector2(width, height)
    )

func _checkpoint_points() -> Array[Vector2]:
    var points: Array[Vector2] = []
    var pools: Dictionary = _pools()
    var stages: Array = pools.get("stages", []) as Array
    var raw: Variant = []
    if stages is Array and not stages.is_empty():
        var stage: Variant = stages[0]
        if stage != null:
            raw = stage.get("checkpoint_positions")
            if not (raw is Array) or (raw as Array).is_empty():
                raw = stage.get_meta("checkpoint_positions", [])
    if (not (raw is Array) or (raw as Array).is_empty()) and scene_defaults.get("checkpoint_positions") is Array:
        raw = scene_defaults.get("checkpoint_positions")
    if raw is Array:
        for entry in raw:
            if entry is Vector2:
                points.append(entry)
            elif entry is Array and entry.size() >= 2:
                points.append(Vector2(float(entry[0]), float(entry[1])))
    return points

func _mode7_project_point(world_pos: Vector2) -> Vector2:
    if scene_root == null:
        return Vector2(-INF, -INF)
    var cam_pos_value: Variant = scene_root.get("camera_position")
    var cam_pos: Vector2 = cam_pos_value if cam_pos_value is Vector2 else Vector2.ZERO
    var cam_angle: float = float(scene_root.get("camera_angle") if scene_root.get("camera_angle") != null else 0.0)
    var cam_height: float = float(scene_root.get("camera_height") if scene_root.get("camera_height") != null else 120.0)
    var focal: float = float(scene_root.get("focal_length") if scene_root.get("focal_length") != null else 300.0)
    var horizon: float = float(scene_root.get("horizon_y") if scene_root.get("horizon_y") != null else 300.0)
    var viewport_size: Vector2 = get_viewport_rect().size
    var cos_a: float = cos(cam_angle)
    var sin_a: float = sin(cam_angle)
    var dx: float = world_pos.x - cam_pos.x
    var dy: float = world_pos.y - cam_pos.y
    var rx: float = dx * cos_a + dy * sin_a
    var ry: float = -dx * sin_a + dy * cos_a
    if ry <= 8.0:
        return Vector2(-INF, -INF)
    var screen_x: float = viewport_size.x * 0.5 + (rx / ry) * focal
    var screen_y: float = horizon + (cam_height * focal) / ry
    return Vector2(screen_x, screen_y)

func _mode7_marker_anchor(world_pos: Vector2, marker_index: int, marker_total: int) -> Vector2:
    if scene_root == null:
        return Vector2(-INF, -INF)
    var viewport_size: Vector2 = get_viewport_rect().size
    var cam_pos_value: Variant = scene_root.get("camera_position")
    var cam_pos: Vector2 = cam_pos_value if cam_pos_value is Vector2 else Vector2.ZERO
    var cam_angle: float = float(scene_root.get("camera_angle") if scene_root.get("camera_angle") != null else 0.0)
    var horizon: float = float(scene_root.get("horizon_y") if scene_root.get("horizon_y") != null else 300.0)
    var cos_a: float = cos(cam_angle)
    var sin_a: float = sin(cam_angle)
    var dx: float = world_pos.x - cam_pos.x
    var dy: float = world_pos.y - cam_pos.y
    var rx: float = dx * cos_a + dy * sin_a
    var ry: float = -dx * sin_a + dy * cos_a
    var projected: Vector2 = _mode7_project_point(world_pos)
    if is_finite(projected.x) and is_finite(projected.y):
        return Vector2(
            clampf(projected.x, 72.0, viewport_size.x * 0.62),
            clampf(projected.y, max(horizon + 28.0, viewport_size.y * 0.22), viewport_size.y * 0.62)
        )
    var lane_bias: float = clampf(rx / max(abs(rx) + abs(ry), 1.0), -1.0, 1.0)
    var stack_ratio: float = float(marker_index + 1) / float(max(marker_total + 1, 2))
    var marker_x: float = clampf(
        viewport_size.x * 0.28 + lane_bias * viewport_size.x * 0.16,
        84.0,
        viewport_size.x * 0.60
    )
    var marker_y: float = lerpf(max(horizon + 40.0, viewport_size.y * 0.26), viewport_size.y * 0.60, stack_ratio)
    return Vector2(marker_x, marker_y)

func _draw_mode7_marker(center: Vector2, fill: Color, outline: Color, radius: float) -> void:
    if not is_finite(center.x) or not is_finite(center.y):
        return
    draw_circle(center, radius, fill)
    draw_arc(center, radius + 8.0, 0.0, TAU, 32, outline, 3.0)
    draw_line(center + Vector2(-radius - 6.0, 0.0), center + Vector2(radius + 6.0, 0.0), outline, 2.0)
    draw_line(center + Vector2(0.0, -radius - 6.0), center + Vector2(0.0, radius + 6.0), outline, 2.0)

func _draw_fighter_boxes(fighters: Array) -> void:
    for fighter in fighters:
        if fighter == null:
            continue
        for hurt_rect in _fighter_hurtbox_rects(fighter):
            if hurt_rect is Rect2:
                _draw_rect_overlay(hurt_rect as Rect2, hurtbox_fill, hurtbox_outline)
        var hit_rects: Array = _fighter_hitbox_rects(fighter)
        var hit_rect: Rect2 = _rect_union(hit_rects)
        if hit_rect.size.x > 0.0 and hit_rect.size.y > 0.0:
            for active_rect in hit_rects:
                if active_rect is Rect2:
                    _draw_rect_overlay(active_rect as Rect2, hitbox_fill, hitbox_outline)
        else:
            var ghost: Variant = _recent_hitboxes.get(str(fighter.name))
            if ghost is Dictionary:
                var ghost_rect: Variant = (ghost as Dictionary).get("rect")
                if ghost_rect is Rect2:
                    _draw_rect_overlay(
                        ghost_rect,
                        Color(hitbox_fill.r, hitbox_fill.g, hitbox_fill.b, 0.16),
                        Color(hitbox_outline.r, hitbox_outline.g, hitbox_outline.b, 0.45)
                    )
        var fighter_pos: Vector2 = fighter.position
        var facing: float = 1.0 if float(fighter.get("facing_direction") if fighter.get("facing_direction") != null else 1.0) >= 0.0 else -1.0
        draw_circle(fighter_pos, 6.0, origin_color)
        draw_line(fighter_pos, fighter_pos + Vector2(42.0 * facing, -12.0), origin_color, 2.0)

func _draw_projectile_boxes(projectiles: Array) -> void:
    for projectile in projectiles:
        if projectile == null or not bool(projectile.get("active")):
            continue
        _draw_rect_overlay(_projectile_rect(projectile), projectile_fill, projectile_outline)

func _draw_kart_boxes(karts: Array) -> void:
    for kart in karts:
        if kart == null:
            continue
        var radius: float = float(kart.get("collision_radius") if kart.get("collision_radius") != null else 56.0)
        draw_circle(kart.position, radius, Color(collision_color.r, collision_color.g, collision_color.b, 0.12))
        draw_arc(kart.position, radius, 0.0, TAU, 32, collision_color, 2.0)
        var angle: float = deg_to_rad(float(kart.get("facing_angle") if kart.get("facing_angle") != null else 0.0))
        var tip: Vector2 = kart.position + Vector2(cos(angle), sin(angle)) * radius
        draw_line(kart.position, tip, collision_color, 3.0)

func _draw_item_boxes(item_boxes: Array) -> void:
    for item_box in item_boxes:
        if item_box == null or not bool(item_box.get("active")):
            continue
        var rect: Rect2 = Rect2(item_box.position - Vector2(28.0, 28.0), Vector2(56.0, 56.0))
        _draw_rect_overlay(rect, Color(1.0, 0.7, 0.22, 0.18), Color(1.0, 0.82, 0.45, 0.95))

func _draw_checkpoints() -> void:
    var points: Array[Vector2] = _checkpoint_points()
    if points.size() < 2:
        return
    if not _announced_ready:
        _announced_ready = true
        print("[trace] debug.hitboxes_visible fighters=%d checkpoints=%d" % [_pools().get("fighters", []).size(), points.size()])
    for i in range(points.size()):
        draw_line(points[i], points[(i + 1) % points.size()], Color(checkpoint_color.r, checkpoint_color.g, checkpoint_color.b, 0.22), 4.0)
        draw_circle(points[i], 12.0, checkpoint_color)

func _draw_mode7_markers() -> void:
    var points: Array[Vector2] = _checkpoint_points()
    if not _announced_ready:
        _announced_ready = true
        print("[trace] debug.mode7_markers checkpoints=%d" % points.size())
    if points.is_empty():
        var viewport_size: Vector2 = get_viewport_rect().size
        for idx in range(4):
            _draw_mode7_marker(
                Vector2(viewport_size.x * (0.34 + float(idx) * 0.08), viewport_size.y * 0.28),
                Color(checkpoint_color.r, checkpoint_color.g, checkpoint_color.b, 0.30),
                checkpoint_color,
                14.0
            )
        return
    for idx in range(points.size()):
        _draw_mode7_marker(
            _mode7_marker_anchor(points[idx], idx, points.size()),
            Color(checkpoint_color.r, checkpoint_color.g, checkpoint_color.b, 0.30),
            checkpoint_color,
            14.0
        )
    for item_box in _pools().get("item_boxs", []):
        if item_box == null or not bool(item_box.get("active")):
            continue
        var projected_item: Vector2 = _mode7_project_point(item_box.position)
        if not is_finite(projected_item.x) or not is_finite(projected_item.y):
            continue
        _draw_rect_overlay(
            Rect2(projected_item - Vector2(12.0, 12.0), Vector2(24.0, 24.0)),
            Color(1.0, 0.7, 0.22, 0.3),
            Color(1.0, 0.82, 0.45, 0.95)
        )

func _draw() -> void:
    var pools: Dictionary = _pools()
    var render_mode: String = _render_mode()
    if render_mode == "mode7_surface" or render_mode == "race_3d_surface":
        _draw_mode7_markers()
    else:
        _draw_checkpoints()
        if _checkpoint_points().size() > 0 and (pools.get("karts", []) as Array).size() > 0:
            _draw_mode7_markers()
    _draw_fighter_boxes(pools.get("fighters", []))
    _draw_projectile_boxes(pools.get("projectiles", []))
    if render_mode != "mode7_surface" and render_mode != "race_3d_surface":
        _draw_kart_boxes(pools.get("karts", []))
        _draw_item_boxes(pools.get("item_boxs", []))
"""


_DEBUG_LOG_GD = """extends RichTextLabel

var scene_root: Node
var scene_defaults: Dictionary = {}
var _start_ms: int = 0
var _log_lines: Array[String] = []
var _watch_values: Dictionary = {}
const MAX_LINES: int = 20

func setup(root: Node, defaults: Dictionary = {}) -> void:
    scene_root = root
    scene_defaults = defaults.duplicate(true)

func _ready() -> void:
    top_level = true
    mouse_filter = Control.MOUSE_FILTER_IGNORE
    position = Vector2(1240.0, 150.0)
    size = Vector2(620.0, 760.0)
    custom_minimum_size = size
    scroll_active = false
    autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
    fit_content = false
    add_theme_font_size_override("normal_font_size", 12)
    add_theme_color_override("default_color", Color(0.96, 0.98, 1.0, 0.94))
    var normal_box: StyleBoxFlat = StyleBoxFlat.new()
    normal_box.bg_color = Color(0.02, 0.03, 0.05, 0.32)
    normal_box.corner_radius_top_left = 10
    normal_box.corner_radius_top_right = 10
    normal_box.corner_radius_bottom_left = 10
    normal_box.corner_radius_bottom_right = 10
    normal_box.border_width_left = 1
    normal_box.border_width_top = 1
    normal_box.border_width_right = 1
    normal_box.border_width_bottom = 1
    normal_box.border_color = Color(0.28, 0.36, 0.45, 0.5)
    add_theme_stylebox_override("normal", normal_box)
    _start_ms = Time.get_ticks_msec()
    _append_line("debug.log.ready")
    print("[trace] debug.overlay_ready kind=log")

func _process(_delta: float) -> void:
    if scene_root == null:
        return
    _capture_changes()
    text = _status_header() + "\\n\\n" + "\\n".join(_log_lines)

func _pools() -> Dictionary:
    if scene_root == null:
        return {}
    var pools: Variant = scene_root.get("entity_pools")
    return pools if pools is Dictionary else {}

func _timestamp() -> String:
    return "[%07dms]" % max(Time.get_ticks_msec() - _start_ms, 0)

func _append_line(message: String) -> void:
    _log_lines.append("%s %s" % [_timestamp(), message])
    if _log_lines.size() > MAX_LINES:
        _log_lines = _log_lines.slice(_log_lines.size() - MAX_LINES, _log_lines.size())

func _watch(key: String, value, message: String) -> void:
    var rendered: String = str(value)
    if _watch_values.get(key, null) == rendered:
        return
    _watch_values[key] = rendered
    _append_line(message)

func _status_header() -> String:
    var pools: Dictionary = _pools()
    var header: Array[String] = []
    var round_state: Variant = scene_root.get("round_state")
    if round_state != null:
        header.append("ROUND: %s" % str(round_state))
    var fighters: Variant = pools.get("fighters", [])
    if fighters is Array:
        for i in range(min(2, fighters.size())):
            var fighter = fighters[i]
            if fighter == null:
                continue
            header.append(
                "P%d HP=%s ACT=%s RAGE=%s" % [
                    i + 1,
                    str(fighter.get("current_health")),
                    str(fighter.get("current_action")),
                    str(fighter.get("rage_stacks")),
                ]
            )
    var karts: Variant = pools.get("karts", [])
    if karts is Array:
        for i in range(min(2, karts.size())):
            var kart = karts[i]
            if kart == null:
                continue
            header.append(
                "K%d SPD=%s LAP=%s POS=%s ITEM=%s" % [
                    i + 1,
                    str(snapped(float(kart.get("speed") if kart.get("speed") != null else 0.0), 0.1)),
                    str(kart.get("current_lap")),
                    str(kart.get("position_rank")),
                    str(kart.get("held_item") if kart.get("held_item") != null else kart.get("current_item")),
                ]
            )
    return " | ".join(header)

func _capture_bootstrap_once() -> void:
    if _watch_values.get("__bootstrap__", null) != "ready":
        _watch_values["__bootstrap__"] = "ready"
        var pools: Dictionary = _pools()
        _append_line("scene.entities fighters=%d karts=%d hud=%d items=%d" % [
            pools.get("fighters", []).size(),
            pools.get("karts", []).size(),
            pools.get("hud_bars", []).size(),
            pools.get("item_boxs", []).size(),
        ])

func _capture_changes() -> void:
    _capture_bootstrap_once()
    var round_state: Variant = scene_root.get("round_state")
    if round_state != null:
        _watch("game.round_state", round_state, "round.state -> %s" % str(round_state))

    var pools: Dictionary = _pools()
    var fighters: Variant = pools.get("fighters", [])
    if fighters is Array:
        for i in range(fighters.size()):
            var fighter = fighters[i]
            if fighter == null:
                continue
            var prefix: String = "fighter.%s" % fighter.name
            _watch(prefix + ".action", fighter.get("current_action"), "%s action=%s" % [fighter.name, str(fighter.get("current_action"))])
            _watch(prefix + ".health", fighter.get("current_health"), "%s health=%s" % [fighter.name, str(fighter.get("current_health"))])
            _watch(prefix + ".rage", fighter.get("rage_stacks"), "%s rage=%s" % [fighter.name, str(fighter.get("rage_stacks"))])
            if bool(fighter.get("hit_this_frame")):
                _append_line("%s hit damage=%s projectile=%s" % [
                    fighter.name,
                    str(fighter.get("damage_taken_this_frame")),
                    str(fighter.get("hit_was_projectile")),
                ])

    var karts: Variant = pools.get("karts", [])
    if karts is Array:
        for kart in karts:
            if kart == null:
                continue
            var prefix: String = "kart.%s" % kart.name
            _watch(prefix + ".speed", snapped(float(kart.get("speed") if kart.get("speed") != null else 0.0), 0.1), "%s speed=%.1f" % [kart.name, float(kart.get("speed") if kart.get("speed") != null else 0.0)])
            _watch(prefix + ".lap", kart.get("current_lap"), "%s lap=%s" % [kart.name, str(kart.get("current_lap"))])
            _watch(prefix + ".rank", kart.get("position_rank"), "%s rank=%s" % [kart.name, str(kart.get("position_rank"))])
            var item_value: Variant = kart.get("held_item")
            if item_value == null:
                item_value = kart.get("current_item")
            _watch(prefix + ".item", item_value, "%s item=%s" % [kart.name, str(item_value)])
            _watch(prefix + ".boost", kart.get("is_boosting"), "%s boosting=%s" % [kart.name, str(kart.get("is_boosting"))])
            _watch(prefix + ".finish", kart.get("has_finished"), "%s finished=%s" % [kart.name, str(kart.get("has_finished"))])

    var active_projectiles: int = 0
    for projectile in pools.get("projectiles", []):
        if projectile != null and bool(projectile.get("active")):
            active_projectiles += 1
    _watch("scene.projectiles", active_projectiles, "projectiles.active=%d" % active_projectiles)

    var active_items: int = 0
    for item_box in pools.get("item_boxs", []):
        if item_box != null and bool(item_box.get("active")):
            active_items += 1
    _watch("scene.item_boxes", active_items, "item_boxes.active=%d" % active_items)
"""


def write_debug_scripts(godot_dir: Path) -> list[Path]:
    debug_dir = godot_dir / "scripts" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        debug_dir / "rayxi_debug_boxes.gd",
        debug_dir / "rayxi_debug_log.gd",
    ]
    paths[0].write_text(_DEBUG_BOXES_GD + "\n", encoding="utf-8")
    paths[1].write_text(_DEBUG_LOG_GD + "\n", encoding="utf-8")
    return paths
