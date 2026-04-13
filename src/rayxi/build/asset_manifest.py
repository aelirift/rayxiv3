"""Req-owned asset prompt manifests and workspace validation.

This keeps visual generation game-agnostic:
- HLR / build_contract define the actors, stages, pickups, hazards, and mechanics
- this module turns that req-owned contract into concrete asset requests
- per-game overrides can add or refine art direction without becoming a second
  gameplay authority
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rayxi.spec.build_contract import BuildContract
from rayxi.spec.mechanic_contract import MechanicManifest
from rayxi.spec.models import GameIdentity


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_prompt_template(template: str, variables: dict[str, Any]) -> str:
    normalized = {key: _normalize_prompt_value(value) for key, value in (variables or {}).items()}
    return template.format_map(_SafeDict(normalized)).strip()


def _normalize_prompt_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_normalize_prompt_value(item) for item in value)
    if isinstance(value, dict):
        parts = [f"{key}: {_normalize_prompt_value(val)}" for key, val in value.items()]
        return ", ".join(parts)
    return str(value)


class AssetPromptEntry(BaseModel):
    id: str
    label: str
    asset_type: str
    scope: str
    destination_dir: str
    bind_target: str = ""
    role_name: str = ""
    source_mechanics: list[str] = Field(default_factory=list)
    required_for_ship: bool = True
    source: str = "deterministic"
    prompt_label: str
    prompt_template_id: str = ""
    prompt_template: str = ""
    prompt_variables: dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    negative_prompt: str = ""
    expected_files: list[str] = Field(default_factory=list)
    frame_count: int = 1
    aspect_ratio: str = "1:1"
    transparent_background: bool = True
    style_tags: list[str] = Field(default_factory=list)
    review_checklist: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        if not self.prompt and self.prompt_template:
            self.prompt = _render_prompt_template(self.prompt_template, self.prompt_variables)


class AssetPromptManifest(BaseModel):
    game_name: str
    genre: str
    prompt: str
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    generated_by: str = "deterministic"
    entries: list[AssetPromptEntry] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AssetValidationItem(BaseModel):
    asset_id: str
    label: str
    status: str
    destination_dir: str
    found_files: list[str] = Field(default_factory=list)
    missing_files: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class AssetValidationReport(BaseModel):
    game_name: str
    genre: str
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    summary: dict[str, int] = Field(default_factory=dict)
    items: list[AssetValidationItem] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


_FIGHTER_ACTION_SPECS: tuple[tuple[str, str, int, str], ...] = (
    ("idle", "Idle loop", 4, "neutral fighting stance, stable breathing cycle, feet planted"),
    ("walk", "Walk cycle", 6, "forward walk cycle with combat guard up, same camera angle and scale"),
    ("jump", "Jump arc", 3, "jump start, mid-air extension, landing-ready pose"),
    ("crouch", "Crouch pose", 1, "compact defensive crouch, readable silhouette"),
    ("light_punch", "Light punch", 3, "quick jab sequence, fast recovery, clean hand silhouette"),
    ("medium_punch", "Medium punch", 3, "mid-strength straight punch sequence with torso rotation"),
    ("heavy_punch", "Heavy punch", 4, "power strike sequence with strong anticipation and follow-through"),
    ("light_kick", "Light kick", 3, "fast low-risk kick sequence, readable foot placement"),
    ("medium_kick", "Medium kick", 3, "mid-strength kick sequence with clean hip rotation"),
    ("heavy_kick", "Heavy kick", 4, "power kick sequence with big arc and stable balance"),
    ("block", "Block pose", 2, "high guard defensive pose with clear forearm shield line"),
    ("hit", "Hit reaction", 2, "brief damage recoil with readable impact response"),
    ("dizzy", "Stunned loop", 4, "staggered dizzy loop, unstable but not grotesque"),
    ("ko", "Knockout pose", 1, "clean defeated pose on the ground"),
    ("special_projectile", "Projectile special", 4, "energy-cast sequence that clearly launches a ranged special"),
    ("special_uppercut", "Rising strike special", 4, "vertical rising strike sequence with strong upward force"),
    ("special_spinning", "Spinning special", 5, "traveling spin kick sequence with consistent limb arcs"),
)

_FIGHTER_VFX_SPECS: tuple[tuple[str, str, int], ...] = (
    ("projectile", "energy projectile animation", 4),
    ("powered_projectile", "powered energy projectile animation with stronger glow and shape definition", 4),
    ("hit_spark", "impact spark effect", 4),
    ("block_spark", "defensive impact spark effect", 4),
    ("rage_burst_vfx", "burst effect that communicates a rage power-up state", 6),
)

_VEHICLE_ASSET_SPECS: tuple[tuple[str, str, int], ...] = (
    ("kart_idle", "hero kart render in racing stance, driver seated, readable silhouette", 1),
    ("kart_turn_left", "kart banking left under load, tires angled, speed readable", 1),
    ("kart_turn_right", "kart banking right under load, tires angled, speed readable", 1),
    ("kart_boost", "kart with boost state, rear exhaust trail, aggressive acceleration read", 1),
    ("driver_portrait", "driver portrait for HUD or selection card", 1),
)

_COMMON_RACE_ASSETS: tuple[tuple[str, str, int, str], ...] = (
    ("track_backdrop", "wide race backdrop", 1, "very wide modern arcade racing backdrop with mountains, clouds, depth, and horizon scale"),
    ("track_overview_map", "track overview map", 1, "clean overhead course map for minimap and course preview"),
    ("countdown_marshal", "countdown marshal", 1, "floating countdown marshal character in a cloud, expressive and readable"),
    ("item_box", "item box", 1, "random item pickup box with bright readable silhouette"),
    ("shell_projectile", "shell projectile", 1, "arcade kart shell projectile, readable at small size"),
    ("banana_hazard", "banana hazard", 1, "slippery peel hazard, readable at small size"),
    ("boost_item", "boost item", 1, "speed boost collectible or mushroom-like pickup with clear energy read"),
)

_SPRITE_NEGATIVE = (
    "no text, no HUD, no watermark, no extra limbs, no broken fingers, no twisted neck, "
    "no anatomy drift, no costume drift, no second character, no weapon unless explicitly requested, "
    "transparent background"
)

_SCENE_NEGATIVE = "no text, no HUD, no watermark, no UI framing, no logos"

_CONSISTENCY_REVIEW = [
    "Keep the same face, body proportions, costume silhouette, and hair across every frame in the set.",
    "Keep the same camera angle, scale, and ground plane from frame to frame.",
    "Reject frames with extra fingers, twisted joints, broken anatomy, or costume color drift.",
    "Reject frames that change apparent gender, age, ethnicity, or body type unless intentionally requested.",
]

_TRACK_REVIEW = [
    "Track should read as a full course environment, not a toy-scale loop.",
    "Road width should visually support at least four karts side by side.",
    "Background should provide horizon depth and long-course atmosphere.",
]


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return slug or "asset"


def _title(text: str) -> str:
    return str(text or "").replace("_", " ").strip().title()


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = str(item or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        out.append(clean)
        seen.add(key)
    return out


def _feature_ids(manifest: MechanicManifest, *tokens: str) -> list[str]:
    wanted = [str(token or "").lower() for token in tokens if token]
    matched: list[str] = []
    for feature in manifest.features:
        blob = " ".join(
            [
                feature.id,
                feature.name,
                feature.summary,
                *feature.signals.system_names,
                *feature.signals.role_names,
                *feature.signals.property_ids,
                *feature.signals.keywords,
            ]
        ).lower()
        if any(token in blob for token in wanted):
            matched.append(feature.id)
    return _unique(matched)


def _combat_actor_names(hlr: GameIdentity, contract: BuildContract) -> list[str]:
    declared = list(hlr.get_enum("characters") or [])
    if declared:
        return declared
    return list(contract.role_groups.get("combat_actor_roles") or contract.role_groups.get("actor_roles") or ["fighter"])


def _vehicle_actor_names(hlr: GameIdentity, contract: BuildContract) -> list[str]:
    declared = list(hlr.get_enum("characters") or [])
    if declared:
        return declared
    return list(contract.role_groups.get("vehicle_actor_roles") or contract.role_groups.get("actor_roles") or ["driver"])


def _expected_sequence_files(label: str, frame_count: int) -> list[str]:
    if frame_count <= 1:
        return [f"{label}.png"]
    return [f"{label}_{idx}.png" for idx in range(frame_count)]


def _shared_character_dir(genre: str, character: str) -> str:
    return f"games/_lib/{genre}/{character}"


def _game_slot_dir(game_name: str, slot_name: str) -> str:
    return f"games/{game_name}/assets/slots/{slot_name}"


def _game_common_dir(game_name: str) -> str:
    return f"games/{game_name}/assets/common"


def _base_style_tags(prompt: str) -> list[str]:
    lower = prompt.lower()
    tags = ["production-ready", "clean silhouette", "consistent design"]
    if "realistic" in lower:
        tags.append("realistic")
    if "anime" in lower or "fighter" in lower or "sf2" in lower:
        tags.append("stylized 2d fighter art")
    if "kart" in lower or "racing" in lower:
        tags.append("modern arcade racing art")
    return _unique(tags)


def _fighter_entry(
    *,
    manifest: MechanicManifest,
    genre: str,
    game_name: str,
    character_name: str,
    prompt: str,
    label: str,
    display_label: str,
    frame_count: int,
    pose_description: str,
    scope: str = "shared_character",
    bind_target: str | None = None,
) -> AssetPromptEntry:
    bind = bind_target or character_name
    destination_dir = _shared_character_dir(genre, bind) if scope == "shared_character" else _game_slot_dir(game_name, bind)
    hero_name = _title(character_name)
    prompt_template = (
        "Create a production-ready asset for a {game_type}. "
        "Role: {role_kind}. Character: {character_name}. Animation set: {animation_name}. "
        "Create {frame_count} transparent frame(s) that preserve the same character model across the entire set. "
        "Action direction: {action_description}. "
        "Visual style: {style_direction}. "
        "Gameplay context: {game_prompt}"
    )
    prompt_variables = {
        "game_type": "2D fighting game",
        "role_kind": "combat actor",
        "character_name": hero_name,
        "animation_name": display_label,
        "frame_count": frame_count,
        "action_description": pose_description,
        "style_direction": "realistic proportions, polished arcade readability, transparent background",
        "game_prompt": prompt,
    }
    return AssetPromptEntry(
        id=_slug(f"{bind}_{label}"),
        label=f"{hero_name} {display_label}",
        asset_type="combat_actor_animation",
        scope=scope,
        destination_dir=destination_dir,
        bind_target=bind,
        role_name="fighter",
        source_mechanics=_feature_ids(manifest, "combat", "fight", label, "rage", "projectile", "block", "jump"),
        prompt_label=_slug(f"{bind}_{label}_prompt_v1"),
        prompt_template_id="combat_actor_animation_v1",
        prompt_template=prompt_template,
        prompt_variables=prompt_variables,
        negative_prompt=_SPRITE_NEGATIVE,
        expected_files=_expected_sequence_files(label, frame_count),
        frame_count=frame_count,
        aspect_ratio="3:4",
        transparent_background=True,
        style_tags=_unique(_base_style_tags(prompt) + ["combat actor", display_label.lower()]),
        review_checklist=list(_CONSISTENCY_REVIEW),
        notes=[
            "Use separate transparent frames or a sprite sheet that can be split into the listed filenames.",
            "Ground contact should stay believable so collision and hurtbox overlays line up with the art.",
        ],
    )


def _fighter_vfx_entry(
    *,
    manifest: MechanicManifest,
    genre: str,
    character_name: str,
    prompt: str,
    label: str,
    description: str,
    frame_count: int,
) -> AssetPromptEntry:
    prompt_template = (
        "Create a production-ready asset for a {game_type}. "
        "Role: {role_kind}. Bound character: {character_name}. Effect: {effect_name}. "
        "Create {frame_count} transparent frame(s) with strong readability at gameplay scale. "
        "Effect direction: {effect_description}. "
        "Visual style: {style_direction}. "
        "Gameplay context: {game_prompt}"
    )
    prompt_variables = {
        "game_type": "2D fighting game",
        "role_kind": "combat VFX",
        "character_name": _title(character_name),
        "effect_name": _title(label).replace("_", " "),
        "frame_count": frame_count,
        "effect_description": description,
        "style_direction": "clean silhouette, strong color separation, transparent background",
        "game_prompt": prompt,
    }
    return AssetPromptEntry(
        id=_slug(f"{character_name}_{label}"),
        label=f"{_title(character_name)} { _title(label) }".replace(" Vfx", " VFX"),
        asset_type="combat_vfx",
        scope="shared_character",
        destination_dir=_shared_character_dir(genre, character_name),
        bind_target=character_name,
        role_name="projectile",
        source_mechanics=_feature_ids(manifest, "projectile", "special", "rage", "combat", label),
        prompt_label=_slug(f"{character_name}_{label}_prompt_v1"),
        prompt_template_id="combat_vfx_v1",
        prompt_template=prompt_template,
        prompt_variables=prompt_variables,
        negative_prompt=_SPRITE_NEGATIVE,
        expected_files=_expected_sequence_files(label, frame_count),
        frame_count=frame_count,
        aspect_ratio="1:1",
        transparent_background=True,
        style_tags=_unique(_base_style_tags(prompt) + ["combat vfx"]),
        review_checklist=[
            "Effect shape should stay readable over a busy stage background.",
            "Frames should progress smoothly without random color or shape jumps.",
        ],
        notes=["Prefer transparent PNG frames so the effect can sit over sprites and debug overlays cleanly."],
    )


def _vehicle_entry(
    *,
    manifest: MechanicManifest,
    game_name: str,
    driver_name: str,
    bind_target: str,
    prompt: str,
    label: str,
    description: str,
) -> AssetPromptEntry:
    driver_title = _title(driver_name)
    prompt_template = (
        "Create a production-ready asset for a {game_type}. "
        "Role: {role_kind}. Driver: {character_name}. Render target: {asset_name}. "
        "Create {frame_count} transparent render(s) that preserve the same driver identity and the same vehicle silhouette. "
        "Action direction: {action_description}. "
        "Visual style: {style_direction}. "
        "Gameplay context: {game_prompt}"
    )
    prompt_variables = {
        "game_type": "arcade kart racer",
        "role_kind": "vehicle actor",
        "character_name": driver_title,
        "asset_name": _title(label),
        "frame_count": 1,
        "action_description": description,
        "style_direction": "premium modern kart materials, readable gameplay silhouette, transparent background",
        "game_prompt": prompt,
    }
    return AssetPromptEntry(
        id=_slug(f"{bind_target}_{label}"),
        label=f"{driver_title} { _title(label) }",
        asset_type="vehicle_render",
        scope="game_slot",
        destination_dir=_game_slot_dir(game_name, bind_target),
        bind_target=bind_target,
        role_name="kart",
        source_mechanics=_feature_ids(manifest, "race", "kart", "vehicle", "drift", "item"),
        prompt_label=_slug(f"{bind_target}_{label}_prompt_v1"),
        prompt_template_id="vehicle_render_v1",
        prompt_template=prompt_template,
        prompt_variables=prompt_variables,
        negative_prompt=_SPRITE_NEGATIVE,
        expected_files=_expected_sequence_files(label, 1),
        frame_count=1,
        aspect_ratio="4:3",
        transparent_background=True,
        style_tags=_unique(_base_style_tags(prompt) + ["vehicle actor", "kart"]),
        review_checklist=list(_CONSISTENCY_REVIEW),
        notes=["Use transparent PNG so the renderer can place the kart against multiple tracks and debug overlays."],
    )


def _race_common_entry(
    *,
    manifest: MechanicManifest,
    game_name: str,
    prompt: str,
    label: str,
    display_label: str,
    description: str,
    frame_count: int = 1,
) -> AssetPromptEntry:
    aspect_ratio = "16:9" if "backdrop" in label else "1:1"
    review = list(_TRACK_REVIEW if "track" in label else _CONSISTENCY_REVIEW[:2])
    prompt_template = (
        "Create a production-ready asset for a {game_type}. "
        "Role: {role_kind}. Asset: {asset_name}. "
        "Create {frame_count} frame(s). "
        "Direction: {asset_description}. "
        "Visual style: {style_direction}. "
        "Gameplay context: {game_prompt}"
    )
    prompt_variables = {
        "game_type": "arcade kart racer",
        "role_kind": "stage" if "track" in label else "supporting gameplay prop",
        "asset_name": _title(display_label),
        "frame_count": frame_count,
        "asset_description": description,
        "style_direction": (
            "very wide long-course environment with strong depth"
            if "track" in label
            else "small-scale gameplay prop with high readability and consistent premium art direction"
        ),
        "game_prompt": prompt,
    }
    return AssetPromptEntry(
        id=_slug(label),
        label=_title(display_label),
        asset_type="race_common_asset",
        scope="game_common",
        destination_dir=_game_common_dir(game_name),
        bind_target="common",
        role_name="stage" if "track" in label else "item",
        source_mechanics=_feature_ids(manifest, "race", "lap", "checkpoint", "item", "countdown", label),
        prompt_label=_slug(f"{label}_prompt_v1"),
        prompt_template_id="race_common_asset_v1",
        prompt_template=prompt_template,
        prompt_variables=prompt_variables,
        negative_prompt=_SCENE_NEGATIVE if "track" in label else _SPRITE_NEGATIVE,
        expected_files=_expected_sequence_files(label, frame_count),
        frame_count=frame_count,
        aspect_ratio=aspect_ratio,
        transparent_background="track" not in label,
        style_tags=_unique(_base_style_tags(prompt) + ["race common", display_label.lower()]),
        review_checklist=review,
        notes=[
            "For track backdrops, bias toward long-course scale and a wide road impression rather than a toy-scale loop."
        ] if "track" in label else ["Keep the item silhouette readable at small gameplay sizes."],
    )


def _load_override_entries(repo_root: Path, game_name: str) -> list[AssetPromptEntry]:
    override_path = repo_root / "games" / game_name / "assets" / "asset_overrides.json"
    if not override_path.exists():
        return []
    raw = json.loads(override_path.read_text(encoding="utf-8"))
    items = raw.get("entries", [])
    if not isinstance(items, list):
        return []
    entries: list[AssetPromptEntry] = []
    for item in items:
        try:
            entries.append(AssetPromptEntry.model_validate(item))
        except Exception:
            continue
    return entries


def build_asset_prompt_manifest(
    prompt: str,
    hlr: GameIdentity,
    contract: BuildContract,
    mechanic_manifest: MechanicManifest,
    repo_root: Path,
) -> AssetPromptManifest:
    entries: list[AssetPromptEntry] = []
    notes = [
        "This manifest is req-owned. It can borrow style direction from templates or overrides, but downstream asset consumers should only use the prompts and filenames recorded here.",
        "Override entries can add franchise-specific art direction without changing gameplay authority.",
    ]

    combat_roles = list(contract.role_groups.get("combat_actor_roles") or [])
    vehicle_roles = list(contract.role_groups.get("vehicle_actor_roles") or [])

    if combat_roles:
        for character_name in _combat_actor_names(hlr, contract):
            for action_label, display_label, frame_count, pose_description in _FIGHTER_ACTION_SPECS:
                entries.append(
                    _fighter_entry(
                        manifest=mechanic_manifest,
                        genre=contract.genre,
                        game_name=contract.game_name,
                        character_name=character_name,
                        prompt=prompt,
                        label=action_label,
                        display_label=display_label,
                        frame_count=frame_count,
                        pose_description=pose_description,
                    )
                )
            for vfx_label, vfx_description, frame_count in _FIGHTER_VFX_SPECS:
                entries.append(
                    _fighter_vfx_entry(
                        manifest=mechanic_manifest,
                        genre=contract.genre,
                        character_name=character_name,
                        prompt=prompt,
                        label=vfx_label,
                        description=vfx_description,
                        frame_count=frame_count,
                    )
                )
        entries.append(
            AssetPromptEntry(
                id="stage_background",
                label="Stage Background",
                asset_type="stage_background",
                scope="game_common",
                destination_dir=_game_common_dir(contract.game_name),
                bind_target="common",
                role_name="stage",
                source_mechanics=_feature_ids(mechanic_manifest, "combat", "round", "stage", "background"),
                prompt_label="stage_background_prompt_v1",
                prompt_template_id="stage_background_v1",
                prompt_template=(
                    "Create a production-ready asset for a {game_type}. "
                    "Role: {role_kind}. Asset: {asset_name}. "
                    "Create {frame_count} widescreen frame(s). "
                    "Direction: {asset_description}. "
                    "Visual style: {style_direction}. "
                    "Gameplay context: {game_prompt}"
                ),
                prompt_variables={
                    "game_type": "2D fighting game",
                    "role_kind": "stage background",
                    "asset_name": "Stage Background",
                    "frame_count": 1,
                    "asset_description": "layered depth, strong atmosphere, readable silhouettes for two combatants and projectile effects",
                    "style_direction": "widescreen premium stage art with a clear center lane for debug overlays and collisions",
                    "game_prompt": prompt,
                },
                negative_prompt=_SCENE_NEGATIVE,
                expected_files=["stage_background.png"],
                frame_count=1,
                aspect_ratio="16:9",
                transparent_background=False,
                style_tags=_unique(_base_style_tags(prompt) + ["stage background", "combat"]),
                review_checklist=list(_TRACK_REVIEW),
                notes=["Keep the center lane readable so hitboxes, fireballs, and overlays stay easy to inspect."],
            )
        )

    if vehicle_roles:
        drivers = _vehicle_actor_names(hlr, contract)
        for index, driver_name in enumerate(drivers[:2], start=1):
            bind_target = f"p{index}"
            for label, description, _frame_count in _VEHICLE_ASSET_SPECS:
                entries.append(
                    _vehicle_entry(
                        manifest=mechanic_manifest,
                        game_name=contract.game_name,
                        driver_name=driver_name,
                        bind_target=bind_target,
                        prompt=prompt,
                        label=label,
                        description=description,
                    )
                )
        for label, display_label, frame_count, description in _COMMON_RACE_ASSETS:
            entries.append(
                _race_common_entry(
                    manifest=mechanic_manifest,
                    game_name=contract.game_name,
                    prompt=prompt,
                    label=label,
                    display_label=display_label,
                    description=description,
                    frame_count=frame_count,
                )
            )

    entries.extend(_load_override_entries(repo_root, contract.game_name))
    by_id: dict[str, AssetPromptEntry] = {}
    for entry in entries:
        by_id[entry.id] = entry

    return AssetPromptManifest(
        game_name=contract.game_name,
        genre=contract.genre,
        prompt=prompt,
        entries=list(by_id.values()),
        notes=notes,
    )


def validate_asset_workspace(manifest: AssetPromptManifest, repo_root: Path) -> AssetValidationReport:
    items: list[AssetValidationItem] = []
    blockers: list[str] = []
    for entry in manifest.entries:
        destination = repo_root / entry.destination_dir
        found: list[str] = []
        missing: list[str] = []
        if destination.exists() and destination.is_dir():
            for filename in entry.expected_files:
                if (destination / filename).exists():
                    found.append(filename)
                else:
                    missing.append(filename)
        else:
            missing = list(entry.expected_files)
        issues: list[str] = []
        if not destination.exists():
            issues.append(f"missing destination directory: {entry.destination_dir}")
        elif not destination.is_dir():
            issues.append(f"destination is not a directory: {entry.destination_dir}")
        if missing:
            issues.append(f"missing {len(missing)} expected file(s)")
        if not found:
            status = "missing"
        elif missing:
            status = "partial"
        else:
            status = "ready"
        if entry.required_for_ship and status != "ready":
            blockers.append(f"{entry.label}: {', '.join(missing[:4])}")
        items.append(
            AssetValidationItem(
                asset_id=entry.id,
                label=entry.label,
                status=status,
                destination_dir=entry.destination_dir,
                found_files=found,
                missing_files=missing,
                issues=issues,
            )
        )
    summary = {
        "total": len(items),
        "ready": sum(1 for item in items if item.status == "ready"),
        "partial": sum(1 for item in items if item.status == "partial"),
        "missing": sum(1 for item in items if item.status == "missing"),
        "ship_blockers": len(blockers),
    }
    return AssetValidationReport(
        game_name=manifest.game_name,
        genre=manifest.genre,
        summary=summary,
        items=items,
        blockers=blockers,
        notes=[
            "A missing asset here is not a gameplay blocker by itself; it means the visual package is not ship-ready yet.",
            "Use the prompt labels in the manifest to generate or regenerate the missing visuals, then rebuild.",
        ],
    )
