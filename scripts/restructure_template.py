"""Restructure 2d_fighter.json into 3 levels (high/mid/detail) without losing data.

Output: 2d_fighter_3level.json (new file alongside the original)
Input:  2d_fighter.json (untouched)

Layout:
  high_level   — HLR reference: roles, systems (name + description), scenes (none in current template), entity types
  mid_level    — MLR reference: per-system property declarations (names, types, written/read by, write_verb), action sets
  detail_level — DLR reference: default values, initial values, formulas
"""

import json
from pathlib import Path
from copy import deepcopy

SRC = Path(__file__).parent.parent / "knowledge" / "mechanic_templates" / "2d_fighter.json"
DST = Path(__file__).parent.parent / "knowledge" / "mechanic_templates" / "2d_fighter_3level.json"


def restructure():
    template = json.loads(SRC.read_text())

    out = {
        "_meta": deepcopy(template["_meta"]),
        "high_level": {
            "roles": {},
            "systems": {},
        },
        "mid_level": {
            "system_properties": {},
            "normal_attack_template": {},
            "special_move_template": {},
            "fighter_animations_required": [],
        },
        "detail_level": {
            "default_values": {},
            "initial_values": {},
            "formulas": {},
        },
    }

    # ---- HIGH LEVEL ----
    # Roles (with godot node + description)
    out["high_level"]["roles"] = deepcopy(template.get("roles", {}))

    # Systems (name + description only, no properties)
    for mech_name, mech in template.get("mechanics", {}).items():
        out["high_level"]["systems"][mech_name] = {
            "description": mech.get("description", ""),
            "scope": mech.get("scope", "role_generic"),
        }

    # ---- MID LEVEL ----
    # Per-system property declarations (structure only, no values)
    for mech_name, mech in template.get("mechanics", {}).items():
        sys_props = {}
        for role_key, role_data in mech.items():
            if not role_key.startswith("contributes_to_"):
                continue
            if not isinstance(role_data, dict):
                continue
            role_name = role_key.replace("contributes_to_", "")
            role_props = {"config": [], "state": [], "derived": []}
            for cat in ["config", "state", "derived"]:
                for p in role_data.get(cat, []):
                    if not isinstance(p, dict):
                        continue
                    # Strip values, keep declaration
                    decl = {
                        "name": p["name"],
                        "type": p.get("type", ""),
                        "purpose": p.get("purpose", ""),
                    }
                    if "written_by" in p:
                        decl["written_by"] = p["written_by"]
                    if "read_by" in p:
                        decl["read_by"] = p["read_by"]
                    if "write_verb" in p:
                        decl["write_verb"] = p["write_verb"]
                    role_props[cat].append(decl)
            # Only add role if it has properties
            if any(role_props[cat] for cat in role_props):
                sys_props[role_name] = role_props
        if sys_props:
            out["mid_level"]["system_properties"][mech_name] = sys_props

        # Normal attack template (structure)
        if "normal_attack_template" in mech:
            atk_tmpl = deepcopy(mech["normal_attack_template"])
            # Strip default values from per_attack — those go to detail_level
            for prop in atk_tmpl.get("per_attack", []):
                if "default" in prop:
                    del prop["default"]
            out["mid_level"]["normal_attack_template"][mech_name] = atk_tmpl

        # Special move template (structure)
        if "per_special_move_template" in mech:
            special_tmpl = deepcopy(mech["per_special_move_template"])
            for prop in special_tmpl.get("config", []):
                if "default" in prop:
                    del prop["default"]
            out["mid_level"]["special_move_template"][mech_name] = special_tmpl

        # Fighter animations
        if "fighter_animations_required" in mech:
            out["mid_level"]["fighter_animations_required"].extend(
                mech["fighter_animations_required"]
            )

    # ---- DETAIL LEVEL ----
    # Default values, initial values, formulas — extracted from each property
    for mech_name, mech in template.get("mechanics", {}).items():
        for role_key, role_data in mech.items():
            if not role_key.startswith("contributes_to_"):
                continue
            if not isinstance(role_data, dict):
                continue
            role_name = role_key.replace("contributes_to_", "")
            for cat in ["config", "state", "derived"]:
                for p in role_data.get(cat, []):
                    if not isinstance(p, dict):
                        continue
                    key = f"{role_name}.{p['name']}"
                    if "default" in p:
                        out["detail_level"]["default_values"][key] = p["default"]
                    if "initial" in p:
                        out["detail_level"]["initial_values"][key] = p["initial"]
                    if "formula" in p:
                        out["detail_level"]["formulas"][key] = p["formula"]

        # Default values for normal attack template
        if "normal_attack_template" in mech:
            for prop in mech["normal_attack_template"].get("per_attack", []):
                if "default" in prop:
                    # These are templated names like "{attack}_startup"
                    key = f"normal_attack.{prop['name']}"
                    out["detail_level"]["default_values"][key] = prop["default"]

        # Default values for special move template
        if "per_special_move_template" in mech:
            for prop in mech["per_special_move_template"].get("config", []):
                if "default" in prop:
                    key = f"special_move.{prop['name']}"
                    out["detail_level"]["default_values"][key] = prop["default"]

    # Property count summary preserved
    if "property_count_summary" in template:
        out["detail_level"]["property_count_summary"] = template["property_count_summary"]

    DST.write_text(json.dumps(out, indent=2) + "\n")

    # Stats
    sys_count = len(out["high_level"]["systems"])
    prop_count = sum(
        len(role_data[cat])
        for sys in out["mid_level"]["system_properties"].values()
        for role_data in sys.values()
        for cat in role_data
    )
    default_count = len(out["detail_level"]["default_values"])
    initial_count = len(out["detail_level"]["initial_values"])
    formula_count = len(out["detail_level"]["formulas"])

    print(f"Restructured: {DST.name}")
    print(f"  high_level: {len(out['high_level']['roles'])} roles, {sys_count} systems")
    print(f"  mid_level:  {prop_count} property declarations across {sys_count} systems")
    print(f"  detail_level: {default_count} defaults, {initial_count} initials, {formula_count} formulas")
    print(f"  Source: {SRC.stat().st_size} bytes → Output: {DST.stat().st_size} bytes")


if __name__ == "__main__":
    restructure()
