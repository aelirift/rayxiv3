"""Genre expectation hints shared by HLR and mechanic coverage.

These are not template authorities. They are deterministic reference cues the
pipeline can use when a prompt implies a known style or genre and the req stack
must stay complete even without a template.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GenreExpectation:
    id: str
    name: str
    summary: str
    required_for_basic_play: bool = False
    keywords: tuple[str, ...] = ()
    trace_keywords: tuple[str, ...] = ()
    test_keywords: tuple[str, ...] = ()
    role_names: tuple[str, ...] = ()
    scene_names: tuple[str, ...] = ()


_GENRE_EXPECTATIONS: dict[str, tuple[GenreExpectation, ...]] = {
    "kart_racer": (
        GenreExpectation(
            id="third_person_chase_camera",
            name="Third Person Chase Camera",
            summary=(
                "Primary race view follows the lead vehicle from behind with forward depth, "
                "wide track readability, and a gameplay-usable horizon view."
            ),
            required_for_basic_play=True,
            keywords=("third-person", "third person", "chase camera", "behind vehicle", "3d race view"),
            trace_keywords=("camera.update", "camera.follow"),
            test_keywords=("camera", "chase", "track visible", "horizon"),
            role_names=("kart", "vehicle", "camera"),
            scene_names=("racing",),
        ),
        GenreExpectation(
            id="race_start_and_launch_boost",
            name="Race Start and Launch Boost",
            summary=(
                "Countdown transitions cleanly into racing, and timed acceleration at race start can "
                "grant a launch boost."
            ),
            required_for_basic_play=True,
            keywords=("countdown", "go", "launch boost", "rocket start", "race start"),
            trace_keywords=("countdown.", "launch_boost", "race.start"),
            test_keywords=("countdown", "launch", "start boost"),
            role_names=("kart", "race_manager"),
            scene_names=("countdown", "racing"),
        ),
        GenreExpectation(
            id="boost_pad_system",
            name="Boost Pad System",
            summary="Driving over boost pads grants a visible short-duration speed increase.",
            keywords=("boost pad", "dash panel", "speed pad", "track boost"),
            trace_keywords=("boost_pad.", "speed_boost"),
            test_keywords=("boost pad", "speed strip"),
            role_names=("kart", "track_section", "track_prop"),
            scene_names=("racing",),
        ),
        GenreExpectation(
            id="jump_trick_and_landing_boost",
            name="Jump Trick and Landing Boost",
            summary=(
                "Ramps or jumps can trigger a short airborne trick state that pays out a landing boost."
            ),
            keywords=("jump", "ramp", "trick", "landing boost", "hop"),
            trace_keywords=("jump.", "trick.", "landing_boost"),
            test_keywords=("jump", "ramp", "trick"),
            role_names=("kart", "track_section"),
            scene_names=("racing",),
        ),
        GenreExpectation(
            id="coin_speed_economy",
            name="Coin Speed Economy",
            summary=(
                "Collectible race resources increase top-end speed or progression value and appear in HUD."
            ),
            keywords=("coin", "collectible speed resource", "pickup currency"),
            trace_keywords=("coin.collect", "speed_bonus"),
            test_keywords=("coin", "pickup", "speed bonus"),
            role_names=("kart", "pickup", "hud"),
            scene_names=("racing",),
        ),
        GenreExpectation(
            id="alternate_traversal_modes",
            name="Alternate Traversal Modes",
            summary=(
                "Later-era kart racers often include at least one traversal variant such as gliding, "
                "underwater handling, or adhesion/anti-gravity sections."
            ),
            keywords=("glider", "glide", "underwater", "anti-gravity", "adhesion", "wall ride"),
            trace_keywords=("traversal.", "glide.", "underwater.", "anti_gravity."),
            test_keywords=("glide", "underwater", "anti-gravity"),
            role_names=("kart", "track_section"),
            scene_names=("racing",),
        ),
        GenreExpectation(
            id="vehicle_selection_and_loadout",
            name="Vehicle Selection And Loadout",
            summary=(
                "Character selection can be paired with vehicle/loadout selection so handling, speed, "
                "and feel are part of the pre-race build."
            ),
            keywords=("vehicle select", "kart select", "loadout", "tires", "glider setup"),
            trace_keywords=("vehicle_select", "loadout.confirm"),
            test_keywords=("vehicle select", "loadout"),
            role_names=("kart", "driver"),
            scene_names=("character_select", "vehicle_select", "track_select"),
        ),
    ),
    "2d_fighter": (
        GenreExpectation(
            id="duel_round_feedback",
            name="Duel Round Feedback",
            summary=(
                "The duel HUD should communicate timer, per-round wins, round transitions, and KO clearly."
            ),
            keywords=("round indicator", "timer", "ko", "round win", "match hud"),
            trace_keywords=("round.", "ko.", "hud."),
            test_keywords=("round", "timer", "ko"),
            role_names=("fighter", "hud_bar", "hud_text", "game"),
            scene_names=("gameplay",),
        ),
        GenreExpectation(
            id="combat_animation_coverage",
            name="Combat Animation Coverage",
            summary=(
                "Core combat states should have readable animations: idle, walk, jump, crouch, attacks, "
                "hit reaction, block reaction, knockdown, and specials."
            ),
            keywords=(
                "animation coverage",
                "idle",
                "walk",
                "jump",
                "crouch",
                "hit reaction",
                "block reaction",
                "special animation",
            ),
            trace_keywords=("to=idle", "to=walk", "to=jump", "to=crouch", "combat.hit", "combat.blocked"),
            test_keywords=("animation", "idle", "walk", "special"),
            role_names=("fighter", "sprite"),
            scene_names=("gameplay",),
        ),
        GenreExpectation(
            id="combat_impact_feedback",
            name="Combat Impact Feedback",
            summary=(
                "Hits, blocks, and special interactions should produce readable audiovisual feedback such as "
                "sparks, flashes, shake, or visible state change."
            ),
            keywords=("hit spark", "impact effect", "block spark", "screen shake", "impact feedback"),
            trace_keywords=("combat.hit", "combat.blocked", "vfx."),
            test_keywords=("hit spark", "impact"),
            role_names=("fighter", "projectile", "effect"),
            scene_names=("gameplay",),
        ),
    ),
}


def expectations_for_genre(genre: str | None) -> list[GenreExpectation]:
    return list(_GENRE_EXPECTATIONS.get((genre or "").strip().lower(), ()))


def expectations_prompt_text(genre: str | None) -> str:
    expectations = expectations_for_genre(genre)
    if not expectations:
        return ""
    lines = [
        "- "
        + f"{item.name}: {item.summary}"
        + (" Required for basic play." if item.required_for_basic_play else "")
        for item in expectations
    ]
    return "\n".join(lines)
