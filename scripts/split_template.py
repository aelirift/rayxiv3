"""Split the 3-level template into 3 separate files: HLT, MLT, DLT.

Input:  2d_fighter_3level.json
Output: 2d_fighter_hlt.json, 2d_fighter_mlt.json, 2d_fighter_dlt.json
"""

import json
from pathlib import Path

SRC = Path(__file__).parent.parent / "knowledge" / "mechanic_templates" / "2d_fighter_3level.json"
DST_DIR = Path(__file__).parent.parent / "knowledge" / "mechanic_templates"


def split():
    template = json.loads(SRC.read_text())
    meta = template["_meta"]

    # HLT — high_level template
    hlt = {
        "_meta": {**meta, "level": "HLT", "description": "High-Level Template — for HLR reference"},
        "roles": template["high_level"]["roles"],
        "systems": template["high_level"]["systems"],
    }

    # MLT — mid_level template
    mlt = {
        "_meta": {**meta, "level": "MLT", "description": "Mid-Level Template — for MLR reference"},
        "system_properties": template["mid_level"]["system_properties"],
        "normal_attack_template": template["mid_level"]["normal_attack_template"],
        "special_move_template": template["mid_level"]["special_move_template"],
        "fighter_animations_required": template["mid_level"]["fighter_animations_required"],
    }

    # DLT — detail_level template
    dlt = {
        "_meta": {**meta, "level": "DLT", "description": "Detail-Level Template — for DLR reference"},
        "default_values": template["detail_level"]["default_values"],
        "initial_values": template["detail_level"]["initial_values"],
        "formulas": template["detail_level"]["formulas"],
    }
    if "property_count_summary" in template["detail_level"]:
        dlt["property_count_summary"] = template["detail_level"]["property_count_summary"]

    hlt_path = DST_DIR / "2d_fighter_hlt.json"
    mlt_path = DST_DIR / "2d_fighter_mlt.json"
    dlt_path = DST_DIR / "2d_fighter_dlt.json"

    hlt_path.write_text(json.dumps(hlt, indent=2) + "\n")
    mlt_path.write_text(json.dumps(mlt, indent=2) + "\n")
    dlt_path.write_text(json.dumps(dlt, indent=2) + "\n")

    print(f"Split into 3 files:")
    print(f"  {hlt_path.name}: {hlt_path.stat().st_size} bytes")
    print(f"    - {len(hlt['roles'])} roles, {len(hlt['systems'])} systems")
    print(f"  {mlt_path.name}: {mlt_path.stat().st_size} bytes")
    prop_count = sum(
        len(role_props[cat])
        for sys in mlt["system_properties"].values()
        for role_props in sys.values()
        for cat in role_props
    )
    print(f"    - {prop_count} property declarations")
    print(f"  {dlt_path.name}: {dlt_path.stat().st_size} bytes")
    print(f"    - {len(dlt['default_values'])} defaults, {len(dlt['initial_values'])} initials, {len(dlt['formulas'])} formulas")


if __name__ == "__main__":
    split()
