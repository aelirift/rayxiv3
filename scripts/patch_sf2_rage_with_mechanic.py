"""Apply all auto-generated mechanic_spec content to the sf2_rage Godot project.

Reads output/sf2_rage/{hlr.json, impact_map_final.json, dlr_mechanic_constants.json}
and runs:
  1. mechanic_gen — emits rage_meter_system.gd from the typed impact map
  2. hud_gen (LLM) — emits p1/p2_rage_meter.gd Control widgets
  3. mechanic_patcher — injects fighter state vars, wires fighting.gd,
     patches combat_system.gd damage hook

Zero hand-written rage meter code after this runs.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
sys.stdout.reconfigure(line_buffering=True)


async def main():
    sys.path.insert(0, "src")
    from rayxi.spec.models import GameIdentity
    from rayxi.build.mechanic_gen import generate_custom_systems
    from rayxi.build.hud_gen import generate_custom_hud_widgets
    from rayxi.build.mechanic_patcher import patch_mechanic_specs

    game_dir = Path("output/sf2_rage")
    godot_dir = game_dir / "godot"
    impact_path = game_dir / "impact_map_final.json"
    constants_path = game_dir / "dlr_mechanic_constants.json"
    hlr_path = game_dir / "hlr.json"

    hlr = GameIdentity.model_validate_json(hlr_path.read_text())
    print(f"Loaded HLR: {hlr.game_name}  mechanic_specs: {len(hlr.mechanic_specs)}")

    # 1. mechanic_gen — emit rage_meter_system.gd
    print("\n=== 1. mechanic_gen (typed impact map → GDScript) ===")
    sys_files = generate_custom_systems(
        impact_map_path=impact_path,
        mechanic_constants_path=constants_path,
        output_scripts_dir=godot_dir / "scripts" / "systems",
    )
    for f in sys_files:
        print(f"  wrote: {f.relative_to(godot_dir)}  ({f.stat().st_size} bytes)")

    # 2. hud_gen — LLM generates each HUD widget from its mechanic_spec hud_entity
    print("\n=== 2. hud_gen (LLM per hud_entity) ===")
    hud_files = await generate_custom_hud_widgets(
        hlr=hlr,
        output_scripts_dir=godot_dir / "scripts" / "hud",
        constants_path=constants_path,
    )
    for f in hud_files:
        print(f"  wrote: {f.relative_to(godot_dir)}  ({f.stat().st_size} bytes)")

    # 3. mechanic_patcher — inject fighter state, wire scene, patch combat
    print("\n=== 3. mechanic_patcher (auto-wire existing files) ===")
    patches = patch_mechanic_specs(godot_dir, hlr)
    for file_path, ops in patches.items():
        print(f"  {Path(file_path).name}")
        for op in ops:
            print(f"    ✓ {op}")

    print("\n=== Summary ===")
    print(f"  Generated system files: {len(sys_files)}")
    print(f"  Generated HUD widget files: {len(hud_files)}")
    print(f"  Files patched: {len(patches)}")
    total_ops = sum(len(v) for v in patches.values())
    print(f"  Total patch operations: {total_ops}")


if __name__ == "__main__":
    asyncio.run(main())
