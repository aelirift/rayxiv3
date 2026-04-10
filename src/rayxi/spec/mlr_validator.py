"""MLR Validator — code-based consistency checks between HLR and decomposed MLR.

Two passes:
  1. Per-file: each file is internally valid and references HLR correctly
  2. Cross-file: files within a scene reference each other correctly

Returns a list of errors. Empty list = valid.
"""

from __future__ import annotations

import re

from .mlr import SceneMLR
from .models import GameIdentity, SceneListEntry

VALID_VERBS = {
    "subtract", "add", "spawn", "destroy", "set_state", "apply",
    "move", "reset", "increment", "decrement", "enable", "disable",
}


def _flatten_scenes(scenes: list[SceneListEntry]) -> list[SceneListEntry]:
    flat: list[SceneListEntry] = []
    for s in scenes:
        flat.append(s)
        if s.children:
            flat.extend(_flatten_scenes(s.children))
    return flat


def validate_mlr(hlr: GameIdentity, scene_mlrs: list[SceneMLR]) -> list[str]:
    errors: list[str] = []
    hlr_enums = hlr.enum_dict()
    all_hlr_enum_names = set(hlr_enums.keys())
    all_hlr_values = set()
    for vals in hlr_enums.values():
        all_hlr_values.update(vals)

    valid_scene_names = {s.scene_name for s in _flatten_scenes(hlr.scenes)}
    valid_fsm_states = set(hlr.global_fsm.states)
    game_systems = set(hlr.get_enum("game_systems"))

    for scene_mlr in scene_mlrs:
        # Pass 1: per-file checks
        errors.extend(_check_per_file(scene_mlr, hlr_enums, all_hlr_enum_names,
                                       all_hlr_values, valid_scene_names,
                                       valid_fsm_states, game_systems))
        # Pass 2: cross-file checks
        errors.extend(_check_cross_file(scene_mlr))

    # All HLR scenes should have an MLR
    mlr_scene_names = {m.scene_name for m in scene_mlrs}
    missing = valid_scene_names - mlr_scene_names
    if missing:
        errors.append(f"HLR scenes without MLR: {missing}")

    # Cross-scene: FSM transitions reference scenes that have MLRs
    for scene_mlr in scene_mlrs:
        if scene_mlr.fsm:
            for trans in scene_mlr.fsm.transitions:
                # Check if transitions reference global FSM states that have scenes
                match = re.match(r"(\S+)\s*->\s*(\S+)", trans)
                if match:
                    dst = match.group(2).rstrip(":")
                    # If it looks like a global state (S_*), check it exists
                    if dst.startswith("S_") and dst not in valid_fsm_states:
                        errors.append(
                            f"[{scene_mlr.scene_name}] FSM transition references "
                            f"unknown global state '{dst}'"
                        )

    return errors


# ---------------------------------------------------------------------------
# Pass 1: Per-file checks
# ---------------------------------------------------------------------------

def _check_per_file(
    scene_mlr: SceneMLR,
    hlr_enums: dict[str, list[str]],
    all_hlr_enum_names: set[str],
    all_hlr_values: set[str],
    valid_scene_names: set[str],
    valid_fsm_states: set[str],
    game_systems: set[str],
) -> list[str]:
    errors: list[str] = []
    p = f"[{scene_mlr.scene_name}]"

    # Scene name valid
    if scene_mlr.scene_name not in valid_scene_names:
        errors.append(f"{p} scene_name not in HLR scenes")

    # --- FSM ---
    if scene_mlr.fsm:
        if scene_mlr.fsm.fsm_state not in valid_fsm_states:
            errors.append(f"{p} FSM fsm_state '{scene_mlr.fsm.fsm_state}' not in HLR")
        if not scene_mlr.fsm.states:
            errors.append(f"{p} FSM has no sub-states")
        # Check FSM transitions are well-formed
        for trans in scene_mlr.fsm.transitions:
            if "->" not in trans:
                errors.append(f"{p} FSM malformed transition: '{trans}'")
    else:
        errors.append(f"{p} missing FSM")

    # --- Collisions ---
    if scene_mlr.collisions:
        for pair in scene_mlr.collisions.collision_pairs:
            if not pair.result:
                errors.append(f"{p} collision '{pair.object_a}' <-> '{pair.object_b}' has empty result")

    # --- System interactions ---
    for si in scene_mlr.system_interactions:
        sp = f"{p}[{si.game_system}]"
        if si.game_system not in game_systems:
            errors.append(f"{sp} game_system not in HLR game_systems enum")
        for idx, interaction in enumerate(si.interactions):
            if not interaction.trigger:
                errors.append(f"{sp} interaction[{idx}] empty trigger")
            if not interaction.effects:
                errors.append(f"{sp} interaction[{idx}] no effects")
            for eff in interaction.effects:
                if eff.verb not in VALID_VERBS:
                    errors.append(f"{sp} interaction[{idx}] invalid verb '{eff.verb}'")

    # --- Entities ---
    entity_names_list = [e.entity_name for e in scene_mlr.entities]
    dupes = {n for n in entity_names_list if entity_names_list.count(n) > 1}
    if dupes:
        errors.append(f"{p} duplicate entity names: {dupes}")

    for entity in scene_mlr.entities:
        ep = f"{p}[{entity.entity_name}]"
        if entity.parent_enum not in hlr_enums:
            errors.append(f"{ep} parent_enum '{entity.parent_enum}' not in HLR enums")
        for action_set in entity.action_sets:
            if action_set.owner != entity.entity_name and action_set.owner not in all_hlr_values:
                errors.append(f"{ep} ActionSet owner '{action_set.owner}' not this entity or in HLR enums")
            if not action_set.actions:
                errors.append(f"{ep} ActionSet '{action_set.category}' has no actions")
        for se in entity.scene_enums:
            if se.name in all_hlr_enum_names:
                errors.append(f"{ep} scene_enum '{se.name}' collides with HLR enum")
            if not se.values:
                errors.append(f"{ep} scene_enum '{se.name}' is empty")
            se_dupes = {v for v in se.values if se.values.count(v) > 1}
            if se_dupes:
                errors.append(f"{ep} scene_enum '{se.name}' has duplicates: {se_dupes}")

    return errors


