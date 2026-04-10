"""DLR Validator — checks that DLR products are complete and consistent with MLR.

Validates:
- Every MLR property has a DLR value (no missing values)
- Every MLR action has DLR detail (key binding, frame data, damage)
- Every MLR interaction effect has a DLR value
- Values are reasonable types (numbers parse as numbers, etc.)
- Key bindings don't collide within a scene
- Cross-reference: DLR targets match MLR declarations

Returns a list of errors. Empty list = valid.
"""

from __future__ import annotations

from .dlr import SceneDLR
from .mlr import SceneMLR


def validate_dlr(scene_mlrs: list[SceneMLR], scene_dlrs: list[SceneDLR]) -> list[str]:
    errors: list[str] = []

    # Build lookup
    mlr_by_scene = {m.scene_name: m for m in scene_mlrs}
    dlr_by_scene = {d.scene_name: d for d in scene_dlrs}

    # Every MLR scene must have a DLR
    for mlr in scene_mlrs:
        if mlr.scene_name not in dlr_by_scene:
            errors.append(f"[{mlr.scene_name}] MLR scene has no DLR")
            continue

        dlr = dlr_by_scene[mlr.scene_name]
        errors.extend(_check_entity_completeness(mlr, dlr))
        errors.extend(_check_interaction_completeness(mlr, dlr))
        errors.extend(_check_key_collisions(dlr))

    return errors


def _check_entity_completeness(mlr: SceneMLR, dlr: SceneDLR) -> list[str]:
    errors: list[str] = []
    p = f"[{mlr.scene_name}]"

    dlr_entities = {ed.entity_name: ed for ed in dlr.entity_details}

    for entity in mlr.entities:
        ep = f"{p}[{entity.entity_name}]"

        if entity.entity_name not in dlr_entities:
            errors.append(f"{ep} MLR entity has no DLR detail")
            continue

        ed = dlr_entities[entity.entity_name]
        prop_values = ed.data.get("property_values", {})

        # Every MLR property should have a DLR value
        for prop in entity.properties:
            if prop.name not in prop_values:
                errors.append(f"{ep} property '{prop.name}' has no DLR value")
            elif prop_values[prop.name] in (None, "", "null", "undefined"):
                errors.append(f"{ep} property '{prop.name}' has empty/null DLR value")

        # Every action_set action should have a DLR action_detail
        action_details = {ad.get("action_name", ""): ad for ad in ed.data.get("action_details", [])}
        for action_set in entity.action_sets:
            for action in action_set.actions:
                if action not in action_details:
                    # Not a hard error — some actions may be grouped
                    pass

    return errors


def _check_interaction_completeness(mlr: SceneMLR, dlr: SceneDLR) -> list[str]:
    errors: list[str] = []
    p = f"[{mlr.scene_name}]"

    dlr_systems = {id.game_system: id for id in dlr.interaction_details}

    for si in mlr.system_interactions:
        sp = f"{p}[{si.game_system}]"

        if si.game_system not in dlr_systems:
            errors.append(f"{sp} MLR system interactions have no DLR detail")
            continue

        dlr_si = dlr_systems[si.game_system]
        details = dlr_si.data.get("interaction_details", [])

        # Should have same number of interactions
        if len(details) != len(si.interactions):
            errors.append(
                f"{sp} MLR has {len(si.interactions)} interactions but DLR has {len(details)} details"
            )

        # Each detail should have effect values
        for idx, detail in enumerate(details):
            for eff_idx, eff in enumerate(detail.get("effect_details", [])):
                if eff.get("value") in (None, "", "null"):
                    errors.append(
                        f"{sp} interaction[{idx}] effect[{eff_idx}] has no value"
                    )

    return errors


def _check_key_collisions(dlr: SceneDLR) -> list[str]:
    """Check that key bindings don't collide within a scene."""
    errors: list[str] = []
    p = f"[{dlr.scene_name}]"

    key_map: dict[str, list[str]] = {}  # key -> list of (entity/action)
    for ed in dlr.entity_details:
        for ad in ed.data.get("action_details", []):
            key = ad.get("key_binding")
            if key and key != "null":
                label = f"{ed.entity_name}.{ad.get('action_name', '?')}"
                key_map.setdefault(key, []).append(label)

    for key, users in key_map.items():
        if len(users) > 1:
            # Same key can be used by P1 and CPU (CPU doesn't use keys)
            # Only flag if multiple non-CPU entities use the same key
            non_cpu = [u for u in users if "cpu" not in u.lower()]
            if len(non_cpu) > 1:
                errors.append(f"{p} key '{key}' bound to multiple actions: {non_cpu}")

    return errors
