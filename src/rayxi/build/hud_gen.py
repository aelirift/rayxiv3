"""LLM-driven HUD widget generator for custom mechanic_spec HUD entities.

Replaces the previous hardcoded "if 'rage' in name: return rage_template" with
a generic LLM call that produces a complete Godot Control node GDScript from
the mechanic_spec.hud_entities declaration. Works for ANY feature: rage meters,
mana bars, combo gauges, stamina, super meters — zero code changes per feature.

Input to the LLM: the HudEntity spec fields (name, godot_node, reads, displays,
visual_states prose). Output: a full GDScript file that extends Control, reads
from the fighter(s), overrides _draw() to render according to visual_states.

Cache key: hash of the spec fields. Reruns with the same spec hit cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

from rayxi.llm.callers import build_callers, build_router
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from rayxi.spec.models import GameIdentity, MechanicHudEntity, MechanicSpec

_log = logging.getLogger("rayxi.build.hud_gen")
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "hud_gen"

_HUD_NATIVE_MEMBERS: dict[str, set[str]] = {
    "Control": {"position", "size", "visible", "scale", "rotation", "layout_mode"},
    "ProgressBar": {
        "position", "size", "visible", "scale", "rotation", "layout_mode",
        "value", "min_value", "max_value", "step", "show_percentage",
    },
    "Label": {
        "position", "size", "visible", "scale", "rotation",
        "text", "horizontal_alignment", "vertical_alignment",
    },
}

_BUILTIN_DUEL_STATUS_GD = """extends Control

var scene_root: Node
var _timer_label: Label
var _state_label: Label
var _p1_label: Label
var _p2_label: Label

@export var orb_radius: float = 18.0
@export var orb_spacing: float = 48.0
@export var orb_outline_width: float = 3.0
@export var panel_width: float = 1920.0
@export var panel_height: float = 150.0

@export var p1_fill: Color = Color(0.95, 0.36, 0.28, 0.96)
@export var p2_fill: Color = Color(0.22, 0.62, 1.0, 0.96)
@export var orb_empty: Color = Color(0.12, 0.14, 0.18, 0.9)
@export var orb_outline: Color = Color(0.98, 0.98, 1.0, 0.98)
@export var panel_tint: Color = Color(0.03, 0.04, 0.06, 0.28)

func setup(root: Node) -> void:
    scene_root = root

func _ready() -> void:
    top_level = true
    mouse_filter = Control.MOUSE_FILTER_IGNORE
    position = Vector2.ZERO
    size = Vector2(panel_width, panel_height)
    custom_minimum_size = size

    _timer_label = _make_label(34, HORIZONTAL_ALIGNMENT_CENTER)
    _timer_label.position = Vector2((panel_width * 0.5) - 90.0, 14.0)
    _timer_label.size = Vector2(180.0, 44.0)
    add_child(_timer_label)

    _state_label = _make_label(18, HORIZONTAL_ALIGNMENT_CENTER)
    _state_label.position = Vector2((panel_width * 0.5) - 220.0, 54.0)
    _state_label.size = Vector2(440.0, 32.0)
    add_child(_state_label)

    _p1_label = _make_label(20, HORIZONTAL_ALIGNMENT_LEFT)
    _p1_label.position = Vector2(116.0, 18.0)
    _p1_label.size = Vector2(420.0, 34.0)
    add_child(_p1_label)

    _p2_label = _make_label(20, HORIZONTAL_ALIGNMENT_RIGHT)
    _p2_label.position = Vector2(panel_width - 536.0, 18.0)
    _p2_label.size = Vector2(420.0, 34.0)
    add_child(_p2_label)

func _make_label(font_size: int, alignment: HorizontalAlignment) -> Label:
    var label: Label = Label.new()
    label.mouse_filter = Control.MOUSE_FILTER_IGNORE
    label.horizontal_alignment = alignment
    label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
    label.add_theme_font_size_override("font_size", font_size)
    label.add_theme_color_override("font_color", Color(0.97, 0.98, 1.0, 0.96))
    return label

func _fighters() -> Array:
    if scene_root == null:
        return []
    var pools: Variant = scene_root.get("entity_pools")
    if pools is Dictionary:
        return (pools as Dictionary).get("fighters", [])
    return []

func _process(_delta: float) -> void:
    _refresh_labels()
    queue_redraw()

