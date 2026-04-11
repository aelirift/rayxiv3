"""character_gen — deterministically emit character .gd files from the impact map.

Replaces the hand-written ryu.gd/chun_li.gd template files with a generated
file that declares EVERY property the impact map says the fighter role owns.
This closes the drift between hand-written templates and LLM-generated systems
that reference properties: if the impact map has `fighter.X`, the character
file has `var X`. Period.

Source of truth: `imap.properties_owned_by(role)` returns every PropertyNode
with `owner == role`. For each node, this module emits:

  - config category   → `@export var name: Type = default`
  - state category    → `var name: Type = default`
  - derived category  → getter method (if derivation is a simple expression)

Type and default heuristics:

- Primitive types (int/float/bool/string) get strong typing because GDScript's
  strict type checker has well-known defaults.
- Collection types (Dictionary/Array/dict/list) are intentionally UNTYPED with
  a literal default, so LLM-generated systems can assign `null` to clear them
  without tripping Godot's "invalid assignment" runtime error. This is the
  pragmatic fix for the active_hitbox issue we hit on Apr 11.
- Unknown types fall back to `Variant = null`.

No LLM calls. This runs after scene_gen and before Godot import.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rayxi.spec.impact_map import Category, ImpactMap, PropertyNode

_log = logging.getLogger("rayxi.build.character_gen")

# Godot native members that must never be redeclared on CharacterBody2D/Node2D.
# Mirror of mechanic_patcher._GODOT_NATIVE_MEMBERS — kept inline to avoid the
# build module cross-import.
_GODOT_NATIVE_MEMBERS = frozenset({
    "position", "global_position", "velocity", "rotation", "rotation_degrees",
    "scale", "skew", "transform", "global_transform", "z_index",
    "visible", "modulate", "self_modulate", "name", "owner", "script",
    "process_mode", "process_priority", "process_physics_priority",
    "unique_name_in_owner", "floor_max_angle", "floor_snap_length",
    "floor_stop_on_slope", "floor_constant_speed", "floor_block_on_wall",
    "platform_on_leave", "platform_floor_layers", "platform_wall_layers",
    "wall_min_slide_angle", "slide_on_ceiling", "up_direction", "motion_mode",
    "collision_layer", "collision_mask", "collision_priority",
})

# Types that stay strongly-typed in the generated code.
_STRONG_TYPES = {
    "int":     ("int",     "0"),
    "integer": ("int",     "0"),
    "float":   ("float",   "0.0"),
    "number":  ("float",   "0.0"),
    "bool":    ("bool",    "false"),
    "boolean": ("bool",    "false"),
    "string":  ("String",  '""'),
    "str":     ("String",  '""'),
    "vector2": ("Vector2", "Vector2.ZERO"),
    "color":   ("Color",   "Color.WHITE"),
    "rect2":   ("Rect2",   "Rect2()"),
}

# Types that get UNTYPED declarations (so null is assignable).
_COLLECTION_TYPES = {
    "dictionary": "{}",
    "dict":       "{}",
    "array":      "[]",
    "list":       "[]",
    "object":     "null",
    "variant":    "null",
}


def _emit_var_line(node: PropertyNode) -> str | None:
    """Return a single GDScript var declaration for a PropertyNode, or None
    if this property should not be declared on the character file (natives
    skipped, derived handled elsewhere).
    """
    if node.name in _GODOT_NATIVE_MEMBERS:
        return None
    if node.category == Category.DERIVED:
        return None  # Derived handled separately as getter methods.

    t = (node.type or "").lower().strip()

    # Enum-valued strings use the first enum value as default when no
    # initial_value is set. This keeps the canonical contract visible.
    if t in ("string", "str") and node.enum_values:
        default_literal = f'"{node.enum_values[0]}"'
        decl_keyword = "@export var" if node.category == Category.CONFIG else "var"
        return f"{decl_keyword} {node.name}: String = {default_literal}  # enum"

    if t in _STRONG_TYPES:
        gd_type, default = _STRONG_TYPES[t]
        decl_keyword = "@export var" if node.category == Category.CONFIG else "var"
        return f"{decl_keyword} {node.name}: {gd_type} = {default}"

    if t in _COLLECTION_TYPES:
        default = _COLLECTION_TYPES[t]
        # Collections are intentionally untyped so systems can assign null
        # to clear them. Still use @export for config so the inspector shows
        # them, but leave type annotation off so Variant is inferred.
        decl_keyword = "@export var" if node.category == Category.CONFIG else "var"
        return f"{decl_keyword} {node.name} = {default}  # untyped (accepts null)"

    # Unknown type — fall back to untyped Variant.
    decl_keyword = "@export var" if node.category == Category.CONFIG else "var"
    return f"{decl_keyword} {node.name} = null  # type={t or '?'}"


def emit_character(
    character_name: str,
    imap: ImpactMap,
    role: str = "fighter",
    godot_base_node: str = "CharacterBody2D",
) -> str:
    """Build the full GDScript source for one character file.

    Walks imap.properties_owned_by(role), sorts by category (config → state),
    emits one var per property, plus the character identifier as a comment.
    """
    nodes = imap.properties_owned_by(role)
    # Group by category for readable output.
    by_cat: dict[Category, list[PropertyNode]] = {
        Category.CONFIG: [],
        Category.STATE: [],
        Category.DERIVED: [],
    }
    for n in nodes:
        by_cat.setdefault(n.category, []).append(n)

    for lst in by_cat.values():
        lst.sort(key=lambda n: n.name)

    lines: list[str] = [
        f"extends {godot_base_node}",
        f"## {character_name} — generated by character_gen from impact map.",
        f"## Role: {role} | Properties: {len(nodes)} (from imap.properties_owned_by('{role}'))",
        "## DO NOT HAND-EDIT. Re-run scripts/build_game.py to refresh.",
        "",
    ]

    if by_cat[Category.CONFIG]:
        lines.append("# --- Config properties (@export, set at game start) ---")
        seen: set[str] = set()
        for node in by_cat[Category.CONFIG]:
            if node.name in seen:
                continue
            seen.add(node.name)
            line = _emit_var_line(node)
            if line:
                lines.append(line)
        lines.append("")

    if by_cat[Category.STATE]:
        lines.append("# --- State properties (var, mutated at runtime) ---")
        seen_state: set[str] = set()
        for node in by_cat[Category.STATE]:
            if node.name in seen_state:
                continue
            seen_state.add(node.name)
            line = _emit_var_line(node)
            if line:
                lines.append(line)
        lines.append("")

    # Derived getters — simple pass-through for now. A full derivation
    # walker lives in mechanic_gen; characters only get getters when
    # the derivation is trivial (e.g. a single ref).
    if by_cat[Category.DERIVED]:
        lines.append("# --- Derived properties (getters — read-only) ---")
        for node in by_cat[Category.DERIVED]:
            t = (node.type or "Variant").lower()
            gd_type = _STRONG_TYPES.get(t, ("Variant", "null"))[0]
            lines.append(f"func get_{node.name}() -> {gd_type}:")
            lines.append(f"\t# TODO: derivation for {node.id}")
            lines.append(f"\treturn {_STRONG_TYPES.get(t, ('Variant', 'null'))[1]}")
            lines.append("")

    return "\n".join(lines) + "\n"


def emit_all_characters(
    imap: ImpactMap,
    characters: list[str],
    output_dir: Path,
    role: str = "fighter",
    godot_base_node: str = "CharacterBody2D",
) -> dict[str, Path]:
    """Emit one {character}.gd per character in `characters`. Returns a map
    of character name → written path.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for char in characters:
        source = emit_character(char, imap, role=role, godot_base_node=godot_base_node)
        out_path = output_dir / f"{char}.gd"
        out_path.write_text(source, encoding="utf-8")
        written[char] = out_path
        _log.info(
            "character_gen: wrote %s (%d bytes, %d lines)",
            out_path.name, len(source), source.count("\n"),
        )
    return written
