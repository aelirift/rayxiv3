"""Deterministic mechanic-behavior fallbacks for coverage and browser tests."""

from __future__ import annotations

import json
from typing import Any

from rayxi.spec.mechanic_contract import (
    MechanicBehavior,
    MechanicFeature,
    MechanicTestAction,
    MechanicVerification,
)


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


def _slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in (text or "").lower()).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "behavior"


def _text_blob(parts: list[str]) -> str:
    return " \n ".join(part for part in parts if part).lower()


def _feature_token_blob(feature: MechanicFeature) -> str:
    return _text_blob(
        [
            feature.id,
            feature.name,
            feature.summary,
            *feature.signals.system_names,
            *feature.signals.role_names,
            *feature.signals.property_ids,
            *feature.signals.scene_names,
            *feature.signals.keywords,
        ]
    )


def _has_any_token(feature: MechanicFeature, *tokens: str) -> bool:
    blob = _feature_token_blob(feature)
    return any((token or "").lower() in blob for token in tokens if token)


def _has_role_token(feature: MechanicFeature, *tokens: str) -> bool:
    roles_blob = " ".join(feature.signals.role_names).lower()
    return any((token or "").lower() in roles_blob for token in tokens if token)


def _step(
    action: str,
    *,
    description: str,
    keys: str | None = None,
    method: str = "press",
    wait_ms: int = 600,
    hold_ms: int = 0,
    verify_change: bool = False,
    diff_threshold: float | None = None,
    navigate_query: str | None = None,
    url_query: str | None = None,
    sequence: list[dict[str, Any]] | None = None,
    verification: MechanicVerification | None = None,
) -> MechanicTestAction:
    return MechanicTestAction(
        action=action,
        description=description,
        keys=keys,
        method=method,
        wait_ms=wait_ms,
        hold_ms=hold_ms,
        verify_change=verify_change,
        diff_threshold=diff_threshold,
        navigate_query=navigate_query,
        url_query=url_query,
        sequence=list(sequence or []),
        verification=verification or MechanicVerification(),
    )


def _merge_verification(a: MechanicVerification, b: MechanicVerification) -> MechanicVerification:
    return MechanicVerification(
        trace_any=_unique(a.trace_any + b.trace_any),
        trace_all=_unique(a.trace_all + b.trace_all),
        trace_any_global=_unique(a.trace_any_global + b.trace_any_global),
        trace_all_global=_unique(a.trace_all_global + b.trace_all_global),
        trace_none=_unique(a.trace_none + b.trace_none),
        trace_none_global=_unique(a.trace_none_global + b.trace_none_global),
        checks=_unique(a.checks + b.checks),
        notes=_unique(a.notes + b.notes),
    )


