"""Verify nothing was lost in the 3-level restructure.

Spot-checks several properties across the original and restructured templates.
"""

import json
from pathlib import Path

ORIG = Path(__file__).parent.parent / "knowledge" / "mechanic_templates" / "2d_fighter.json"
NEW = Path(__file__).parent.parent / "knowledge" / "mechanic_templates" / "2d_fighter_3level.json"


def find_in_orig(orig: dict, mech: str, role: str, cat: str, name: str) -> dict | None:
    """Find a property in the original template."""
    m = orig.get("mechanics", {}).get(mech, {})
    contributes = m.get(f"contributes_to_{role}", {})
    for p in contributes.get(cat, []):
        if p.get("name") == name:
            return p
    return None


def find_in_new(new: dict, mech: str, role: str, cat: str, name: str) -> tuple[dict | None, str | None, str | None, str | None]:
    """Find a property's declaration + values in the restructured template.
    Returns (declaration, default, initial, formula)."""
    sys_props = new.get("mid_level", {}).get("system_properties", {}).get(mech, {})
    role_props = sys_props.get(role, {})
    decl = None
    for p in role_props.get(cat, []):
        if p.get("name") == name:
            decl = p
            break

    key = f"{role}.{name}"
    default = new.get("detail_level", {}).get("default_values", {}).get(key)
    initial = new.get("detail_level", {}).get("initial_values", {}).get(key)
    formula = new.get("detail_level", {}).get("formulas", {}).get(key)
    return decl, default, initial, formula


def verify_prop(orig: dict, new: dict, mech: str, role: str, cat: str, name: str) -> bool:
    orig_p = find_in_orig(orig, mech, role, cat, name)
    if not orig_p:
        print(f"  SKIP {mech}.{role}.{cat}.{name} — not in original")
        return True

    decl, default, initial, formula = find_in_new(new, mech, role, cat, name)
    if not decl:
        print(f"  FAIL {mech}.{role}.{cat}.{name} — missing in mid_level")
        return False

    issues = []

    # Check declaration fields
    for field in ["type", "purpose", "written_by", "read_by", "write_verb"]:
        if field in orig_p:
            if decl.get(field) != orig_p[field]:
                issues.append(f"{field}: orig={orig_p[field]!r} new={decl.get(field)!r}")

    # Check value fields moved to detail_level
    if "default" in orig_p and str(orig_p["default"]) != str(default):
        issues.append(f"default: orig={orig_p['default']!r} new={default!r}")
    if "initial" in orig_p and str(orig_p["initial"]) != str(initial):
        issues.append(f"initial: orig={orig_p['initial']!r} new={initial!r}")
    if "formula" in orig_p and orig_p["formula"] != formula:
        issues.append(f"formula: orig={orig_p['formula']!r} new={formula!r}")

    if issues:
        print(f"  FAIL {mech}.{role}.{cat}.{name}:")
        for i in issues:
            print(f"    {i}")
        return False

    print(f"  OK   {mech}.{role}.{cat}.{name}")
    return True


def main():
    orig = json.loads(ORIG.read_text())
    new = json.loads(NEW.read_text())

    # Spot check various properties
    checks = [
        # (mechanic, role, category, name)
        ("health_system", "fighter", "config", "max_health"),
        ("health_system", "fighter", "config", "damage_reduction"),
        ("health_system", "fighter", "state", "current_health"),
        ("health_system", "fighter", "state", "is_ko"),
        ("health_system", "fighter", "derived", "health_percent"),
        ("health_system", "game", "config", "round_time_seconds"),
        ("health_system", "hud_bar", "state", "display_value"),
        ("combat_system", "fighter", "state", "hitstop_timer"),
        ("combat_system", "fighter", "state", "pushback_velocity"),
        ("combat_system", "game", "config", "hitstop_light"),
        ("movement_system", "fighter", "config", "walk_speed"),
        ("movement_system", "fighter", "state", "position"),
        ("movement_system", "fighter", "state", "velocity"),
        ("movement_system", "fighter", "state", "facing_direction"),
        ("movement_system", "game", "config", "gravity"),
        ("stun_system", "fighter", "state", "stun_meter"),
        ("stun_system", "fighter", "state", "is_stunned"),
        ("projectile_system", "projectile", "config", "damage"),
        ("projectile_system", "projectile", "state", "position"),
        ("projectile_system", "fighter", "state", "is_firing_projectile"),
        ("input_system", "fighter", "state", "current_action"),
        ("animation_system", "fighter", "config", "sprite_scale"),
        ("collision_system", "fighter", "config", "stand_hurtbox"),
        ("round_system", "game", "state", "current_round"),
        ("round_system", "game", "state", "round_state"),
        ("round_system", "game", "derived", "round_timer_seconds"),
        ("ai_system", "fighter", "config", "is_cpu"),
        ("charge_system", "fighter", "state", "charge_direction"),
        ("combo_system", "fighter", "state", "combo_count"),
    ]

    print("=== Property spot checks ===")
    passed = 0
    for check in checks:
        if verify_prop(orig, new, *check):
            passed += 1
    print(f"\n{passed}/{len(checks)} properties verified intact")
    print()

    # Also verify normal_attack_template structure
    print("=== Normal attack template ===")
    orig_atk = orig["mechanics"]["special_move_system"].get("normal_attack_template", {})
    new_atk = new["mid_level"]["normal_attack_template"].get("special_move_system", {})
    print(f"  Original attacks: {len(orig_atk.get('attacks', []))}")
    print(f"  New attacks:      {len(new_atk.get('attacks', []))}")
    print(f"  Original per_attack props: {len(orig_atk.get('per_attack', []))}")
    print(f"  New per_attack props:      {len(new_atk.get('per_attack', []))}")
    if orig_atk.get("attacks") == new_atk.get("attacks"):
        print(f"  Attack list: MATCH")
    else:
        print(f"  Attack list: MISMATCH")
    print()

    # Special move template
    print("=== Special move template ===")
    orig_sp = orig["mechanics"]["special_move_system"].get("per_special_move_template", {})
    new_sp = new["mid_level"]["special_move_template"].get("special_move_system", {})
    print(f"  Original config props: {len(orig_sp.get('config', []))}")
    print(f"  New config props:      {len(new_sp.get('config', []))}")
    print()

    # Animations
    print("=== Animations ===")
    orig_anims = orig["mechanics"]["animation_system"].get("fighter_animations_required", [])
    new_anims = new["mid_level"]["fighter_animations_required"]
    print(f"  Original: {len(orig_anims)}")
    print(f"  New:      {len(new_anims)}")
    if set(orig_anims) == set(new_anims):
        print(f"  MATCH")
    else:
        print(f"  MISMATCH: missing={set(orig_anims) - set(new_anims)}")


if __name__ == "__main__":
    main()
