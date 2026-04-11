"""build_game — generic post-spec build pipeline for any game.

Given a game_name whose spec artifacts already exist under `output/{game_name}/`
(hlr.json, impact_map_final.json, dlr_mechanic_constants.json), drive the
deterministic build stages to produce a Godot web export at
`games/{game_name}/export/`.

Stages:
  1. Bootstrap godot_dir from the genre template (copies engine scaffolding +
     the .tscn layout; scripts/systems/ is then scrubbed so codegen owns it)
  2. Patch project.godot (name, main_scene) and export_presets.cfg (output path)
  3. Load spec artifacts, backfill phases from HLT if missing
  4. codegen_runner.generate_all_systems — Python/typed/LLM per system
  5. scene_gen.emit_scene — overwrites scenes/{main_scene}.gd deterministically
  6. hud_gen.generate_custom_hud_widgets — LLM per mechanic_spec HUD entity
  7. mechanic_patcher.patch_mechanic_specs — character fighter-prop injection,
     combat damage hook
  8. godot --import + godot --export-release "Web"

Usage:
    python3 scripts/build_game.py sf2_rage 2d_fighter
    python3 scripts/build_game.py mk 2d_fighter --main-scene fighting
    python3 scripts/build_game.py my_game 2d_fighter \\
        --template-src output/sf2_test/godot --main-scene fighting

The only game-specific knowledge in this script is what the pipeline CANNOT
derive: the path to the genre template's godot/ directory and the main scene
name. Everything else (systems, pools, constants, wiring) comes from the
impact map and HLT.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _load_hlt(genre: str, repo_root: Path) -> dict:
    path = repo_root / "knowledge" / "mechanic_templates" / f"{genre}_hlt.json"
    if not path.exists():
        sys.exit(f"ERROR: missing HLT at {path}")
    return json.loads(path.read_text())


def _load_constants(constants_path: Path) -> dict:
    if not constants_path.exists():
        return {}
    raw = json.loads(constants_path.read_text())
    out: dict = {}
    for sys_name, bucket in raw.items():
        if not isinstance(bucket, dict):
            continue
        if "constants" in bucket:
            out[sys_name] = {c["name"]: c.get("value") for c in bucket["constants"]}
        else:
            out[sys_name] = {
                k: (v.get("value") if isinstance(v, dict) else v)
                for k, v in bucket.items()
            }
    return out


def _patch_project_godot(godot_dir: Path, game_name: str, main_scene: str) -> None:
    proj = godot_dir / "project.godot"
    if not proj.exists():
        return
    content = proj.read_text()
    content = re.sub(
        r'config/name="[^"]*"',
        f'config/name="{game_name}"',
        content,
    )
    content = re.sub(
        r'run/main_scene="[^"]*"',
        f'run/main_scene="res://scenes/{main_scene}.tscn"',
        content,
    )
    proj.write_text(content)


def _patch_export_presets(godot_dir: Path, game_name: str, repo_root: Path) -> None:
    """Redirect every preset's `export_path` to games/{game_name}/export/index.html.

    Templates typically ship with an export_path pointing to their own name;
    fix it inline so the web server (which mounts games/{name}/export) picks
    up the fresh build.
    """
    presets = godot_dir / "export_presets.cfg"
    if not presets.exists():
        return
    # godot_dir = output/{game}/godot; target = games/{game}/export/
    # Use ../../../ to walk from godot_dir back to repo root, then down.
    target = f"../../../games/{game_name}/export/index.html"
    content = presets.read_text()
    content = re.sub(r'export_path="[^"]*"', f'export_path="{target}"', content)
    presets.write_text(content)
    (repo_root / "games" / game_name / "export").mkdir(parents=True, exist_ok=True)


def _resolve_char_asset_dir(game_name: str, genre: str, char: str, repo_root: Path) -> Path | None:
    """Resolve the source directory for a character's sprites.

    Priority:
      1. games/{game}/assets/{char}/   — game-specific override (generated for this game)
      2. games/_lib/{genre}/{char}/    — shared across all games of this genre
    Returns None if neither exists — the game will have placeholder ColorRects
    until `scripts/gen_fighter_assets.py {game} {char}` is run.
    """
    per_game = repo_root / "games" / game_name / "assets" / char
    if per_game.is_dir() and any(per_game.iterdir()):
        return per_game
    shared = repo_root / "games" / "_lib" / genre / char
    if shared.is_dir() and any(shared.iterdir()):
        return shared
    return None


def _copy_character_assets(
    game_name: str,
    genre: str,
    godot_dir: Path,
    characters: list[str],
    repo_root: Path,
) -> dict[str, dict[str, str]]:
    """Copy each character's sprites into godot_dir/assets/{char}/.

    For each character, checks the per-game asset dir first, then the shared
    per-genre lib. Does NOT call MiniMax — sprite generation is explicit via
    `scripts/gen_fighter_assets.py`.

    Returns a dict of {char_name: {anim_label: res_path_string}}.
    """
    dst_root = godot_dir / "assets"
    atlas: dict[str, dict[str, str]] = {}
    for char in characters:
        src_dir = _resolve_char_asset_dir(game_name, genre, char, repo_root)
        if src_dir is None:
            continue
        dst_dir = dst_root / char
        dst_dir.mkdir(parents=True, exist_ok=True)
        char_atlas: dict[str, str] = {}
        for src_img in sorted(list(src_dir.glob("*.png")) + list(src_dir.glob("*.jpg"))):
            dst_img = dst_dir / src_img.name
            dst_img.write_bytes(src_img.read_bytes())
            # Derive label: strip leading "{char}_" prefix if present
            stem = src_img.stem
            label = stem[len(char) + 1 :] if stem.startswith(char + "_") else stem
            char_atlas[label] = f"res://assets/{char}/{src_img.name}"
        if char_atlas:
            atlas[char] = char_atlas
            print(f"    assets: {char} ← {src_dir.relative_to(repo_root)} ({len(char_atlas)} sprites)")
    return atlas


def _inject_sprites_into_tscn(
    godot_dir: Path,
    main_scene: str,
    characters: list[str],
    atlas: dict[str, dict[str, str]],
) -> None:
    """Swap the placeholder ColorRect inside each p<N>_fighter for a Sprite2D
    pointing at the character's idle texture (falling back to the first
    available sprite). Leaves the ColorRect alone for characters without assets.
    """
    tscn = godot_dir / "scenes" / f"{main_scene}.tscn"
    if not tscn.exists() or not atlas:
        return
    content = tscn.read_text()

    # Collect the set of textures we'll reference + assign ext_resource ids.
    existing_ids = [int(m.group(1)) for m in re.finditer(r'id="(\d+)"', content)]
    next_id = (max(existing_ids) if existing_ids else 0) + 1

    texture_blocks: list[str] = []
    tex_id_for_char: dict[str, int] = {}
    for char in characters:
        char_atlas = atlas.get(char)
        if not char_atlas:
            continue
        # Pick idle if available, else first
        tex_path = char_atlas.get("idle") or next(iter(char_atlas.values()))
        tex_id_for_char[char] = next_id
        texture_blocks.append(
            f'[ext_resource type="Texture2D" path="{tex_path}" id="{next_id}"]'
        )
        next_id += 1

    if not texture_blocks:
        return

    # Insert the texture ext_resources right after the last existing ext_resource line.
    last_ext_resource = list(re.finditer(r'\[ext_resource[^\]]*\]\n', content))
    if not last_ext_resource:
        return
    insert_at = last_ext_resource[-1].end()
    content = content[:insert_at] + "\n".join(texture_blocks) + "\n" + content[insert_at:]

    # Replace each p<N>_fighter's Sprite ColorRect child with a Sprite2D
    # referencing the char's texture. We use a regex that matches the whole
    # child ColorRect block.
    for i, char in enumerate(characters, start=1):
        tex_id = tex_id_for_char.get(char)
        if tex_id is None:
            continue
        # Find the Sprite under p<i>_fighter and rewrite its type + contents.
        pattern = (
            r'(\[node name="Sprite" type=")ColorRect(" parent="p' + str(i) +
            r'_fighter"\][^\[]*?)color = Color\([^)]*\)\n'
        )
        def _replace(m, tex_id=tex_id):
            head = m.group(1) + "Sprite2D" + m.group(2)
            # Strip offset_* and color lines — keep nothing from the ColorRect body
            head = re.sub(r'offset_\w+ = [^\n]*\n', '', head)
            # Sprite2D has position/scale properties; we'll set a reasonable default
            return (
                head +
                "position = Vector2(0, -150)\n"
                "scale = Vector2(0.4, 0.4)\n"
                f'texture = ExtResource("{tex_id}")\n'
            )
        content = re.sub(pattern, _replace, content, count=1, flags=re.DOTALL)

    # Bump load_steps since we added new ext_resources
    content = re.sub(
        r'(\[gd_scene load_steps=)(\d+)(\s+format=3\])',
        lambda m: m.group(1) + str(int(m.group(2)) + len(texture_blocks)) + m.group(3),
        content,
        count=1,
    )

    tscn.write_text(content)


def _patch_main_tscn_scripts(
    godot_dir: Path, main_scene: str, characters: list[str]
) -> None:
    """Rewrite the fighting.tscn so p1_fighter/p2_fighter point to the
    characters declared in HLR. If only one character, both fighters use it
    (mirror match). If 2+ characters, p<N>_fighter uses characters[N-1].

    This is the ONE place the pipeline converts from hand-authored template
    .tscn to a game-specific scene tree — all other game knowledge stays in
    the impact map or HLT.
    """
    tscn = godot_dir / "scenes" / f"{main_scene}.tscn"
    if not tscn.exists() or not characters:
        return
    content = tscn.read_text()

    # Collect existing ext_resource ids for characters
    # Replace any res://scripts/characters/*.gd with the first character's path
    # (single-script case), and if there are 2+ characters, inject a second
    # ExtResource and swap p2_fighter's script.
    char1 = characters[0]
    char2 = characters[1] if len(characters) >= 2 else char1

    # Make sure the tscn references the characters' .gd files.
    # Step A: rewrite the single existing ExtResource to point to char1.
    content = re.sub(
        r'path="res://scripts/characters/[^"]*\.gd"',
        f'path="res://scripts/characters/{char1}.gd"',
        content,
        count=1,
    )

    # Step B: if mirror match, done. If not, add a second ExtResource for char2
    # and point p2_fighter's script at it.
    if char1 != char2 and f'{char2}.gd' not in content:
        # Find the existing ExtResource line for char1 to get its format, then
        # append a new one with a higher id. Parse all existing ExtResource ids.
        existing_ids = [int(m.group(1)) for m in re.finditer(r'id="(\d+)"', content)]
        new_id = (max(existing_ids) if existing_ids else 0) + 1
        new_ext = (
            f'[ext_resource type="Script" '
            f'path="res://scripts/characters/{char2}.gd" id="{new_id}"]'
        )
        # Insert after the existing character ExtResource line
        pattern = f'[ext_resource type="Script" path="res://scripts/characters/{char1}.gd"'
        idx = content.find(pattern)
        if idx >= 0:
            line_end = content.find("\n", idx) + 1
            content = content[:line_end] + new_ext + "\n" + content[line_end:]
            # Now swap p2_fighter's script reference. Find the ExtResource id
            # the p1_fighter uses (from the char1 line).
            m = re.search(
                r'path="res://scripts/characters/' + re.escape(char1) + r'\.gd" id="(\d+)"',
                content,
            )
            if m:
                p1_id = m.group(1)
                # Locate the p2_fighter node block and replace its script id.
                # Capture the id digits in a dedicated group so we can swap.
                p2_block = re.search(
                    r'(\[node name="p2_fighter"[^\[]*?script = ExtResource\(")(' + p1_id + r')("\))',
                    content,
                    re.DOTALL,
                )
                if p2_block:
                    content = (
                        content[:p2_block.start(2)] + str(new_id)
                        + content[p2_block.end(2):]
                    )

    tscn.write_text(content)


async def build(
    game_name: str,
    genre: str,
    template_src: Path,
    main_scene: str,
    template_character: str,
    repo_root: Path,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
    sys.stdout.reconfigure(line_buffering=True)

    sys.path.insert(0, str(repo_root / "src"))
    from rayxi.spec.impact_map import ImpactMap
    from rayxi.spec.models import GameIdentity
    from rayxi.build.character_gen import emit_all_characters
    from rayxi.build.codegen_runner import generate_all_systems
    from rayxi.build.hud_gen import generate_custom_hud_widgets
    from rayxi.build.mechanic_patcher import patch_mechanic_specs
    from rayxi.build.scene_gen import emit_scene

    game_dir = repo_root / "output" / game_name
    godot_dir = game_dir / "godot"

    # --- Stage 0: load HLT ---
    hlt = _load_hlt(genre, repo_root)
    hlt_phases = {n: i.get("phase", "physics") for n, i in hlt.get("systems", {}).items()}
    hlt_roles = hlt.get("roles", {})
    hlt_property_enums = {
        k: v for k, v in hlt.get("property_enums", {}).items()
        if k and not k.startswith("_") and isinstance(v, list)
    }
    system_descriptions = {n: i.get("description", "") for n, i in hlt.get("systems", {}).items()}

    # --- Stage 1: Reset godot_dir from template ---
    print("=" * 70)
    print(f"Step 1: Reset {godot_dir} ← {template_src}")
    print("=" * 70)
    if not template_src.exists():
        sys.exit(f"ERROR: missing template_src at {template_src}")
    if godot_dir.exists():
        shutil.rmtree(godot_dir)
    shutil.copytree(template_src, godot_dir)

    systems_dir = godot_dir / "scripts" / "systems"
    for existing in sorted(systems_dir.glob("*.gd")):
        existing.unlink()
        uid = existing.with_suffix(".gd.uid")
        if uid.exists():
            uid.unlink()
    print(f"  Scrubbed {systems_dir.name}/*.gd so codegen owns them")

    _patch_project_godot(godot_dir, game_name, main_scene)
    _patch_export_presets(godot_dir, game_name, repo_root)
    print(f"  Patched project.godot → main_scene={main_scene}.tscn, name={game_name}")
    print(f"  Patched export_presets.cfg → games/{game_name}/export/")

    # --- Stage 2: Load spec artifacts ---
    print("\n" + "=" * 70)
    print("Step 2: Load spec artifacts")
    print("=" * 70)
    imap_path = game_dir / "impact_map_final.json"
    hlr_path = game_dir / "hlr.json"
    if not imap_path.exists() or not hlr_path.exists():
        sys.exit(f"ERROR: missing spec artifacts in {game_dir}. Run run_to_impact.py first.")
    imap = ImpactMap.model_validate_json(imap_path.read_text())
    hlr = GameIdentity.model_validate_json(hlr_path.read_text())
    if not imap.phases:
        for s in imap.systems:
            imap.phases[s] = hlt_phases.get(s, "physics")
        print(f"  Backfilled phases for {len(imap.systems)} systems from HLT")
    # Backfill enum_values from the HLT's property_enums block onto any node
    # that matches. Safe to re-run (overwrites with the authoritative set).
    enum_applied = 0
    for prop_id, values in hlt_property_enums.items():
        node = imap.nodes.get(prop_id)
        if node is not None:
            node.enum_values = list(values)
            enum_applied += 1
    if enum_applied:
        print(f"  Backfilled enum_values on {enum_applied} nodes from HLT")
    constants = _load_constants(game_dir / "dlr_mechanic_constants.json")
    print(f"  Impact map: {len(imap.nodes)} nodes, {len(imap.systems)} systems")
    print(f"  Constants: {len(constants)} system buckets")

    # Character files from HLR — generated deterministically from the impact
    # map. Wipe the template's hand-written .gd files first so we never layer
    # on top of stale declarations with conflicting types.
    characters = hlr.get_enum("characters") or [template_character]
    char_dir = godot_dir / "scripts" / "characters"
    if char_dir.exists():
        for stale in list(char_dir.glob("*.gd")) + list(char_dir.glob("*.gd.uid")):
            stale.unlink()
    written = emit_all_characters(imap, characters, char_dir)
    for char, path in written.items():
        print(f"  character_gen: {path.name} ({path.stat().st_size} bytes)")
    _patch_main_tscn_scripts(godot_dir, main_scene, characters)
    asset_atlas = _copy_character_assets(game_name, genre, godot_dir, characters, repo_root)
    if asset_atlas:
        _inject_sprites_into_tscn(godot_dir, main_scene, characters, asset_atlas)

    # --- Stage 3: codegen_runner ---
    print("\n" + "=" * 70)
    print("Step 3: codegen_runner — per-system dispatch")
    print("=" * 70)
    t = time.time()
    manifest = await generate_all_systems(
        imap=imap,
        hlr=hlr,
        output_dir=systems_dir,
        constants=constants,
        hlt_roles=hlt_roles,
        system_descriptions=system_descriptions,
        concurrency=4,
    )
    print(f"  [{time.time()-t:.1f}s] generated {len(manifest)} system files")
    strategy_counts: dict[str, int] = {}
    print(f"\n  {'system':<28} {'strategy':<18} {'bytes':<8}")
    print("  " + "-" * 64)
    for entry in manifest:
        print(f"  {entry['system']:<28} {entry['strategy']:<18} {entry['bytes']:<8}")
        strategy_counts[entry['strategy']] = strategy_counts.get(entry['strategy'], 0) + 1
    print(f"  Summary: {strategy_counts}")

    # --- Stage 4: scene_gen (deterministic) ---
    print("\n" + "=" * 70)
    print(f"Step 4: scene_gen — overwrite scenes/{main_scene}.gd")
    print("=" * 70)
    scene_path = emit_scene(
        imap=imap,
        hlr=hlr,
        constants=constants,
        godot_dir=godot_dir,
        scene_name=main_scene,
        hlt_roles=hlt_roles,
    )
    print(f"  wrote {scene_path.relative_to(godot_dir)} "
          f"({scene_path.stat().st_size} bytes)")

    # --- Stage 5: hud_gen (LLM) ---
    print("\n" + "=" * 70)
    print("Step 5: hud_gen — custom widgets from mechanic_specs")
    print("=" * 70)
    hud_files = await generate_custom_hud_widgets(
        hlr=hlr,
        output_scripts_dir=godot_dir / "scripts" / "hud",
        constants_path=game_dir / "dlr_mechanic_constants.json",
    )
    for f in hud_files:
        print(f"  wrote {f.relative_to(godot_dir)} ({f.stat().st_size} bytes)")

    # --- Stage 6: mechanic_patcher (character props + combat hook) ---
    print("\n" + "=" * 70)
    print("Step 6: mechanic_patcher — fighter props + combat hook")
    print("=" * 70)
    patches = patch_mechanic_specs(godot_dir, hlr, imap=imap)
    total_ops = sum(len(v) for v in patches.values())
    print(f"  {total_ops} patch operation(s) across {len(patches)} file(s)")
    for f, ops in patches.items():
        for op in ops:
            print(f"    ✓ {Path(f).name}: {op}")

    # --- Stage 7: save manifest for inspection ---
    (game_dir / "codegen_manifest.json").write_text(
        json.dumps({
            "game_name": game_name,
            "genre": genre,
            "systems": manifest,
            "hud_widgets": [{"file": str(f), "strategy": "llm"} for f in hud_files],
            "patches": patches,
            "strategy_counts": strategy_counts,
        }, indent=2)
    )

    # --- Stage 8: Godot import + export ---
    print("\n" + "=" * 70)
    print("Step 7: Godot import + export")
    print("=" * 70)
    dot_godot = godot_dir / ".godot"
    if dot_godot.exists():
        shutil.rmtree(dot_godot)
    r = subprocess.run(
        ["godot", "--headless", "--import"],
        cwd=str(godot_dir), capture_output=True, text=True, timeout=120,
    )
    err_lines = [l for l in (r.stderr or "").splitlines()
                 if "error" in l.lower() and "deprecated" not in l.lower()]
    if err_lines:
        print("  Import errors:")
        for l in err_lines[:20]:
            print(f"    {l}")
    else:
        print("  Import: clean")

    r = subprocess.run(
        ["godot", "--headless", "--export-release", "Web"],
        cwd=str(godot_dir), capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        print("  Export failed:")
        print(f"    {r.stderr[-500:]}")
        return 1
    print("  Export: OK")
    exported = repo_root / "games" / game_name / "export" / "index.html"
    if exported.exists():
        size = sum(f.stat().st_size for f in exported.parent.iterdir())
        print(f"  Served at: /godot/{game_name}/  ({size // 1024} KB)")

    print("\n" + "=" * 70)
    print(f"DONE — {game_name} built from {genre} template")
    print("=" * 70)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Build a Godot game from spec artifacts.")
    parser.add_argument("game_name", help="Game directory name under output/")
    parser.add_argument("genre", help="Genre template name (e.g. 2d_fighter)")
    parser.add_argument("--template-src", default="output/sf2_test/godot",
                        help="Godot template project directory to bootstrap from")
    parser.add_argument("--main-scene", default="fighting",
                        help="Main scene name (without .tscn extension)")
    parser.add_argument("--template-character", default="ryu",
                        help="Character .gd file in template used as base to clone for each HLR character")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    template_src = Path(args.template_src)
    if not template_src.is_absolute():
        template_src = repo_root / template_src

    return asyncio.run(build(
        game_name=args.game_name,
        genre=args.genre,
        template_src=template_src,
        main_scene=args.main_scene,
        template_character=args.template_character,
        repo_root=repo_root,
    ))


if __name__ == "__main__":
    sys.exit(main())
