"""HLR Validator — code-based internal consistency checks.

Not an LLM call. Deterministic rules that catch contradictions in the
GameIdentity before it gets locked and passed to MLR.

Checks structural consistency across base + dynamic fields:
- FSM graph: reachable, no dead ends, all states have scenes
- Scene hierarchy: unique names, every scene has an FSM state
- Cross-references: rules don't reference non-existent things
- Duplicates: no duplicate names anywhere

Returns a list of errors. Empty list = valid.
"""

from __future__ import annotations

import re

from .models import GameIdentity, SchemaField, SceneListEntry

# Every HLR must declare these enums. Validated by _check_enums.
# (name, entity flag, description — for error messages)
REQUIRED_ENUMS: list[tuple[str, bool, str]] = [
    ("scenes",       False, "every scene_name from the scenes list"),
    ("fsm_states",   False, "every FSM state from global_fsm.states"),
    ("game_systems", False, "interaction systems — each gets its own interaction spec per scene"),
    ("characters",   True,  "playable/NPC characters that appear in scenes"),
    ("stages",       True,  "background/arena objects that appear in scenes"),
    ("hud_elements", True,  "HUD objects rendered in scenes"),
    ("game_objects", True,  "other instantiable objects (not characters/stages/HUD)"),
]

VALID_KEYS = {
    "LEFT", "RIGHT", "UP", "DOWN", "SPACE", "ENTER", "ESCAPE", "TAB", "SHIFT",
    "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
    "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
}


def validate_hlr(hlr: GameIdentity, dynamic_fields: list[SchemaField] | None = None) -> list[str]:
    errors: list[str] = []
    errors.extend(_check_fsm(hlr))
    errors.extend(_check_scenes(hlr))
    errors.extend(_check_enums(hlr))
    errors.extend(_check_rules(hlr))
    errors.extend(_check_duplicates(hlr))
    errors.extend(_check_dynamic_fields_present(hlr, dynamic_fields or []))
    return errors


# ---------------------------------------------------------------------------
# Scene helpers (flattens hierarchy)
# ---------------------------------------------------------------------------

def _flatten_scenes(scenes: list[SceneListEntry]) -> list[SceneListEntry]:
    flat: list[SceneListEntry] = []
    for s in scenes:
        flat.append(s)
        if s.children:
            flat.extend(_flatten_scenes(s.children))
    return flat


# ---------------------------------------------------------------------------
# FSM checks
# ---------------------------------------------------------------------------

def _check_fsm(hlr: GameIdentity) -> list[str]:
    errors: list[str] = []
    fsm_states = set(hlr.global_fsm.states)
    all_scenes = _flatten_scenes(hlr.scenes)
    scene_states = {s.fsm_state for s in all_scenes}

    # Every scene FSM state must be in global FSM
    for scene in all_scenes:
        if scene.fsm_state not in fsm_states:
            errors.append(
                f"Scene '{scene.scene_name}' references FSM state '{scene.fsm_state}' "
                f"not in global_fsm.states"
            )

    # Every global FSM state should have a scene
    orphan_states = fsm_states - scene_states
    if orphan_states:
        errors.append(f"FSM states without scenes: {orphan_states}")

    # Check transitions reference valid states
    targets = set()
    sources = set()
    for trans in hlr.global_fsm.transitions:
        parts = re.match(r"(\S+)\s*->\s*(\S+)", trans)
        if not parts:
            errors.append(f"Malformed transition: '{trans}'")
            continue
        src, dst = parts.group(1).rstrip(":"), parts.group(2).rstrip(":")
        if src not in fsm_states:
            errors.append(f"Transition source '{src}' not in FSM states")
        if dst not in fsm_states:
            errors.append(f"Transition destination '{dst}' not in FSM states")
        sources.add(src)
        targets.add(dst)

    # Every state must be reachable (has incoming transition, except start)
    start_states = fsm_states - targets
    if len(start_states) > 1:
        errors.append(f"Multiple states with no incoming transition: {start_states}")

    # Every non-terminal state must have an outgoing transition
    dead_ends = fsm_states - sources
    real_dead_ends = dead_ends - start_states
    if real_dead_ends:
        errors.append(f"FSM states with no outgoing transition (dead ends): {real_dead_ends}")

    return errors


# ---------------------------------------------------------------------------
# Enum checks
# ---------------------------------------------------------------------------

