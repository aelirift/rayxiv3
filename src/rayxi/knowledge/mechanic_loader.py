"""Mechanic template loader — expands KB templates into full role schemas.

Takes a genre mechanic template (e.g. 2d_fighter.json) and a game's HLR,
and produces the complete property schema for each role.

The LLM doesn't invent properties. The template defines them.
The LLM only fills VALUES in DLR.

Usage:
    from rayxi.knowledge.mechanic_loader import load_fighter_schema

    schema = load_fighter_schema(template_path, hlr)
    # schema.fighter_config: list of config properties
    # schema.fighter_state: list of state properties
    # schema.fighter_derived: list of derived properties
    # schema.per_character: dict[char_name, list of instance-unique properties]
    # schema.game_config: list of game-level config
    # schema.game_state: list of game-level state
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from rayxi.spec.models import GameIdentity

_log = logging.getLogger("rayxi.knowledge.mechanic_loader")


@dataclass
class PropertySpec:
    """One property in the expanded schema."""
    name: str
    type: str
    category: str       # "config", "state", "derived"
    scope: str          # "role_generic" or "instance_unique"
    default: str = ""
    initial: str = ""   # for state: initial value
    formula: str = ""   # for derived: computation formula
    written_by: list[str] = field(default_factory=list)
    read_by: list[str] = field(default_factory=list)
    write_verb: str = ""  # how the system writes this property: subtract, set, set_state, increment, etc.
    purpose: str = ""
    source_mechanic: str = ""  # which mechanic contributed this


@dataclass
class RoleSchema:
    """Complete expanded property schema for one role."""
    role_name: str
    godot_base_node: str = ""
    properties: list[PropertySpec] = field(default_factory=list)
    animations_required: list[str] = field(default_factory=list)

    @property
    def config_props(self) -> list[PropertySpec]:
        return [p for p in self.properties if p.category == "config"]

    @property
    def state_props(self) -> list[PropertySpec]:
        return [p for p in self.properties if p.category == "state"]

    @property
    def derived_props(self) -> list[PropertySpec]:
        return [p for p in self.properties if p.category == "derived"]

    @property
    def generic_props(self) -> list[PropertySpec]:
        return [p for p in self.properties if p.scope == "role_generic"]

    @property
    def unique_props(self) -> list[PropertySpec]:
        return [p for p in self.properties if p.scope == "instance_unique"]


@dataclass
class ExpandedGameSchema:
    """Complete property schema for the entire game."""
    game_name: str
    fighter_schema: RoleSchema = field(default_factory=lambda: RoleSchema("fighter"))
    projectile_schema: RoleSchema = field(default_factory=lambda: RoleSchema("projectile"))
    hud_bar_schema: RoleSchema = field(default_factory=lambda: RoleSchema("hud_bar"))
    hud_text_schema: RoleSchema = field(default_factory=lambda: RoleSchema("hud_text"))
    stage_schema: RoleSchema = field(default_factory=lambda: RoleSchema("stage"))
    game_config: list[PropertySpec] = field(default_factory=list)
    game_state: list[PropertySpec] = field(default_factory=list)
    game_derived: list[PropertySpec] = field(default_factory=list)
    per_character_unique: dict[str, list[PropertySpec]] = field(default_factory=dict)
    mechanic_descriptions: dict[str, str] = field(default_factory=dict)  # system_name → description


def _expand_properties(
    props_list: list[dict],
    category: str,
    scope: str,
    mechanic_name: str,
) -> list[PropertySpec]:
    """Expand a list of property defs from the template into PropertySpecs."""
    result: list[PropertySpec] = []
    for p in props_list:
        result.append(PropertySpec(
            name=p["name"],
            type=p.get("type", ""),
            category=category,
            scope=scope,
            default=str(p.get("default", "")),
            initial=str(p.get("initial", "")),
            formula=p.get("formula", ""),
            written_by=p.get("written_by", []) if isinstance(p.get("written_by"), list) else [p.get("written_by", "")],
            read_by=p.get("read_by", []) if isinstance(p.get("read_by"), list) else [p.get("read_by", "")],
            write_verb=p.get("write_verb", ""),
            purpose=p.get("purpose", ""),
            source_mechanic=mechanic_name,
        ))
    return result


def _expand_templated_attacks(
    attack_template: dict,
    mechanic_name: str,
) -> list[PropertySpec]:
    """Expand the normal_attack_template: 18 attacks × N properties each."""
    result: list[PropertySpec] = []
    attacks = attack_template["attacks"]
    per_attack = attack_template["per_attack"]

    for attack in attacks:
        for prop_def in per_attack:
            prop_name = prop_def["name"].replace("{attack}", attack)
            result.append(PropertySpec(
                name=prop_name,
                type=prop_def.get("type", ""),
                category="config",
                scope="role_generic",
                default=str(prop_def.get("default", "")),
                write_verb=prop_def.get("write_verb", ""),
                purpose=f"{attack}: {prop_def.get('purpose', '')}",
                source_mechanic=mechanic_name,
            ))

    return result


def _expand_special_moves(
    special_template: dict,
    character_name: str,
    move_names: list[str],
    mechanic_name: str,
) -> list[PropertySpec]:
    """Expand per_special_move_template for a character's specific moves."""
    result: list[PropertySpec] = []
    config_template = special_template.get("config", [])

    for move in move_names:
        for prop_def in config_template:
            prop_name = prop_def["name"].replace("{move}", move)
            result.append(PropertySpec(
                name=prop_name,
                type=prop_def.get("type", ""),
                category="config",
                scope="instance_unique",
                default=str(prop_def.get("default", "")),
                write_verb=prop_def.get("write_verb", ""),
                purpose=f"{character_name}.{move}: {prop_def.get('purpose', '')}",
                source_mechanic=mechanic_name,
            ))

    return result