def _merge_behavior_actions(actions: list[MechanicTestAction]) -> list[MechanicTestAction]:
    merged: dict[str, MechanicTestAction] = {}
    order: list[str] = []
    for action in actions:
        key = "|".join(
            [
                action.action.lower(),
                str(action.keys or ""),
                action.method,
                str(action.navigate_query or ""),
                str(action.url_query or ""),
                json.dumps(action.sequence, sort_keys=True),
            ]
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = action
            order.append(key)
            continue
        existing.description = existing.description or action.description
        existing.wait_ms = max(existing.wait_ms, action.wait_ms)
        existing.hold_ms = max(existing.hold_ms, action.hold_ms)
        existing.verify_change = existing.verify_change or action.verify_change
        if existing.diff_threshold is None:
            existing.diff_threshold = action.diff_threshold
        elif action.diff_threshold is not None:
            existing.diff_threshold = min(existing.diff_threshold, action.diff_threshold)
        if not existing.sequence and action.sequence:
            existing.sequence = list(action.sequence)
        existing.verification = _merge_verification(existing.verification, action.verification)
    return [merged[key] for key in order]


def merge_behaviors(behaviors: list[MechanicBehavior]) -> list[MechanicBehavior]:
    merged: dict[str, MechanicBehavior] = {}
    for behavior in behaviors:
        behavior_id = _slug(behavior.id or behavior.name)
        behavior.id = behavior_id
        existing = merged.get(behavior_id)
        if existing is None:
            behavior.actions = _merge_behavior_actions(list(behavior.actions))
            merged[behavior_id] = behavior
            continue
        existing.summary = existing.summary or behavior.summary
        existing.required_for_basic_play = existing.required_for_basic_play or behavior.required_for_basic_play
        existing.priority = min(existing.priority, behavior.priority)
        existing.source = existing.source if existing.source != "deterministic_fallback" else behavior.source
        existing.system_names = _unique(existing.system_names + behavior.system_names)
        existing.role_names = _unique(existing.role_names + behavior.role_names)
        existing.property_ids = _unique(existing.property_ids + behavior.property_ids)
        existing.scene_names = _unique(existing.scene_names + behavior.scene_names)
        existing.preconditions = _unique(existing.preconditions + behavior.preconditions)
        existing.actions = _merge_behavior_actions(existing.actions + behavior.actions)
    return list(merged.values())


def _combat_baseline_behavior(feature: MechanicFeature) -> MechanicBehavior:
    scene_name = feature.signals.scene_names[0] if feature.signals.scene_names else "gameplay"
    return MechanicBehavior(
        id="combat_baseline",
        name="Combat Baseline",
        summary="Initial gameplay frame shows both combat actors, overlays, and grounded placement.",
        priority=10,
        source="deterministic_fallback",
        required_for_basic_play=True,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                f"[{scene_name}] Initial state (baseline)",
                description="Capture gameplay scene on first entry and establish combat baselines.",
                wait_ms=500,
                verification=MechanicVerification(
                    checks=[
                        "two_fighters_visible",
                        "fighters_grounded",
                        "capture_baseline_entity_count",
                        "no_white_halo",
                        "no_fighter_overlap",
                    ],
                    trace_any_global=["hud.widget_ready name=rayxi_duel_status"],
                ),
            )
        ],
    )


def _combat_movement_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="combat_movement",
        name="Combat Movement",
        summary="Walk, jump, and crouch work for the primary combat actor.",
        priority=20,
        source="deterministic_fallback",
        required_for_basic_play=True,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Walk right",
                keys="d",
                method="hold",
                hold_ms=500,
                wait_ms=250,
                verify_change=True,
                diff_threshold=1.0,
                description="Hold D to move the primary combat actor right.",
                verification=MechanicVerification(
                    checks=["two_fighters_visible", "fighters_grounded", "no_fighter_overlap"],
                    trace_any=["to=walk_forward"],
                ),
            ),
            _step(
                "Walk left",
                keys="a",
                method="hold",
                hold_ms=500,
                wait_ms=250,
                verify_change=True,
                diff_threshold=1.0,
                description="Hold A to move the primary combat actor left.",
                verification=MechanicVerification(
                    checks=["two_fighters_visible", "fighters_grounded", "no_fighter_overlap"],
                    trace_any=["to=walk_back"],
                ),
            ),
            _step(
                "Jump",
                keys="w",
                wait_ms=1200,
                description="Press W to trigger a jump.",
                verification=MechanicVerification(
                    trace_any=["movement.jump", "to=jump_neutral", "to=jump_forward", "to=jump_back"]
                ),
            ),
            _step(
                "Crouch",
                keys="s",
                method="hold",
                hold_ms=600,
                wait_ms=400,
                description="Hold S to trigger crouch.",
                verification=MechanicVerification(trace_any=["input.crouch_change", "to=crouch"]),
            ),
        ],
    )


