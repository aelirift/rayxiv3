"""build_game — generic post-spec build pipeline for any game.

Given a game_name whose spec artifacts already exist under `output/{game_name}/`
(hlr.json, impact_map_final.json, dlr_mechanic_constants.json), drive the
deterministic build stages to produce a Godot web export at
`games/{game_name}/export/`.

Stages:
  1. Bootstrap godot_dir from the genre template (copies engine scaffolding +
     the .tscn layout; scripts/systems/ and scripts/characters/ are scrubbed
     so codegen owns them)
  2. Patch project.godot (name, main_scene) and export_presets.cfg (output path)
  3. Load spec artifacts + compiled build_contract, backfill phases/enums from reqs if missing
  4. character_gen.emit_all_characters — deterministic {char}.gd files from imap
  5. codegen_runner.generate_all_systems — Python/typed/LLM per system
  6. scene_gen.emit_scene — overwrites scenes/{main_scene}.gd deterministically
  7. hud_gen.generate_custom_hud_widgets — LLM per mechanic_spec HUD entity
  8. godot --import + godot --export-release "Web"

Usage:
    python3 scripts/build_game.py my_game some_genre
    python3 scripts/build_game.py my_game some_genre --main-scene gameplay
    python3 scripts/build_game.py my_game some_genre \\
        --template-src path/to/bootstrap/godot --template-character protagonist

The only game-specific knowledge in this script is what the pipeline CANNOT
derive: the path to the genre template's godot/ directory and the main scene
name. Everything else (systems, pools, constants, wiring) must come from the
compiled req artifacts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import stat
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def _load_constants(constants_path: Path) -> dict:
    if not constants_path.exists():
        return {}
    raw = json.loads(constants_path.read_text(encoding="utf-8"))
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
    # Keep the preset path relative to the Godot project so exports stay
    # portable across machines and workspaces.
    target = f"../../../games/{game_name}/export/index.html"
    content = presets.read_text(encoding="utf-8")
    content = re.sub(r'export_path="[^"]*"', f'export_path="{target}"', content)
    presets.write_text(content, encoding="utf-8")
    (repo_root / "games" / game_name / "export").mkdir(parents=True, exist_ok=True)


def _clean_generated_scripts(directory: Path) -> None:
    if not directory.exists():
        return
    for stale in list(directory.glob("*.gd")) + list(directory.glob("*.gd.uid")):
        stale.unlink()


def _rmtree_with_attrs(path: Path) -> None:
    def _onerror(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        func(target)

    shutil.rmtree(path, onerror=_onerror)


def _resolve_godot_binary(requested: str | None = None) -> str | None:
    candidates = [
        requested,
        os.environ.get("GODOT_BIN"),
        "godot",
        "godot4",
        "godot.exe",
        "godot4.exe",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if Path(candidate).exists():
            return str(Path(candidate))
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _synthesize_project_godot(game_name: str, main_scene: str) -> str:
    return "\n".join(
        [
            "[application]",
            "",
            f'config/name="{game_name}"',
            f'run/main_scene="res://scenes/{main_scene}.tscn"',
            'config/features=PackedStringArray("4.4")',
            "",
            "[display]",
            "",
            "window/size/viewport_width=1920",
            "window/size/viewport_height=1080",
            'window/stretch/mode="viewport"',
            "",
            "[rendering]",
            "",
            'renderer/rendering_method="gl_compatibility"',
            "",
        ]
    )


def _synthesize_export_presets(game_name: str) -> str:
    return "\n".join(
        [
            "[preset.0]",
            "",
            'name="Web"',
            'platform="Web"',
            "runnable=true",
            "dedicated_server=false",
            'custom_features=""',
            'export_filter="all_resources"',
            'include_filter=""',
            'exclude_filter=""',
            f'export_path="../../../games/{game_name}/export/index.html"',
            'encryption_include_filters=""',
            'encryption_exclude_filters=""',
            "encrypt_pck=false",
            "encrypt_directory=false",
            "",
            "[preset.0.options]",
            "",
            'custom_template/debug=""',
            'custom_template/release=""',
            "variant/extensions_support=false",
            "variant/thread_support=false",
            "vram_texture_compression/for_desktop=true",
            "vram_texture_compression/for_mobile=false",
            "html/export_icon=true",
            'html/custom_html_shell=""',
            'html/head_include=""',
            "html/canvas_resize_policy=2",
            "html/focus_canvas_on_start=true",
            "html/experimental_virtual_keyboard=false",
            "progressive_web_app/enabled=false",
            "",
        ]
    )


def _scene_root_name(main_scene: str) -> str:
    return "".join(part.capitalize() for part in main_scene.split("_")) or "GameRoot"


def _pick_default_main_scene_name(scene_names: list[str]) -> str:
    priority = ("fight_main", "race_main", "gameplay", "fight", "racing", "battle", "combat", "race", "play")
    excluded_tokens = ("start", "title", "menu", "select", "intro", "over", "result", "credits")
    for token in priority:
        for scene_name in scene_names:
            lower_name = scene_name.lower()
            if any(excluded in lower_name for excluded in excluded_tokens):
                continue
            if lower_name == token or token in lower_name:
                return scene_name
    for scene_name in scene_names:
        lower_name = scene_name.lower()
        if any(excluded in lower_name for excluded in excluded_tokens):
            continue
        return scene_name
    return scene_names[0] if scene_names else "gameplay"


def _background_node_lines(role_defs: dict[str, dict]) -> list[str]:
    stage_meta = role_defs.get("stage") or {}
    stage_base_node = str(stage_meta.get("godot_base_node") or "ColorRect")
    lines = [f'[node name="Background" type="{stage_base_node}" parent="."]']
    if stage_base_node == "ColorRect":
        lines.extend(
            [
                "offset_right = 1920.0",
                "offset_bottom = 1080.0",
                "color = Color(0.12, 0.11, 0.09, 1)",
            ]
        )
    else:
        lines.extend(
            [
                "position = Vector2(0, 0)",
            ]
        )
    return lines


def _primary_actor_role(role_defs: dict[str, dict]) -> str:
    priority = ["fighter", "vehicle", "kart", "car", "racer", "driver", "bike", "ship", "player"]
    for role_name in priority:
        if role_name in role_defs:
            return role_name
    excluded = {"projectile", "hud_bar", "hud_text", "stage", "collectible", "item_box", "obstacle", "camera", "game_objects"}
    candidates = [
        role_name
        for role_name in sorted(role_defs.keys())
        if role_name not in excluded and not role_name.startswith("hud")
    ]
    return candidates[0] if candidates else "fighter"


def _actor_spawn_positions(actor_role: str) -> tuple[tuple[float, float], tuple[float, float]]:
    if actor_role == "fighter":
        return (520.0, 860.0), (1400.0, 860.0)
    return (760.0, 840.0), (1040.0, 840.0)


def _vector2_points(raw_points) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if not isinstance(raw_points, list):
        return points
    for entry in raw_points:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            try:
                points.append((float(entry[0]), float(entry[1])))
            except (TypeError, ValueError):
                continue
    return points


def _vehicle_spawn_seed(scene_defaults: dict | None, count: int = 2) -> list[dict[str, float]]:
    checkpoint_points = _vector2_points((scene_defaults or {}).get("checkpoint_positions"))
    if len(checkpoint_points) < 2:
        return []
    start_x, start_y = checkpoint_points[0]
    next_x, next_y = checkpoint_points[1]
    dx = next_x - start_x
    dy = next_y - start_y
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 1e-3:
        return []
    tangent_x = dx / length
    tangent_y = dy / length
    normal_x = -tangent_y
    normal_y = tangent_x
    heading = math.atan2(tangent_y, tangent_x)
    base_x = start_x - tangent_x * 140.0
    base_y = start_y - tangent_y * 140.0
    lane_spacing = 92.0
    row_spacing = 86.0
    middle = (count - 1) * 0.5
    seeded: list[dict[str, float]] = []
    for idx in range(count):
        lateral = (idx - middle) * lane_spacing
        longitudinal = float(idx // 2) * row_spacing
        seeded.append(
            {
                "x": base_x + normal_x * lateral - tangent_x * longitudinal,
                "y": base_y + normal_y * lateral - tangent_y * longitudinal,
                "facing_angle": heading,
            }
        )
    return seeded


def _actor_placeholder_lines(actor_role: str, parent_name: str) -> list[str]:
    if actor_role == "fighter":
        return [
            f'[node name="Sprite" type="ColorRect" parent="{parent_name}"]',
            "offset_left = -45.0",
            "offset_top = -180.0",
            "offset_right = 45.0",
            "offset_bottom = 0.0",
        ]
    return [
        f'[node name="Sprite" type="ColorRect" parent="{parent_name}"]',
        "offset_left = -60.0",
        "offset_top = -32.0",
        "offset_right = 60.0",
        "offset_bottom = 32.0",
    ]


def _synthesize_fighting_scene(
    main_scene: str,
    template_character: str,
    role_defs: dict[str, dict],
    actor_role: str,
    actor_base_node: str,
) -> str:
    ext_resources: list[str] = [
        f'[ext_resource type="Script" path="res://scenes/{main_scene}.gd" id="1"]',
        f'[ext_resource type="Script" path="res://scripts/characters/{template_character}.gd" id="2"]',
    ]
    next_id = 3
    role_script_ids: dict[str, str] = {}
    for role_name in sorted(role_defs.keys()):
        if role_name == actor_role:
            continue
        acq = (role_defs.get(role_name) or {}).get("scene_acquisition", {})
        if acq.get("method") not in {"nodes_in_scene", "named_node"}:
            continue
        role_script_ids[role_name] = str(next_id)
        ext_resources.append(
            f'[ext_resource type="Script" path="res://scripts/entities/{role_name}.gd" id="{next_id}"]'
        )
        next_id += 1

    p1_pos, p2_pos = _actor_spawn_positions(actor_role)
    p1_name = f"p1_{actor_role}"
    p2_name = f"p2_{actor_role}"
    lines: list[str] = [f"[gd_scene load_steps={len(ext_resources) + 1} format=3]", ""]
    lines.extend(ext_resources)
    lines.extend(
        [
            "",
            f'[node name="{_scene_root_name(main_scene)}" type="Node2D"]',
            'script = ExtResource("1")',
            "",
            *_background_node_lines(role_defs),
            "",
            '[node name="BackdropGlow" type="ColorRect" parent="."]',
            "offset_left = 240.0",
            "offset_top = 80.0",
            "offset_right = 1680.0",
            "offset_bottom = 740.0",
            "color = Color(0.2, 0.16, 0.08, 0.25)",
            "",
            f'[node name="{p1_name}" type="{actor_base_node}" parent="."]',
            'script = ExtResource("2")',
            f"position = Vector2({p1_pos[0]}, {p1_pos[1]})",
            "",
            *_actor_placeholder_lines(actor_role, p1_name),
            "color = Color(0.88, 0.88, 0.92, 1)",
            "",
            f'[node name="{p2_name}" type="{actor_base_node}" parent="."]',
            'script = ExtResource("2")',
            f"position = Vector2({p2_pos[0]}, {p2_pos[1]})",
            "",
            *_actor_placeholder_lines(actor_role, p2_name),
            "color = Color(0.78, 0.22, 0.18, 1)",
            "",
        ]
    )

    if "hud_bar" in role_script_ids:
        script_id = role_script_ids["hud_bar"]
        lines.extend(
            [
                '[node name="p1_health_bar" type="ProgressBar" parent="."]',
                f'script = ExtResource("{script_id}")',
                "offset_left = 120.0",
                "offset_top = 70.0",
                "offset_right = 820.0",
                "offset_bottom = 118.0",
                "value = 1.0",
                "max_value = 1.0",
                "",
                '[node name="p2_health_bar" type="ProgressBar" parent="."]',
                f'script = ExtResource("{script_id}")',
                "offset_left = 1100.0",
                "offset_top = 70.0",
                "offset_right = 1800.0",
                "offset_bottom = 118.0",
                "value = 1.0",
                "max_value = 1.0",
                "",
            ]
        )

    if "hud_text" in role_script_ids:
        script_id = role_script_ids["hud_text"]
        lines.extend(
            [
                '[node name="timer_display" type="Label" parent="."]',
                f'script = ExtResource("{script_id}")',
                "offset_left = 910.0",
                "offset_top = 48.0",
                "offset_right = 1010.0",
                "offset_bottom = 106.0",
                "text = \"99\"",
                "horizontal_alignment = 1",
                "",
            ]
        )

    return "\n".join(lines) + "\n"


def _synthesize_godot_project(
    godot_dir: Path,
    game_name: str,
    main_scene: str,
    template_character: str,
    role_defs: dict[str, dict],
    actor_role: str,
    actor_base_node: str,
) -> None:
    (godot_dir / "scenes").mkdir(parents=True, exist_ok=True)
    (godot_dir / "scripts" / "systems").mkdir(parents=True, exist_ok=True)
    (godot_dir / "scripts" / "characters").mkdir(parents=True, exist_ok=True)
    (godot_dir / "scripts" / "entities").mkdir(parents=True, exist_ok=True)
    (godot_dir / "scripts" / "hud").mkdir(parents=True, exist_ok=True)
    (godot_dir / "assets").mkdir(parents=True, exist_ok=True)
    (godot_dir / "project.godot").write_text(
        _synthesize_project_godot(game_name, main_scene), encoding="utf-8"
    )
    (godot_dir / "export_presets.cfg").write_text(
        _synthesize_export_presets(game_name), encoding="utf-8"
    )
    (godot_dir / "scenes" / f"{main_scene}.tscn").write_text(
        _synthesize_fighting_scene(main_scene, template_character, role_defs, actor_role, actor_base_node),
        encoding="utf-8",
    )


def _ensure_script_ext_resource(content: str, script_path: str) -> tuple[str, str]:
    existing = re.search(
        r'\[ext_resource type="Script" path="' + re.escape(script_path) + r'" id="(\d+)"\]',
        content,
    )
    if existing:
        return content, existing.group(1)

    existing_ids = [int(m.group(1)) for m in re.finditer(r'id="(\d+)"', content)]
    new_id = str((max(existing_ids) if existing_ids else 0) + 1)
    block = f'[ext_resource type="Script" path="{script_path}" id="{new_id}"]\n'
    ext_lines = list(re.finditer(r'\[ext_resource[^\]]*\]\n', content))
    if ext_lines:
        insert_at = ext_lines[-1].end()
        content = content[:insert_at] + block + content[insert_at:]
    else:
        header_end = content.find("\n")
        content = content[: header_end + 1] + "\n" + block + content[header_end + 1 :]
    content = re.sub(
        r'(\[gd_scene load_steps=)(\d+)(\s+format=3\])',
        lambda m: m.group(1) + str(int(m.group(2)) + 1) + m.group(3),
        content,
        count=1,
    )
    return content, new_id


def _patch_runtime_role_scripts(
    godot_dir: Path,
    main_scene: str,
    role_defs: dict[str, dict],
    *,
    actor_role: str | None = None,
) -> None:
    tscn = godot_dir / "scenes" / f"{main_scene}.tscn"
    if not tscn.exists():
        return
    content = tscn.read_text(encoding="utf-8")
    original = content

    def _compile_name_matcher(method: str, acq: dict) -> Callable[[str], bool] | None:
        if method == "named_node":
            target_name = acq.get("node_name")
            if not target_name:
                return None
            return lambda node_name, target_name=target_name: node_name == target_name
        if method == "nodes_in_scene":
            pattern = acq.get("pattern")
            if not pattern:
                return None
            regex = re.compile("^" + re.escape(pattern).replace("\\*", ".*") + "$")
            return lambda node_name, regex=regex: bool(regex.match(node_name))
        return None

    for role_name, role_meta in sorted(role_defs.items()):
        if role_name == "fighter" or role_name == actor_role:
            continue
        acq = (role_meta or {}).get("scene_acquisition", {})
        matcher = _compile_name_matcher(acq.get("method", "runtime_array"), acq)
        if matcher is None:
            continue
        content, ext_id = _ensure_script_ext_resource(
            content,
            f"res://scripts/entities/{role_name}.gd",
        )

        def _replace_node(match, matcher=matcher, ext_id=ext_id):
            header = match.group(1)
            node_name = match.group(2)
            if not matcher(node_name):
                return match.group(0)
            return header + f'script = ExtResource("{ext_id}")\n'

        content = re.sub(
            r'(\[node name="([^"]+)" type="[^"]+"(?: parent="[^"]+")?\]\n)(?:script = ExtResource\("[^"]+"\)\n)?',
            _replace_node,
            content,
        )

    if content != original:
        tscn.write_text(content, encoding="utf-8")


def _bootstrap_godot_dir(
    game_name: str,
    template_src: Path | None,
    godot_dir: Path,
    main_scene: str,
    template_character: str,
    role_defs: dict[str, dict],
    actor_role: str,
    actor_base_node: str,
    repo_root: Path,
) -> str:
    if godot_dir.exists():
        _rmtree_with_attrs(godot_dir)
    if template_src is not None and template_src.exists():
        shutil.copytree(template_src, godot_dir)
        mode = "template"
    else:
        _synthesize_godot_project(
            godot_dir,
            game_name,
            main_scene,
            template_character,
            role_defs,
            actor_role,
            actor_base_node,
        )
        mode = "synthesized"

    (godot_dir / "scripts" / "systems").mkdir(parents=True, exist_ok=True)
    (godot_dir / "scripts" / "characters").mkdir(parents=True, exist_ok=True)
    (godot_dir / "scripts" / "entities").mkdir(parents=True, exist_ok=True)
    (godot_dir / "scripts" / "hud").mkdir(parents=True, exist_ok=True)
    _clean_generated_scripts(godot_dir / "scripts" / "systems")
    _clean_generated_scripts(godot_dir / "scripts" / "characters")
    _clean_generated_scripts(godot_dir / "scripts" / "entities")
    _clean_generated_scripts(godot_dir / "scripts" / "hud")
    _patch_project_godot(godot_dir, game_name, main_scene)
    _patch_export_presets(godot_dir, game_name, repo_root)
    _patch_runtime_role_scripts(godot_dir, main_scene, role_defs, actor_role=actor_role)
    return mode


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


def _copy_visual_image(src_img: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
    except Exception:
        dst_img = dst_dir / src_img.name
        dst_img.write_bytes(src_img.read_bytes())
        return dst_img

    if src_img.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        dst_img = dst_dir / src_img.name
        dst_img.write_bytes(src_img.read_bytes())
        return dst_img

    def _largest_alpha_bbox(image):
        alpha = image.getchannel("A")
        width, height = image.size
        pixels = alpha.load()
        visited = bytearray(width * height)
        best_count = 0
        best_bbox: tuple[int, int, int, int] | None = None
        for y in range(height):
            for x in range(width):
                idx = y * width + x
                if visited[idx]:
                    continue
                visited[idx] = 1
                if pixels[x, y] <= 8:
                    continue
                stack = [(x, y)]
                count = 0
                min_x = max_x = x
                min_y = max_y = y
                while stack:
                    cx, cy = stack.pop()
                    count += 1
                    if cx < min_x:
                        min_x = cx
                    if cx > max_x:
                        max_x = cx
                    if cy < min_y:
                        min_y = cy
                    if cy > max_y:
                        max_y = cy
                    for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                        if nx < 0 or ny < 0 or nx >= width or ny >= height:
                            continue
                        nidx = ny * width + nx
                        if visited[nidx]:
                            continue
                        visited[nidx] = 1
                        if pixels[nx, ny] > 8:
                            stack.append((nx, ny))
                if count > best_count:
                    best_count = count
                    best_bbox = (min_x, min_y, max_x + 1, max_y + 1)
        if best_bbox is None or best_count < 48:
            return image.getbbox()
        return best_bbox

    converted = Image.open(src_img).convert("RGBA")
    pixels = list(converted.getdata())
    for idx, (r, g, b, a) in enumerate(pixels):
        if a > 0 and r >= 244 and g >= 244 and b >= 244:
            pixels[idx] = (r, g, b, 0)
    converted.putdata(pixels)
    width, height = converted.size
    rgba = converted.load()
    edge_stack: list[tuple[int, int]] = []
    visited = bytearray(width * height)

    def _is_light_background(px: tuple[int, int, int, int]) -> bool:
        r, g, b, a = px
        if a <= 0:
            return False
        high = min(r, g, b) >= 226
        low_variance = max(r, g, b) - min(r, g, b) <= 24
        return high and low_variance

    for x in range(width):
        edge_stack.append((x, 0))
        edge_stack.append((x, height - 1))
    for y in range(height):
        edge_stack.append((0, y))
        edge_stack.append((width - 1, y))
    while edge_stack:
        x, y = edge_stack.pop()
        if x < 0 or y < 0 or x >= width or y >= height:
            continue
        idx = y * width + x
        if visited[idx]:
            continue
        visited[idx] = 1
        pixel = rgba[x, y]
        if not _is_light_background(pixel):
            continue
        rgba[x, y] = (pixel[0], pixel[1], pixel[2], 0)
        edge_stack.extend(((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))
    bbox = _largest_alpha_bbox(converted)
    if bbox is not None:
        min_x, min_y, max_x, max_y = bbox
        padding = 6
        min_x = max(min_x - padding, 0)
        min_y = max(min_y - padding, 0)
        max_x = min(max_x + padding, converted.width)
        max_y = min(max_y + padding, converted.height)
        if max_x > min_x and max_y > min_y:
            converted = converted.crop((min_x, min_y, max_x, max_y))
    dst_img = dst_dir / f"{src_img.stem}.png"
    converted.save(dst_img)
    return dst_img


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
            dst_img = _copy_visual_image(src_img, dst_dir)
            # Derive label: strip leading "{char}_" prefix if present
            stem = dst_img.stem
            label = stem[len(char) + 1 :] if stem.startswith(char + "_") else stem
            char_atlas[label] = f"res://assets/{char}/{dst_img.name}"
        if char_atlas:
            atlas[char] = char_atlas
            print(f"    assets: {char} ← {src_dir.relative_to(repo_root)} ({len(char_atlas)} sprites)")
    return atlas


def _slot_visual_asset_dir(game_name: str, repo_root: Path, slot_name: str) -> Path | None:
    candidate = repo_root / "games" / game_name / "assets" / "slots" / slot_name
    if candidate.is_dir() and any(candidate.iterdir()):
        return candidate
    return None


def _copy_slot_visual_assets(
    game_name: str,
    godot_dir: Path,
    repo_root: Path,
    slot_names: list[str],
) -> dict[str, dict[str, str]]:
    dst_root = godot_dir / "assets"
    atlas: dict[str, dict[str, str]] = {}
    for slot_name in slot_names:
        src_dir = _slot_visual_asset_dir(game_name, repo_root, slot_name)
        if src_dir is None:
            continue
        prefix = f"slot_{slot_name}"
        dst_dir = dst_root / prefix
        dst_dir.mkdir(parents=True, exist_ok=True)
        slot_atlas: dict[str, str] = {}
        for src_img in sorted(list(src_dir.glob("*.png")) + list(src_dir.glob("*.jpg"))):
            stem = src_img.stem
            label = stem[len(prefix) + 1 :] if stem.startswith(prefix + "_") else stem
            if not label:
                continue
            staged_img = _copy_visual_image(src_img, dst_dir)
            dst_img = dst_dir / f"{label}.png"
            if staged_img != dst_img:
                if dst_img.exists():
                    dst_img.unlink()
                staged_img.rename(dst_img)
            slot_atlas[label] = f"res://assets/{prefix}/{dst_img.name}"
        for sidecar in sorted(src_dir.glob("*.json")):
            if sidecar.name.lower() in {"asset_overrides.json", "asset_prompt_manifest.json"}:
                continue
            shutil.copyfile(sidecar, dst_dir / sidecar.name)
        if slot_atlas:
            atlas[slot_name] = slot_atlas
            print(f"    slot assets: {slot_name} ← {src_dir.relative_to(repo_root)} ({len(slot_atlas)} sprites)")
    return atlas


def _resolve_common_asset_dir(game_name: str, genre: str, repo_root: Path) -> Path | None:
    per_game = repo_root / "games" / game_name / "assets" / "common"
    if per_game.is_dir() and any(per_game.iterdir()):
        return per_game
    shared = repo_root / "games" / "_lib" / genre / "common"
    if shared.is_dir() and any(shared.iterdir()):
        return shared
    return None


def _copy_common_assets(
    game_name: str,
    genre: str,
    godot_dir: Path,
    repo_root: Path,
) -> dict[str, str]:
    src_dir = _resolve_common_asset_dir(game_name, genre, repo_root)
    if src_dir is None:
        return {}
    dst_dir = godot_dir / "assets" / "common"
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    staged_files: list[Path] = []
    for src_img in sorted(list(src_dir.glob("*.png")) + list(src_dir.glob("*.jpg")) + list(src_dir.glob("*.jpeg"))):
        dst_img = _copy_visual_image(src_img, dst_dir)
        copied[src_img.name] = f"res://assets/common/{dst_img.name}"
        staged_files.append(dst_img)
    for sidecar in sorted(src_dir.glob("*.json")):
        if sidecar.name.lower() in {"asset_overrides.json", "asset_prompt_manifest.json"}:
            continue
        shutil.copyfile(sidecar, dst_dir / sidecar.name)

    alias_specs: dict[str, tuple[str, ...]] = {
        "track_backdrop.png": ("track_backdrop", "track_backdrop_", "backdrop_"),
        "track_overview_map.png": ("track_overview_map", "track_map", "track_overview_"),
        "road_surface_tile.png": ("road_surface_tile", "road_tile", "track_road_tile"),
        "road_shoulder_tile.png": ("road_shoulder_tile", "shoulder_tile", "track_shoulder_tile"),
        "grass_ground_tile.png": ("grass_ground_tile", "grass_tile", "ground_tile"),
        "barrier_segment.png": ("barrier_segment", "guardrail", "barrier_"),
        "direction_sign.png": ("direction_sign", "arrow_sign", "sign_"),
        "festival_banner.png": ("festival_banner", "banner_"),
        "tree_cluster.png": ("tree_cluster", "tree_", "scenery_tree"),
        "cloud_card.png": ("cloud_card", "cloud_"),
    }
    for alias_name, prefixes in alias_specs.items():
        alias_path = dst_dir / alias_name
        if alias_path.exists():
            copied[alias_name] = f"res://assets/common/{alias_name}"
            continue
        match: Path | None = None
        for staged in staged_files:
            stem = staged.stem.lower()
            if any(stem == prefix or stem.startswith(prefix) for prefix in prefixes):
                match = staged
                break
        if match is None:
            continue
        shutil.copyfile(match, alias_path)
        copied[alias_name] = f"res://assets/common/{alias_name}"
    if copied:
        print(f"    common assets: common <- {src_dir.relative_to(repo_root)} ({len(copied)} files)")
    return copied


def _slot_characters(characters: list[str]) -> list[str]:
    if not characters:
        return []
    if len(characters) == 1:
        return [characters[0], characters[0]]
    return characters[:2]


def _inject_sprites_into_tscn(
    godot_dir: Path,
    main_scene: str,
    characters: list[str],
    atlas: dict[str, dict[str, str]],
    actor_role: str,
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
    for i, char in enumerate(_slot_characters(characters), start=1):
        tex_id = tex_id_for_char.get(char)
        if tex_id is None:
            continue
        # Find the Sprite under p<i>_fighter and rewrite its type + contents.
        pattern = (
            r'(\[node name="Sprite" type=")ColorRect(" parent="p' + str(i) +
            r'_' + re.escape(actor_role) + r'"\][^\[]*?)color = Color\([^)]*\)\n'
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


def _inject_slot_override_sprites(
    godot_dir: Path,
    main_scene: str,
    actor_role: str,
    slot_atlas: dict[str, dict[str, str]],
) -> None:
    tscn = godot_dir / "scenes" / f"{main_scene}.tscn"
    if not tscn.exists() or not slot_atlas:
        return
    content = tscn.read_text(encoding="utf-8")
    existing_ids = [int(m.group(1)) for m in re.finditer(r'id="(\d+)"', content)]
    next_id = (max(existing_ids) if existing_ids else 0) + 1
    texture_blocks: list[str] = []
    tex_id_by_slot: dict[str, str] = {}
    for slot_name, atlas in sorted(slot_atlas.items()):
        tex_path = atlas.get("idle") or atlas.get("idle_0") or next(iter(atlas.values()))
        tex_id_by_slot[slot_name] = str(next_id)
        texture_blocks.append(f'[ext_resource type="Texture2D" path="{tex_path}" id="{next_id}"]')
        next_id += 1
    if texture_blocks:
        last_ext_resource = list(re.finditer(r'\[ext_resource[^\]]*\]\n', content))
        if last_ext_resource:
            insert_at = last_ext_resource[-1].end()
            content = content[:insert_at] + "\n".join(texture_blocks) + "\n" + content[insert_at:]
            content = re.sub(
                r'(\[gd_scene load_steps=)(\d+)(\s+format=3\])',
                lambda m: m.group(1) + str(int(m.group(2)) + len(texture_blocks)) + m.group(3),
                content,
                count=1,
            )

    for slot_name, ext_id in tex_id_by_slot.items():
        slot_index_match = re.match(r"p(\d+)$", slot_name)
        if slot_index_match is None:
            continue
        slot_index = int(slot_index_match.group(1))
        node_name = f"p{slot_index}_{actor_role}"
        content = _set_node_property(content, node_name, "visual_asset_prefix", f'"slot_{slot_name}"')
        pattern = (
            r'(\[node name="Sprite" type=")(?:ColorRect|Sprite2D)(" parent="' + re.escape(node_name) + r'"\][\s\S]*?)(?=\n\[node|\Z)'
        )

        def _replace(m, ext_id=ext_id):
            block = m.group(1) + "Sprite2D" + m.group(2)
            block = re.sub(r'^offset_\w+ = [^\n]*\n?', '', block, flags=re.MULTILINE)
            block = re.sub(r'^color = [^\n]*\n?', '', block, flags=re.MULTILINE)
            block = re.sub(r'^texture = ExtResource\("[^"]+"\)\n?', '', block, flags=re.MULTILINE)
            block = re.sub(r'^scale = [^\n]*\n?', '', block, flags=re.MULTILINE)
            block = re.sub(r'^position = [^\n]*\n?', '', block, flags=re.MULTILINE)
            if not block.endswith("\n"):
                block += "\n"
            return (
                block
                + "position = Vector2(0, -150)\n"
                + "scale = Vector2(0.4, 0.4)\n"
                + f'texture = ExtResource("{ext_id}")\n'
            )

        content = re.sub(pattern, _replace, content, count=1, flags=re.DOTALL)

    tscn.write_text(content, encoding="utf-8")


def _set_node_property(content: str, node_name: str, prop_name: str, prop_value: str) -> str:
    pattern = r'(\[node name="' + re.escape(node_name) + r'"[^\[]*?)(\n\[node|\Z)'
    match = re.search(pattern, content, flags=re.DOTALL)
    if not match:
        return content
    block = match.group(1)
    block = re.sub(rf'^{re.escape(prop_name)} = [^\n]*\n?', '', block, flags=re.MULTILINE)
    if not block.endswith("\n"):
        block += "\n"
    block += f"{prop_name} = {prop_value}\n"
    return content[:match.start(1)] + block + match.group(2) + content[match.end(2):]


def _script_ext_resource_id(content: str, script_path: str) -> str | None:
    match = re.search(
        r'path="' + re.escape(script_path) + r'" id="(\d+)"',
        content,
    )
    return match.group(1) if match else None


def _set_node_script_ext_resource(content: str, node_name: str, ext_id: str) -> str:
    pattern = r'(\[node name="' + re.escape(node_name) + r'"[^\[]*?)(\n\[node|\Z)'
    match = re.search(pattern, content, flags=re.DOTALL)
    if not match:
        return content
    block = match.group(1)
    block = re.sub(r'^script = ExtResource\("[^"]+"\)\n?', '', block, flags=re.MULTILINE)
    if not block.endswith("\n"):
        block += "\n"
    lines = block.splitlines()
    if lines:
        header = lines[0]
        rest = lines[1:]
        block = "\n".join([header, f'script = ExtResource("{ext_id}")', *rest]).rstrip("\n") + "\n"
    else:
        block = f'script = ExtResource("{ext_id}")\n'
    return content[:match.start(1)] + block + match.group(2) + content[match.end(2):]


def _patch_main_tscn_scripts(
    godot_dir: Path,
    main_scene: str,
    characters: list[str],
    actor_role: str,
    floor_y: float | None = None,
    has_ai_controlled_flag: bool = False,
    scene_defaults: dict | None = None,
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
    char1_id = _script_ext_resource_id(content, f"res://scripts/characters/{char1}.gd")

    # Step B: if mirror match, done. If not, add a second ExtResource for char2
    # and point p2_fighter's script at it.
    char2_id = char1_id
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
            char2_id = str(new_id)
    elif char1 != char2:
        char2_id = _script_ext_resource_id(content, f"res://scripts/characters/{char2}.gd")

    p1_name = f"p1_{actor_role}"
    p2_name = f"p2_{actor_role}"
    if char1_id is not None:
        content = _set_node_script_ext_resource(content, p1_name, char1_id)
    if char2_id is not None:
        content = _set_node_script_ext_resource(content, p2_name, char2_id)
    content = _set_node_property(content, p1_name, "player_slot", "0")
    content = _set_node_property(content, p1_name, "is_cpu", "false")
    content = _set_node_property(content, p2_name, "player_slot", "1")
    content = _set_node_property(content, p2_name, "is_cpu", "true")
    if has_ai_controlled_flag:
        content = _set_node_property(content, p1_name, "is_ai_controlled", "false")
        content = _set_node_property(content, p2_name, "is_ai_controlled", "true")
    vehicle_seed = [] if actor_role == "fighter" else _vehicle_spawn_seed(scene_defaults, 2)
    if vehicle_seed:
        p1_seed, p2_seed = vehicle_seed[:2]
        content = _set_node_property(content, p1_name, "position", f"Vector2({p1_seed['x']}, {p1_seed['y']})")
        content = _set_node_property(content, p2_name, "position", f"Vector2({p2_seed['x']}, {p2_seed['y']})")
        content = _set_node_property(content, p1_name, "facing_angle", repr(float(p1_seed["facing_angle"])))
        content = _set_node_property(content, p2_name, "facing_angle", repr(float(p2_seed["facing_angle"])))
    elif floor_y is not None and float(floor_y) > 0.0:
        floor_y_literal = repr(float(floor_y))
        p1_pos, p2_pos = _actor_spawn_positions(actor_role)
        content = _set_node_property(content, p1_name, "position", f"Vector2({p1_pos[0]}, {floor_y_literal})")
        content = _set_node_property(content, p2_name, "position", f"Vector2({p2_pos[0]}, {floor_y_literal})")

    tscn.write_text(content)


def _literal_initial_value(imap, property_id: str):
    from rayxi.spec.expr import LiteralExpr

    node = imap.nodes.get(property_id)
    if node is None or node.initial_value is None:
        return None
    if isinstance(node.initial_value, LiteralExpr):
        return node.initial_value.value
    return None


def _rewrite_expr_refs(expr, alias_map: dict[str, str]):
    if expr is None or not alias_map:
        return expr
    from rayxi.spec.expr import parse_expr

    data = expr.model_dump(mode="json")

    def _walk(value):
        if isinstance(value, dict):
            if value.get("kind") == "ref":
                path = value.get("path")
                if isinstance(path, str) and path in alias_map:
                    value["path"] = alias_map[path]
            for child in value.values():
                _walk(child)
        elif isinstance(value, list):
            for child in value:
                _walk(child)

    _walk(data)
    return parse_expr(data)


def _dedupe_model_list(items: list) -> list:
    seen: set[str] = set()
    out: list = []
    for item in items:
        key = json.dumps(item.model_dump(mode="json"), sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _apply_property_aliases(imap, alias_map: dict[str, str], bare_aliases: dict[str, str]) -> None:
    if not alias_map and not bare_aliases:
        return

    for node in imap.nodes.values():
        node.initial_value = _rewrite_expr_refs(node.initial_value, alias_map)
        node.derivation = _rewrite_expr_refs(node.derivation, alias_map)
        if node.owner == "hud_bar" and node.name in {"target_property", "max_property"}:
            literal = getattr(node.initial_value, "value", None)
            if isinstance(literal, str) and literal in bare_aliases:
                node.initial_value.value = bare_aliases[literal]

    for edge in imap.write_edges:
        if edge.target in alias_map:
            edge.target = alias_map[edge.target]
        edge.condition = _rewrite_expr_refs(edge.condition, alias_map)
        edge.formula = _rewrite_expr_refs(edge.formula, alias_map)

    for edge in imap.read_edges:
        if edge.source in alias_map:
            edge.source = alias_map[edge.source]

    for alias_id, canonical_id in alias_map.items():
        alias_node = imap.nodes.get(alias_id)
        canonical_node = imap.nodes.get(canonical_id)
        if alias_node is None or canonical_node is None:
            continue
        if canonical_node.initial_value is None and alias_node.initial_value is not None:
            canonical_node.initial_value = alias_node.initial_value
        if canonical_node.derivation is None and alias_node.derivation is not None:
            canonical_node.derivation = alias_node.derivation
        if not canonical_node.enum_values and alias_node.enum_values:
            canonical_node.enum_values = list(alias_node.enum_values)
        imap.nodes.pop(alias_id, None)
        imap.audit.append(f"build_contract alias normalized: {alias_id} -> {canonical_id}")

    imap.write_edges = _dedupe_model_list(imap.write_edges)
    imap.read_edges = _dedupe_model_list(imap.read_edges)


async def build(
    game_name: str,
    genre: str,
    template_src: Path | None,
    main_scene: str | None,
    template_character: str | None,
    repo_root: Path,
    godot_bin: str | None = None,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
    sys.stdout.reconfigure(line_buffering=True)

    sys.path.insert(0, str(repo_root / "src"))
    from rayxi.spec.impact_map import ImpactMap
    from rayxi.spec.build_contract import BuildContract, compile_build_contract
    from rayxi.spec.mechanic_coverage import (
        audit_build_coverage,
        build_mechanic_manifest,
        load_mechanic_manifest,
        write_mechanic_artifact,
    )
    from rayxi.build.asset_manifest import build_asset_prompt_manifest, validate_asset_workspace
    from rayxi.spec.models import GameIdentity
    from rayxi.build.character_gen import emit_all_characters, emit_runtime_role_scripts
    from rayxi.build.codegen_runner import generate_all_systems
    from rayxi.build.debug_gen import write_debug_scripts
    from rayxi.build.hud_gen import generate_custom_hud_widgets, write_builtin_hud_scripts
    from rayxi.build.scene_gen import emit_scene

    game_dir = repo_root / "output" / game_name
    godot_dir = game_dir / "godot"

    # --- Stage 0: load compiled build contract ---
    contract_path = game_dir / "build_contract.json"
    if not contract_path.exists():
        sys.exit(
            f"ERROR: missing compiled build contract at {contract_path}. "
            "Re-run the req pipeline to regenerate canonical build metadata."
        )
    contract = BuildContract.model_validate_json(contract_path.read_text(encoding="utf-8"))
    role_defs = {name: role.model_dump(exclude_none=True) for name, role in contract.roles.items()}
    primary_actor_role = _primary_actor_role(role_defs)
    primary_actor_meta = role_defs.get(primary_actor_role, {})
    primary_actor_base_node = primary_actor_meta.get("godot_base_node") or "CharacterBody2D"
    contract_property_enums = {
        k: v for k, v in contract.property_enums.items()
        if k and not k.startswith("_") and isinstance(v, list)
    }
    system_descriptions = dict(contract.system_descriptions)
    resolved_godot_bin = _resolve_godot_binary(godot_bin)
    if resolved_godot_bin:
        os.environ["GODOT_BIN"] = resolved_godot_bin
    imap_path = game_dir / "impact_map_final.json"
    hlr_path = game_dir / "hlr.json"
    if not imap_path.exists() or not hlr_path.exists():
        sys.exit(f"ERROR: missing spec artifacts in {game_dir}. Run run_to_impact.py first.")
    imap = ImpactMap.model_validate_json(imap_path.read_text(encoding="utf-8"))
    hlr = GameIdentity.model_validate_json(hlr_path.read_text(encoding="utf-8"))
    trace_path = game_dir / "trace.json"
    prompt_text = hlr.game_name
    if trace_path.exists():
        try:
            prompt_text = str(json.loads(trace_path.read_text(encoding="utf-8")).get("user_prompt") or prompt_text)
        except Exception:
            prompt_text = hlr.game_name
    coverage_manifest = load_mechanic_manifest(game_dir / "mechanic_manifest.json")
    if coverage_manifest is None:
        coverage_manifest = await build_mechanic_manifest(prompt_text, hlr)
        write_mechanic_artifact(game_dir / "mechanic_manifest.json", coverage_manifest)
    template_json_path = repo_root / "knowledge" / "mechanic_templates" / f"{hlr.genre}.json"
    hlt_json_path = repo_root / "knowledge" / "mechanic_templates" / f"{hlr.genre}_hlt.json"
    contract = compile_build_contract(
        hlr,
        imap,
        template_path=template_json_path if template_json_path.exists() else None,
        hlt_path=hlt_json_path if hlt_json_path.exists() else None,
        manifest=coverage_manifest,
    )
    contract_path.write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    asset_prompt_manifest = build_asset_prompt_manifest(prompt_text, hlr, contract, coverage_manifest, repo_root)
    (game_dir / "asset_prompt_manifest.json").write_text(
        asset_prompt_manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    game_asset_dir = repo_root / "games" / game_name / "assets"
    game_asset_dir.mkdir(parents=True, exist_ok=True)
    (game_asset_dir / "asset_prompt_manifest.json").write_text(
        asset_prompt_manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    asset_validation = validate_asset_workspace(asset_prompt_manifest, repo_root)
    (game_dir / "asset_validation.json").write_text(
        asset_validation.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (game_asset_dir / "asset_validation.json").write_text(
        asset_validation.model_dump_json(indent=2),
        encoding="utf-8",
    )
    print(f"  Asset manifest: {len(asset_prompt_manifest.entries)} request(s)")
    print(
        "  Asset readiness: "
        f"{asset_validation.summary.get('ready', 0)} ready / "
        f"{asset_validation.summary.get('partial', 0)} partial / "
        f"{asset_validation.summary.get('missing', 0)} missing"
    )
    if asset_validation.blockers:
        print("  Asset blockers:")
        for blocker in asset_validation.blockers[:8]:
            print(f"    - {blocker}")
    role_defs = {name: role.model_dump(exclude_none=True) for name, role in contract.roles.items()}
    primary_actor_role = _primary_actor_role(role_defs)
    primary_actor_meta = role_defs.get(primary_actor_role, {})
    primary_actor_base_node = primary_actor_meta.get("godot_base_node") or "CharacterBody2D"
    contract_property_enums = {
        k: v for k, v in contract.property_enums.items()
        if k and not k.startswith("_") and isinstance(v, list)
    }
    system_descriptions = dict(contract.system_descriptions)
    resolved_main_scene = main_scene or _pick_default_main_scene_name(
        list(contract.scenes) or [scene.scene_name for scene in hlr.scenes]
    )
    resolved_scene_defaults = dict(contract.scene_defaults.get(resolved_main_scene, {}))
    _apply_property_aliases(
        imap,
        dict(contract.property_aliases),
        dict(contract.property_name_aliases),
    )
    if not imap.phases:
        for system_name in imap.systems:
            imap.phases[system_name] = contract.phases.get(system_name, "physics")
    constants = _load_constants(game_dir / "dlr_mechanic_constants.json")
    declared_characters = hlr.get_enum("characters") or []
    resolved_template_character = template_character or (declared_characters[0] if declared_characters else "player_character")
    characters = declared_characters or [resolved_template_character]

    # --- Stage 1: Bootstrap godot_dir from template or compiled req contract ---
    print("=" * 70)
    print(f"Step 1: Reset {godot_dir} ← {template_src}")
    print("=" * 70)
    bootstrap_mode = _bootstrap_godot_dir(
        game_name=game_name,
        template_src=template_src,
        godot_dir=godot_dir,
        main_scene=resolved_main_scene,
        template_character=resolved_template_character,
        role_defs=role_defs,
        actor_role=primary_actor_role,
        actor_base_node=primary_actor_base_node,
        repo_root=repo_root,
    )
    systems_dir = godot_dir / "scripts" / "systems"
    print(f"  Bootstrap mode: {bootstrap_mode}")
    if bootstrap_mode == "template":
        print(f"  Source scaffold: {template_src}")
    else:
        print("  Source scaffold: synthesized from compiled req contract")
    print(f"  Scrubbed generated scripts under {godot_dir / 'scripts'}")

    print(f"  Patched project.godot -> main_scene={resolved_main_scene}.tscn, name={game_name}")
    print(f"  Patched export_presets.cfg → games/{game_name}/export/")

    # --- Stage 2: Load spec artifacts ---
    print("\n" + "=" * 70)
    print("Step 2: Load spec artifacts")
    print("=" * 70)
    print(f"  Phases available for {len(imap.systems)} systems")
    # Backfill enum_values from the compiled req contract onto any node
    # that matches. Safe to re-run (overwrites with the authoritative set).
    enum_applied = 0
    for prop_id, values in contract_property_enums.items():
        node = imap.nodes.get(prop_id)
        if node is not None:
            node.enum_values = list(values)
            enum_applied += 1
    if enum_applied:
        print(f"  Backfilled enum_values on {enum_applied} nodes from build_contract")
    print(f"  Impact map: {len(imap.nodes)} nodes, {len(imap.systems)} systems")
    print(f"  Constants: {len(constants)} system buckets")

    # Character files from HLR — generated deterministically from the impact
    # map. Wipe the template's hand-written .gd files first so we never layer
    # on top of stale declarations with conflicting types.
    char_dir = godot_dir / "scripts" / "characters"
    written = emit_all_characters(
        imap,
        characters,
        char_dir,
        role=primary_actor_role,
        godot_base_node=primary_actor_base_node,
        constants=constants,
        role_context=dict(contract.role_groups),
    )
    for char, path in written.items():
        print(f"  character_gen: {path.name} ({path.stat().st_size} bytes)")
    entity_dir = godot_dir / "scripts" / "entities"
    role_written = emit_runtime_role_scripts(
        imap,
        role_defs,
        entity_dir,
        constants=constants,
        role_context=dict(contract.role_groups),
    )
    for role_name, path in role_written.items():
        print(f"  runtime_role_gen: {role_name} -> {path.name} ({path.stat().st_size} bytes)")
    floor_y = _literal_initial_value(imap, "game.floor_y")
    _patch_main_tscn_scripts(
        godot_dir,
        resolved_main_scene,
        characters,
        primary_actor_role,
        floor_y=floor_y,
        has_ai_controlled_flag=f"{primary_actor_role}.is_ai_controlled" in imap.nodes,
        scene_defaults=resolved_scene_defaults,
    )
    _patch_runtime_role_scripts(godot_dir, resolved_main_scene, role_defs, actor_role=primary_actor_role)
    asset_atlas = _copy_character_assets(game_name, genre, godot_dir, characters, repo_root)
    if asset_atlas:
        _inject_sprites_into_tscn(godot_dir, resolved_main_scene, characters, asset_atlas, primary_actor_role)
    slot_atlas = _copy_slot_visual_assets(game_name, godot_dir, repo_root, ["p1", "p2"])
    if slot_atlas:
        _inject_slot_override_sprites(godot_dir, resolved_main_scene, primary_actor_role, slot_atlas)
    _copy_common_assets(game_name, genre, godot_dir, repo_root)

    # --- Stage 3: codegen_runner ---
    print("\n" + "=" * 70)
    print("Step 3: codegen_runner — per-system dispatch")
    print("=" * 70)
    t = time.time()
    system_manifest = await generate_all_systems(
        imap=imap,
        hlr=hlr,
        output_dir=systems_dir,
        constants=constants,
        role_defs=role_defs,
        role_groups=dict(contract.role_groups),
        capabilities=dict(contract.capabilities),
        system_descriptions=system_descriptions,
        concurrency=4,
    )
    print(f"  [{time.time()-t:.1f}s] generated {len(system_manifest)} system files")
    strategy_counts: dict[str, int] = {}
    print(f"\n  {'system':<28} {'strategy':<18} {'bytes':<8}")
    print("  " + "-" * 64)
    for entry in system_manifest:
        print(f"  {entry['system']:<28} {entry['strategy']:<18} {entry['bytes']:<8}")
        strategy_counts[entry['strategy']] = strategy_counts.get(entry['strategy'], 0) + 1
    print(f"  Summary: {strategy_counts}")
    failed_systems = [entry for entry in system_manifest if entry.get("strategy") == "FAILED"]
    if failed_systems:
        print("  Build halted: some systems failed code generation")
        for entry in failed_systems:
            print(f"    {entry['system']}: {entry.get('error', 'unknown error')}")
        return 1

    # --- Stage 4: scene_gen (deterministic) ---
    print("\n" + "=" * 70)
    print(f"Step 4: scene_gen — overwrite scenes/{resolved_main_scene}.gd")
    print("=" * 70)
    scene_path = emit_scene(
        imap=imap,
        hlr=hlr,
        constants=constants,
        godot_dir=godot_dir,
        scene_name=resolved_main_scene,
        role_defs=role_defs,
        scene_defaults=resolved_scene_defaults,
        role_groups=dict(contract.role_groups),
        capabilities=dict(contract.capabilities),
    )
    print(f"  wrote {scene_path.relative_to(godot_dir)} "
          f"({scene_path.stat().st_size} bytes)")
    debug_paths = write_debug_scripts(godot_dir)
    for debug_path in debug_paths:
        print(f"  wrote {debug_path.relative_to(godot_dir)} ({debug_path.stat().st_size} bytes)")

    # --- Stage 5: hud_gen ---
    print("\n" + "=" * 70)
    print("Step 5: hud_gen — custom widgets from mechanic_specs")
    print("=" * 70)
    builtin_hud_paths = write_builtin_hud_scripts(godot_dir / "scripts" / "hud")
    for builtin_hud_path in builtin_hud_paths:
        print(f"  wrote {builtin_hud_path.relative_to(godot_dir)} ({builtin_hud_path.stat().st_size} bytes)")
    hud_artifacts = await generate_custom_hud_widgets(
        hlr=hlr,
        output_scripts_dir=godot_dir / "scripts" / "hud",
        constants_path=game_dir / "dlr_mechanic_constants.json",
    )
    for artifact in hud_artifacts:
        f = Path(artifact["path"])
        print(f"  wrote {f.relative_to(godot_dir)} ({f.stat().st_size} bytes)")

    # --- Stage 6: save manifest for inspection ---
    (game_dir / "codegen_manifest.json").write_text(
        json.dumps({
            "game_name": game_name,
            "genre": genre,
            "systems": system_manifest,
            "hud_widgets": [
                {"file": str(Path(artifact["path"])), "strategy": str(artifact["strategy"])}
                for artifact in hud_artifacts
            ],
            "strategy_counts": strategy_counts,
        }, indent=2)
    )

    # --- Stage 7: Godot import + export ---
    print("\n" + "=" * 70)
    print("Step 7: Godot import + export")
    print("=" * 70)
    if not resolved_godot_bin:
        print("  Export failed:")
        print("    No Godot binary found. Pass --godot-bin or set GODOT_BIN.")
        return 1
    dot_godot = godot_dir / ".godot"
    if dot_godot.exists():
        shutil.rmtree(dot_godot)
    r = subprocess.run(
        [resolved_godot_bin, "--headless", "--import"],
        cwd=str(godot_dir), capture_output=True, text=True, timeout=120,
    )
    err_lines = [l for l in (r.stderr or "").splitlines()
                 if "error" in l.lower() and "deprecated" not in l.lower()]
    if err_lines:
        print("  Import errors:")
        for l in err_lines[:20]:
            print(f"    {l}")
        return 1
    print("  Import: clean")

    r = subprocess.run(
        [resolved_godot_bin, "--headless", "--export-release", "Web"],
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
    build_coverage = audit_build_coverage(
        coverage_manifest,
        contract,
        codegen_manifest=system_manifest,
        exported=exported.exists(),
        export_path=exported,
    )
    write_mechanic_artifact(game_dir / "mechanic_coverage_build.json", build_coverage)
    if build_coverage.blockers:
        print(f"  Mechanic coverage: {len(build_coverage.blockers)} blocker(s)")
        for blocker in build_coverage.blockers[:10]:
            print(f"    x {blocker}")
        return 1

    print("\n" + "=" * 70)
    print(f"DONE — {game_name} built from {genre} template")
    print("=" * 70)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Build a Godot game from spec artifacts.")
    parser.add_argument("game_name", help="Game directory name under output/")
    parser.add_argument("genre", help="Genre template name (e.g. 2d_fighter)")
    parser.add_argument("--template-src", default=None,
                        help="Optional Godot bootstrap project directory; omit to synthesize from the compiled req contract")
    parser.add_argument("--main-scene", default=None,
                        help="Optional main scene name (without .tscn extension); defaults to the best gameplay scene in the req artifacts")
    parser.add_argument("--template-character", default=None,
                        help="Optional character .gd file in the bootstrap project used as the actor script seed; defaults to the first HLR character")
    parser.add_argument("--godot-bin", default=None,
                        help="Godot executable name or absolute path (falls back to GODOT_BIN/godot/godot4)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    template_src: Path | None = None
    if args.template_src:
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
        godot_bin=args.godot_bin,
    ))


if __name__ == "__main__":
    sys.exit(main())