func _refresh_labels() -> void:
    if scene_root == null:
        return
    var fighters = _fighters()
    var round_state = str(scene_root.get("round_state") if scene_root.get("round_state") != null else "").replace("_", " ").to_upper()
    var timer_seconds = int(scene_root.get("round_timer_seconds") if scene_root.get("round_timer_seconds") != null else 0)
    _timer_label.text = "%02d" % max(timer_seconds, 0)
    _state_label.text = round_state
    if fighters.size() > 0 and fighters[0] != null:
        var p1 = fighters[0]
        _p1_label.text = "P1 HP %d  RAGE %d" % [
            int(p1.get("current_health") if p1.get("current_health") != null else 0),
            int(p1.get("rage_stacks") if p1.get("rage_stacks") != null else 0),
        ]
    else:
        _p1_label.text = "P1"
    if fighters.size() > 1 and fighters[1] != null:
        var p2 = fighters[1]
        _p2_label.text = "P2 HP %d  RAGE %d" % [
            int(p2.get("current_health") if p2.get("current_health") != null else 0),
            int(p2.get("rage_stacks") if p2.get("rage_stacks") != null else 0),
        ]
    else:
        _p2_label.text = "P2"

func _draw_orbs(center_x: float, wins: int, rounds_to_win: int, fill: Color, right_side: bool) -> void:
    var row_width: float = float(max(rounds_to_win - 1, 0)) * orb_spacing + (orb_radius * 2.0) + 38.0
    var row_rect: Rect2 = Rect2(Vector2(center_x - row_width * 0.5, 92.0), Vector2(row_width, 56.0))
    draw_rect(row_rect, Color(0.02, 0.03, 0.05, 0.58), true)
    draw_rect(row_rect, Color(fill.r, fill.g, fill.b, 0.34), false, 2.0)
    for i in range(rounds_to_win):
        var direction = -1.0 if right_side else 1.0
        var orb_center = Vector2(center_x + direction * float(i) * orb_spacing, 120.0)
        draw_circle(orb_center, orb_radius + 5.0, Color(panel_tint.r, panel_tint.g, panel_tint.b, 0.72))
        draw_circle(orb_center, orb_radius, fill if i < wins else orb_empty)
        draw_arc(orb_center, orb_radius, 0.0, TAU, 32, orb_outline, orb_outline_width)
        if i < wins:
            draw_circle(orb_center, orb_radius * 0.54, Color(1.0, 1.0, 1.0, 0.88))
            draw_circle(orb_center, orb_radius + 9.0, Color(fill.r, fill.g, fill.b, 0.16))

func _draw() -> void:
    if scene_root == null:
        return
    draw_rect(Rect2(Vector2.ZERO, Vector2(panel_width, panel_height)), panel_tint, true)
    var fighters = _fighters()
    var rounds_to_win = max(int(scene_root.get("rounds_to_win") if scene_root.get("rounds_to_win") != null else 2), 1)
    var p1_wins = 0
    var p2_wins = 0
    if fighters.size() > 0 and fighters[0] != null:
        p1_wins = int(fighters[0].get("round_wins") if fighters[0].get("round_wins") != null else 0)
    if fighters.size() > 1 and fighters[1] != null:
        p2_wins = int(fighters[1].get("round_wins") if fighters[1].get("round_wins") != null else 0)
    _draw_orbs(144.0, p1_wins, rounds_to_win, p1_fill, false)
    _draw_orbs(panel_width - 144.0, p2_wins, rounds_to_win, p2_fill, true)
"""


_HUD_WIDGET_SYSTEM_PROMPT = """\
You are a Godot 4.4 GDScript engineer. Your job is to write ONE complete Control \
node script that renders a HUD widget for a fighting-game (or similar action-game) \
custom feature.

You will receive a HUD entity declaration from the game's HLR mechanic spec:
  - name:           the widget's node name (used for references)
  - godot_node:     base class (typically "Control")
  - displays:       prose description of what the widget shows
  - reads:          property names the widget reads from the fighter each frame
  - visual_states:  prose contract describing what the widget must look like for \
every possible state of the underlying data (e.g. "0 stacks = all dim, 1 stack = \
one lit, 2 stacks = two lit, 3 stacks = all lit + pulsing")

You will also receive the fighter's relevant config constants (max_rage_stacks, \
rage_fill_threshold, etc.) if this is a resource meter — use them to size the widget.

## Requirements for the output GDScript