def _combat_normals_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="combat_normals",
        name="Combat Normals",
        summary="Ground normals can be executed for the primary combat actor.",
        priority=30,
        source="deterministic_fallback",
        required_for_basic_play=True,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Light punch",
                keys="u",
                wait_ms=600,
                description="Press U for the primary light strike.",
                verification=MechanicVerification(checks=["no_white_halo"], trace_any=["to=light_punch"]),
            ),
            _step(
                "Heavy punch",
                keys="o",
                wait_ms=800,
                description="Press O for the primary heavy strike.",
                verification=MechanicVerification(checks=["no_white_halo"], trace_any=["to=heavy_punch"]),
            ),
            _step(
                "Light kick",
                keys="j",
                wait_ms=600,
                description="Press J for the secondary light strike.",
                verification=MechanicVerification(trace_any=["to=light_kick"]),
            ),
            _step(
                "Heavy kick",
                keys="l",
                wait_ms=800,
                description="Press L for the secondary heavy strike.",
                verification=MechanicVerification(trace_any=["to=heavy_kick"]),
            ),
            _step(
                "Crouch light punch",
                keys="s+u",
                method="sequence",
                wait_ms=0,
                description="Hold down and press U for a low punch.",
                sequence=[
                    {"type": "down", "key": "s"},
                    {"type": "wait", "ms": 80},
                    {"type": "down", "key": "u"},
                    {"type": "wait", "ms": 90},
                    {"type": "up", "key": "u"},
                    {"type": "wait", "ms": 260},
                    {"type": "up", "key": "s"},
                    {"type": "wait", "ms": 180},
                ],
                verification=MechanicVerification(trace_any=["to=crouch_light_punch"]),
            ),
            _step(
                "Crouch heavy kick",
                keys="s+l",
                method="sequence",
                wait_ms=0,
                description="Hold down and press L for a low kick.",
                sequence=[
                    {"type": "down", "key": "s"},
                    {"type": "wait", "ms": 80},
                    {"type": "down", "key": "l"},
                    {"type": "wait", "ms": 110},
                    {"type": "up", "key": "l"},
                    {"type": "wait", "ms": 320},
                    {"type": "up", "key": "s"},
                    {"type": "wait", "ms": 200},
                ],
                verification=MechanicVerification(trace_any=["to=crouch_heavy_kick"]),
            ),
        ],
    )


def _combat_projectile_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="combat_projectile_special",
        name="Projectile Special",
        summary="Quarter-circle projectile special spawns a visible projectile entity.",
        priority=40,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload special-ready mode",
                navigate_query="rayxi_test_mode=projectile_ready",
                wait_ms=1200,
                description="Reload a farther dummy spacing before projectile checks.",
                verification=MechanicVerification(trace_any=["test.mode mode=projectile_ready"]),
            ),
            _step(
                "Click canvas to focus (special ready)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after special-ready reload.",
            ),
            _step(
                "Pre-projectile-special baseline",
                wait_ms=500,
                description="Recapture entity-count baseline immediately before the projectile special.",
                verification=MechanicVerification(checks=["capture_baseline_entity_count"]),
            ),
            _step(
                "Projectile special sequence",
                keys="s+d,u",
                method="sequence",
                wait_ms=0,
                description="Quarter-circle forward plus punch should spawn a projectile.",
                sequence=[
                    {"type": "down", "key": "s"},
                    {"type": "wait", "ms": 45},
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 45},
                    {"type": "up", "key": "s"},
                    {"type": "wait", "ms": 25},
                    {"type": "down", "key": "u"},
                    {"type": "wait", "ms": 60},
                    {"type": "up", "key": "u"},
                    {"type": "wait", "ms": 80},
                    {"type": "up", "key": "d"},
                    {"type": "wait", "ms": 40},
                ],
                verification=MechanicVerification(
                    checks=["projectile_visible"],
                    trace_all=["input.special_detected", "projectile.spawn"],
                ),
            ),
        ],
    )


