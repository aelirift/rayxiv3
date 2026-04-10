"""Deterministic entity codegen — builds GDScript from DLR without LLM calls.

Handles simple/passive entities: HUD, background, UI.
Characters, game_objects, and anything with complex action_sets goes to LLM builder.

Decision logic:
  1. Check object_type — hud/background/ui are candidates
  2. Check action_sets — if any have frame_data or key_bindings, not deterministic
  3. If deterministic: template from godot_node_type + DLR property_values
  4. If not: skip (LLM builder handles it)

Usage:
    from rayxi.build.codegen import build_deterministic_entities

    results = build_deterministic_entities(scene_mlr, scene_dlr, output_dir)
"""

from __future__ import annotations

import logging
from pathlib import Path

from rayxi.spec.dlr import EntityDetail, SceneDLR
from rayxi.spec.mlr import SceneMLR
from rayxi.spec.models import EntitySpec, GODOT_NODE_TYPES
from rayxi.trace import get_trace

from .templates import STYLEBOX_HELPER, generate_entity_script

_log = logging.getLogger("rayxi.build.codegen")

# Object types eligible for deterministic codegen
_DETERMINISTIC_TYPES = {"hud", "background", "ui"}


def is_deterministic(entity: EntitySpec, entity_detail: EntityDetail | None = None) -> bool:
    """Check if an entity can be built deterministically (no LLM needed).

    Criteria:
      - object_type is hud, background, or ui
      - No action_sets with actual game actions (empty or display-only is OK)
      - DLR has no frame_data or key_bindings (if detail available)
    """
    if entity.object_type not in _DETERMINISTIC_TYPES:
        return False

    # If entity has non-trivial action_sets, it needs LLM
    for action_set in entity.action_sets:
        if action_set.actions:
            # Check if actions are display-only (blink, fade, show/hide)
            display_actions = {"blink", "fade", "show", "hide", "pulse", "flash",
                               "fade_in", "fade_out", "slide_in", "slide_out"}
            non_display = [a for a in action_set.actions
                           if a.lower() not in display_actions]
            if non_display:
                return False

    # Check DLR detail if available
    if entity_detail:
        action_details = entity_detail.data.get("action_details", [])
        for ad in action_details:
            # Has frame data → game object, not simple display
            fd = ad.get("frame_data", {})
            if fd and any(v is not None for v in fd.values()):
                return False
            # Has key bindings → interactive, needs LLM
            if ad.get("key_binding") is not None:
                return False

    return True


def _find_detail(entity: EntitySpec, scene_dlr: SceneDLR) -> EntityDetail | None:
    """Find the DLR detail for an entity."""
    for detail in scene_dlr.entity_details:
        if detail.entity_name == entity.entity_name and detail.scene_name == entity.scene_name:
            return detail
    return None


def build_entity_script(entity: EntitySpec, detail: EntityDetail) -> str:
    """Build GDScript for one deterministic entity."""
    node_type = entity.godot_node_type
    if not node_type or node_type not in GODOT_NODE_TYPES:
        # Fallback based on object_type
        fallback_map = {
            "hud": "Control",
            "background": "Sprite2D",
            "ui": "Control",
        }
        node_type = fallback_map.get(entity.object_type, "Node2D")
        _log.warning("Entity %s has no/invalid godot_node_type, falling back to %s",
                      entity.entity_name, node_type)

    property_values = detail.data.get("property_values", {})
    property_types = {p.name: p.type for p in entity.properties}

    script = generate_entity_script(
        godot_node_type=node_type,
        entity_name=entity.entity_name,
        property_values=property_values,
        property_types=property_types,
    )

    # Add stylebox helper if needed
    if any(name in property_values for name in ("bg_color",)):
        script += STYLEBOX_HELPER

    return script


def build_deterministic_entities(
    scene_mlr: SceneMLR,
    scene_dlr: SceneDLR,
    output_dir: Path,
) -> list[dict]:
    """Build all deterministic entities for one scene.

    Returns list of {entity_name, object_type, method, output_file, success, error}.
    """
    trace = get_trace()
    results: list[dict] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for entity in scene_mlr.entities:
        detail = _find_detail(entity, scene_dlr)

        if not is_deterministic(entity, detail):
            results.append({
                "entity_name": entity.entity_name,
                "object_type": entity.object_type,
                "method": "llm",
                "output_file": "",
                "success": False,
                "error": "not_deterministic — queued for LLM builder",
            })
            continue

        if detail is None:
            results.append({
                "entity_name": entity.entity_name,
                "object_type": entity.object_type,
                "method": "deterministic",
                "output_file": "",
                "success": False,
                "error": "no DLR detail found",
            })
            continue

        build_id = ""
        if trace:
            build_id = trace.build_start(entity.entity_name, "deterministic",
                                          scene=scene_mlr.scene_name)
        try:
            script = build_entity_script(entity, detail)
            out_file = output_dir / f"{entity.entity_name}.gd"
            out_file.write_text(script, encoding="utf-8")
            _log.info("Codegen: %s/%s → %s (%d bytes)",
                       scene_mlr.scene_name, entity.entity_name, out_file, len(script))
            if trace:
                trace.build_end(build_id, success=True, output_file=str(out_file))
            results.append({
                "entity_name": entity.entity_name,
                "object_type": entity.object_type,
                "method": "deterministic",
                "output_file": str(out_file),
                "success": True,
                "error": "",
            })
        except Exception as exc:
            _log.error("Codegen: %s/%s failed: %s",
                        scene_mlr.scene_name, entity.entity_name, exc)
            if trace:
                trace.build_end(build_id, success=False, error=str(exc)[:200])
            results.append({
                "entity_name": entity.entity_name,
                "object_type": entity.object_type,
                "method": "deterministic",
                "output_file": "",
                "success": False,
                "error": str(exc)[:200],
            })

    return results