def load_game_schema(
    template_path: Path,
    hlr: GameIdentity,
) -> ExpandedGameSchema:
    """Load a mechanic template and expand it for a specific game's HLR.

    1. Load the genre template
    2. For each mechanic, add its contributed properties to the appropriate roles
    3. Expand normal attack templates (role-generic, 18 attacks)
    4. Expand special move templates (instance-unique, per character)
    5. Return the complete schema
    """
    template = json.loads(template_path.read_text())
    schema = ExpandedGameSchema(game_name=hlr.game_name)

    # Set Godot base nodes from template roles
    roles = template.get("roles", {})
    if "fighter" in roles:
        schema.fighter_schema.godot_base_node = roles["fighter"].get("godot_base_node", "CharacterBody2D")
    if "projectile" in roles:
        schema.projectile_schema.godot_base_node = roles["projectile"].get("godot_base_node", "Area2D")
    if "stage" in roles:
        schema.stage_schema.godot_base_node = roles["stage"].get("godot_base_node", "Sprite2D")

    # Process each mechanic
    for mech_name, mech in template.get("mechanics", {}).items():
        # Store mechanic description for SystemNode embedding
        if mech.get("description"):
            schema.mechanic_descriptions[mech_name] = mech["description"]

        is_instance_unique = mech.get("scope") == "instance_unique"
        base_scope = "instance_unique" if is_instance_unique else "role_generic"

        # Fighter contributions
        fighter_contrib = mech.get("contributes_to_fighter", {})
        for cat in ["config", "state"]:
            props = _expand_properties(
                fighter_contrib.get(cat, []), cat, base_scope, mech_name)
            schema.fighter_schema.properties.extend(props)
        for prop_def in fighter_contrib.get("derived", []):
            schema.fighter_schema.properties.append(PropertySpec(
                name=prop_def["name"],
                type=prop_def.get("type", ""),
                category="derived",
                scope="role_generic",
                formula=prop_def.get("formula", ""),
                read_by=prop_def.get("read_by", []),
                purpose=prop_def.get("purpose", ""),
                source_mechanic=mech_name,
            ))

        # Game contributions
        game_contrib = mech.get("contributes_to_game", {})
        for prop_def_list in game_contrib.get("config", []):
            schema.game_config.extend(
                _expand_properties([prop_def_list], "config", "role_generic", mech_name))
        for prop_def_list in game_contrib.get("state", []):
            schema.game_state.extend(
                _expand_properties([prop_def_list], "state", "role_generic", mech_name))
        for prop_def in game_contrib.get("derived", []):
            schema.game_derived.append(PropertySpec(
                name=prop_def["name"],
                type=prop_def.get("type", ""),
                category="derived",
                scope="role_generic",
                formula=prop_def.get("formula", ""),
                read_by=prop_def.get("read_by", []),
                purpose=prop_def.get("purpose", ""),
                source_mechanic=mech_name,
            ))

        # Projectile contributions
        proj_contrib = mech.get("contributes_to_projectile", {})
        for cat in ["config", "state"]:
            props = _expand_properties(
                proj_contrib.get(cat, []), cat, "role_generic", mech_name)
            schema.projectile_schema.properties.extend(props)

        # HUD contributions
        hud_bar_contrib = mech.get("contributes_to_hud_bar", {})
        for cat in ["config", "state"]:
            props = _expand_properties(
                hud_bar_contrib.get(cat, []), cat, "role_generic", mech_name)
            schema.hud_bar_schema.properties.extend(props)

        # Normal attack template expansion
        if "normal_attack_template" in mech:
            attack_props = _expand_templated_attacks(
                mech["normal_attack_template"], mech_name)
            schema.fighter_schema.properties.extend(attack_props)

        # Animations
        if "fighter_animations_required" in mech:
            schema.fighter_schema.animations_required.extend(
                mech["fighter_animations_required"])

    # Expand special moves per character
    special_mech = template.get("mechanics", {}).get("special_move_system", {})
    special_template = special_mech.get("per_special_move_template", {})
    characters = hlr.get_enum("characters")
    special_moves_enum = hlr.get_enum("special_moves")

    # Map special moves to characters using KB game data
    # For now: assign all special moves to each character (LLM/KB refines later at DLR)
    # The template gives every character the STRUCTURE; DLR fills which are enabled
    for char in characters:
        char_specials = special_moves_enum  # all moves available, enabled=true/false per char
        char_props = _expand_special_moves(
            special_template, char, char_specials, "special_move_system")
        schema.per_character_unique[char] = char_props

    # Summary
    total_generic = len(schema.fighter_schema.generic_props)
    total_unique_per_char = {
        char: len(props) for char, props in schema.per_character_unique.items()
    }
    total_game = len(schema.game_config) + len(schema.game_state) + len(schema.game_derived)
    total_projectile = len(schema.projectile_schema.properties)
    total_animations = len(schema.fighter_schema.animations_required)

    _log.info(
        "Schema: fighter=%d generic + %s unique per char, game=%d, projectile=%d, animations=%d",
        total_generic, total_unique_per_char, total_game, total_projectile, total_animations,
    )

    return schema