def _combat_uppercut_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="combat_rising_special",
        name="Rising Special",
        summary="Dragon-punch style motion executes and connects against the opponent.",
        priority=45,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload uppercut-ready mode",
                navigate_query="rayxi_test_mode=uppercut_ready",
                wait_ms=1200,
                description="Reload a closer dummy spacing before rising-special verification.",
                verification=MechanicVerification(trace_any=["test.mode mode=uppercut_ready"]),
            ),
            _step(
                "Click canvas to focus (uppercut ready)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after uppercut-ready reload.",
            ),
            _step(
                "Uppercut special sequence",
                keys="d,s,d+u",
                method="sequence",
                wait_ms=0,
                description="Forward, down, down-forward plus punch should trigger the rising special move.",
                sequence=[
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 45},
                    {"type": "up", "key": "d"},
                    {"type": "wait", "ms": 35},
                    {"type": "down", "key": "s"},
                    {"type": "wait", "ms": 45},
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 45},
                    {"type": "down", "key": "u"},
                    {"type": "wait", "ms": 60},
                    {"type": "up", "key": "u"},
                    {"type": "wait", "ms": 500},
                    {"type": "up", "key": "d"},
                    {"type": "up", "key": "s"},
                    {"type": "wait", "ms": 450},
                ],
                verification=MechanicVerification(
                    trace_any=["movement.special_start"],
                    trace_all_global=[
                        "input.special_detected move=special_uppercut",
                        "special.motion_detected motion=dp",
                        "special.executed fighter=",
                        "combat.hit attacker=p1_fighter defender=p2_fighter move=special_uppercut",
                    ],
                ),
            ),
        ],
    )


def _combat_spinning_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="combat_advancing_special",
        name="Advancing Special",
        summary="Quarter-circle-back kick special executes with visible movement.",
        priority=50,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload spinning-ready mode",
                navigate_query="rayxi_test_mode=dummy",
                wait_ms=1200,
                description="Reload a clean dummy state before advancing-special verification.",
                verification=MechanicVerification(trace_any=["test.mode mode=dummy"]),
            ),
            _step(
                "Click canvas to focus (spinning ready)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after spinning-ready reload.",
            ),
            _step(
                "Spinning special sequence",
                keys="s+a,j",
                method="sequence",
                wait_ms=0,
                description="Quarter-circle back plus kick should trigger the advancing special move.",
                sequence=[
                    {"type": "down", "key": "s"},
                    {"type": "wait", "ms": 45},
                    {"type": "down", "key": "a"},
                    {"type": "wait", "ms": 45},
                    {"type": "up", "key": "s"},
                    {"type": "wait", "ms": 35},
                    {"type": "down", "key": "j"},
                    {"type": "wait", "ms": 60},
                    {"type": "up", "key": "j"},
                    {"type": "wait", "ms": 450},
                    {"type": "up", "key": "a"},
                    {"type": "wait", "ms": 450},
                ],
                verification=MechanicVerification(
                    trace_any=["movement.special_start"],
                    trace_all_global=[
                        "input.special_detected move=special_spinning",
                        "special.motion_detected motion=qcb",
                        "special.executed fighter=",
                    ],
                ),
            ),
        ],
    )