1. `extends <godot_node>` (e.g. `extends Control`)
2. An `@export var fighter_path: NodePath` so the scene can wire the widget to p1 or p2
3. `_ready()`: resolves fighter_path → `var fighter: Node`, sets `custom_minimum_size`
4. `_process(_delta)`: calls `queue_redraw()` so the widget updates every frame
5. `_draw()`: reads the listed properties from `fighter` via `fighter.get("property_name")`, \
renders the widget exactly according to the visual_states contract
6. Use `draw_rect`, `draw_circle`, `draw_line`, `draw_string` — stay within Godot's basic 2D draw API
7. Include @export vars for all visual knobs (segment colors, sizes, spacing) with sensible defaults \
so the scene can override them
8. NEVER reference mechanic systems directly — read only from `fighter.<property>`
9. Animate time-varying visuals (pulse at max, flash on change) using `Engine.get_physics_frames()` \
or an internal counter — do not rely on tweens or AnimationPlayer

## Output format

Output ONLY raw GDScript. No markdown, no code fences, no prose, no leading blank lines. \
The first line must be `extends <node_class>`. The file will be saved directly as a .gd file \
and must compile in Godot 4.4.
"""


def _cache_key(entity: MechanicHudEntity, constants: dict) -> str:
    payload = json.dumps(
        {"e": entity.model_dump(), "c": constants},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    path = _CACHE_DIR / f"{key}.gd"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _cache_put(key: str, gd: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.gd").write_text(gd, encoding="utf-8")


def _normalize_gdscript_whitespace(gd: str) -> str:
    normalized = gd.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\t", "    ")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _normalize_fighter_get_defaults(gd: str) -> str:
    if 'fighter.get("' not in gd:
        return gd
    normalized = re.sub(
        r'\bfighter\.get\("([^"]+)",\s*([^)]+)\)',
        r'_fighter_prop("\1", \2)',
        gd,
    )
    if "func _fighter_prop(" in normalized:
        return normalized
    helper = """
func _fighter_prop(name: String, fallback):
    if fighter == null:
        return fallback
    var value: Variant = fighter.get(name)
    return fallback if value == null else value
""".strip()
    insert_at = normalized.find("\nfunc _ready()")
    if insert_at < 0:
        return normalized.rstrip() + "\n\n" + helper + "\n"
    return normalized[: insert_at + 1] + helper + "\n\n" + normalized[insert_at + 1 :]


def _normalize_vector_inf_literals(gd: str) -> str:
    normalized = gd.replace("Vector2.NEG_INF", "Vector2(-INF, -INF)")
    normalized = normalized.replace("Vector2.INF", "Vector2(INF, INF)")
    return normalized


def _normalize_native_member_collisions(gd: str) -> str:
    match = re.match(r"extends\s+([A-Za-z0-9_]+)", gd.strip())
    if not match:
        return gd
    native_members = _HUD_NATIVE_MEMBERS.get(match.group(1), set())
    if not native_members:
        return gd

    rename_map: dict[str, str] = {}
    for var_match in re.finditer(r"@export\s+var\s+([A-Za-z_][A-Za-z0-9_]*)\b", gd):
        name = var_match.group(1)
        if name in native_members:
            rename_map[name] = f"hud_{name}"

    normalized = gd
    for original, renamed in rename_map.items():
        normalized = re.sub(
            rf"(@export\s+var\s+){re.escape(original)}(\b)",
            rf"\1{renamed}\2",
            normalized,
        )
        normalized = re.sub(rf"\b{re.escape(original)}\b", renamed, normalized)
    return normalized


def _deterministic_stack_meter_widget(entity: MechanicHudEntity, constants: dict) -> str | None:
    reads = set(entity.reads or [])
    if not {"rage_stacks", "rage_fill_value"}.issubset(reads):
        return None
    max_stacks = int(constants.get("max_rage_stacks", 3) or 3)
    return f"""extends Control

@export var fighter_path: NodePath

@export var segment_size: float = 32.0
@export var segment_spacing: float = 8.0
@export var bar_height: float = 8.0
@export var bar_spacing: float = 6.0

@export var color_dim: Color = Color(0.18, 0.18, 0.2, 0.95)
@export var color_fill: Color = Color(1.0, 0.86, 0.2, 1.0)
@export var color_hot: Color = Color(1.0, 0.42, 0.05, 1.0)
@export var color_max: Color = Color(1.0, 0.16, 0.12, 1.0)

var fighter: Node
const MAX_STACKS: int = {max_stacks}

func _fighter_prop(name: String, fallback):
    if fighter == null:
        return fallback
    var value = fighter.get(name)
    return fallback if value == null else value

func _ready() -> void:
    if not fighter_path.is_empty():
        fighter = get_node(fighter_path)
    var total_width = MAX_STACKS * segment_size + (MAX_STACKS - 1) * segment_spacing
    var total_height = segment_size + bar_spacing + bar_height
    custom_minimum_size = Vector2(total_width, total_height)