def format_schema_summary(schema: ExpandedGameSchema) -> str:
    """Human-readable schema summary."""
    lines = [f"Game Schema: {schema.game_name}", ""]

    # Fighter
    f = schema.fighter_schema
    lines.append(f"Fighter role ({f.godot_base_node}):")
    lines.append(f"  Config (role_generic):  {len([p for p in f.config_props if p.scope == 'role_generic'])}")
    lines.append(f"  State (role_generic):   {len([p for p in f.state_props if p.scope == 'role_generic'])}")
    lines.append(f"  Derived:               {len(f.derived_props)}")
    lines.append(f"  Normal attacks:        {len([p for p in f.properties if 'punch' in p.name or 'kick' in p.name])}")
    lines.append(f"  Animations:            {len(f.animations_required)}")
    lines.append(f"  Total generic:         {len(f.generic_props)}")
    lines.append("")

    # Per-character unique
    for char, props in schema.per_character_unique.items():
        lines.append(f"  {char} unique: {len(props)} properties")
    lines.append("")

    # Game
    lines.append(f"Game level:")
    lines.append(f"  Config:  {len(schema.game_config)}")
    lines.append(f"  State:   {len(schema.game_state)}")
    lines.append(f"  Derived: {len(schema.game_derived)}")
    lines.append("")

    # Projectile
    lines.append(f"Projectile role ({schema.projectile_schema.godot_base_node}):")
    lines.append(f"  Properties: {len(schema.projectile_schema.properties)}")
    lines.append("")

    # Total
    total_per_char = len(f.properties)
    for props in schema.per_character_unique.values():
        total_per_char_with_unique = len(f.properties) + len(props)
        break
    else:
        total_per_char_with_unique = total_per_char

    lines.append(f"TOTAL per fighter: {total_per_char_with_unique} (generic + unique)")

    return "\n".join(lines)