def _combat_blocking_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="combat_blocking",
        name="Combat Blocking",
        summary="High and low guard interactions block the expected attacks.",
        priority=60,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload guard-high mode",
                navigate_query="rayxi_test_mode=guard_high",
                wait_ms=1600,
                description="Reload with a guarding dummy that holds back.",
                verification=MechanicVerification(trace_any=["test.mode mode=guard_high"]),
            ),
            _step(
                "Click canvas to focus (guard high)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after guard-high reload.",
            ),
            _step(
                "Stand block test",
                keys="d,o",
                method="sequence",
                wait_ms=0,
                description="A standing heavy punch against a high-guard dummy should be blocked.",
                sequence=[
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 140},
                    {"type": "up", "key": "d"},
                    {"type": "wait", "ms": 40},
                    {"type": "down", "key": "o"},
                    {"type": "wait", "ms": 80},
                    {"type": "up", "key": "o"},
                    {"type": "wait", "ms": 650},
                ],
                verification=MechanicVerification(trace_all=["combat.blocked"]),
            ),
            _step(
                "Reload guard-low mode",
                navigate_query="rayxi_test_mode=guard_low",
                wait_ms=1600,
                description="Reload with a dummy that crouch-blocks.",
                verification=MechanicVerification(trace_any=["test.mode mode=guard_low"]),
            ),
            _step(
                "Click canvas to focus (guard low)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after guard-low reload.",
            ),
            _step(
                "Crouch block test",
                keys="d,s+l",
                method="sequence",
                wait_ms=0,
                description="A low kick against a crouch-block dummy should be blocked.",
                sequence=[
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 150},
                    {"type": "up", "key": "d"},
                    {"type": "wait", "ms": 30},
                    {"type": "down", "key": "s"},
                    {"type": "wait", "ms": 80},
                    {"type": "down", "key": "l"},
                    {"type": "wait", "ms": 140},
                    {"type": "up", "key": "l"},
                    {"type": "wait", "ms": 420},
                    {"type": "up", "key": "s"},
                    {"type": "wait", "ms": 250},
                ],
                verification=MechanicVerification(trace_all=["combat.blocked"]),
            ),
        ],
    )


def _combat_collision_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="combat_collision_pushout",
        name="Combat Collision Pushout",
        summary="Actors should not overlap through each other when walking into contact.",
        priority=55,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload collision-ready dummy",
                navigate_query="rayxi_test_mode=dummy",
                wait_ms=1200,
                description="Reload a clean dummy state before pushout verification.",
                verification=MechanicVerification(trace_any=["test.mode mode=dummy"]),
            ),
            _step(
                "Click canvas to focus (collision dummy)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after collision-ready reload.",
            ),
            _step(
                "Walk straight at opponent (collision)",
                keys="d",
                method="hold",
                hold_ms=2200,
                wait_ms=400,
                description="Hold D until the lead actor reaches the opponent; collision must prevent walk-through.",
                verification=MechanicVerification(
                    checks=["p1_did_not_walk_through_p2"],
                    trace_any_global=["collision.pushout"],
                ),
            ),
        ],
    )


def _combat_rage_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="resource_stack_meter",
        name="Resource Stack Meter",
        summary="Taking damage fills the meter and a powered special consumes the stored stack.",
        priority=70,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        property_ids=list(feature.signals.property_ids),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload aggressor mode",
                navigate_query="rayxi_test_mode=aggressor",
                wait_ms=900,
                description="Reload with an aggressive opponent so resource gain is deterministic.",
                verification=MechanicVerification(trace_any=["test.mode mode=aggressor"]),
            ),
            _step(
                "Click canvas to focus (aggressor)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after aggressor reload.",
            ),
            _step(
                "Take damage (wait for AI)",
                wait_ms=3000,
                description="Wait for the opponent to land hits so the resource meter visibly charges.",
                verification=MechanicVerification(trace_all=["combat.hit", "rage.fill_progress"]),
            ),
            _step(
                "Reload rage-ready mode",
                navigate_query="rayxi_test_mode=rage_ready",
                wait_ms=1200,
                description="Reload a deterministic powered-special setup with one seeded stack.",
                verification=MechanicVerification(trace_all=["test.mode mode=rage_ready", "rage.test_seed"]),
            ),
            _step(
                "Click canvas to focus (rage ready)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after rage-ready reload.",
            ),
            _step(
                "Fire powered special",
                keys="s+d,u",
                method="sequence",
                wait_ms=0,
                description="Quarter-circle forward plus punch should consume a stack and fire a powered projectile.",
                sequence=[
                    {"type": "down", "key": "s"},
                    {"type": "wait", "ms": 40},
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 40},
                    {"type": "up", "key": "s"},
                    {"type": "wait", "ms": 30},
                    {"type": "down", "key": "u"},
                    {"type": "wait", "ms": 60},
                    {"type": "up", "key": "u"},
                    {"type": "wait", "ms": 80},
                    {"type": "up", "key": "d"},
                    {"type": "wait", "ms": 40},
                ],
                verification=MechanicVerification(
                    trace_all=["rage.stack_consumed", "projectile.spawn"],
                    trace_any_global=["move=special_projectile powered=true", "powered_special=true"],
                ),
            ),
        ],
    )


