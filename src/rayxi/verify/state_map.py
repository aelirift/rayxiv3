"""State map — FSM visualization and validation.

Parses HLR global_fsm + MLR scene FSMs into a unified graph.
Validates: reachability, dead ends, orphan states, missing transitions.
Outputs: DOT format for Graphviz, mermaid format, validation errors.

Usage:
    from rayxi.verify.state_map import validate_state_map, generate_dot

    issues = validate_state_map(hlr, scene_mlrs)
    dot_str = generate_dot(hlr, scene_mlrs)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rayxi.spec.mlr import SceneMLR
from rayxi.spec.models import GameIdentity, SceneListEntry
from rayxi.trace import get_trace


@dataclass
class StateNode:
    name: str
    scene: str  # "" for global
    is_sub_state: bool = False


@dataclass
class StateEdge:
    source: str
    target: str
    condition: str = ""
    scene: str = ""


@dataclass
class StateGraph:
    nodes: list[StateNode] = field(default_factory=list)
    edges: list[StateEdge] = field(default_factory=list)


def _flatten_scenes(scenes: list[SceneListEntry]) -> list[SceneListEntry]:
    flat: list[SceneListEntry] = []
    for s in scenes:
        flat.append(s)
        if s.children:
            flat.extend(_flatten_scenes(s.children))
    return flat


def _parse_transition(trans: str) -> tuple[str, str, str]:
    """Parse 'STATE_A -> STATE_B: on condition' → (src, dst, condition)."""
    m = re.match(r"(\S+)\s*->\s*(\S+?)(?::?\s*(.*))?$", trans)
    if not m:
        return ("", "", trans)
    src = m.group(1).rstrip(":")
    dst = m.group(2).rstrip(":")
    cond = (m.group(3) or "").strip()
    return (src, dst, cond)


def build_state_graph(hlr: GameIdentity, scene_mlrs: list[SceneMLR]) -> StateGraph:
    """Build a unified state graph from HLR + all MLR scene FSMs."""
    graph = StateGraph()

    # Global FSM nodes
    for state in hlr.global_fsm.states:
        graph.nodes.append(StateNode(name=state, scene="global"))

    # Global FSM edges
    for trans in hlr.global_fsm.transitions:
        src, dst, cond = _parse_transition(trans)
        if src and dst:
            graph.edges.append(StateEdge(source=src, target=dst, condition=cond, scene="global"))

    # Scene sub-state FSMs
    for scene_mlr in scene_mlrs:
        if not scene_mlr.fsm:
            continue
        for state in scene_mlr.fsm.states:
            graph.nodes.append(StateNode(
                name=f"{scene_mlr.scene_name}:{state}",
                scene=scene_mlr.scene_name,
                is_sub_state=True,
            ))
        for trans in scene_mlr.fsm.transitions:
            src, dst, cond = _parse_transition(trans)
            if src and dst:
                # Check for exit transitions (cross-scene)
                if dst.startswith("[EXIT") or dst.startswith("EXIT"):
                    graph.edges.append(StateEdge(
                        source=f"{scene_mlr.scene_name}:{src}",
                        target=dst,
                        condition=cond,
                        scene=scene_mlr.scene_name,
                    ))
                else:
                    graph.edges.append(StateEdge(
                        source=f"{scene_mlr.scene_name}:{src}",
                        target=f"{scene_mlr.scene_name}:{dst}",
                        condition=cond,
                        scene=scene_mlr.scene_name,
                    ))

    return graph


def validate_state_map(hlr: GameIdentity, scene_mlrs: list[SceneMLR]) -> list[str]:
    """Validate the combined state graph. Returns list of issues."""
    trace = get_trace()
    issues: list[str] = []
    graph = build_state_graph(hlr, scene_mlrs)
    all_scenes = _flatten_scenes(hlr.scenes)

    node_names = {n.name for n in graph.nodes}
    edge_sources = {e.source for e in graph.edges}
    edge_targets = {e.target for e in graph.edges}

    # --- Global FSM checks ---

    # Every scene must have an FSM state
    for scene in all_scenes:
        if scene.fsm_state not in {n.name for n in graph.nodes if n.scene == "global"}:
            issues.append(f"Scene '{scene.scene_name}' FSM state '{scene.fsm_state}' not in global graph")

    # Reachability: every global state (except start) must have incoming edge
    global_nodes = {n.name for n in graph.nodes if n.scene == "global"}
    global_targets = {e.target for e in graph.edges if e.scene == "global"}
    unreachable = global_nodes - global_targets
    if len(unreachable) > 1:
        issues.append(f"Global FSM: multiple states with no incoming edge (possible unreachable): {unreachable}")

    # Dead ends: every global state (except terminal) must have outgoing edge
    global_sources = {e.source for e in graph.edges if e.scene == "global"}
    dead_ends = global_nodes - global_sources
    if dead_ends:
        # Filter: start states are allowed to have no outgoing IF they're the only entry
        real_dead_ends = dead_ends - unreachable
        if real_dead_ends:
            issues.append(f"Global FSM: dead-end states (no outgoing edge): {real_dead_ends}")

    # --- Scene sub-FSM checks ---

    for scene_mlr in scene_mlrs:
        if not scene_mlr.fsm:
            issues.append(f"Scene '{scene_mlr.scene_name}' has no FSM")
            continue

        scene_nodes = {n.name for n in graph.nodes if n.scene == scene_mlr.scene_name}
        scene_sources = {e.source for e in graph.edges if e.scene == scene_mlr.scene_name}
        scene_targets = {e.target for e in graph.edges
                         if e.scene == scene_mlr.scene_name and not e.target.startswith("[EXIT")}

        # Every sub-state should be reachable
        unreachable_sub = scene_nodes - scene_targets
        if len(unreachable_sub) > 1:
            issues.append(
                f"Scene '{scene_mlr.scene_name}': multiple sub-states with no incoming edge: "
                f"{[s.split(':')[1] for s in unreachable_sub]}"
            )

        # Every sub-state (except terminal/exit) should have outgoing
        no_outgoing = scene_nodes - scene_sources
        if no_outgoing:
            # Check if they have EXIT edges
            exit_sources = {e.source for e in graph.edges
                            if e.scene == scene_mlr.scene_name and e.target.startswith("[EXIT")}
            real_no_out = no_outgoing - exit_sources
            if real_no_out:
                issues.append(
                    f"Scene '{scene_mlr.scene_name}': sub-states with no outgoing edge: "
                    f"{[s.split(':')[1] for s in real_no_out]}"
                )

    if trace:
        trace.verify("state_map", "global+scenes", passed=len(issues) == 0, issues=issues)

    return issues


def generate_dot(hlr: GameIdentity, scene_mlrs: list[SceneMLR]) -> str:
    """Generate DOT (Graphviz) format for the state graph."""
    graph = build_state_graph(hlr, scene_mlrs)
    lines = ["digraph GameFSM {", '  rankdir=LR;', '  node [shape=box, style=rounded];', ""]

    # Global FSM cluster
    lines.append("  subgraph cluster_global {")
    lines.append('    label="Global FSM";')
    lines.append('    style=dashed;')
    for node in graph.nodes:
        if node.scene == "global":
            lines.append(f'    "{node.name}";')
    lines.append("  }")
    lines.append("")

    # Scene sub-FSM clusters
    scenes_with_fsm = {n.scene for n in graph.nodes if n.scene != "global"}
    for scene in sorted(scenes_with_fsm):
        lines.append(f"  subgraph cluster_{scene} {{")
        lines.append(f'    label="{scene}";')
        lines.append('    style=filled;')
        lines.append('    color=lightgrey;')
        for node in graph.nodes:
            if node.scene == scene:
                short = node.name.split(":")[1] if ":" in node.name else node.name
                lines.append(f'    "{node.name}" [label="{short}"];')
        lines.append("  }")
        lines.append("")

    # Edges
    for edge in graph.edges:
        label = edge.condition[:40] if edge.condition else ""
        if label:
            lines.append(f'  "{edge.source}" -> "{edge.target}" [label="{label}"];')
        else:
            lines.append(f'  "{edge.source}" -> "{edge.target}";')

    lines.append("}")
    return "\n".join(lines)


def generate_mermaid(hlr: GameIdentity, scene_mlrs: list[SceneMLR]) -> str:
    """Generate Mermaid stateDiagram format."""
    graph = build_state_graph(hlr, scene_mlrs)
    lines = ["stateDiagram-v2", ""]

    # Global states
    for node in graph.nodes:
        if node.scene == "global":
            lines.append(f"    {node.name}")

    lines.append("")

    # Global transitions
    for edge in graph.edges:
        if edge.scene == "global":
            cond = f": {edge.condition[:40]}" if edge.condition else ""
            lines.append(f"    {edge.source} --> {edge.target}{cond}")

    lines.append("")

    # Scene sub-FSMs as nested states
    scenes_with_fsm = sorted({n.scene for n in graph.nodes if n.scene != "global"})
    for scene in scenes_with_fsm:
        lines.append(f"    state {scene} {{")
        for node in graph.nodes:
            if node.scene == scene:
                short = node.name.split(":")[1] if ":" in node.name else node.name
                lines.append(f"        {short}")
        for edge in graph.edges:
            if edge.scene == scene:
                src_short = edge.source.split(":")[1] if ":" in edge.source else edge.source
                tgt_short = edge.target.split(":")[1] if ":" in edge.target else edge.target
                cond = f": {edge.condition[:30]}" if edge.condition else ""
                lines.append(f"        {src_short} --> {tgt_short}{cond}")
        lines.append("    }")
        lines.append("")

    return "\n".join(lines)