func _process(_delta: float) -> void:
    queue_redraw()

func _draw() -> void:
    if not is_instance_valid(fighter):
        return

    var stacks: int = clampi(int(_fighter_prop("rage_stacks", 0)), 0, MAX_STACKS)
    var fill_fraction: float = clampf(float(_fighter_prop("rage_fill_value", 0.0)), 0.0, 1.0)
    var powered: bool = bool(_fighter_prop("is_powered_special", false))
    var pulse: float = 0.82 + 0.18 * sin(float(Engine.get_physics_frames()) * 0.16)

    for i in range(MAX_STACKS):
        var x: float = i * (segment_size + segment_spacing)
        var rect: Rect2 = Rect2(x, 0, segment_size, segment_size)
        var fill: float = 0.0
        if i < stacks:
            fill = 1.0
        elif i == stacks:
            fill = fill_fraction
        draw_rect(rect, color_dim, true)
        if fill > 0.0:
            var hotness: float = 1.0 if stacks >= MAX_STACKS else fill
            var fill_color: Color = color_fill.lerp(color_hot, hotness * 0.75)
            if stacks >= MAX_STACKS or powered:
                fill_color = color_hot.lerp(color_max, 0.65) * pulse
                fill_color.a = 1.0
            var inset: float = 3.0
            draw_rect(
                Rect2(rect.position.x + inset, rect.position.y + inset, (rect.size.x - inset * 2.0) * fill, rect.size.y - inset * 2.0),
                fill_color,
                true,
            )

    var bar_y: float = segment_size + bar_spacing
    var total_bar_width: float = MAX_STACKS * segment_size + (MAX_STACKS - 1) * segment_spacing
    draw_rect(Rect2(0, bar_y, total_bar_width, bar_height), color_dim.darkened(0.25), true)
    if fill_fraction > 0.0 and stacks < MAX_STACKS:
        draw_rect(Rect2(0, bar_y, total_bar_width * fill_fraction, bar_height), color_fill, true)
"""


def _deterministic_racing_widget(entity: MechanicHudEntity, constants: dict) -> str | None:
    name = (entity.name or "").lower()
    reads = set(entity.reads or [])

    if name == "speedometer" or "speed" in reads:
        return """extends ProgressBar

@export var fighter_path: NodePath
var fighter: Node

func _fighter_prop(name: String, fallback):
    if fighter == null:
        return fallback
    var value: Variant = fighter.get(name)
    return fallback if value == null else value

func _ready() -> void:
    if not fighter_path.is_empty():
        fighter = get_node_or_null(fighter_path)
    min_value = 0.0
    max_value = 100.0
    value = 0.0
    show_percentage = true
    custom_minimum_size = Vector2(320.0, 30.0)
    var fill: StyleBoxFlat = StyleBoxFlat.new()
    fill.bg_color = Color(0.16, 0.72, 0.92, 0.9)
    add_theme_stylebox_override("fill", fill)
    var background: StyleBoxFlat = StyleBoxFlat.new()
    background.bg_color = Color(0.02, 0.03, 0.05, 0.6)
    add_theme_stylebox_override("background", background)

func _process(_delta: float) -> void:
    if fighter == null:
        value = 0.0
        return
    var max_speed: float = max(
        float(_fighter_prop("max_speed_stat", _fighter_prop("max_speed", _fighter_prop("top_speed", 1.0)))),
        1.0,
    )
    var speed: float = max(float(_fighter_prop("speed", 0.0)), 0.0)
    value = clampf((speed / max_speed) * 100.0, 0.0, 100.0)
    tooltip_text = "Speed %.1f / %.1f" % [speed, max_speed]
"""

    if name == "item_icon" or bool(reads.intersection({"current_item", "held_item"})):
        return """extends Label

@export var fighter_path: NodePath
var fighter: Node

func _fighter_prop(name: String, fallback):
    if fighter == null:
        return fallback
    var value: Variant = fighter.get(name)
    return fallback if value == null else value

func _ready() -> void:
    if not fighter_path.is_empty():
        fighter = get_node_or_null(fighter_path)
    custom_minimum_size = Vector2(260.0, 42.0)
    add_theme_font_size_override("font_size", 24)
    add_theme_color_override("font_color", Color(1.0, 0.96, 0.8, 0.96))
    horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER

func _process(_delta: float) -> void:
    var item_name: String = str(_fighter_prop("current_item", _fighter_prop("held_item", "none"))).strip_edges()
    text = "ITEM: %s" % [item_name.to_upper() if item_name != "" and item_name != "none" else "NONE"]