def _race_rendering_behavior(feature: MechanicFeature) -> MechanicBehavior:
    scene_name = feature.signals.scene_names[0] if feature.signals.scene_names else "gameplay"
    return MechanicBehavior(
        id="race_baseline",
        name="Race Baseline",
        summary="Initial race frame shows a readable race view, track depth, HUD, and debug overlays.",
        priority=10,
        source="deterministic_fallback",
        required_for_basic_play=True,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                f"[{scene_name}] Initial state (baseline)",
                description="Capture gameplay scene on first entry and establish race baselines.",
                wait_ms=500,
                verification=MechanicVerification(
                    checks=[
                        "capture_baseline_entity_count",
                        "race_checkpoint_marker_visible",
                        "race_item_marker_visible",
                        "race_hud_visible",
                    ],
                    trace_all_global=[
                        f"scene.ready scene={scene_name}",
                        "debug.overlay_ready kind=boxes",
                        "debug.overlay_ready kind=log",
                        "stage.track_seeded",
                    ],
                    trace_none_global=["race_progress.finish"],
                ),
            )
        ],
    )


def _race_countdown_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="race_countdown_sequence",
        name="Race Countdown Sequence",
        summary="Countdown runs from 3 to GO before the race unlocks.",
        priority=15,
        source="deterministic_fallback",
        required_for_basic_play=True,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        property_ids=list(feature.signals.property_ids),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Wait for countdown",
                wait_ms=3800,
                verify_change=True,
                diff_threshold=0.25,
                description="Allow the pre-race countdown to progress to GO.",
                verification=MechanicVerification(
                    trace_all_global=[
                        "countdown.start value=3",
                        "countdown.tick value=2",
                        "countdown.tick value=1",
                        "countdown.complete",
                    ]
                ),
            )
        ],
    )


def _race_physics_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="vehicle_physics_controls",
        name="Vehicle Physics Controls",
        summary="Primary vehicle accelerates and steers with visible movement.",
        priority=20,
        source="deterministic_fallback",
        required_for_basic_play=True,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        property_ids=list(feature.signals.property_ids),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Accelerate",
                keys="w",
                method="hold",
                hold_ms=1200,
                wait_ms=250,
                verify_change=True,
                diff_threshold=0.5,
                description="Hold W to accelerate the lead vehicle forward.",
                verification=MechanicVerification(
                    trace_all=["input.update actor=", "physics.update kart="]
                ),
            ),
            _step(
                "Steer right while accelerating",
                keys="w+d",
                method="sequence",
                wait_ms=0,
                verify_change=True,
                diff_threshold=0.5,
                description="Hold W, then add D to verify steering under load.",
                sequence=[
                    {"type": "down", "key": "w"},
                    {"type": "wait", "ms": 350},
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 900},
                    {"type": "up", "key": "d"},
                    {"type": "wait", "ms": 120},
                    {"type": "up", "key": "w"},
                    {"type": "wait", "ms": 220},
                ],
                verification=MechanicVerification(
                    trace_all=["input.update actor=", "physics.turn kart="]
                ),
            ),
        ],
    )


