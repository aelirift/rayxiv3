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

from rayxi.spec.impact_map import ImpactMap
from rayxi.spec.models import GameIdentity

_log = logging.getLogger("rayxi.build.scene_gen")


def _role_owner(raw_owner: str) -> str:
    """Strip instance suffix — 'character.ryu' → 'character', 'hud.p1_bar' → 'hud'.
    Role-generic owners stay as-is.
    """
    return raw_owner.split(".", 1)[0]


def pool_owners_from_imap(imap: ImpactMap, hlt_roles: dict | None) -> list[str]:
    """Entity-pool owners: distinct `imap.nodes[*].owner` values (after stripping
    instance suffix) that have a matching entry in `hlt.roles`. Owners without
    role metadata don't get pools — they're either singletons (game), instance
    widgets (mechanic_patcher handles them), or unowned metadata.
    """
    roles = hlt_roles or {}
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


def _game_prop_gd_type(t: str) -> str:
    return _TYPE_TO_GDTYPE.get(t.lower(), "Variant")


def _game_prop_default(t: str) -> str:
    return _TYPE_TO_DEFAULT.get(t.lower(), "null")


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
    var parts = pattern.split("*", false)
    if parts.size() == 1:
        return name == pattern
    var pos: int = 0
    if not pattern.begins_with("*"):
        if not name.begins_with(parts[0]):
            return false
        pos = parts[0].length()
        parts = parts.slice(1)
    for i in range(parts.size()):
        var part = parts[i]
        if part == "":
            continue
        var found = name.find(part, pos)
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


def _emit_hud_widgets(hlr: GameIdentity) -> list[str]:
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

    if not widgets:
        return []

    lines = ["    # --- HUD widgets from mechanic_specs ---"]
    for i, (name, fighter_idx) in enumerate(widgets):
        var_name = f"_hud_{i}"
        lines.append(
            f'    var {var_name} = preload("res://scripts/hud/{name}.gd").new()'
        )
        if fighter_idx is not None:
            lines.append(
                f'    if entity_pools.get("fighters", []).size() > {fighter_idx}:'
            )
            lines.append(
                f'        {var_name}.fighter_path = entity_pools["fighters"][{fighter_idx}].get_path()'
            )
        # Default widget position: stagger across the top of the screen.
        # Widgets can override in their own _ready() if needed.
        x_pos = 100 + i * 1560
        lines.append(f"    {var_name}.position = Vector2({x_pos}, 120)")
        lines.append(f"    add_child({var_name})")
    return lines


def emit_scene(
    imap: ImpactMap,
    hlr: GameIdentity,
    constants: dict,
    godot_dir: Path,
    scene_name: str = "fighting",
    hlt_roles: dict | None = None,
) -> Path:
    """Emit scenes/{scene_name}.gd deterministically from the impact map.

    Returns the written path. Overwrites any existing file.
    """
    roles = hlt_roles or {}
    pool_owners = pool_owners_from_imap(imap, roles)
    all_ordered = imap.ordered_systems()

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

    # Game-scoped properties become var declarations on the scene root so
    # systems can write `get_parent().round_state = "fighting"` (or whatever).
    # The impact map models `game.*` as a singleton owner — this is the
    # pipeline's way of projecting that singleton onto the scene tree.
    game_nodes = [n for n in imap.nodes.values() if n.owner == "game"]
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

    ready_body = ["func _ready():"]

    ready_body.append("    # --- populate entity pools (from hlt.roles scene_acquisition) ---")
    for owner in pool_owners:
        ready_body.extend(_emit_pool_population(owner, roles))
    ready_body.append("")

    ready_body.append("    # --- load DLR constants into per-system config buckets ---")
    ready_body.extend(_emit_config_dict(constants))
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
    ready_body.append("")

    hud_block = _emit_hud_widgets(hlr)
    if hud_block:
        ready_body.extend(hud_block)
        ready_body.append("")

    ready_body.append(
        '    print("[trace] scene.ready scene=%s systems=%d pools=%s" % '
        f'["{scene_name}", systems.size(), str(entity_pools.keys())])'
    )
    ready_body.append("")

    process_body = ["func _physics_process(delta):"]
    for s in ordered:
        process_body.append(_emit_system_process(s))

    lines = header + ready_body + process_body + [_emit_name_matcher()]
    source = "\n".join(lines) + "\n"

    out_path = godot_dir / "scenes" / f"{scene_name}.gd"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(source, encoding="utf-8")
    _log.info("scene_gen: wrote %s (%d bytes)", out_path, len(source))
    return out_path