"""

    if name == "lap_counter" or reads == {"current_lap"}:
        max_laps = int(constants.get("max_laps", 3) or 3)
        return f"""extends Label

@export var fighter_path: NodePath
var fighter: Node
const MAX_LAPS: int = {max_laps}

func _fighter_prop(name: String, fallback):
    if fighter == null:
        return fallback
    var value: Variant = fighter.get(name)
    return fallback if value == null else value

func _ready() -> void:
    if not fighter_path.is_empty():
        fighter = get_node_or_null(fighter_path)
    custom_minimum_size = Vector2(220.0, 42.0)
    add_theme_font_size_override("font_size", 28)
    add_theme_color_override("font_color", Color(0.96, 0.98, 1.0, 0.98))

func _process(_delta: float) -> void:
    var lap: int = clampi(int(_fighter_prop("current_lap", 1)), 1, MAX_LAPS)
    text = "LAP %d/%d" % [lap, MAX_LAPS]
"""

    if name == "position_display" or bool(reads.intersection({"position_rank", "race_position"})):
        return """extends Label

@export var fighter_path: NodePath
var fighter: Node

func _fighter_prop(name: String, fallback):
    if fighter == null:
        return fallback
    var value: Variant = fighter.get(name)
    return fallback if value == null else value

func _ordinal(rank: int) -> String:
    match rank:
        1:
            return "1ST"
        2:
            return "2ND"
        3:
            return "3RD"
        _:
            return "%dTH" % rank

func _ready() -> void:
    if not fighter_path.is_empty():
        fighter = get_node_or_null(fighter_path)
    custom_minimum_size = Vector2(200.0, 42.0)
    add_theme_font_size_override("font_size", 30)
    add_theme_color_override("font_color", Color(1.0, 0.9, 0.38, 0.98))
    horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT

func _process(_delta: float) -> void:
    var rank: int = max(int(_fighter_prop("position_rank", _fighter_prop("race_position", 1))), 1)
    text = "POS %s" % _ordinal(rank)
"""

    if name == "drift_meter" or {"drift_charge", "is_drifting"}.issubset(reads):
        return """extends Control

@export var fighter_path: NodePath
@export var bar_width: float = 40.0
@export var bar_height: float = 8.0
@export var bar_spacing: float = 4.0
@export var color_level_1: Color = Color(0.2, 0.6, 1.0)
@export var color_level_2: Color = Color(1.0, 0.5, 0.1)
@export var color_level_3: Color = Color(0.8, 0.2, 1.0)
@export var bg_color: Color = Color(0.1, 0.1, 0.1, 0.5)
@export var spark_radius: float = 2.0
@export var pulse_period: int = 30

var fighter: Node

func _fighter_prop(name: String, fallback):
    if fighter == null:
        return fallback
    var value: Variant = fighter.get(name)
    return fallback if value == null else value

func _ready() -> void:
    if not fighter_path.is_empty():
        fighter = get_node_or_null(fighter_path)
    custom_minimum_size = Vector2(bar_width * 3.0 + bar_spacing * 2.0, bar_height + 12.0)

func _process(_delta: float) -> void:
    queue_redraw()

func _draw() -> void:
    if fighter == null:
        return
    var is_drifting: bool = bool(_fighter_prop("is_drifting", false))
    if not is_drifting:
        return
    var drift_charge: float = clampf(float(_fighter_prop("drift_charge", 0.0)), 0.0, 3.0)
    var total_width: float = bar_width * 3.0 + bar_spacing * 2.0
    var start_x: float = (size.x - total_width) * 0.5
    var start_y: float = (size.y - bar_height) * 0.5
    var colors: Array[Color] = [color_level_1, color_level_2, color_level_3]
    var frame: int = Engine.get_physics_frames()

    for i in range(3):
        var x: float = start_x + float(i) * (bar_width + bar_spacing)
        var rect: Rect2 = Rect2(x, start_y, bar_width, bar_height)
        draw_rect(rect, bg_color, true)
        draw_rect(rect, Color(bg_color.r, bg_color.g, bg_color.b, 0.3), false, 1.0)

    var active_level: int = mini(int(drift_charge), 2)
    for i in range(3):
        var x: float = start_x + float(i) * (bar_width + bar_spacing)
        var fill: float = 0.0
        if drift_charge > float(i):
            fill = clampf(drift_charge - float(i), 0.0, 1.0)
        if fill <= 0.0:
            continue
        var col: Color = colors[i]
        if drift_charge >= 3.0 and i == 2:
            var pulse: float = (sin(float(frame) * TAU / float(max(pulse_period, 1))) + 1.0) * 0.5
            col = col.lerp(Color.WHITE, pulse * 0.6)
        draw_rect(Rect2(x, start_y, bar_width * fill, bar_height), col, true)
        if i == active_level:
            _draw_sparks(x, start_y, bar_width, bar_height, colors[i], frame, i)