def _race_drift_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="vehicle_drift_boost",
        name="Vehicle Drift Boost",
        summary="Drift charge and release produce a visible boost sequence.",
        priority=30,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        property_ids=list(feature.signals.property_ids),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload drift-ready mode",
                navigate_query="rayxi_test_mode=drift_ready",
                wait_ms=350,
                description="Reload with seeded speed so drift mechanics become testable quickly.",
                verification=MechanicVerification(trace_any=["test.mode mode=drift_ready"]),
            ),
            _step(
                "Click canvas to focus (drift ready)",
                keys="click",
                wait_ms=120,
                description="Refocus canvas after drift-ready reload.",
            ),
            _step(
                "Drift",
                keys="w+d+Shift",
                method="sequence",
                wait_ms=0,
                verify_change=True,
                diff_threshold=0.25,
                description="Accelerate, steer, and hold Shift to trigger drift and boost traces.",
                sequence=[
                    {"type": "down", "key": "w"},
                    {"type": "wait", "ms": 80},
                    {"type": "down", "key": "d"},
                    {"type": "wait", "ms": 80},
                    {"type": "down", "key": "Shift"},
                    {"type": "wait", "ms": 900},
                    {"type": "up", "key": "Shift"},
                    {"type": "wait", "ms": 160},
                    {"type": "up", "key": "d"},
                    {"type": "up", "key": "w"},
                    {"type": "wait", "ms": 260},
                ],
                verification=MechanicVerification(
                    trace_any=["drift_boost.drift_start", "drift_boost.tier_up", "drift_boost.boost_start"]
                ),
            ),
        ],
    )


def _race_item_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="vehicle_item_usage",
        name="Vehicle Item Usage",
        summary="Using a seeded item produces a mechanic-specific trace such as projectile, boost, or shield.",
        priority=40,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        property_ids=list(feature.signals.property_ids),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload item-ready mode",
                navigate_query="rayxi_test_mode=item_ready",
                wait_ms=1200,
                description="Reload with a seeded item so item usage is deterministic.",
                verification=MechanicVerification(trace_any=["test.mode mode=item_ready"]),
            ),
            _step(
                "Click canvas to focus (item ready)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after item-ready reload.",
            ),
            _step(
                "Use item",
                keys="Space",
                wait_ms=900,
                description="Press Space to consume the seeded vehicle item.",
                verification=MechanicVerification(
                    trace_any=[
                        "item.use",
                        "item.spawn_projectile",
                        "item.boost_applied",
                        "item_usage.activate",
                        "item_usage.spawn_shell",
                        "item_usage.spawn_banana",
                        "item_usage.boost_start",
                        "item_usage.invincibility_start",
                    ]
                ),
            ),
        ],
    )


def _race_collision_behavior(feature: MechanicFeature) -> MechanicBehavior:
    return MechanicBehavior(
        id="vehicle_collision",
        name="Vehicle Collision",
        summary="Driving into another actor produces a collision-resolution trace.",
        priority=50,
        source="deterministic_fallback",
        required_for_basic_play=feature.required_for_basic_play,
        system_names=list(feature.signals.system_names),
        role_names=list(feature.signals.role_names),
        property_ids=list(feature.signals.property_ids),
        scene_names=list(feature.signals.scene_names),
        actions=[
            _step(
                "Reload collision-ready mode",
                navigate_query="rayxi_test_mode=collision_ready",
                wait_ms=1200,
                description="Reload with actors positioned for a quick collision test.",
                verification=MechanicVerification(trace_any=["test.mode mode=collision_ready"]),
            ),
            _step(
                "Click canvas to focus (collision ready)",
                keys="click",
                wait_ms=300,
                description="Refocus canvas after collision-ready reload.",
            ),
            _step(
                "Drive into rival",
                keys="w",
                method="hold",
                hold_ms=2200,
                wait_ms=350,
                verify_change=True,
                diff_threshold=0.5,
                description="Hold W until the lead vehicle reaches the rival and collision resolves.",
                verification=MechanicVerification(
                    trace_any_global=["collision.kart_kart", "collision.vehicle_vehicle", "collision.actor_actor", "collision."]
                ),
            ),
        ],
    )


