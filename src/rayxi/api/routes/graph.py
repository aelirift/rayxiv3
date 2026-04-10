"""POST /api/graph — build a combined layer graph for the frontend.

Runs all five Semantic Stack layers and returns a unified node+edge graph
that Cytoscape.js can render directly, with pre-computed positions and
layer class tags for the filter panel.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from rayxi.api.config import schemas_dir
from rayxi.layers.base.map_scanner import MapScanner
from rayxi.layers.intent.semantic_interpreter import SemanticInterpreter
from rayxi.layers.reliability.symbolic_simulator import SymbolicSimulator
from rayxi.layers.state.race_mapper import RaceMapper
from rayxi.layers.state.state_schema import StateSchema
from rayxi.llm.callers import build_callers

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / helpers
# ---------------------------------------------------------------------------


class GraphRequest(BaseModel):
    source: str = ""
    schema_id: str = "payment_fsm"
    interpret: bool = True  # default ON — needed for testing
    model: str = "glm"
    initial_state: str | None = None  # null → first non-terminal state
    drop_probability: float = Field(default=0.3, ge=0.0, le=1.0)
    seed: int | None = None
    max_steps: int = Field(default=100, ge=1)


def _fn_id(qualified_name: str) -> str:
    """Stable Cytoscape element ID for a function node."""
    return "fn__" + qualified_name.replace("::", "__").replace(".", "_")


def _layout_states(schema: StateSchema, start: str) -> dict[str, dict]:
    """BFS over the FSM to assign (x, y) positions to state nodes.

    Returns  {state_name: {"x": float, "y": float}}.
    """
    level_buckets: dict[int, list[str]] = {}
    visited: set[str] = set()
    queue = [start]
    levels: dict[str, int] = {start: 0}
    visited.add(start)
    level_buckets[0] = [start]

    while queue:
        cur = queue.pop(0)
        for t in schema.outgoing_transitions(cur):
            nxt = t.to_state
            if nxt not in visited:
                visited.add(nxt)
                lvl = levels[cur] + 1
                levels[nxt] = lvl
                level_buckets.setdefault(lvl, []).append(nxt)
                queue.append(nxt)

    # Unreachable states appended at the bottom
    max_lvl = max(levels.values()) if levels else 0
    for s in schema.states:
        if s not in levels:
            max_lvl += 1
            levels[s] = max_lvl
            level_buckets.setdefault(max_lvl, []).append(s)

    positions: dict[str, dict] = {}
    for level, bucket in sorted(level_buckets.items()):
        n = len(bucket)
        for j, state in enumerate(bucket):
            x = (j - (n - 1) / 2.0) * 200
            y = level * 160
            positions[state] = {"x": x, "y": y}

    return positions


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/api/graph")
async def api_graph(req: GraphRequest):
    # ── Resolve + load schema ─────────────────────────────────────────────
    schema_path = schemas_dir() / f"{req.schema_id}.json"
    if not schema_path.exists():
        raise HTTPException(404, f"Schema '{req.schema_id}' not found in {schemas_dir()}")
    try:
        schema = StateSchema.load(schema_path)
    except Exception as exc:
        raise HTTPException(400, f"Schema parse error: {exc}") from exc

    nodes: list[dict] = []
    edges: list[dict] = []
    fn_node_ids: dict[str, str] = {}  # qualified_name → cytoscape node id

    # ── Layer 1 (AST) + Layer 2 (Intent, optional) ───────────────────────
    fm = None
    dual_names: dict[str, object] = {}

    if req.source.strip():
        fm = MapScanner().scan_source(req.source, module_stem="code")

        if req.interpret and fm.functions:
            try:
                callers = build_callers()
                llm = callers.get(req.model) or next(iter(callers.values()))
                interp = SemanticInterpreter(llm, req.model)
                dn_list = await interp.interpret_all(fm.functions)
                dual_names = {dn.original_name: dn for dn in dn_list}
            except Exception:
                # Non-fatal — graph renders without intent data
                pass

        for i, entry in enumerate(fm.functions):
            col, row = i % 2, i // 2
            node_id = _fn_id(entry.qualified_name)
            fn_node_ids[entry.qualified_name] = node_id
            dn = dual_names.get(entry.original_name)

            layer_list = [1]
            classes = ["layer-1"]
            if dn:
                layer_list.append(2)
                classes.append("layer-2")
                if dn.conflict:
                    classes.append("conflict")

            nodes.append(
                {
                    "id": node_id,
                    "label": entry.original_name,
                    "sublabel": dn.computed_name if dn else "",
                    "shape": "rectangle",
                    "group": "function",
                    "position": {"x": 80 + col * 220, "y": 80 + row * 130},
                    "layers": layer_list,
                    "classes": classes,
                    "data": {
                        "qualified_name": entry.qualified_name,
                        "signature": entry.structural_signature,
                        "is_async": entry.is_async,
                        "conflict": dn.conflict if dn else False,
                        "conflict_reason": dn.conflict_reason if dn else "",
                        "start_line": entry.start_line,
                        "end_line": entry.end_line,
                    },
                }
            )

    # ── Layer 3: FSM States + Transitions ────────────────────────────────
    non_terminal = [s for s in schema.states if not schema.is_terminal(s)]
    layout_start = req.initial_state or (non_terminal[0] if non_terminal else schema.states[0])
    state_positions = _layout_states(schema, layout_start)

    # x-offset: states sit to the right of the function grid
    fn_cols = 2 if fn_node_ids else 0
    x_offset = fn_cols * 220 + 160

    for state in schema.states:
        pos = state_positions.get(state, {"x": 0, "y": 0})
        classes = ["layer-3"]
        if schema.is_terminal(state):
            classes.append("terminal")

        nodes.append(
            {
                "id": f"state__{state}",
                "label": state,
                "sublabel": "terminal" if schema.is_terminal(state) else "",
                "shape": "ellipse",
                "group": "state",
                "position": {"x": pos["x"] + x_offset, "y": pos["y"] + 80},
                "layers": [3],
                "classes": classes,
                "data": {
                    "is_terminal": schema.is_terminal(state),
                    "outgoing_count": len(schema.outgoing_transitions(state)),
                },
            }
        )

    for t in schema.transitions:
        edges.append(
            {
                "id": f"trans__{t.from_state}__{t.to_state}",
                "source": f"state__{t.from_state}",
                "target": f"state__{t.to_state}",
                "label": "",
                "layer": 3,
                "edge_type": "transition",
                "classes": ["layer-3", "transition"],
                "data": {},
            }
        )

    # ── Layer 4: Race Map ─────────────────────────────────────────────────
    if fm and fm.functions:
        races = RaceMapper(schema).find_races(fm)
        for ri, race in enumerate(races):
            writers = race.writers
            for i in range(len(writers)):
                for j in range(i + 1, len(writers)):
                    w1, w2 = writers[i], writers[j]
                    src_id = fn_node_ids.get(w1, _fn_id(w1))
                    tgt_id = fn_node_ids.get(w2, _fn_id(w2))
                    edge_cls = race.race_type.lower().replace("_", "-")
                    edges.append(
                        {
                            "id": f"race__{ri}__{i}__{j}",
                            "source": src_id,
                            "target": tgt_id,
                            "label": race.state_key,
                            "layer": 4,
                            "edge_type": edge_cls,
                            "classes": ["layer-4", f"race-{edge_cls}", f"risk-{race.risk_level.lower()}"],
                            "data": {
                                "state_key": race.state_key,
                                "race_type": race.race_type,
                                "risk": race.risk_level,
                            },
                        }
                    )

    # ── Layer 5: Simulation ───────────────────────────────────────────────
    initial_state = req.initial_state or (non_terminal[0] if non_terminal else schema.states[0])

    sim = SymbolicSimulator(
        schema,
        drop_probability=req.drop_probability,
        seed=req.seed,
        max_steps=req.max_steps,
    )
    sim_result = sim.run(initial_state)

    stuck_set = set(sim_result.stuck_states)
    for node in nodes:
        if node["group"] == "state":
            state_name = node["id"][len("state__") :]
            if state_name in stuck_set:
                if "stuck" not in node["classes"]:
                    node["classes"].append("stuck")
                if 5 not in node["layers"]:
                    node["layers"].append(5)

    for stuck in sim_result.stuck_states:
        edges.append(
            {
                "id": f"sim_drop__{stuck}",
                "source": f"state__{stuck}",
                "target": f"state__{stuck}",
                "label": "dropped",
                "layer": 5,
                "edge_type": "sim-drop",
                "classes": ["layer-5", "sim-drop"],
                "data": {"dropped_count": sim_result.dropped_count},
            }
        )

    # ── Filter options ────────────────────────────────────────────────────
    filter_options = {
        "1": {
            "label": "AST Site Map",
            "color": "#4ade80",
            "items": [{"id": n["id"], "label": n["label"]} for n in nodes if 1 in n["layers"]],
        },
        "2": {
            "label": "Intent / Dual-Naming",
            "color": "#60a5fa",
            "items": [
                {"id": n["id"], "label": n["label"], "sublabel": n["sublabel"]} for n in nodes if 2 in n["layers"]
            ],
        },
        "3": {
            "label": "FSM States",
            "color": "#fb923c",
            "items": [{"id": n["id"], "label": n["label"]} for n in nodes if 3 in n["layers"]],
        },
        "4": {
            "label": "Race Map",
            "color": "#f87171",
            "items": [
                {"id": e["id"], "label": e.get("data", {}).get("state_key", e["id"])} for e in edges if e["layer"] == 4
            ],
        },
        "5": {
            "label": "Simulation",
            "color": "#c084fc",
            "items": [{"id": n["id"], "label": n["label"]} for n in nodes if 5 in n["layers"]],
        },
    }

    return {
        "nodes": nodes,
        "edges": edges,
        "filter_options": filter_options,
        "meta": {
            "schema_id": schema.schema_id,
            "initial_state": initial_state,
            "function_count": sum(1 for n in nodes if n["group"] == "function"),
            "state_count": sum(1 for n in nodes if n["group"] == "state"),
            "race_count": sum(1 for e in edges if e["layer"] == 4),
            "deadlock_detected": sim_result.deadlock_detected,
            "stuck_states": sim_result.stuck_states,
            "drop_probability": req.drop_probability,
        },
    }
