"""Compiled build contract.

This is the req-authoritative artifact consumed by downstream build/codegen.
Templates may be borrowed while producing it, but once this file exists the
build should not need to read any template file again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rayxi.spec.impact_map import ImpactMap, Scope
from rayxi.spec.mechanic_contract import MechanicFeature, MechanicManifest
from rayxi.spec.models import GameIdentity


class BuildRoleDef(BaseModel):
    name: str
    godot_base_node: str = "Node2D"
    scene_acquisition: dict[str, Any] = Field(default_factory=lambda: {"method": "runtime_array"})
    source: str = "synthesized"
    borrowed_from: str | None = None


class BuildContract(BaseModel):
    game_name: str
    genre: str
    systems: list[str]
    scenes: list[str]
    phases: dict[str, str] = Field(default_factory=dict)
    system_descriptions: dict[str, str] = Field(default_factory=dict)
    property_enums: dict[str, list[str]] = Field(default_factory=dict)
    property_aliases: dict[str, str] = Field(default_factory=dict)
    property_name_aliases: dict[str, str] = Field(default_factory=dict)
    roles: dict[str, BuildRoleDef] = Field(default_factory=dict)
    system_origins: dict[str, str] = Field(default_factory=dict)
    scene_defaults: dict[str, dict[str, Any]] = Field(default_factory=dict)
    role_groups: dict[str, list[str]] = Field(default_factory=dict)
    capabilities: dict[str, bool] = Field(default_factory=dict)
    mechanics: list[MechanicFeature] = Field(default_factory=list)


_KNOWN_PROPERTY_ALIASES: tuple[tuple[str, str], ...] = (
    ("fighter.current_hp", "fighter.current_health"),
    ("fighter.max_hp", "fighter.max_health"),
    ("kart.is_ai", "kart.is_ai_controlled"),
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _role_owner(raw_owner: str) -> str:
    return raw_owner.split(".", 1)[0]


def _is_runtime_role_owner(node_owner: str, node_scope: Scope | str) -> bool:
    if node_owner == "game":
        return False
    if node_owner.startswith("hud."):
        return False
    if node_owner.startswith("character.") and node_scope == Scope.CHARACTER_DEF:
        return False
    return True


def _system_annotations(hlr: GameIdentity) -> tuple[dict[str, str], dict[str, str]]:
    descriptions: dict[str, str] = {}
    origins: dict[str, str] = {}
    for enum in hlr.enums:
        if enum.name != "game_systems":
            continue
        descriptions = dict(enum.value_descriptions or {})
        origins = dict(enum.value_template_origins or {})
        break
    return descriptions, origins


def _fallback_role_def(role_name: str) -> BuildRoleDef:
    lower_name = role_name.lower()
    fallback: dict[str, BuildRoleDef] = {
        "fighter": BuildRoleDef(
            name="fighter",
            godot_base_node="CharacterBody2D",
            scene_acquisition={"method": "nodes_in_scene", "pattern": "p*_fighter"},
        ),
        "projectile": BuildRoleDef(
            name="projectile",
            godot_base_node="Area2D",
            scene_acquisition={"method": "runtime_array"},
        ),
        "hud_bar": BuildRoleDef(
            name="hud_bar",
            godot_base_node="ProgressBar",
            scene_acquisition={"method": "nodes_in_scene", "pattern": "*_bar"},
        ),
        "hud_text": BuildRoleDef(
            name="hud_text",
            godot_base_node="Label",
            scene_acquisition={"method": "nodes_in_scene", "pattern": "*_display"},
        ),
        "stage": BuildRoleDef(
            name="stage",
            godot_base_node="ColorRect",
            scene_acquisition={"method": "named_node", "node_name": "Background"},
        ),
    }
    if role_name in fallback:
        return fallback[role_name]
    if any(token in lower_name for token in ("vehicle", "kart", "car", "racer", "driver", "bike", "ship", "pilot")):
        return BuildRoleDef(
            name=role_name,
            godot_base_node="CharacterBody2D",
            scene_acquisition={"method": "nodes_in_scene", "pattern": f"p*_{role_name}"},
        )
    if any(token in lower_name for token in ("projectile", "shell", "bullet", "missile", "banana", "pickup", "collectible", "item", "obstacle")):
        return BuildRoleDef(
            name=role_name,
            godot_base_node="Area2D",
            scene_acquisition={"method": "runtime_array"},
        )
    if any(token in lower_name for token in ("track", "course", "arena", "background", "stage")):
        return BuildRoleDef(
            name=role_name,
            godot_base_node="ColorRect",
            scene_acquisition={"method": "named_node", "node_name": "Background"},
        )
    if lower_name.startswith("hud_"):
        return BuildRoleDef(
            name=role_name,
            godot_base_node="Control",
            scene_acquisition={"method": "nodes_in_scene", "pattern": f"*{role_name}*"},
        )
    return BuildRoleDef(name=role_name)


def _role_defs_from_sources(
    owners: set[str],
    template_roles: dict[str, Any],
    hlt_roles: dict[str, Any],
) -> dict[str, BuildRoleDef]:
    role_defs: dict[str, BuildRoleDef] = {}
    for owner in sorted(owners):
        hlt_role = hlt_roles.get(owner, {})
        template_role = template_roles.get(owner, {})
        if hlt_role or template_role:
            source = "hlt" if hlt_role else "template"
            borrowed_from = f"{source}.roles.{owner}"
            role_defs[owner] = BuildRoleDef(
                name=owner,
                godot_base_node=(
                    hlt_role.get("godot_base_node")
                    or template_role.get("godot_base_node")
                    or "Node2D"
                ),
                scene_acquisition=(
                    hlt_role.get("scene_acquisition")
                    or template_role.get("scene_acquisition")
                    or {"method": "runtime_array"}
                ),
                source=source,
                borrowed_from=borrowed_from,
            )
            continue
        role_defs[owner] = _fallback_role_def(owner)
    return role_defs


def _property_alias_maps(imap: ImpactMap) -> tuple[dict[str, str], dict[str, str]]:
    """Compile deterministic alias maps for duplicate req property names.

    These aliases live in the compiled req contract, so downstream build/codegen
    can collapse known duplicate properties before generating code.
    """
    full_aliases: dict[str, str] = {}
    bare_aliases: dict[str, str] = {}
    for alias_id, canonical_id in _KNOWN_PROPERTY_ALIASES:
        if alias_id not in imap.nodes or canonical_id not in imap.nodes:
            continue
        full_aliases[alias_id] = canonical_id
        bare_aliases[alias_id.split(".", 1)[1]] = canonical_id.split(".", 1)[1]
    return full_aliases, bare_aliases


_RACE_ROLE_TOKENS = ("vehicle", "kart", "car", "bike", "ship", "racer", "driver")
_COMBAT_ROLE_TOKENS = ("fighter", "combatant", "duelist", "brawler")
_RACE_SCENE_TOKENS = ("race", "racing", "track", "circuit", "lap")
_COMBAT_SCENE_TOKENS = ("fight", "battle", "combat", "match", "arena")
_STAGE_ROLE_TOKENS = ("stage", "track", "course", "arena", "background", "world", "map")
_CAMERA_ROLE_TOKENS = ("camera",)
_PROJECTILE_ROLE_TOKENS = ("projectile", "shell", "bullet", "missile", "beam", "shot", "orb", "fireball")
_PICKUP_ROLE_TOKENS = ("item", "pickup", "collectible", "powerup", "box", "crate", "coin", "pickup")
_HAZARD_ROLE_TOKENS = ("banana", "peel", "trap", "hazard", "obstacle", "shell", "mine", "bomb")


def _role_base_node(role_def: BuildRoleDef) -> str:
    return str(getattr(role_def, "godot_base_node", "") or "")


def _role_scene_acquisition(role_def: BuildRoleDef) -> dict[str, Any]:
    return dict(getattr(role_def, "scene_acquisition", {}) or {})


def _append_role(group: list[str], role_name: str) -> None:
    if role_name and role_name not in group:
        group.append(role_name)


def _group_roles(
    hlr: GameIdentity,
    role_defs: dict[str, BuildRoleDef],
) -> dict[str, list[str]]:
    is_checkpoint_race = _is_checkpoint_race_profile(hlr, role_defs)
    is_duel_combat = _is_duel_combat_profile(hlr, role_defs)
    groups: dict[str, list[str]] = {
        "actor_roles": [],
        "vehicle_actor_roles": [],
        "combat_actor_roles": [],
        "stage_roles": [],
        "camera_roles": [],
        "projectile_roles": [],
        "pickup_roles": [],
        "hazard_roles": [],
        "hud_roles": [],
    }

    for role_name in sorted(role_defs.keys()):
        role_def = role_defs[role_name]
        lower_name = role_name.lower()
        base_node = _role_base_node(role_def)
        acquisition = _role_scene_acquisition(role_def)
        acquisition_method = str(acquisition.get("method") or "")

        is_hud = lower_name.startswith("hud_") or base_node in {"Control", "ProgressBar", "Label", "TextureRect"}
        is_camera = any(token in lower_name for token in _CAMERA_ROLE_TOKENS)
        is_stage = (
            role_name == "stage"
            or any(token in lower_name for token in _STAGE_ROLE_TOKENS)
            or (base_node == "ColorRect" and acquisition_method == "named_node")
        )
        is_pickup = any(token in lower_name for token in _PICKUP_ROLE_TOKENS)
        is_projectile = any(token in lower_name for token in _PROJECTILE_ROLE_TOKENS)
        is_hazard = any(token in lower_name for token in _HAZARD_ROLE_TOKENS)
        is_actor = (
            base_node == "CharacterBody2D"
            or acquisition_method == "nodes_in_scene"
        ) and not any((is_hud, is_camera, is_stage))

        if is_hud:
            _append_role(groups["hud_roles"], role_name)
        if is_camera:
            _append_role(groups["camera_roles"], role_name)
        if is_stage:
            _append_role(groups["stage_roles"], role_name)

        if is_actor:
            _append_role(groups["actor_roles"], role_name)
            if any(token in lower_name for token in _RACE_ROLE_TOKENS) or (is_checkpoint_race and not any(token in lower_name for token in _COMBAT_ROLE_TOKENS)):
                _append_role(groups["vehicle_actor_roles"], role_name)
            if any(token in lower_name for token in _COMBAT_ROLE_TOKENS) or (is_duel_combat and not any(token in lower_name for token in _RACE_ROLE_TOKENS)):
                _append_role(groups["combat_actor_roles"], role_name)

        if base_node == "Area2D":
            if is_pickup:
                _append_role(groups["pickup_roles"], role_name)
            if is_projectile:
                _append_role(groups["projectile_roles"], role_name)
            if is_hazard or (not is_pickup and not is_projectile):
                _append_role(groups["hazard_roles"], role_name)

    if not groups["actor_roles"]:
        for role_name in groups["combat_actor_roles"] + groups["vehicle_actor_roles"]:
            _append_role(groups["actor_roles"], role_name)
    return groups


def _capability_flags(
    hlr: GameIdentity,
    role_defs: dict[str, BuildRoleDef],
    role_groups: dict[str, list[str]],
) -> dict[str, bool]:
    system_names = _system_names_from_hlr(hlr)
    joined_system_names = " ".join(sorted(system_names))
    checkpoint_race = _is_checkpoint_race_profile(hlr, role_defs)
    duel_combat = _is_duel_combat_profile(hlr, role_defs)
    has_cameras = bool(role_groups.get("camera_roles")) or "camera_tracking" in joined_system_names
    return {
        "checkpoint_race": checkpoint_race,
        "duel_combat": duel_combat,
        "has_projectiles": bool(role_groups.get("projectile_roles")),
        "has_pickups": bool(role_groups.get("pickup_roles")),
        "has_cameras": has_cameras,
        "mode7_surface": checkpoint_race and has_cameras and bool(role_groups.get("actor_roles")),
    }


def _checkpoint_race_scene_defaults(scene_name: str) -> dict[str, Any]:
    return {
        "checkpoint_positions": [
            [420.0, 540.0],
            [960.0, 240.0],
            [1500.0, 540.0],
            [960.0, 840.0],
        ],
        "item_box_positions": [
            [690.0, 400.0],
            [1230.0, 400.0],
            [1230.0, 680.0],
            [690.0, 680.0],
        ],
        "hud_layout": {
            "lap_counter": [60.0, 36.0],
            "position_display": [1590.0, 36.0],
            "speedometer": [60.0, 938.0],
            "item_icon": [820.0, 928.0],
            "finish_banner": [760.0, 68.0],
            "minimap": [1640.0, 760.0],
        },
    }


def _duel_combat_scene_defaults(scene_name: str) -> dict[str, Any]:
    return {
        "hud_layout": {
            "p1_rage_meter": [100.0, 126.0],
            "p2_rage_meter": [1590.0, 126.0],
        },
    }


def _system_names_from_hlr(hlr: GameIdentity) -> set[str]:
    names: set[str] = set()
    for spec in getattr(hlr, "mechanic_specs", []) or []:
        system_name = getattr(spec, "system_name", "") or ""
        if system_name:
            names.add(system_name.lower())
    return names


def _global_rules_text(hlr: GameIdentity) -> str:
    return " ".join(getattr(hlr, "global_rules", []) or []).lower()


def _is_checkpoint_race_profile(
    hlr: GameIdentity,
    role_defs: dict[str, BuildRoleDef],
) -> bool:
    role_names = {name.lower() for name in role_defs.keys()}
    system_names = _system_names_from_hlr(hlr)
    rules_text = _global_rules_text(hlr)
    has_vehicle_actor = any(any(token in role_name for token in _RACE_ROLE_TOKENS) for role_name in role_names)
    has_race_systems = any(
        token in " ".join(system_names)
        for token in ("vehicle_movement", "race_progress", "position_ranking", "drift_boost", "camera_tracking")
    )
    has_race_rules = any(token in rules_text for token in ("checkpoint", "lap", "race", "position rank", "finish line"))
    return ("stage" in role_names or "camera" in role_names) and (has_vehicle_actor or has_race_systems or has_race_rules)


def _is_duel_combat_profile(
    hlr: GameIdentity,
    role_defs: dict[str, BuildRoleDef],
) -> bool:
    role_names = {name.lower() for name in role_defs.keys()}
    system_names = _system_names_from_hlr(hlr)
    joined_system_names = " ".join(system_names)
    rules_text = _global_rules_text(hlr)
    has_combat_actor = any(any(token in role_name for token in _COMBAT_ROLE_TOKENS) for role_name in role_names)
    has_combat_systems = any(
        token in joined_system_names
        for token in (
            "combat_system",
            "health_system",
            "blocking_system",
            "projectile_system",
            "special_move_system",
            "stun_system",
            "combo_system",
            "rage_meter_system",
            "round_system",
        )
    )
    combat_rule_hits = sum(
        1
        for token in ("damage", "attack", "blocking", "blockstun", "ko", "hitbox", "hurtbox", "combo", "stun")
        if token in rules_text
    )
    has_combat_rules = combat_rule_hits >= 2
    return has_combat_actor or has_combat_systems or has_combat_rules


def _synthesize_scene_defaults(
    hlr: GameIdentity,
    role_defs: dict[str, BuildRoleDef],
) -> dict[str, dict[str, Any]]:
    scene_defaults: dict[str, dict[str, Any]] = {}
    is_checkpoint_race = _is_checkpoint_race_profile(hlr, role_defs)
    is_duel_combat = _is_duel_combat_profile(hlr, role_defs)

    for scene in hlr.scenes:
        scene_name = scene.scene_name
        lower_name = scene_name.lower()
        if is_checkpoint_race and any(token in lower_name for token in _RACE_SCENE_TOKENS):
            scene_defaults[scene_name] = _checkpoint_race_scene_defaults(scene_name)
        elif is_duel_combat and any(token in lower_name for token in _COMBAT_SCENE_TOKENS):
            scene_defaults[scene_name] = _duel_combat_scene_defaults(scene_name)

    return scene_defaults


def compile_build_contract(
    hlr: GameIdentity,
    imap: ImpactMap,
    template_path: Path | None = None,
    hlt_path: Path | None = None,
    manifest: MechanicManifest | None = None,
) -> BuildContract:
    """Compile the post-DLR build contract consumed by downstream stages."""
    template = _read_json(template_path) if template_path else {}
    hlt = _read_json(hlt_path) if hlt_path else {}

    template_roles = template.get("roles", {})
    hlt_roles = hlt.get("roles", {})
    system_descriptions, system_origins = _system_annotations(hlr)

    property_enums = {
        node_id: list(node.enum_values)
        for node_id, node in imap.nodes.items()
        if node.enum_values
    }

    owner_names = {
        _role_owner(node.owner)
        for node in imap.nodes.values()
        if _is_runtime_role_owner(node.owner, node.scope)
    }
    role_defs = _role_defs_from_sources(owner_names, template_roles, hlt_roles)
    property_aliases, property_name_aliases = _property_alias_maps(imap)
    scene_defaults = _synthesize_scene_defaults(hlr, role_defs)
    role_groups = _group_roles(hlr, role_defs)
    capabilities = _capability_flags(hlr, role_defs, role_groups)

    return BuildContract(
        game_name=hlr.game_name,
        genre=hlr.genre,
        systems=list(imap.systems),
        scenes=list(imap.scenes),
        phases={system: imap.phase_for(system) for system in imap.systems},
        system_descriptions=system_descriptions,
        property_enums=property_enums,
        property_aliases=property_aliases,
        property_name_aliases=property_name_aliases,
        roles=role_defs,
        system_origins=system_origins,
        scene_defaults=scene_defaults,
        role_groups=role_groups,
        capabilities=capabilities,
        mechanics=list((manifest.features if manifest is not None else [])),
    )
