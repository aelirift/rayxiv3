"""Linker — generates a document explaining how all MLR files connect.

Outputs structure only: which files exist, what they contain (summary),
and how they reference each other. Does NOT repeat file contents.
"""

from __future__ import annotations

from .mlr import SceneMLR
from .models import GameIdentity, SceneListEntry


def _flatten_scenes(scenes: list[SceneListEntry]) -> list[SceneListEntry]:
    flat: list[SceneListEntry] = []
    for s in scenes:
        flat.append(s)
        if s.children:
            flat.extend(_flatten_scenes(s.children))
    return flat


def generate_link_doc(hlr: GameIdentity, scene_mlrs: list[SceneMLR]) -> str:
    """Generate the linking document."""
    lines: list[str] = []

    # Header
    lines.append(f"# {hlr.game_name} — MLR Link Document")
    lines.append("")
    lines.append(f"Genre: {hlr.genre} | Mode: {hlr.player_mode}")
    if hlr.win_condition:
        lines.append(f"Win: {hlr.win_condition}")
    lines.append("")

    # Global flow
    lines.append("## Game Flow")
    lines.append("")
    for trans in hlr.global_fsm.transitions:
        lines.append(f"  {trans}")
    lines.append("")

    # Enum summary
    lines.append("## HLR Enums (global vocabulary)")
    lines.append("")
    for e in hlr.enums:
        lines.append(f"- **{e.name}**: {', '.join(e.values)}")
    lines.append("")

    # Per-scene file map
    lines.append("## Scene File Map")
    lines.append("")

    for scene_mlr in scene_mlrs:
        sn = scene_mlr.scene_name
        lines.append(f"### {sn}/")
        lines.append("")

        # FSM
        if scene_mlr.fsm:
            states = ", ".join(scene_mlr.fsm.states)
            lines.append(f"- `_fsm.json` — {len(scene_mlr.fsm.states)} sub-states: {states}")

        # Collisions
        if scene_mlr.collisions:
            n = len(scene_mlr.collisions.collision_pairs)
            if n > 0:
                pairs = [f"{p.object_a} <-> {p.object_b}" for p in scene_mlr.collisions.collision_pairs]
                lines.append(f"- `_collisions.json` — {n} pairs: {', '.join(pairs)}")
            else:
                lines.append("- `_collisions.json` — no collisions")

        # System interactions
        for si in scene_mlr.system_interactions:
            n = len(si.interactions)
            lines.append(f"- `_interactions_{si.game_system}.json` — {n} interactions")

        # Entities
        for entity in scene_mlr.entities:
            n_props = len(entity.properties)
            n_actions = sum(len(a.actions) for a in entity.action_sets)
            n_enums = len(entity.scene_enums)
            parts = []
            if n_props:
                parts.append(f"{n_props} props")
            if n_actions:
                parts.append(f"{n_actions} actions")
            if n_enums:
                parts.append(f"{n_enums} enums")
            detail = f" ({', '.join(parts)})" if parts else ""
            lines.append(f"- `{entity.entity_name}.json` — {entity.object_type}{detail}")

        lines.append("")

    # Cross-references: how files connect
    lines.append("## How Files Connect")
    lines.append("")
    lines.append("```")
    lines.append("HLR (hlr.json)")
    lines.append("  declares: enums, scenes, game_systems, global FSM")
    lines.append("  |")
    lines.append("  v")
    lines.append("Per-scene MLR:")
    lines.append("  _fsm.json        <-- sub-states within the scene")
    lines.append("       |")
    lines.append("       v (FSM context feeds into)")
    lines.append("  _collisions.json  <-- which objects can touch")
    lines.append("       |")
    lines.append("       v (collisions + FSM feed into)")
    lines.append("  _interactions_{system}.json  <-- what happens (per game system)")
    lines.append("       |   references -->  entity.json objects + properties")
    lines.append("       |   references -->  _fsm.json states (conditions)")
    lines.append("       |   references -->  _collisions.json pairs (triggers)")
    lines.append("       |")
    lines.append("  {entity}.json     <-- what each thing IS and CAN DO")
    lines.append("       |   parent_enum --> HLR enum (scoping)")
    lines.append("       |   action_sets --> actions this entity performs")
    lines.append("       |   properties  --> DLR fills actual values")
    lines.append("```")
    lines.append("")

    # Per-scene wiring summary
    lines.append("## Scene Wiring")
    lines.append("")

    for scene_mlr in scene_mlrs:
        sn = scene_mlr.scene_name
        entity_names = [e.entity_name for e in scene_mlr.entities]
        system_names = [si.game_system for si in scene_mlr.system_interactions]

        lines.append(f"**{sn}**:")
        if entity_names:
            lines.append(f"  Entities: {', '.join(entity_names)}")
        if system_names:
            lines.append(f"  Systems: {', '.join(system_names)}")

        # Which entities are referenced by which systems
        for si in scene_mlr.system_interactions:
            targets = set()
            for interaction in si.interactions:
                for eff in interaction.effects:
                    root = eff.target.split(".")[0]
                    targets.add(root)
            if targets:
                lines.append(f"    {si.game_system} touches: {', '.join(sorted(targets))}")

        lines.append("")

    return "\n".join(lines)
