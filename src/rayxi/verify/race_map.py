"""Race condition detector — finds concurrent property mutations.

Analyzes MLR interactions within each scene to find cases where
two different triggers/systems could modify the same entity.property
in the same FSM state.

Example race: in FIGHTING state, both "combat" and "round_management"
systems modify p1_fighter.health — one from hit damage, one from
time-expire health comparison.

Usage:
    from rayxi.verify.race_map import detect_races

    races = detect_races(scene_mlrs)
"""

from __future__ import annotations

from dataclasses import dataclass

from rayxi.spec.mlr import SceneMLR
from rayxi.trace import get_trace


@dataclass
class PropertyAccess:
    """One write to an entity.property within a scene."""
    scene: str
    system: str
    trigger: str
    condition: str
    verb: str
    target: str  # entity.property
    interaction_index: int


@dataclass
class RaceCondition:
    """Two or more writes to the same target that could fire concurrently."""
    scene: str
    target: str  # entity.property
    accesses: list[PropertyAccess]
    reason: str


def _extract_accesses(scene_mlr: SceneMLR) -> list[PropertyAccess]:
    """Extract all property write accesses from a scene's interactions."""
    accesses: list[PropertyAccess] = []
    for si in scene_mlr.system_interactions:
        for i, interaction in enumerate(si.interactions):
            for eff in interaction.effects:
                accesses.append(PropertyAccess(
                    scene=scene_mlr.scene_name,
                    system=si.game_system,
                    trigger=interaction.trigger,
                    condition=interaction.condition,
                    verb=eff.verb,
                    target=eff.target,
                    interaction_index=i,
                ))
    return accesses


def detect_races(scene_mlrs: list[SceneMLR]) -> list[RaceCondition]:
    """Detect potential race conditions across all scenes."""
    trace = get_trace()
    all_races: list[RaceCondition] = []

    for scene_mlr in scene_mlrs:
        accesses = _extract_accesses(scene_mlr)

        # Group by target (entity.property)
        by_target: dict[str, list[PropertyAccess]] = {}
        for access in accesses:
            by_target.setdefault(access.target, []).append(access)

        for target, writes in by_target.items():
            if len(writes) < 2:
                continue

            # Check if writes come from different systems
            systems = {w.system for w in writes}
            if len(systems) > 1:
                all_races.append(RaceCondition(
                    scene=scene_mlr.scene_name,
                    target=target,
                    accesses=writes,
                    reason=f"Modified by {len(systems)} systems: {', '.join(sorted(systems))}",
                ))
                continue

            # Same system, different triggers — still a potential race
            triggers = {w.trigger for w in writes}
            if len(triggers) > 1:
                all_races.append(RaceCondition(
                    scene=scene_mlr.scene_name,
                    target=target,
                    accesses=writes,
                    reason=f"Same system '{writes[0].system}' but {len(triggers)} different triggers",
                ))

    if trace:
        issues = [f"{r.scene}/{r.target}: {r.reason}" for r in all_races]
        trace.verify("race_map", "all_scenes", passed=len(all_races) == 0, issues=issues)

    return all_races


def format_races(races: list[RaceCondition]) -> str:
    """Human-readable race condition report."""
    if not races:
        return "No race conditions detected."

    lines = [f"Race conditions detected: {len(races)}", ""]

    for i, race in enumerate(races, 1):
        lines.append(f"  [{i}] {race.scene} / {race.target}")
        lines.append(f"      Reason: {race.reason}")
        for access in race.accesses:
            lines.append(
                f"        - {access.system}.{access.verb}({access.target}) "
                f"trigger={access.trigger[:50]} "
                f"condition={access.condition[:50]}"
            )
        lines.append("")

    return "\n".join(lines)