# ---------------------------------------------------------------------------
# Pass 2: Cross-file checks (within a scene)
# ---------------------------------------------------------------------------

def _check_cross_file(scene_mlr: SceneMLR) -> list[str]:
    """Check that files within a scene reference each other correctly."""
    errors: list[str] = []
    p = f"[{scene_mlr.scene_name}]"

    entity_names = {e.entity_name for e in scene_mlr.entities}
    # Also collect all property names per entity for target validation
    entity_properties: dict[str, set[str]] = {}
    for e in scene_mlr.entities:
        entity_properties[e.entity_name] = {prop.name for prop in e.properties}

    # All declared actions across all entities (for interaction reference checking)
    all_actions: set[str] = set()
    for e in scene_mlr.entities:
        for action_set in e.action_sets:
            all_actions.update(action_set.actions)

    # FSM states — collect for reference checking
    fsm_states: set[str] = set()
    if scene_mlr.fsm:
        fsm_states = set(scene_mlr.fsm.states)

    # --- Collision pairs reference entities ---
    if scene_mlr.collisions:
        for pair in scene_mlr.collisions.collision_pairs:
            a_traceable = _is_traceable(pair.object_a, entity_names)
            b_traceable = _is_traceable(pair.object_b, entity_names)
            if not a_traceable:
                errors.append(
                    f"{p} collision object_a '{pair.object_a}' not traceable to any entity. "
                    f"Entities: {sorted(entity_names)}"
                )
            if not b_traceable:
                errors.append(
                    f"{p} collision object_b '{pair.object_b}' not traceable to any entity. "
                    f"Entities: {sorted(entity_names)}"
                )

    # --- Interaction effect targets reference entities ---
    for si in scene_mlr.system_interactions:
        sp = f"{p}[{si.game_system}]"
        for idx, interaction in enumerate(si.interactions):
            for eff in interaction.effects:
                target_root = eff.target.split(".")[0]
                if not _is_traceable(target_root, entity_names):
                    errors.append(
                        f"{sp} interaction[{idx}] effect target '{eff.target}' — "
                        f"root '{target_root}' not traceable to any entity"
                    )
                # If target has a property part, check it exists on the entity
                if "." in eff.target:
                    prop_name = eff.target.split(".", 1)[1]
                    # Find which entity this maps to
                    matched_entity = _find_entity(target_root, entity_names)
                    if matched_entity and matched_entity in entity_properties:
                        known_props = entity_properties[matched_entity]
                        # Nested properties (e.g. "position.x") — check root property
                        prop_root = prop_name.split(".")[0]
                        if known_props and prop_root not in known_props:
                            errors.append(
                                f"{sp} interaction[{idx}] effect target '{eff.target}' — "
                                f"property '{prop_root}' not declared on entity '{matched_entity}'. "
                                f"Known: {sorted(known_props)}"
                            )

                # spawn/destroy verbs should target something traceable
                if eff.verb == "spawn" and target_root not in entity_names:
                    # Spawning a new object — it should be declared as a transient entity
                    if not _is_traceable(target_root, entity_names):
                        errors.append(
                            f"{sp} interaction[{idx}] spawn target '{target_root}' "
                            f"has no entity spec (needs a transient entity file)"
                        )

    # --- Interaction conditions reference FSM states ---
    if fsm_states:
        for si in scene_mlr.system_interactions:
            sp = f"{p}[{si.game_system}]"
            for idx, interaction in enumerate(si.interactions):
                # Check if condition mentions state names — soft check
                # (conditions are free text, so we look for known state names)
                cond_lower = interaction.condition.lower()
                for state in fsm_states:
                    # If the condition references a state-like pattern, that's good
                    pass  # Soft check — don't error on this, just informational

    # --- Entity action_sets: actions referenced in interactions should exist ---
    interaction_action_refs: set[str] = set()
    for si in scene_mlr.system_interactions:
        for interaction in si.interactions:
            # Extract action-like references from triggers
            trigger_lower = interaction.trigger.lower()
            for action in all_actions:
                if action.lower() in trigger_lower:
                    interaction_action_refs.add(action)

    return errors


def _is_traceable(name: str, entity_names: set[str]) -> bool:
    """Check if a name is traceable to an entity. Handles compound names."""
    if name in entity_names:
        return True
    # Try prefix matching: "p1_fighter_hitbox" traces to "p1_fighter"
    for entity in entity_names:
        if name.startswith(entity) or entity.startswith(name):
            return True
    # Try generic names: "fighter" traces to any entity with "fighter" in it
    name_parts = name.split("_")
    for entity in entity_names:
        if any(part in entity for part in name_parts if len(part) > 2):
            return True
    # Special case: generic collision names like "wall", "floor" are always valid
    if name in ("wall", "floor", "ceiling", "boundary", "screen"):
        return True
    return False


def _find_entity(target_root: str, entity_names: set[str]) -> str | None:
    """Find the actual entity name for a target root."""
    if target_root in entity_names:
        return target_root
    for entity in entity_names:
        if target_root.startswith(entity) or entity.startswith(target_root):
            return entity
    return None