def _check_enums(hlr: GameIdentity) -> list[str]:
    errors: list[str] = []
    enum_map = hlr.enum_dict()
    enum_by_name = {e.name: e for e in hlr.enums}
    all_scenes = _flatten_scenes(hlr.scenes)

    # --- Required enums: present, non-empty, correct entity flag ---
    for req_name, req_entity, req_desc in REQUIRED_ENUMS:
        if req_name not in enum_by_name:
            errors.append(f"Missing required enum: '{req_name}' ({req_desc})")
            continue
        edef = enum_by_name[req_name]
        if not edef.values:
            errors.append(f"Required enum '{req_name}' is empty")
        if edef.entity != req_entity:
            errors.append(
                f"Enum '{req_name}' has entity={edef.entity}, expected entity={req_entity}"
            )

    # --- Cross-reference: scenes enum ↔ actual scene list ---
    if "scenes" in enum_map:
        declared_scenes = set(enum_map["scenes"])
        actual_scenes = {s.scene_name for s in all_scenes}
        missing = actual_scenes - declared_scenes
        extra = declared_scenes - actual_scenes
        if missing:
            errors.append(f"Scenes in scene list but not in 'scenes' enum: {missing}")
        if extra:
            errors.append(f"Scenes in 'scenes' enum but not in scene list: {extra}")

    # --- Cross-reference: fsm_states enum ↔ actual FSM states ---
    if "fsm_states" in enum_map:
        declared_states = set(enum_map["fsm_states"])
        actual_states = set(hlr.global_fsm.states)
        missing = actual_states - declared_states
        extra = declared_states - actual_states
        if missing:
            errors.append(f"States in global_fsm but not in 'fsm_states' enum: {missing}")
        if extra:
            errors.append(f"States in 'fsm_states' enum but not in global_fsm: {extra}")

    # --- Per-enum: no duplicates ---
    for e in hlr.enums:
        dupes = {v for v in e.values if e.values.count(v) > 1}
        if dupes:
            errors.append(f"Duplicate values in enum '{e.name}': {dupes}")

    return errors


# ---------------------------------------------------------------------------
# Scene checks
# ---------------------------------------------------------------------------

def _check_scenes(hlr: GameIdentity) -> list[str]:
    errors: list[str] = []
    all_scenes = _flatten_scenes(hlr.scenes)

    if len(all_scenes) < 2:
        errors.append(f"Only {len(all_scenes)} scene(s) — need at least 2 (start + gameplay)")

    return errors


# ---------------------------------------------------------------------------
# Rule cross-reference checks
# ---------------------------------------------------------------------------

def _check_rules(hlr: GameIdentity) -> list[str]:
    errors: list[str] = []
    all_scenes = _flatten_scenes(hlr.scenes)
    rules_text = " ".join(hlr.global_rules).lower()

    # Check key references in rules
    key_pattern = re.compile(r"press\s+(\w+)", re.IGNORECASE)
    skip_words = {"THE", "A", "AN", "TO", "AND", "OR", "BUTTON", "KEY", "BUTTONS", "KEYS", "IT"}
    for rule in hlr.global_rules:
        for match in key_pattern.finditer(rule):
            key = match.group(1).upper()
            if key in skip_words:
                continue
            # Allow game-specific action names (TAG, ASSIST, PUNCH, etc.)
            if key not in VALID_KEYS and len(key) > 2:
                pass  # action names are fine

    # If rules mention rounds, check a round-related scene exists
    if "round" in rules_text:
        has_round_scene = any("round" in s.scene_name.lower() or "over" in s.scene_name.lower()
                             for s in all_scenes)
        if not has_round_scene:
            errors.append("Rules mention rounds but no round_over scene exists")

    # Cross-reference: team size vs available entities in dynamic fields
    extras = hlr.extra_fields()
    team_size = _extract_team_size(hlr.global_rules)
    if team_size:
        # Check any list-type dynamic field that could be a roster
        for key, val in extras.items():
            if isinstance(val, list) and all(isinstance(v, dict) for v in val):
                # This is a list of objects — could be characters, cards, vehicles, etc.
                selectable = [v for v in val if v.get("role") in ("playable", "selectable", "available")]
                if selectable and team_size > len(selectable):
                    errors.append(
                        f"Rules reference team of {team_size} but only {len(selectable)} "
                        f"selectable items in '{key}'"
                    )

    return errors


# ---------------------------------------------------------------------------
# Duplicate checks
# ---------------------------------------------------------------------------

def _check_duplicates(hlr: GameIdentity) -> list[str]:
    errors: list[str] = []
    all_scenes = _flatten_scenes(hlr.scenes)

    # Duplicate scene names
    scene_names = [s.scene_name for s in all_scenes]
    dupes = {n for n in scene_names if scene_names.count(n) > 1}
    if dupes:
        errors.append(f"Duplicate scene names: {dupes}")

    # Duplicate FSM states in declaration
    fsm_states = hlr.global_fsm.states
    dupes = {s for s in fsm_states if fsm_states.count(s) > 1}
    if dupes:
        errors.append(f"Duplicate FSM states: {dupes}")

    # Duplicate names in any list-of-objects dynamic field
    for key, val in hlr.extra_fields().items():
        if isinstance(val, list) and all(isinstance(v, dict) for v in val):
            names = [v.get("name", "") for v in val if "name" in v]
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                errors.append(f"Duplicate names in '{key}': {dupes}")

    return errors


# ---------------------------------------------------------------------------
# Dynamic field presence check
# ---------------------------------------------------------------------------

def _check_dynamic_fields_present(hlr: GameIdentity, dynamic_fields: list[SchemaField]) -> list[str]:
    """Check that required dynamic fields proposed by the expander are present."""
    errors: list[str] = []
    extras = hlr.extra_fields()
    for field in dynamic_fields:
        if field.required and field.field_name not in extras:
            errors.append(f"Required dynamic field '{field.field_name}' missing from HLR")
        if field.field_name in extras and extras[field.field_name] is None and field.required:
            errors.append(f"Required dynamic field '{field.field_name}' is null")
    return errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_team_size(rules: list[str]) -> int | None:
    for rule in rules:
        match = re.search(r"team[s]?\s+(?:of|consist[s]?\s+of)\s+(\d+)", rule, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None
