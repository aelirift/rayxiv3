"""Generate placeholder anime-style Ryu sprites for the SF2 template.

Style: anime, sleek, modern — clean lineart, cel-shaded, dynamic poses.
All sprites share a consistent base description so the character looks the
same across frames.

Target: games/sf2/assets/ryu/*.png
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s", force=True)
sys.stdout.reconfigure(line_buffering=True)

_OUT_DIR = Path(__file__).resolve().parents[1] / "games" / "sf2" / "assets" / "ryu"

# Consistent character description used in every prompt — keeps the sprite
# identity stable across frames even though each is a separate generation call.
_CHAR = (
    "anime style, sleek modern male martial artist, muscular build, white karate gi "
    "with black belt tied at waist, red headband, short dark brown hair, determined "
    "expression, clean bold lineart, cel-shaded coloring, dynamic action pose"
)

_BG = (
    "plain white background, no scenery, no shadow, no text, no hud, no ui elements, "
    "single character only, centered in frame, full body visible"
)


def _p(pose: str) -> str:
    return f"{_CHAR}, {pose}, {_BG}"


# (label, prompt, aspect_ratio)
CHARACTER_SPRITES: list[tuple[str, str, str]] = [
    ("ryu_idle",
     _p("ready fighting stance, fists raised in front of chest, legs bent, facing right, poised for combat"), "3:4"),
    ("ryu_walk",
     _p("mid-stride walking forward, one leg lifted, fists up in guard position, facing right"), "3:4"),
    ("ryu_jump",
     _p("mid-air jumping, both legs tucked up, body compact, one fist extended, facing right"), "3:4"),
    ("ryu_crouch",
     _p("crouched low to the ground, one knee bent fully, one hand up in defensive guard, facing right"), "3:4"),
    ("ryu_light_punch",
     _p("quick jab with right fist extended forward horizontally, left fist at chin, facing right, motion lines"), "3:4"),
    ("ryu_heavy_punch",
     _p("powerful straight cross punch, right arm fully extended forward, torso rotated for power, facing right, motion blur"), "3:4"),
    ("ryu_light_kick",
     _p("snap front kick with right leg extended horizontally at knee height, balancing on left, facing right"), "3:4"),
    ("ryu_heavy_kick",
     _p("roundhouse kick with right leg swinging horizontally at head height, body rotated, facing right, motion blur"), "3:4"),
    ("ryu_hadouken",
     _p("hadouken stance, both palms pressed together at hip level forming a glowing blue energy sphere between the hands, leaning forward, facing right"), "3:4"),
    ("ryu_shoryuken",
     _p("rising uppercut shoryuken, body ascending diagonally upward, right arm fully extended high overhead, left arm trailing down, motion streaks"), "3:4"),
    ("ryu_tatsumaki",
     _p("spinning hurricane kick in mid-air, body horizontal, one leg extended outward, arms spread for balance, spinning motion blur"), "3:4"),
    ("ryu_hit",
     _p("staggering backward from being hit, head tilted back, face wincing, one arm flung back, body off balance"), "3:4"),
    ("ryu_block",
     _p("defensive block stance, both forearms crossed in front of face and chest, body tensed, facing right"), "3:4"),
    ("ryu_ko",
     _p("knocked out lying flat on the ground on back, arms sprawled, eyes closed, X's for eyes, defeated"), "3:4"),
    ("ryu_dizzy",
     _p("dazed standing, swaying unsteadily, eyes swirling, small birds or stars circling the head, mouth open, confused"), "3:4"),
]

VFX_SPRITES: list[tuple[str, str, str]] = [
    ("hadouken_projectile",
     "anime style glowing blue energy sphere, crackling with blue lightning, wispy plasma trails behind, "
     "flying horizontally, no character, no background, plain white background, centered", "1:1"),
    ("hadouken_powered",
     "anime style larger powered glowing red-orange energy sphere, bigger than normal hadouken, crackling with fiery lightning, "
     "intense heat distortion, flying horizontally, no character, plain white background, centered", "1:1"),
    ("hit_spark",
     "anime style bright yellow impact burst effect, star-shaped explosion, radial lines, cartoon impact flash, "
     "no character, plain white background, centered, single effect", "1:1"),
    ("block_spark",
     "anime style pale blue defensive shield impact effect, hexagonal shield pattern, soft glow, deflection sparks, "
     "no character, plain white background, centered", "1:1"),
    ("rage_burst_vfx",
     "anime style intense red aura burst, fiery orange and red flames radiating outward, rage power-up effect, "
     "crackling energy, no character, plain white background, centered", "1:1"),
]

STAGE_SPRITES: list[tuple[str, str, str]] = [
    ("suzaku_castle_bg",
     "anime style japanese feudal castle courtyard at sunset, wide side-scrolling background, "
     "stone floor tiles, castle walls with red pillars in the distance, orange sunset sky with clouds, "
     "cherry blossom petals drifting, detailed anime art style, landscape orientation, no characters, no text", "16:9"),
]


async def main():
    from rayxi.llm.image_gen import MiniMaxImageCaller

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    caller = MiniMaxImageCaller()

    all_specs = CHARACTER_SPRITES + VFX_SPRITES + STAGE_SPRITES

    # Skip any that already exist (idempotent rerun)
    pending = [(name, prompt, ratio) for name, prompt, ratio in all_specs
               if not (_OUT_DIR / f"{name}.png").exists()]
    skipped = len(all_specs) - len(pending)
    if skipped:
        print(f"Skipping {skipped} already-generated sprites.")

    print(f"Generating {len(pending)} sprites with concurrency 4 …")
    results = await caller.generate_many(pending, concurrency=4)

    for name, png in results.items():
        out_path = _OUT_DIR / f"{name}.png"
        out_path.write_bytes(png)
        print(f"  saved: {out_path.name}  ({len(png)} bytes)")

    # Report any missing
    expected = {name for name, _, _ in all_specs}
    got = {p.stem for p in _OUT_DIR.glob("*.png")}
    missing = expected - got
    if missing:
        print(f"\nMISSING: {sorted(missing)}")
    else:
        print(f"\nAll {len(expected)} sprites present in {_OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
