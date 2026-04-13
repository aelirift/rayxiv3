"""Mechanic coverage audit."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rayxi.spec.build_contract import BuildContract
from rayxi.llm.json_tools import parse_json_response
from rayxi.spec.genre_expectations import expectations_for_genre, expectations_prompt_text
from rayxi.spec.mechanic_behavior_fallback import (
    default_behaviors_for_feature,
    feature_test_needles,
    merge_behaviors,
)
from rayxi.spec.impact_map import ImpactMap
from rayxi.spec.mechanic_contract import (
    CoverageSignals,
    MechanicBehavior,
    MechanicFeature,
    MechanicManifest,
    MechanicTestAction,
    MechanicVerification,
)
from rayxi.spec.models import GameIdentity, MechanicSpec, SceneListEntry


class FeatureCoverageResult(BaseModel):
    feature_id: str
    feature_name: str
    required_for_basic_play: bool = True
    status: str
    evidence: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    matched_signals: dict[str, list[str]] = Field(default_factory=dict)


class MechanicCoverageReport(BaseModel):
    game_name: str
    stage: str
    prompt: str = ""
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    generated_by: str = "deterministic"
    summary: dict[str, int] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    features: list[MechanicFeature] = Field(default_factory=list)
    results: list[FeatureCoverageResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


_SYSTEM_REQUIRED_TOKENS = (
    "input",
    "movement",
    "combat",
    "health",
    "damage",
    "projectile",
    "special",
    "block",
    "round",
    "win",
    "race",
    "lap",
    "checkpoint",
    "rank",
    "item",
    "collision",
    "camera",
)

_PROMPT_FEATURE_HINTS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("rage", ("rage", "meter", "stack"), ("rage", "meter", "stack", "powered special")),
    ("projectile", ("fireball", "projectile", "hadouken", "shot"), ("projectile", "fireball", "shot")),
    ("blocking", ("block", "guard"), ("block", "guard")),
    ("jump", ("jump", "air"), ("jump", "airborne")),
    ("crouch", ("crouch", "duck"), ("crouch", "low")),
    ("drift", ("drift", "boost"), ("drift", "boost")),
    ("item", ("item", "pickup", "box"), ("item", "pickup")),
    ("lap", ("lap", "checkpoint", "finish"), ("lap", "checkpoint", "finish")),
    ("minimap", ("minimap", "map"), ("minimap",)),
)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "feature"


def _pretty_name(text: str) -> str:
    return text.replace("_", " ").strip().title()


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


def _flatten_scenes(scenes: list[SceneListEntry]) -> list[SceneListEntry]:
    flat: list[SceneListEntry] = []
    for scene in scenes:
        flat.append(scene)
        flat.extend(_flatten_scenes(scene.children or []))
    return flat


def _enum_meta(hlr: GameIdentity, enum_name: str) -> tuple[list[str], dict[str, str], dict[str, str]]:
    for enum in hlr.enums:
        if enum.name == enum_name:
            return (
                list(enum.values),
                dict(enum.value_descriptions or {}),
                dict(enum.value_template_origins or {}),
            )
    return [], {}, {}


def _hlr_runtime_roles(hlr: GameIdentity) -> list[str]:
    roles: list[str] = []
    for spec in hlr.mechanic_specs:
        for prop in spec.properties:
            if prop.role not in {"game", "character", "hud"}:
                roles.append(prop.role)
    return _unique(roles)


def _hlr_property_ids(hlr: GameIdentity) -> list[str]:
    property_ids: list[str] = []
    for spec in hlr.mechanic_specs:
        for prop in spec.properties:
            property_ids.append(f"{prop.role}.{prop.name}")
    return _unique(property_ids)


def _hlr_context_for_llm(prompt: str, hlr: GameIdentity) -> dict[str, Any]:
    game_systems, descriptions, origins = _enum_meta(hlr, "game_systems")
    scenes = _flatten_scenes(hlr.scenes)
    return {
        "prompt": prompt,
        "game_name": hlr.game_name,
        "genre": hlr.genre,
        "player_mode": hlr.player_mode,
        "scenes": [
            {
                "scene_name": scene.scene_name,
                "purpose": scene.purpose,
                "fsm_state": scene.fsm_state,
            }
            for scene in scenes
        ],
        "global_rules": list(hlr.global_rules or []),
        "win_condition": hlr.win_condition,
        "systems": [
            {
                "name": system_name,
                "description": descriptions.get(system_name, ""),
                "origin": origins.get(system_name, ""),
            }
            for system_name in game_systems
        ],
        "mechanic_specs": [
            {
                "system_name": spec.system_name,
                "summary": spec.summary,
                "properties": [
                    {
                        "role": prop.role,
                        "name": prop.name,
                        "type": prop.type,
                        "written_by": list(prop.written_by or []),
                        "read_by": list(prop.read_by or []),
                        "purpose": prop.purpose,
                    }
                    for prop in spec.properties
                ],
                "hud_entities": [
                    {
                        "name": hud.name,
                        "reads": list(hud.reads or []),
                        "visual_states": hud.visual_states,
                    }
                    for hud in spec.hud_entities
                ],
                "interactions": [
                    {
                        "trigger": interaction.trigger,
                        "condition": interaction.condition,
                        "effects": [
                            {
                                "verb": effect.verb,
                                "target": effect.target,
                                "description": effect.description,
                            }
                            for effect in interaction.effects
                        ],
                    }
                    for interaction in spec.interactions
                ],
            }
            for spec in hlr.mechanic_specs
        ],
        "runtime_roles": _hlr_runtime_roles(hlr),
        "property_ids": _hlr_property_ids(hlr),
    }


def _default_trace_keywords(system_name: str) -> list[str]:
    lower_name = system_name.lower()
    out: list[str] = []
    if "projectile" in lower_name:
        out.extend(["projectile.spawn", "special.executed"])
    if "rage" in lower_name:
        out.extend(["rage.", "powered_special"])
    if "block" in lower_name:
        out.extend(["combat.blocked"])
    if "health" in lower_name or "combat" in lower_name:
        out.extend(["combat.hit"])
    if "movement" in lower_name:
        out.extend(["movement.", "to=walk", "to=jump"])
    if "race" in lower_name or "lap" in lower_name or "checkpoint" in lower_name:
        out.extend(["race.", "lap.", "checkpoint"])
    if "item" in lower_name:
        out.extend(["item.", "pickup."])
    return _unique(out)


def _default_test_keywords(system_name: str) -> list[str]:
    lower_name = system_name.lower()
    out: list[str] = []
    if "projectile" in lower_name:
        out.extend(["Projectile", "fireball", "special"])
    if "rage" in lower_name:
        out.extend(["rage", "powered special"])
    if "block" in lower_name:
        out.extend(["block"])
    if "movement" in lower_name:
        out.extend(["Walk", "Jump", "Crouch"])
    if "lap" in lower_name or "checkpoint" in lower_name or "race" in lower_name:
        out.extend(["lap", "checkpoint", "race"])
    if "item" in lower_name:
        out.extend(["item", "pickup"])
    return _unique(out)


def _expectation_matching_systems(
    hlr: GameIdentity,
    expectation_keywords: tuple[str, ...],
    descriptions: dict[str, str],
) -> list[str]:
    matches: list[str] = []
    keywords = [keyword.lower() for keyword in expectation_keywords if keyword]
    if not keywords:
        return matches
    systems, _, _ = _enum_meta(hlr, "game_systems")
    for system_name in systems:
        spec = hlr.mechanic_spec_for(system_name)
        text = " ".join(
            [
                system_name,
                descriptions.get(system_name, ""),
                getattr(spec, "summary", ""),
            ]
        ).lower()
        if any(keyword in text for keyword in keywords):
            matches.append(system_name)
    return _unique(matches)


def _genre_expectation_features(prompt: str, hlr: GameIdentity) -> list[MechanicFeature]:
    _, descriptions, _ = _enum_meta(hlr, "game_systems")
    features: list[MechanicFeature] = []
    for expectation in expectations_for_genre(hlr.genre):
        matched_systems = _expectation_matching_systems(hlr, expectation.keywords, descriptions)
        features.append(
            MechanicFeature(
                id=_slug(expectation.id),
                name=expectation.name,
                summary=expectation.summary,
                source="genre_expectation",
                required_for_basic_play=expectation.required_for_basic_play,
                signals=CoverageSignals(
                    system_names=matched_systems,
                    role_names=list(expectation.role_names),
                    scene_names=list(expectation.scene_names),
                    keywords=list(expectation.keywords),
                    trace_keywords=list(expectation.trace_keywords),
                    test_keywords=list(expectation.test_keywords),
                ),
            )
        )
    return features


def _feature_from_system(
    prompt: str,
    system_name: str,
    descriptions: dict[str, str],
    origins: dict[str, str],
    spec: MechanicSpec | None,
) -> MechanicFeature:
    lower_prompt = prompt.lower()
    combined_text = " ".join(
        part for part in [system_name, descriptions.get(system_name, ""), getattr(spec, "summary", "")] if part
    ).lower()
    required = (
        any(token in combined_text for token in _SYSTEM_REQUIRED_TOKENS)
        or any(token in lower_prompt for token in system_name.lower().split("_"))
    )
    feature = MechanicFeature(
        id=_slug(system_name),
        name=_pretty_name(system_name),
        summary=(getattr(spec, "summary", "") or descriptions.get(system_name, "")),
        source="hlr_custom_system" if origins.get(system_name) == "(new)" else "hlr_system",
        required_for_basic_play=required,
        signals=CoverageSignals(
            system_names=[system_name],
            role_names=_unique([prop.role for prop in (spec.properties if spec else []) if prop.role]),
            property_ids=_unique(
                [f"{prop.role}.{prop.name}" for prop in (spec.properties if spec else []) if prop.role and prop.name]
            ),
            keywords=_unique(
                [
                    system_name.replace("_", " "),
                    descriptions.get(system_name, ""),
                    getattr(spec, "summary", ""),
                ]
            ),
            trace_keywords=_default_trace_keywords(system_name),
            test_keywords=_default_test_keywords(system_name),
        ),
    )
    feature.behaviors = default_behaviors_for_feature(feature)
    return feature


def _attach_default_behaviors(features: list[MechanicFeature]) -> list[MechanicFeature]:
    for feature in features:
        feature.behaviors = merge_behaviors(list(feature.behaviors) + default_behaviors_for_feature(feature))
    return features


def _unique_features(features: list[MechanicFeature]) -> list[MechanicFeature]:
    by_id: dict[str, MechanicFeature] = {}
    for feature in features:
        feature_id = _slug(feature.id or feature.name)
        feature.id = feature_id
        existing = by_id.get(feature_id)
        if existing is None:
            by_id[feature_id] = feature
            continue
        existing.required_for_basic_play = existing.required_for_basic_play or feature.required_for_basic_play
        existing.summary = existing.summary or feature.summary
        existing.source = existing.source if existing.source != "hlr_system" else feature.source
        existing.signals = CoverageSignals(
            system_names=_unique(existing.signals.system_names + feature.signals.system_names),
            role_names=_unique(existing.signals.role_names + feature.signals.role_names),
            property_ids=_unique(existing.signals.property_ids + feature.signals.property_ids),
            scene_names=_unique(existing.signals.scene_names + feature.signals.scene_names),
            keywords=_unique(existing.signals.keywords + feature.signals.keywords),
            trace_keywords=_unique(existing.signals.trace_keywords + feature.signals.trace_keywords),
            test_keywords=_unique(existing.signals.test_keywords + feature.signals.test_keywords),
        )
        existing.behaviors = merge_behaviors(existing.behaviors + feature.behaviors)
    return _attach_default_behaviors(list(by_id.values()))


def _fallback_manifest(prompt: str, hlr: GameIdentity) -> MechanicManifest:
    systems, descriptions, origins = _enum_meta(hlr, "game_systems")
    features: list[MechanicFeature] = list(_genre_expectation_features(prompt, hlr))
    for system_name in systems:
        features.append(
            _feature_from_system(
                prompt=prompt,
                system_name=system_name,
                descriptions=descriptions,
                origins=origins,
                spec=hlr.mechanic_spec_for(system_name),
            )
        )

    prompt_lower = prompt.lower()
    for label, prompt_tokens, keywords in _PROMPT_FEATURE_HINTS:
        if not any(token in prompt_lower for token in prompt_tokens):
            continue
        if any(label == feature.id or label in feature.id for feature in features):
            continue
        matching_systems = [system for system in systems if label in system.lower()]
        features.append(
            MechanicFeature(
                id=_slug(label),
                name=_pretty_name(label),
                summary=f"Prompt-explicit feature: {label}",
                source="prompt_explicit",
                required_for_basic_play=True,
                signals=CoverageSignals(
                    system_names=matching_systems,
                    keywords=list(keywords),
                    test_keywords=list(keywords),
                    trace_keywords=list(keywords),
                ),
            )
        )

    return MechanicManifest(
        game_name=hlr.game_name,
        prompt=prompt,
        features=_unique_features(features),
        generated_by="deterministic_fallback",
    )


def _merge_with_system_backbone(prompt: str, hlr: GameIdentity, features: list[MechanicFeature]) -> list[MechanicFeature]:
    systems, descriptions, origins = _enum_meta(hlr, "game_systems")
    merged = list(features)
    seen_systems: set[str] = set()
    for feature in merged:
        seen_systems.update(name.lower() for name in feature.signals.system_names)
    for system_name in systems:
        if system_name.lower() in seen_systems:
            continue
        merged.append(
            _feature_from_system(
                prompt=prompt,
                system_name=system_name,
                descriptions=descriptions,
                origins=origins,
                spec=hlr.mechanic_spec_for(system_name),
            )
        )
    return _unique_features(merged)


async def build_mechanic_manifest(
    prompt: str,
    hlr: GameIdentity,
    caller: Any | None = None,
    trace: Any | None = None,
) -> MechanicManifest:
    fallback = _fallback_manifest(prompt, hlr)
    if caller is None:
        return fallback

    context = _hlr_context_for_llm(prompt, hlr)
    system_prompt = (
        "You are an audit compiler for a game-generation pipeline.\n"
        "Convert the user prompt plus the accepted HLR into a canonical feature checklist.\n"
        "Rules:\n"
        "- HLR names are authoritative. Reuse HLR system/scene/role/property names when possible.\n"
        "- Do not invent template names.\n"
        "- If HLR combines a mechanic into a broader system, use that HLR system name.\n"
        "- Include prompt-explicit mechanics, genre-standard expectations implied by the prompt, and the basic-play mechanics needed for a minimally playable result.\n"
        "- Keep features at mechanic/capability level, not title-specific lore.\n"
        "- When you can infer a deterministic browser verification flow, include it as behavior actions and observables.\n"
        "- Behaviors must be mechanic-shaped. A projectile, shell, pizza-heal, or tribute summon should be described as trigger/effect/observable mechanics, not as franchise-specific trivia.\n"
        "- If the prompt implies a known style and the HLR omitted an expected mechanic, still emit a feature with source='genre_expectation'. Leave canonical refs empty rather than pretending it exists.\n"
        "- Return JSON only."
    )
    user_prompt = (
        "Return JSON with shape:\n"
        "{\n"
        '  "features": [\n'
        "    {\n"
        '      "id": "snake_case_id",\n'
        '      "name": "Human Name",\n'
        '      "summary": "short summary",\n'
        '      "source": "prompt_explicit | basic_play | hlr_system | genre_expectation",\n'
        '      "required_for_basic_play": true,\n'
        '      "signals": {\n'
        '        "system_names": [],\n'
        '        "role_names": [],\n'
        '        "property_ids": [],\n'
        '        "scene_names": [],\n'
        '        "keywords": [],\n'
        '        "trace_keywords": [],\n'
        '        "test_keywords": []\n'
        "      },\n"
        '      "behaviors": [\n'
        "        {\n"
        '          "id": "snake_case_behavior_id",\n'
        '          "name": "Human Behavior Name",\n'
        '          "summary": "what this verifies",\n'
        '          "required_for_basic_play": true,\n'
        '          "priority": 100,\n'
        '          "source": "llm",\n'
        '          "system_names": [],\n'
        '          "role_names": [],\n'
        '          "property_ids": [],\n'
        '          "scene_names": [],\n'
        '          "preconditions": [],\n'
        '          "actions": [\n'
        "            {\n"
        '              "action": "step label",\n'
        '              "description": "what the step does",\n'
        '              "keys": "optional exact key chord",\n'
        '              "method": "press|hold|sequence",\n'
        '              "wait_ms": 600,\n'
        '              "hold_ms": 0,\n'
        '              "verify_change": false,\n'
        '              "diff_threshold": 1.0,\n'
        '              "navigate_query": "optional test-mode query",\n'
        '              "sequence": [],\n'
        '              "verification": {\n'
        '                "trace_any": [],\n'
        '                "trace_all": [],\n'
        '                "trace_any_global": [],\n'
        '                "trace_all_global": [],\n'
        '                "trace_none": [],\n'
        '                "trace_none_global": [],\n'
        '                "checks": [],\n'
        '                "notes": []\n'
        "              }\n"
        "            }\n"
        "          ]\n"
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Only include system_names/property_ids/role_names that are already canonical in the HLR.\n"
        "Only include behaviors when the HLR plus prompt make the verification flow concrete enough to execute.\n"
        "If you do not know the exact browser control mapping for a behavior, leave behaviors empty for that feature and let deterministic fallback supply it.\n"
        "At least one feature should exist for each core mechanic needed for basic play.\n"
        "If the genre expectation hints below name a mechanic that is missing from the HLR, include it anyway as a 'genre_expectation' feature so coverage can mark it unresolved upstream.\n\n"
        f"Genre expectation hints for {hlr.genre or 'unknown genre'}:\n"
        f"{expectations_prompt_text(hlr.genre) or '(none)'}\n\n"
        f"Context:\n{json.dumps(context, indent=2)}"
    )

    call_id: str | None = None
    try:
        if trace is not None:
            call_id = trace.llm_start("mechanic_coverage", "feature_manifest", type(caller).__name__, len(user_prompt))
        raw = await caller(system_prompt, user_prompt, json_mode=True, label="coverage_manifest")
        if trace is not None and call_id:
            trace.llm_end(call_id, output_chars=len(raw))
        parsed = parse_json_response(raw)
        raw_features = parsed.get("features", parsed if isinstance(parsed, list) else [])
        features = [MechanicFeature.model_validate(item) for item in raw_features]
        return MechanicManifest(
            game_name=hlr.game_name,
            prompt=prompt,
            features=_merge_with_system_backbone(prompt, hlr, features),
            generated_by="llm+deterministic_merge",
        )
    except Exception as exc:
        if trace is not None and call_id:
            trace.llm_end(call_id, output_chars=0, error=str(exc))
        return fallback


def load_mechanic_manifest(path: Path) -> MechanicManifest | None:
    if not path.exists():
        return None
    try:
        manifest = MechanicManifest.model_validate_json(path.read_text(encoding="utf-8"))
        manifest.features = _attach_default_behaviors(list(manifest.features))
        return manifest
    except Exception:
        return None


def write_mechanic_artifact(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _text_blob(parts: list[str]) -> str:
    return " \n ".join(part for part in parts if part).lower()


def _keyword_matches(haystack: str, needles: list[str]) -> list[str]:
    matches: list[str] = []
    for needle in _unique(needles):
        lowered = needle.lower().strip()
        if len(lowered) < 3:
            continue
        if lowered in haystack:
            matches.append(needle)
    return matches


def _feature_result(
    feature: MechanicFeature,
    *,
    status: str,
    evidence: list[str],
    issues: list[str],
    matched_signals: dict[str, list[str]],
) -> FeatureCoverageResult:
    return FeatureCoverageResult(
        feature_id=feature.id,
        feature_name=feature.name,
        required_for_basic_play=feature.required_for_basic_play,
        status=status,
        evidence=_unique(evidence),
        issues=_unique(issues),
        matched_signals={key: _unique(value) for key, value in matched_signals.items() if value},
    )


def _coverage_status_for_feature(
    feature: MechanicFeature,
    *,
    system_hits: list[str],
    property_hits: list[str],
    keyword_hits: list[str],
    role_hits: list[str],
    scene_hits: list[str],
    issues: list[str],
) -> str:
    strong = bool(system_hits or property_hits)
    support = len(role_hits) + len(scene_hits) + len(keyword_hits)
    if feature.source == "genre_expectation":
        authoritative = len(system_hits) + len(property_hits) + len(keyword_hits)
        if (strong or authoritative >= 2) and not issues:
            return "covered"
        if authoritative >= 1 or role_hits or scene_hits:
            return "partial"
        return "missing"
    return "covered" if strong or support >= 2 else "partial" if support >= 1 else "missing"


def _report(stage: str, manifest: MechanicManifest, results: list[FeatureCoverageResult], *, notes: list[str] | None = None) -> MechanicCoverageReport:
    actionable_required = [
        (feature, result)
        for feature, result in zip(manifest.features, results, strict=False)
        if feature.required_for_basic_play and _feature_is_stage_actionable(feature, stage)
    ]
    summary = {
        "features_total": len(results),
        "covered": sum(1 for result in results if result.status == "covered"),
        "partial": sum(1 for result in results if result.status == "partial"),
        "missing": sum(1 for result in results if result.status == "missing"),
        "required_total": len(actionable_required),
        "required_missing": sum(1 for _, result in actionable_required if result.status != "covered"),
    }
    blockers = [
        f"{result.feature_name}: {result.issues[0] if result.issues else result.status}"
        for feature, result in actionable_required
        if result.status != "covered"
    ]
    return MechanicCoverageReport(
        game_name=manifest.game_name,
        stage=stage,
        prompt=manifest.prompt,
        generated_by=manifest.generated_by,
        summary=summary,
        blockers=blockers,
        features=list(manifest.features),
        results=results,
        notes=list(notes or []),
    )


def _owner_role(owner: str) -> str:
    return owner.split(".", 1)[0]


_ROLE_FAMILIES: tuple[set[str], ...] = (
    {"hud", "hud_bar", "hud_meter", "hud_text", "hud_label", "hud_counter", "hud_widget"},
    {"character", "fighter", "kart", "racer", "vehicle", "driver", "actor", "player", "unit", "avatar"},
    {"stage", "background", "track", "arena", "course", "map"},
    {"item", "pickup", "powerup", "collectible"},
)


def _property_owner_root(prop_id: str) -> str:
    return prop_id.split(".", 1)[0] if "." in prop_id else prop_id


def _property_name(prop_id: str) -> str:
    return prop_id.rsplit(".", 1)[-1]


def _role_equivalents(role_name: str) -> set[str]:
    clean = str(role_name or "").strip().lower()
    if not clean:
        return set()
    matches = {clean}
    for family in _ROLE_FAMILIES:
        if clean in family or any(clean.startswith(prefix) for prefix in family if prefix.startswith("hud")):
            matches.update(family)
    if clean.startswith("hud"):
        matches.update(next((family for family in _ROLE_FAMILIES if "hud" in family), set()))
    return matches


def _find_equivalent_properties(expected_prop_id: str, actual_property_ids: set[str]) -> list[str]:
    expected = str(expected_prop_id or "").strip()
    if not expected:
        return []
    if expected in actual_property_ids:
        return [expected]
    expected_name = _property_name(expected).lower()
    expected_owner = _property_owner_root(expected)
    equivalent_roles = _role_equivalents(expected_owner)
    matches = [
        prop_id
        for prop_id in actual_property_ids
        if _property_name(prop_id).lower() == expected_name
        and _property_owner_root(prop_id).lower() in equivalent_roles
    ]
    return _unique(matches)


def _resolve_property_hits(
    expected_property_ids: list[str],
    actual_property_ids: set[str],
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    matched_actual: list[str] = []
    missing_expected: list[str] = []
    provenance: dict[str, list[str]] = {}
    for expected in expected_property_ids:
        matches = _find_equivalent_properties(expected, actual_property_ids)
        if matches:
            provenance[expected] = matches
            matched_actual.extend(matches)
        else:
            missing_expected.append(expected)
    return _unique(matched_actual), missing_expected, provenance


def _feature_is_stage_actionable(feature: MechanicFeature, stage: str) -> bool:
    if stage == "hlr":
        return True
    if stage in {"mlr", "dlr"}:
        return bool(feature.signals.system_names or feature.signals.property_ids)
    if stage == "build":
        return bool(
            feature.signals.system_names
            or feature.signals.role_names
            or feature.signals.scene_names
            or feature.signals.property_ids
        )
    if stage in {"test_plan", "test_results"}:
        return bool(
            feature.signals.trace_keywords
            or feature.signals.test_keywords
            or feature.signals.keywords
            or feature.signals.system_names
            or feature.behaviors
        )
    return True


def audit_hlr_coverage(manifest: MechanicManifest, hlr: GameIdentity) -> MechanicCoverageReport:
    systems, descriptions, _origins = _enum_meta(hlr, "game_systems")
    scenes = [scene.scene_name for scene in _flatten_scenes(hlr.scenes)]
    roles = _hlr_runtime_roles(hlr)
    property_ids = _hlr_property_ids(hlr)
    hlr_text = _text_blob(
        list(hlr.global_rules or [])
        + [hlr.win_condition or ""]
        + scenes
        + systems
        + list(descriptions.values())
        + [spec.summary for spec in hlr.mechanic_specs]
        + [interaction.trigger for spec in hlr.mechanic_specs for interaction in spec.interactions]
    )

    results: list[FeatureCoverageResult] = []
    for feature in manifest.features:
        matched: dict[str, list[str]] = {}
        evidence: list[str] = []
        issues: list[str] = []

        system_hits = [name for name in feature.signals.system_names if name in systems]
        if system_hits:
            matched["system_names"] = system_hits
            evidence.append(f"HLR systems: {', '.join(system_hits)}")
        elif feature.signals.system_names:
            issues.append(f"missing canonical HLR system(s): {', '.join(feature.signals.system_names)}")

        role_hits = [name for name in feature.signals.role_names if name in roles]
        if role_hits:
            matched["role_names"] = role_hits
            evidence.append(f"HLR roles: {', '.join(role_hits)}")

        property_hits = [prop_id for prop_id in feature.signals.property_ids if prop_id in property_ids]
        if property_hits:
            matched["property_ids"] = property_hits
            evidence.append(f"HLR properties: {', '.join(property_hits[:6])}")

        scene_hits = [scene_name for scene_name in feature.signals.scene_names if scene_name in scenes]
        if scene_hits:
            matched["scene_names"] = scene_hits
            evidence.append(f"HLR scenes: {', '.join(scene_hits)}")

        keyword_hits = _keyword_matches(hlr_text, feature.signals.keywords)
        if keyword_hits:
            matched["keywords"] = keyword_hits
            evidence.append(f"HLR text mentions: {', '.join(keyword_hits[:6])}")

        status = _coverage_status_for_feature(
            feature,
            system_hits=system_hits,
            property_hits=property_hits,
            keyword_hits=keyword_hits,
            role_hits=role_hits,
            scene_hits=scene_hits,
            issues=issues,
        )
        if feature.required_for_basic_play and status != "covered" and not issues:
            issues.append("required mechanic is not authoritatively represented in HLR")
        results.append(_feature_result(feature, status=status, evidence=evidence, issues=issues, matched_signals=matched))

    return _report("hlr", manifest, results)


def audit_mlr_coverage(manifest: MechanicManifest, imap: ImpactMap) -> MechanicCoverageReport:
    systems = set(imap.systems)
    scenes = set(imap.scenes)
    roles = {_owner_role(node.owner) for node in imap.nodes.values()}
    property_ids = set(imap.nodes.keys())
    audit_text = _text_blob(imap.audit + [node.description for node in imap.nodes.values()] + [edge.trigger for edge in imap.write_edges])

    results: list[FeatureCoverageResult] = []
    for feature in manifest.features:
        matched: dict[str, list[str]] = {}
        evidence: list[str] = []
        issues: list[str] = []

        system_hits = [name for name in feature.signals.system_names if name in systems]
        if system_hits:
            matched["system_names"] = system_hits
            for system_name in system_hits:
                system_slice = imap.slice_for_system(system_name)
                own_writes = list(system_slice.get("own_writes", []))
                own_reads = list(system_slice.get("own_reads", []))
                if own_writes or own_reads:
                    evidence.append(
                        f"MLR system '{system_name}' has {len(own_writes)} write(s) and {len(own_reads)} read(s)"
                    )
                else:
                    issues.append(f"system '{system_name}' is in scope but has no read/write coverage")
        elif feature.signals.system_names:
            issues.append(f"missing MLR system(s): {', '.join(feature.signals.system_names)}")

        role_hits = [role_name for role_name in feature.signals.role_names if role_name in roles]
        if role_hits:
            matched["role_names"] = role_hits
            evidence.append(f"MLR roles: {', '.join(role_hits)}")

        property_hits, missing_properties, property_provenance = _resolve_property_hits(
            feature.signals.property_ids,
            property_ids,
        )
        if property_hits:
            matched["property_ids"] = property_hits
            evidence.append(f"MLR properties: {', '.join(property_hits[:6])}")
            for prop_id in property_hits:
                writers = imap.writers_of(prop_id)
                readers = imap.readers_of(prop_id)
                if not writers and not readers:
                    issues.append(f"property '{prop_id}' exists but has no writer/reader edges")
            alias_notes = [
                f"{expected} -> {', '.join(matches[:4])}"
                for expected, matches in property_provenance.items()
                if expected not in matches
            ]
            if alias_notes:
                evidence.append(f"MLR canonicalized properties: {'; '.join(alias_notes[:4])}")
        if missing_properties:
            issues.append(f"missing MLR properties: {', '.join(missing_properties[:6])}")

        scene_hits = [scene_name for scene_name in feature.signals.scene_names if scene_name in scenes]
        if scene_hits:
            matched["scene_names"] = scene_hits
            evidence.append(f"MLR scenes: {', '.join(scene_hits)}")

        keyword_hits = _keyword_matches(audit_text, feature.signals.keywords)
        if keyword_hits:
            matched["keywords"] = keyword_hits
            evidence.append(f"MLR audit mentions: {', '.join(keyword_hits[:6])}")

        status = _coverage_status_for_feature(
            feature,
            system_hits=system_hits,
            property_hits=property_hits,
            keyword_hits=keyword_hits,
            role_hits=role_hits,
            scene_hits=scene_hits,
            issues=issues,
        )
        if feature.required_for_basic_play and status != "covered" and not issues:
            issues.append("required mechanic lacks authoritative MLR structure")
        results.append(_feature_result(feature, status=status, evidence=evidence, issues=issues, matched_signals=matched))

    return _report("mlr", manifest, results)


def audit_dlr_coverage(
    manifest: MechanicManifest,
    imap: ImpactMap,
    constants: dict[str, Any] | None = None,
) -> MechanicCoverageReport:
    systems = set(imap.systems)
    roles = {_owner_role(node.owner) for node in imap.nodes.values()}
    property_ids = set(imap.nodes.keys())
    unfilled_nodes = {node.id for node in imap.unfilled_nodes()}
    unfilled_edges = {(edge.system, edge.target) for edge in imap.unfilled_write_edges()}
    constant_text = _text_blob([json.dumps(constants or {}, indent=2)])

    results: list[FeatureCoverageResult] = []
    for feature in manifest.features:
        matched: dict[str, list[str]] = {}
        evidence: list[str] = []
        issues: list[str] = []

        system_hits = [name for name in feature.signals.system_names if name in systems]
        if system_hits:
            matched["system_names"] = system_hits
            for system_name in system_hits:
                system_slice = imap.slice_for_system(system_name)
                targets = [edge["target"] for edge in system_slice.get("own_writes", [])]
                sources = [edge["source"] for edge in system_slice.get("own_reads", [])]
                touched = _unique(targets + sources)
                if touched:
                    evidence.append(f"DLR system '{system_name}' touches {len(touched)} property(s)")
                else:
                    issues.append(f"system '{system_name}' has no DLR-touched properties")
                edge_gaps = [target for target in targets if (system_name, target) in unfilled_edges]
                if edge_gaps:
                    issues.append(f"system '{system_name}' has unfilled write formulas: {', '.join(edge_gaps[:6])}")
        elif feature.signals.system_names:
            issues.append(f"missing DLR system(s): {', '.join(feature.signals.system_names)}")

        role_hits = [role_name for role_name in feature.signals.role_names if role_name in roles]
        if role_hits:
            matched["role_names"] = role_hits
            evidence.append(f"DLR roles: {', '.join(role_hits)}")

        property_hits, missing_properties, property_provenance = _resolve_property_hits(
            feature.signals.property_ids,
            property_ids,
        )
        if property_hits:
            matched["property_ids"] = property_hits
            evidence.append(f"DLR properties: {', '.join(property_hits[:6])}")
            missing_fill = [prop_id for prop_id in property_hits if prop_id in unfilled_nodes]
            if missing_fill:
                issues.append(f"unfilled DLR properties: {', '.join(missing_fill[:6])}")
            alias_notes = [
                f"{expected} -> {', '.join(matches[:4])}"
                for expected, matches in property_provenance.items()
                if expected not in matches
            ]
            if alias_notes:
                evidence.append(f"DLR canonicalized properties: {'; '.join(alias_notes[:4])}")
        if missing_properties:
            issues.append(f"missing DLR properties: {', '.join(missing_properties[:6])}")

        keyword_hits = _keyword_matches(constant_text, feature.signals.keywords)
        if keyword_hits:
            matched["keywords"] = keyword_hits
            evidence.append(f"DLR constants mention: {', '.join(keyword_hits[:6])}")

        status = _coverage_status_for_feature(
            feature,
            system_hits=system_hits,
            property_hits=property_hits,
            keyword_hits=keyword_hits,
            role_hits=role_hits,
            scene_hits=[],
            issues=issues,
        )
        if feature.required_for_basic_play and status != "covered" and not issues:
            issues.append("required mechanic is not fully filled at DLR")
        results.append(_feature_result(feature, status=status, evidence=evidence, issues=issues, matched_signals=matched))

    return _report("dlr", manifest, results)


def audit_build_coverage(
    manifest: MechanicManifest,
    contract: BuildContract,
    codegen_manifest: list[dict[str, Any]] | None = None,
    *,
    exported: bool = False,
    export_path: Path | None = None,
) -> MechanicCoverageReport:
    codegen_entries = list(codegen_manifest or [])
    systems_generated = {str(entry.get("system") or ""): entry for entry in codegen_entries}
    roles = set(contract.roles.keys())
    scenes = set(contract.scenes)
    property_alias_keys = set(contract.property_aliases.keys()) | set(contract.property_aliases.values())

    results: list[FeatureCoverageResult] = []
    for feature in manifest.features:
        matched: dict[str, list[str]] = {}
        evidence: list[str] = []
        issues: list[str] = []

        system_hits = [name for name in feature.signals.system_names if name in contract.systems]
        if system_hits:
            matched["system_names"] = system_hits
            for system_name in system_hits:
                entry = systems_generated.get(system_name)
                if entry is None:
                    issues.append(f"no generated build artifact for system '{system_name}'")
                    continue
                strategy = str(entry.get("strategy") or "")
                if strategy == "FAILED":
                    issues.append(f"system '{system_name}' codegen failed")
                    continue
                evidence.append(f"build generated '{system_name}' via {strategy or 'unknown'}")
        elif feature.signals.system_names:
            issues.append(f"missing build-contract system(s): {', '.join(feature.signals.system_names)}")

        role_hits = [role_name for role_name in feature.signals.role_names if role_name in roles]
        scene_root_hits: list[str] = []
        if "game" in feature.signals.role_names and (
            system_hits
            or any(prop_id.startswith("game.") for prop_id in feature.signals.property_ids)
        ):
            scene_root_hits.append("game")
            evidence.append("build scene-root state satisfies role 'game'")
        combined_role_hits = _unique(role_hits + scene_root_hits)
        if combined_role_hits:
            matched["role_names"] = combined_role_hits
            evidence.append(f"build roles: {', '.join(combined_role_hits)}")
        elif feature.signals.role_names and feature.signals.system_names:
            issues.append(f"missing build role(s): {', '.join(feature.signals.role_names)}")

        property_hits = [
            prop_id
            for prop_id in feature.signals.property_ids
            if (
                prop_id in property_alias_keys
                or prop_id.startswith("game.")
                or prop_id.split(".", 1)[0] in roles
            )
        ]
        if property_hits:
            matched["property_ids"] = property_hits
            evidence.append(f"build properties: {', '.join(property_hits[:6])}")

        scene_hits = [scene_name for scene_name in feature.signals.scene_names if scene_name in scenes]
        if scene_hits:
            matched["scene_names"] = scene_hits
            evidence.append(f"build scenes: {', '.join(scene_hits)}")

        if exported and export_path and export_path.exists():
            evidence.append(f"export exists: {export_path}")
        elif feature.required_for_basic_play:
            issues.append("web export missing")

        build_system_names = list(contract.systems) if isinstance(contract.systems, list) else list(contract.systems.keys())
        build_text = _text_blob(
            build_system_names
            + list(contract.roles.keys())
            + list(contract.scenes)
            + list(contract.property_aliases.keys())
            + list(contract.property_aliases.values())
        )
        keyword_hits = _keyword_matches(build_text, feature.signals.keywords)
        if keyword_hits:
            matched["keywords"] = keyword_hits
            evidence.append(f"build keywords: {', '.join(keyword_hits[:6])}")
        synthetic_property_hits = list(property_hits)
        if (
            not synthetic_property_hits
            and scene_hits
            and not feature.signals.system_names
            and not feature.signals.role_names
            and not feature.signals.property_ids
        ):
            synthetic_property_hits = ["scene_only_projection"]
        status = _coverage_status_for_feature(
            feature,
            system_hits=system_hits,
            property_hits=synthetic_property_hits,
            keyword_hits=keyword_hits,
            role_hits=combined_role_hits,
            scene_hits=scene_hits,
            issues=issues,
        )
        if feature.required_for_basic_play and status != "covered" and not issues:
            issues.append("required mechanic did not survive build/export cleanly")
        results.append(_feature_result(feature, status=status, evidence=evidence, issues=issues, matched_signals=matched))

    notes: list[str] = []
    if codegen_entries:
        failed = [str(entry.get("system")) for entry in codegen_entries if str(entry.get("strategy")) == "FAILED"]
        if failed:
            notes.append(f"Failed systems: {', '.join(failed)}")
    return _report("build", manifest, results, notes=notes)


def audit_test_plan_coverage(manifest: MechanicManifest, steps: list[dict[str, Any]]) -> MechanicCoverageReport:
    results: list[FeatureCoverageResult] = []
    for feature in manifest.features:
        matched, evidence, issues, status = _audit_test_like_stage(feature, steps, require_pass=False)
        results.append(_feature_result(feature, status=status, evidence=evidence, issues=issues, matched_signals=matched))
    return _report("test_plan", manifest, results)


def audit_test_results_coverage(
    manifest: MechanicManifest,
    steps: list[dict[str, Any]],
    results_data: list[dict[str, Any]],
) -> MechanicCoverageReport:
    joined = _merge_step_results(steps, results_data)
    results: list[FeatureCoverageResult] = []
    for feature in manifest.features:
        matched, evidence, issues, status = _audit_test_like_stage(feature, joined, require_pass=True)
        results.append(_feature_result(feature, status=status, evidence=evidence, issues=issues, matched_signals=matched))
    return _report("test_results", manifest, results)


def _merge_step_results(steps: list[dict[str, Any]], results_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_action: dict[str, list[dict[str, Any]]] = {}
    for result in results_data:
        action = str(result.get("action") or "")
        by_action.setdefault(action, []).append(result)
    for step in steps:
        action = str(step.get("action") or "")
        candidates = by_action.get(action, [])
        result = candidates.pop(0) if candidates else {}
        merged.append({**step, **result})
    return merged


def _audit_test_like_stage(
    feature: MechanicFeature,
    rows: list[dict[str, Any]],
    *,
    require_pass: bool,
) -> tuple[dict[str, list[str]], list[str], list[str], str]:
    matched: dict[str, list[str]] = {}
    evidence: list[str] = []
    issues: list[str] = []
    matching_rows: list[dict[str, Any]] = []
    behavior_needles = feature_test_needles(feature)

    for row in rows:
        row_text = _text_blob(
            [
                str(row.get("action") or ""),
                str(row.get("description") or ""),
                str(row.get("keys") or ""),
                json.dumps(row.get("trace_any") or []),
                json.dumps(row.get("trace_all") or []),
                json.dumps(row.get("trace_any_global") or []),
                json.dumps(row.get("checks") or []),
                str(row.get("notes") or ""),
            ]
        )
        matched_here = False
        trace_hits = _keyword_matches(row_text, feature.signals.trace_keywords)
        test_hits = _keyword_matches(row_text, feature.signals.test_keywords)
        keyword_hits = _keyword_matches(row_text, feature.signals.keywords)
        system_hits = _keyword_matches(row_text, feature.signals.system_names)
        behavior_hits = _keyword_matches(row_text, behavior_needles)
        if trace_hits:
            matched.setdefault("trace_keywords", []).extend(trace_hits)
            matched_here = True
        if test_hits:
            matched.setdefault("test_keywords", []).extend(test_hits)
            matched_here = True
        if keyword_hits:
            matched.setdefault("keywords", []).extend(keyword_hits)
            matched_here = True
        if system_hits:
            matched.setdefault("system_names", []).extend(system_hits)
            matched_here = True
        if behavior_hits:
            matched.setdefault("behavior_keywords", []).extend(behavior_hits)
            matched_here = True
        if matched_here:
            matching_rows.append(row)

    if matching_rows:
        evidence.append(f"matched {len(matching_rows)} test step(s)")
        if require_pass:
            passed = [row for row in matching_rows if bool(row.get("passed"))]
            failed = [row for row in matching_rows if row.get("passed") is False]
            if passed:
                evidence.append(f"{len(passed)} matching step(s) passed")
            if failed:
                issues.append(f"{len(failed)} matching step(s) failed")
            status = "covered" if passed and not failed else "partial" if passed else "missing"
        else:
            status = "covered"
    else:
        status = "missing"
        issues.append("no matching Playwright coverage step found")

    if feature.required_for_basic_play and status != "covered" and not issues:
        issues.append("required mechanic is not covered by Playwright verification")
    return matched, evidence, issues, status