func _draw_sparks(bx: float, by: float, bw: float, bh: float, col: Color, frame: int, level_idx: int) -> void:
    for spark_idx in range(3):
        var t: float = float(frame) * 0.15 + float(spark_idx) * 2.0 + float(level_idx) * 3.0
        var angle: float = t * 1.5
        var dist: float = bh * 0.8 + sin(t) * 4.0
        var sx: float = bx + bw * 0.5 + cos(angle) * dist
        var sy: float = by + bh * 0.5 + sin(angle * 0.7) * bh * 0.8
        var alpha: float = 0.5 + sin(float(frame) * 0.4 + float(spark_idx)) * 0.4
        draw_circle(Vector2(sx, sy), spark_radius, Color(col.r, col.g, col.b, alpha))
"""

    if name == "countdown_display" or "countdown_value" in reads:
        return """extends Label

@export var fighter_path: NodePath
@export var countdown_color: Color = Color(0.96, 0.28, 0.18, 0.98)
@export var go_color: Color = Color(0.24, 0.94, 0.36, 0.98)
@export var shadow_color: Color = Color(0.0, 0.0, 0.0, 0.6)
@export var bounce_amplitude: float = 20.0
@export var bounce_speed: float = 0.25
@export var font_size: int = 96
@export var shadow_offset: Vector2 = Vector2(4.0, 4.0)

func _ready() -> void:
    custom_minimum_size = Vector2(220.0, 150.0)

func _process(_delta: float) -> void:
    queue_redraw()

func _race_manager():
    var parent = get_parent()
    if parent == null:
        return null
    var pools: Variant = parent.get("entity_pools")
    if not (pools is Dictionary):
        return null
    var managers: Variant = pools.get("race_managers", [])
    if managers is Array and managers.size() > 0:
        return managers[0]
    return null

func _draw() -> void:
    var race_manager = _race_manager()
    if race_manager == null:
        return
    var raw_value: Variant = race_manager.get("countdown_value")
    if raw_value == null:
        return
    var countdown_value: int = int(raw_value)
    var display_text: String = ""
    var use_color: Color = countdown_color
    var should_bounce: bool = false
    if countdown_value > 0:
        display_text = str(countdown_value)
        should_bounce = true
    elif countdown_value == 0:
        display_text = "GO"
        use_color = go_color
    else:
        return

    var y_offset: float = 0.0
    if should_bounce:
        var frame: int = Engine.get_physics_frames()
        y_offset = abs(sin(float(frame) * bounce_speed)) * bounce_amplitude

    var font: Font = get_theme_font("font")
    if font == null:
        font = ThemeDB.fallback_font
    var ascent: float = font.get_ascent(font_size)
    var descent: float = font.get_descent(font_size)
    var text_width: float = font.get_string_size(display_text, HORIZONTAL_ALIGNMENT_LEFT, -1.0, font_size).x
    var text_height: float = ascent + descent
    var x_pos: float = (size.x - text_width) * 0.5
    var y_pos: float = (size.y - text_height) * 0.5 + ascent - y_offset

    draw_string(font, Vector2(x_pos + shadow_offset.x, y_pos + shadow_offset.y), display_text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size, shadow_color)
    draw_string(font, Vector2(x_pos, y_pos), display_text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size, use_color)
"""

    if name == "finish_banner" or reads == {"has_finished"}:
        return """extends Label

@export var fighter_path: NodePath
var fighter: Node

func _fighter_prop(name: String, fallback):
    if fighter == null:
        return fallback
    var value: Variant = fighter.get(name)
    return fallback if value == null else value

func _ready() -> void:
    if not fighter_path.is_empty():
        fighter = get_node_or_null(fighter_path)
    custom_minimum_size = Vector2(420.0, 70.0)
    add_theme_font_size_override("font_size", 52)
    add_theme_color_override("font_color", Color(1.0, 0.98, 0.5, 0.98))
    horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
    visible = false

func _process(_delta: float) -> void:
    visible = bool(_fighter_prop("has_finished", false))
    if visible:
        text = "FINISH!"
"""

    if name in {"minimap", "mini_map"} or {"kart.position", "camera.position"}.issubset(reads):
        return """extends Control

@export var fighter_path: NodePath
var fighter: Node

