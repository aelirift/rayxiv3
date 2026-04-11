"""Generate placeholder fighter sprites + VFX via MiniMax for any 2D fighter.

Usage:
    python3 scripts/gen_fighter_assets.py <genre> <character> "<description>"

Example:
    python3 scripts/gen_fighter_assets.py 2d_fighter scorpion \\
        "yellow ninja, black ninja mask showing only eyes, spear weapon, red eyes, hellfire aura"

Writes to `games/_lib/{genre}/{character}/{label}.jpg` — the SHARED genre
asset lib. Any game built with that genre reuses these sprites automatically
(build_game.py prefers per-game overrides, then falls back to _lib).

Idempotent: skips any sprite that already exists. Detects JPEG vs PNG by
magic bytes and uses the correct extension.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
sys.stdout.reconfigure(line_buffering=True)

_REPO = Path(__file__).resolve().parents[1]


# --- Genre-neutral 2D fighter animation set ----------------------------------
# Keeps the action-to-sprite mapping in one place so every 2d_fighter game uses
# the same keys. Template fighter animations reference these labels.
FIGHTER_POSES: list[tuple[str, str, str]] = [
    ("idle",
     "ready fighting stance, fists raised in front of chest, legs bent, facing right, poised for combat",
     "3:4"),
    ("walk",
     "mid-stride walking forward, one leg lifted, fists up in guard position, facing right",
     "3:4"),
    ("jump",
     "mid-air jumping, both legs tucked up, body compact, facing right",
     "3:4"),
    ("crouch",
     "crouched low to the ground, one knee bent fully, one hand up in defensive guard, facing right",
     "3:4"),
    ("light_punch",
     "quick jab with right fist extended forward horizontally, left fist at chin, facing right, motion lines",
     "3:4"),
    ("heavy_punch",
     "powerful straight cross punch, right arm fully extended forward, torso rotated for power, facing right, motion blur",
     "3:4"),
    ("light_kick",
     "snap front kick with right leg extended horizontally at knee height, balancing on left, facing right",
     "3:4"),
    ("heavy_kick",
     "roundhouse kick with right leg swinging horizontally at head height, body rotated, facing right, motion blur",
     "3:4"),
    ("special",
     "signature special move pose, body leaned forward, both hands extended forward unleashing energy, facing right",
     "3:4"),
    ("hit",
     "staggering backward from being hit, head tilted back, face wincing, one arm flung back, body off balance",
     "3:4"),
    ("block",
     "defensive block stance, both forearms crossed in front of face and chest, body tensed, facing right",
     "3:4"),
    ("ko",
     "knocked out lying flat on the ground on back, arms sprawled, eyes closed, defeated",
     "3:4"),
]

# Shared VFX (generated once per game)
VFX_POSES: list[tuple[str, str, str]] = [
    ("hit_spark",
     "anime style bright yellow impact burst effect, star-shaped explosion, radial lines, cartoon impact flash",
     "1:1"),
    ("block_spark",
     "anime style pale blue defensive shield impact effect, hexagonal shield pattern, soft glow",
     "1:1"),
]


_BG_NEGATIVE = (
    "plain white background, no scenery, no shadow, no text, no hud, no ui elements, "
    "single character only, centered in frame, full body visible"
)

_VFX_NEGATIVE = "no character, plain white background, centered, single effect"


def _char_prompt(character_desc: str, pose: str) -> str:
    return f"anime style, {character_desc}, {pose}, clean bold lineart, cel-shaded coloring, {_BG_NEGATIVE}"


def _vfx_prompt(pose: str) -> str:
    return f"{pose}, {_VFX_NEGATIVE}"


async def generate_character(
    genre: str,
    character: str,
    description: str,
    include_vfx: bool = True,
) -> None:
    from rayxi.llm.image_gen import MiniMaxImageCaller

    out_dir = _REPO / "games" / "_lib" / genre / character
    out_dir.mkdir(parents=True, exist_ok=True)

    specs: list[tuple[str, str, str]] = []
    for label, pose, ratio in FIGHTER_POSES:
        specs.append((f"{character}_{label}", _char_prompt(description, pose), ratio))
    if include_vfx:
        for label, pose, ratio in VFX_POSES:
            specs.append((label, _vfx_prompt(pose), ratio))

    pending = [(name, prompt, ratio) for name, prompt, ratio in specs
               if not (out_dir / f"{name}.png").exists()]
    skipped = len(specs) - len(pending)
    if skipped:
        print(f"  Skipping {skipped} already-generated sprites in {out_dir}")

    if not pending:
        print(f"  All {len(specs)} sprites already present.")
        return

    print(f"  Generating {len(pending)} sprites for {character} …")
    caller = MiniMaxImageCaller()
    results = await caller.generate_many(pending, concurrency=4)

    for name, raw in results.items():
        # MiniMax's `image_generation` endpoint returns JPEG bytes despite the
        # API name implying PNG — so detect by magic bytes and pick the right
        # extension. Godot imports both natively.
        if raw.startswith(b"\xff\xd8\xff"):
            ext = "jpg"
        elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        else:
            ext = "png"  # default; Godot may still fail but won't silently corrupt
        (out_dir / f"{name}.{ext}").write_bytes(raw)
        print(f"    saved: {name}.{ext} ({len(raw)} bytes)")

    expected = {name for name, _, _ in specs}
    got = {p.stem for p in out_dir.glob("*.png")}
    missing = expected - got
    if missing:
        print(f"  MISSING: {sorted(missing)}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("genre", help="Genre template name, e.g. 2d_fighter")
    parser.add_argument("character")
    parser.add_argument("description",
                        help="Character description (e.g. 'yellow ninja with spear')")
    parser.add_argument("--no-vfx", action="store_true", help="Skip VFX sprites")
    args = parser.parse_args()

    sys.path.insert(0, str(_REPO / "src"))
    await generate_character(
        genre=args.genre,
        character=args.character,
        description=args.description,
        include_vfx=not args.no_vfx,
    )


if __name__ == "__main__":
    asyncio.run(main())