def default_behaviors_for_feature(feature: MechanicFeature) -> list[MechanicBehavior]:
    behaviors: list[MechanicBehavior] = []
    is_combat = _has_role_token(feature, "fighter", "combatant", "duelist", "brawler") or _has_any_token(
        feature,
        "fighter",
        "hitbox",
        "hurtbox",
        "hadouken",
        "shoryuken",
        "tatsumaki",
        "special move",
    )
    is_vehicle = _has_role_token(feature, "vehicle", "kart", "car", "racer", "driver", "bike", "ship") or _has_any_token(
        feature,
        "kart",
        "vehicle",
        "track",
        "checkpoint",
        "mode 7",
        "pseudo-3d",
        "race",
    )

    if is_combat and _has_any_token(feature, "movement_system", "walk", "jump", "crouch", "gravity"):
        behaviors.extend([_combat_baseline_behavior(feature), _combat_movement_behavior(feature)])
    if is_combat and _has_any_token(feature, "combat_system", "attack", "damage", "light punch", "heavy kick"):
        behaviors.append(_combat_normals_behavior(feature))
    if is_combat and _has_any_token(feature, "projectile", "fireball", "hadouken", "shot"):
        behaviors.append(_combat_projectile_behavior(feature))
    if is_combat and _has_any_token(feature, "uppercut", "dragon punch", "shoryuken", "dp"):
        behaviors.append(_combat_uppercut_behavior(feature))
    if is_combat and _has_any_token(feature, "spin", "spinning", "hurricane", "tatsumaki", "qcb"):
        behaviors.append(_combat_spinning_behavior(feature))
    if is_combat and _has_any_token(feature, "block", "guard", "blockstun", "chip damage"):
        behaviors.append(_combat_blocking_behavior(feature))
    if is_combat and _has_any_token(feature, "collision", "push-out", "pushout", "hitbox", "hurtbox", "overlap"):
        behaviors.append(_combat_collision_behavior(feature))
    if is_combat and _has_any_token(feature, "rage", "meter", "stack", "powered special"):
        behaviors.append(_combat_rage_behavior(feature))

    if is_vehicle and _has_any_token(
        feature,
        "mode 7",
        "mode_7",
        "pseudo-3d",
        "scanline",
        "perspective",
        "renderer",
        "third-person",
        "third person",
        "chase camera",
        "3d race view",
    ):
        behaviors.append(_race_rendering_behavior(feature))
    if is_vehicle and _has_any_token(feature, "countdown", "3-2-1", "go", "race start"):
        behaviors.append(_race_countdown_behavior(feature))
    if is_vehicle and _has_any_token(feature, "kart physics", "vehicle movement", "acceleration", "steering", "speed", "offroad"):
        behaviors.extend([_race_rendering_behavior(feature), _race_physics_behavior(feature)])
    if is_vehicle and _has_any_token(feature, "drift", "boost", "mini-turbo", "skid"):
        behaviors.append(_race_drift_behavior(feature))
    if is_vehicle and _has_any_token(feature, "item", "pickup", "shell", "banana", "power-up"):
        behaviors.append(_race_item_behavior(feature))
    if is_vehicle and _has_any_token(feature, "collision", "spin out", "projectile hit", "collision bounds"):
        behaviors.append(_race_collision_behavior(feature))

    return merge_behaviors(behaviors)


def feature_test_needles(feature: MechanicFeature) -> list[str]:
    needles: list[str] = []
    needles.extend(feature.signals.trace_keywords)
    needles.extend(feature.signals.test_keywords)
    needles.extend(feature.signals.keywords)
    needles.extend(feature.signals.system_names)
    for behavior in feature.behaviors:
        needles.extend([behavior.id, behavior.name, behavior.summary, *behavior.system_names, *behavior.role_names])
        for action in behavior.actions:
            needles.extend([action.action, action.description, action.keys or ""])
            for note in action.verification.notes:
                needles.append(note)
            needles.extend(action.verification.trace_any)
            needles.extend(action.verification.trace_all)
            needles.extend(action.verification.trace_any_global)
            needles.extend(action.verification.trace_all_global)
            needles.extend(action.verification.checks)
    return _unique(needles)