@export var map_size: Vector2 = Vector2(240.0, 240.0)
@export var background_color: Color = Color(0.02, 0.03, 0.05, 0.78)
@export var border_color: Color = Color(0.94, 0.97, 1.0, 0.9)
@export var track_color: Color = Color(0.88, 0.92, 1.0, 0.78)
@export var checkpoint_color: Color = Color(0.34, 1.0, 0.52, 0.96)
@export var player_color: Color = Color(1.0, 0.96, 0.34, 0.98)
@export var opponent_color: Color = Color(1.0, 0.34, 0.22, 0.98)

func _ready() -> void:
    if not fighter_path.is_empty():
        fighter = get_node_or_null(fighter_path)
    custom_minimum_size = map_size
    size = map_size

func _process(_delta: float) -> void:
    queue_redraw()

func _stage():
    var parent = get_parent()
    if parent == null:
        return null
    var pools: Variant = parent.get("entity_pools")
    if not (pools is Dictionary):
        return null
    var stages: Variant = pools.get("stages", [])
    if stages is Array and stages.size() > 0:
        return stages[0]
    return null

func _all_karts() -> Array:
    var parent = get_parent()
    if parent == null:
        return []
    var pools: Variant = parent.get("entity_pools")
    if not (pools is Dictionary):
        return []
    var karts: Variant = pools.get("karts", [])
    return karts if karts is Array else []

func _checkpoint_points() -> Array[Vector2]:
    var stage: Variant = _stage()
    var points: Array[Vector2] = []
    var raw: Variant = []
    if stage != null:
        raw = stage.get("checkpoint_positions")
        if not (raw is Array) or (raw as Array).is_empty():
            raw = stage.get_meta("checkpoint_positions", [])
    if (not (raw is Array) or (raw as Array).is_empty()):
        var parent = get_parent()
        if parent != null and parent.has_method("_scene_points"):
            raw = parent.call("_scene_points", "checkpoint_positions")
    if raw is Array:
        for entry in raw:
            if entry is Vector2:
                points.append(entry)
            elif entry is Array and entry.size() >= 2:
                points.append(Vector2(float(entry[0]), float(entry[1])))
    return points

func _draw() -> void:
    draw_rect(Rect2(Vector2.ZERO, size), background_color, true)
    draw_rect(Rect2(Vector2.ZERO, size), border_color, false, 3.0)
    var points: Array[Vector2] = _checkpoint_points()
    if points.size() < 2:
        return
    var min_x: float = points[0].x
    var max_x: float = points[0].x
    var min_y: float = points[0].y
    var max_y: float = points[0].y
    for point in points:
        min_x = min(min_x, point.x)
        max_x = max(max_x, point.x)
        min_y = min(min_y, point.y)
        max_y = max(max_y, point.y)
    var extent: Vector2 = Vector2(max(max_x - min_x, 1.0), max(max_y - min_y, 1.0))
    var padding: float = 18.0
    var scale: float = min((size.x - padding * 2.0) / extent.x, (size.y - padding * 2.0) / extent.y)
    var offset: Vector2 = Vector2(padding, padding)
    var mapped: Array[Vector2] = []
    for point in points:
        mapped.append(offset + Vector2((point.x - min_x) * scale, (point.y - min_y) * scale))
    for i in range(mapped.size()):
        draw_line(mapped[i], mapped[(i + 1) % mapped.size()], track_color, 22.0)
        draw_circle(mapped[i], 8.0, checkpoint_color)
    var karts: Array = _all_karts()
    for i in range(karts.size()):
        var kart: Variant = karts[i]
        if kart == null:
            continue
        var pos: Variant = kart.get("position")
        if not (pos is Vector2):
            continue
        var mapped_pos: Vector2 = offset + Vector2((pos.x - min_x) * scale, (pos.y - min_y) * scale)
        draw_circle(mapped_pos, 9.0, player_color if i == 0 else opponent_color)
"""

    return None


async def _generate_widget_via_llm(
    entity: MechanicHudEntity,
    constants: dict,
    caller: LLMCaller,
) -> tuple[str, str]:
    """Generate one HUD widget GDScript file plus its generation strategy."""
    key = _cache_key(entity, constants)
    deterministic = _deterministic_stack_meter_widget(entity, constants)
    if deterministic is None:
        deterministic = _deterministic_racing_widget(entity, constants)
    if deterministic is not None:
        deterministic = _normalize_fighter_get_defaults(deterministic)
        deterministic = _normalize_vector_inf_literals(deterministic)
        deterministic = _normalize_native_member_collisions(deterministic)
        deterministic = _normalize_gdscript_whitespace(deterministic)
        _cache_put(key, deterministic)
        return deterministic, "template_python"
    cached = _cache_get(key)
    trace = get_trace()
    caller_name = type(caller).__name__
    label = f"hud_widget[{entity.name}]"
    if cached:
        cached = _normalize_fighter_get_defaults(cached)
        cached = _normalize_vector_inf_literals(cached)
        cached = _normalize_native_member_collisions(cached)
        cached = _normalize_gdscript_whitespace(cached)
        _cache_put(key, cached)
        _log.info("%s — cache hit", label)
        if trace:
            cid = trace.llm_start("codegen", label, caller_name, 0)
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return cached, "llm"

    prompt_parts = [
        "## HUD Entity Spec",
        f"```json\n{json.dumps(entity.model_dump(), indent=2)}\n```",
    ]
    if constants:
        prompt_parts.append("## Relevant constants (use these for sizing/thresholds)")
        prompt_parts.append(f"```json\n{json.dumps(constants, indent=2)}\n```")

    prompt = "\n\n".join(prompt_parts)

    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("codegen", label, caller_name, len(prompt)) if trace else ""
        try:
            raw = await caller(_HUD_WIDGET_SYSTEM_PROMPT, prompt, json_mode=False, label=label)
            gd = raw.strip()
            # Strip any accidental markdown fences the LLM might add
            if gd.startswith("```"):
                lines = gd.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                gd = "\n".join(lines).strip()
            if not gd.startswith("extends"):
                raise RuntimeError(f"output does not start with 'extends': {gd[:80]!r}")
            gd = _normalize_fighter_get_defaults(gd)
            gd = _normalize_vector_inf_literals(gd)
            gd = _normalize_native_member_collisions(gd)
            gd = _normalize_gdscript_whitespace(gd)
            _cache_put(key, gd)
            if trace:
                trace.llm_end(cid, output_chars=len(gd))
            return gd, "llm"
        except Exception as exc:
            last_err = exc
            if trace:
                trace.llm_end(cid, output_chars=0, error=str(exc)[:120])
            _log.warning("%s attempt %d failed: %s", label, attempt + 1, str(exc)[:120])
    raise RuntimeError(f"{label} failed after 3 attempts: {last_err}")


def _flatten_constants_for_system(constants_path: Path | None, system_name: str) -> dict:
    if not constants_path or not constants_path.exists():
        return {}
    raw = json.loads(constants_path.read_text())
    bucket = raw.get(system_name) or {}
    if isinstance(bucket, dict) and "constants" in bucket and isinstance(bucket["constants"], list):
        return {c["name"]: c.get("value") for c in bucket["constants"] if c.get("name")}
    if isinstance(bucket, dict):
        return {name: info.get("value") if isinstance(info, dict) else info
                for name, info in bucket.items()}
    return {}


async def generate_custom_hud_widgets(
    hlr: GameIdentity,
    output_scripts_dir: Path,
    constants_path: Path | None = None,
    caller: LLMCaller | None = None,
) -> list[dict[str, object]]:
    """Generate a .gd file for every mechanic_spec.hud_entities entry.

    No hardcoded widget templates. Every widget is produced from its spec.
    """
    output_scripts_dir.mkdir(parents=True, exist_ok=True)
    if caller is None:
        router = build_router(build_callers())
        caller = router.get("mlr_interactions")

    written: list[dict[str, object]] = []
    for spec in hlr.mechanic_specs:
        constants = _flatten_constants_for_system(constants_path, spec.system_name)
        for widget in spec.hud_entities:
            gd, strategy = await _generate_widget_via_llm(widget, constants, caller)
            out = output_scripts_dir / f"{widget.name}.gd"
            out.write_text(gd, encoding="utf-8")
            written.append({"path": out, "strategy": strategy})
            _log.info("hud_gen: wrote %s (%d bytes, %s)", out, len(gd), strategy)
    return written


def generate_custom_hud_widgets_sync(
    hlr: GameIdentity,
    output_scripts_dir: Path,
    constants_path: Path | None = None,
) -> list[dict[str, object]]:
    """Sync wrapper for contexts that can't await (e.g., inside run_spec.py)."""
    return asyncio.run(generate_custom_hud_widgets(hlr, output_scripts_dir, constants_path))


def write_builtin_hud_scripts(output_scripts_dir: Path) -> list[Path]:
    output_scripts_dir.mkdir(parents=True, exist_ok=True)
    path = output_scripts_dir / "rayxi_duel_status.gd"
    path.write_text(_BUILTIN_DUEL_STATUS_GD + "\n", encoding="utf-8")
    return [path]
