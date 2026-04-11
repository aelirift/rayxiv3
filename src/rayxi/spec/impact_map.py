"""ImpactMap — the central property-level data flow graph.

This is THE spine of a game spec. Every downstream artifact (per-scene manifest,
per-system MLR, per-entity DLR, codegen) is a projection of this map. Nothing
else is a source of truth; everything else is a view.

Strictness model:
  - HLR (seed):   defines systems[], scenes[], and the initial set of property
                  nodes from template + mechanic_specs. Locks scope.
  - MLR (drill):  may ADD nodes and edges within an already-declared system's
                  ownership. Cannot add systems, cannot add scenes, cannot
                  remove any pre-existing node/edge. Logged as additions.
  - DLR (fill):   fills typed initial_value / derivation / formula on every
                  node and edge. No unfilled fields. No prose math.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from typing import TYPE_CHECKING

from .expr import Expr, expr_refs, format_expr, parse_expr, validate_expr

if TYPE_CHECKING:
    from .models import GameIdentity


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WriteKind(str, Enum):
    CONFIG_INIT = "config_init"      # written once at game/character definition time
    LIFECYCLE = "lifecycle"          # written on scene/round transition
    FRAME_UPDATE = "frame_update"    # written each frame during gameplay
    DERIVED = "derived"              # never written, computed from others every read


class Category(str, Enum):
    CONFIG = "config"
    STATE = "state"
    DERIVED = "derived"


class Scope(str, Enum):
    INSTANCE = "instance"              # per-instance (e.g. each fighter's current_hp)
    PLAYER_SLOT = "player_slot"        # per-slot (p1/p2) but persists across rounds
    CHARACTER_DEF = "character_def"    # per-character archetype (Ryu's max_hp)
    GAME = "game"                      # global (round_number, match_timer)


# ---------------------------------------------------------------------------
# Nodes and edges
# ---------------------------------------------------------------------------


class PropertyNode(BaseModel):
    """One property in the impact graph. Every state/config/derived value in the
    game is a PropertyNode. Keyed by `id` = '<owner>.<name>'."""
    id: str                          # 'fighter.current_hp'
    owner: str                       # 'fighter' | 'projectile' | 'game' | 'hud.p1_rage_meter'
    name: str                        # 'current_hp'
    type: str                        # 'int' | 'float' | 'bool' | 'string' | 'Vector2' | 'Color'
    category: Category
    scope: Scope = Scope.INSTANCE
    description: str = ""

    # Value domain — for string/enum properties that have a constrained set of
    # valid values. Populated from the HLT's `property_enums` section at seed
    # time, and from MLR drill-down for custom properties. When non-None, the
    # property access validator and all string-literal comparisons in
    # generated code must match one of these values. This is the canonical
    # cross-system contract for handoffs like `fighter.current_action`.
    enum_values: list[str] | None = None

    # Filled by DLR:
    initial_value: Expr | None = None    # for config + state nodes
    derivation: Expr | None = None       # for derived nodes — computed, never written

    # Audit:
    declared_by: str = "hlr_seed"        # 'hlr_seed' | 'mlr_{system_name}' | 'template'


class WriteEdge(BaseModel):
    """A system writes to a property. Multiple writers per property are allowed
    but each must have a distinct write_kind or scene_scope."""
    kind: Literal["write"] = "write"
    system: str                      # 'combat_system'
    target: str                      # 'fighter.current_hp'
    write_kind: WriteKind
    scene_scope: list[str] = Field(default_factory=list)   # scenes where active ([] = all)
    trigger: str = ""                # prose: when this write fires
    condition: Expr | None = None    # typed guard
    formula: Expr | None = None      # typed update expression
    procedural_note: str = ""        # escape hatch — prose description of procedural operations
                                     # (circular buffers, multi-step state changes) that don't
                                     # fit a pure expression. DLR validator accepts formula OR
                                     # procedural_note but not both empty.
    declared_by: str = "hlr_seed"


class ReadEdge(BaseModel):
    """A system or HUD entity reads a property."""
    kind: Literal["read"] = "read"
    system: str                      # system name OR hud entity name (e.g. 'p1_rage_meter')
    source: str                      # 'fighter.current_hp'
    scene_scope: list[str] = Field(default_factory=list)
    purpose: str = ""
    declared_by: str = "hlr_seed"


# ---------------------------------------------------------------------------
# Impact map
# ---------------------------------------------------------------------------


PHASE_ORDER = ["input", "decision", "physics", "resolution", "display"]


class ImpactMap(BaseModel):
    """The central property-level data flow graph. Single source of truth."""
    game_name: str
    version: int = 1                 # bumped every time the map mutates

    # Strict scope — set at HLR seed time, frozen afterwards
    systems: list[str] = Field(default_factory=list)
    scenes: list[str] = Field(default_factory=list)

    # Execution-order metadata. phases[system] is one of PHASE_ORDER; default is
    # "physics" when unspecified. scene_gen orders _physics_process calls by
    # (phase, topo_order_within_phase). Populated at seed time from the KB
    # template; MLR/DLR do not mutate phases.
    phases: dict[str, str] = Field(default_factory=dict)

    # The graph itself
    nodes: dict[str, PropertyNode] = Field(default_factory=dict)
    write_edges: list[WriteEdge] = Field(default_factory=list)
    read_edges: list[ReadEdge] = Field(default_factory=list)

    # Audit log — every addition after seed time
    audit: list[str] = Field(default_factory=list)

    def phase_for(self, system: str) -> str:
        return self.phases.get(system, "physics")

    def ordered_systems(self) -> list[str]:
        """Return systems sorted by (phase_index, topo order within phase).

        Topo order uses write→read edges across systems: if A writes P and B
        reads P, A runs before B. Cycles are broken in declaration order and
        logged to self.audit.
        """
        phase_index = {p: i for i, p in enumerate(PHASE_ORDER)}
        by_phase: dict[int, list[str]] = {}
        for s in self.systems:
            idx = phase_index.get(self.phase_for(s), phase_index["physics"])
            by_phase.setdefault(idx, []).append(s)

        ordered: list[str] = []
        for idx in sorted(by_phase.keys()):
            phase_systems = by_phase[idx]
            ordered.extend(self._topo_within(phase_systems))
        return ordered

    def _topo_within(self, systems_in_phase: list[str]) -> list[str]:
        sys_set = set(systems_in_phase)
        deps: dict[str, set[str]] = {s: set() for s in systems_in_phase}
        for w in self.write_edges:
            if w.system not in sys_set:
                continue
            for r in self.readers_of(w.target):
                if r.system in sys_set and r.system != w.system:
                    deps[r.system].add(w.system)

        result: list[str] = []
        remaining = {s: set(d) for s, d in deps.items()}
        while remaining:
            ready = [s for s in systems_in_phase
                     if s in remaining and not remaining[s]]
            if not ready:
                cycle_members = sorted(remaining.keys())
                self.audit.append(
                    f"ordered_systems: cycle broken in phase, declaration order used: {cycle_members}"
                )
                ready = cycle_members[:1]
            for s in ready:
                result.append(s)
                remaining.pop(s)
                for rem in remaining.values():
                    rem.discard(s)
        return result

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def writers_of(self, property_id: str) -> list[WriteEdge]:
        return [e for e in self.write_edges if e.target == property_id]

    def readers_of(self, property_id: str) -> list[ReadEdge]:
        return [e for e in self.read_edges if e.source == property_id]

    def properties_written_by(self, system: str) -> list[PropertyNode]:
        targets = {e.target for e in self.write_edges if e.system == system}
        return [self.nodes[t] for t in targets if t in self.nodes]

    def properties_read_by(self, system_or_entity: str) -> list[PropertyNode]:
        sources = {e.source for e in self.read_edges if e.system == system_or_entity}
        return [self.nodes[s] for s in sources if s in self.nodes]

    def properties_owned_by(self, owner: str) -> list[PropertyNode]:
        return [n for n in self.nodes.values() if n.owner == owner]

    def unfilled_nodes(self) -> list[PropertyNode]:
        """Nodes missing DLR values — for strict DLR validation."""
        missing: list[PropertyNode] = []
        for n in self.nodes.values():
            if n.category == Category.DERIVED:
                if n.derivation is None:
                    missing.append(n)
            else:
                if n.initial_value is None:
                    missing.append(n)
        return missing

    def unfilled_write_edges(self) -> list[WriteEdge]:
        """Frame-update writes without typed formulas AND without procedural notes —
        strict DLR fails these. An edge with a procedural_note is considered filled."""
        return [e for e in self.write_edges
                if e.write_kind == WriteKind.FRAME_UPDATE
                and e.formula is None
                and not e.procedural_note]

    # ------------------------------------------------------------------
    # Views / slices — inputs for per-call LLM prompts and codegen
    # ------------------------------------------------------------------

    def slice_for_system(self, system: str) -> dict:
        """Build a scoped view of the map for one system — the authoritative
        cross-system contract the LLM needs to generate correct code.

        Includes every property this system may read or write, PLUS the full
        cross-system picture for each:
          - owned_properties: nodes this system directly touches, with full
            type + enum_values + description
          - own_writes / own_reads: the edges declared for this system
          - peer_writes: other systems that also write our targets
          - downstream_reads: systems that react to what we write
          - property_details: full PropertyNode dump for every id mentioned
            anywhere in the slice, keyed by id. Each entry lists ALL writers
            and ALL readers across the whole impact map — the LLM must use
            this to understand cross-system handoffs. This is what B solves:
            the slice now expresses the full contract, not just this system's
            local view.
        """
        own_writes = [e for e in self.write_edges if e.system == system]
        own_reads = [e for e in self.read_edges if e.system == system]

        # Properties this system directly touches
        touched_ids: set[str] = set()
        touched_ids.update(e.target for e in own_writes)
        touched_ids.update(e.source for e in own_reads)

        own_nodes = {pid: self.nodes[pid] for pid in touched_ids if pid in self.nodes}

        # Peer writers for each property we write — so this system knows who else modifies it
        peer_writes: dict[str, list[dict]] = {}
        for pid in (e.target for e in own_writes):
            others = [
                {"system": e.system, "write_kind": e.write_kind.value, "trigger": e.trigger}
                for e in self.writers_of(pid) if e.system != system
            ]
            if others:
                peer_writes[pid] = others

        # Downstream readers for each property we write — so this system knows who reacts
        downstream_reads: dict[str, list[str]] = {}
        for pid in (e.target for e in own_writes):
            readers = [e.system for e in self.readers_of(pid) if e.system != system]
            if readers:
                downstream_reads[pid] = readers

        # --- Expanded property_details: full node dump + cross-system map ---
        # For every property id the system might touch, include:
        #   type, enum_values, description
        #   writers: [{system, write_kind, trigger}]   — every writer anywhere
        #   readers: [system, ...]                     — every reader anywhere
        #
        # The LLM uses this to see the complete contract. If it needs to read
        # a property that another system writes, it sees the writer list and
        # knows the value will be present by the time process() runs. If the
        # property has enum_values, the LLM MUST use those exact strings.
        property_details: dict[str, dict] = {}
        detail_ids: set[str] = set(touched_ids)
        # Also include any property referenced by peer_writes / downstream_reads
        # so the LLM sees the value domains for cross-system targets even when
        # it's not this system's own read.
        for pid in peer_writes.keys():
            detail_ids.add(pid)
        for pid in downstream_reads.keys():
            detail_ids.add(pid)

        for pid in detail_ids:
            node = self.nodes.get(pid)
            if node is None:
                continue
            writers = [
                {"system": e.system, "write_kind": e.write_kind.value, "trigger": e.trigger}
                for e in self.writers_of(pid)
            ]
            readers = sorted({e.system for e in self.readers_of(pid)})
            property_details[pid] = {
                "id": pid,
                "type": node.type,
                "enum_values": node.enum_values,
                "description": node.description,
                "category": node.category.value,
                "scope": node.scope.value,
                "writers": writers,
                "readers": readers,
            }

        return {
            "system": system,
            "owned_properties": [n.model_dump() for n in own_nodes.values()],
            "own_writes": [e.model_dump() for e in own_writes],
            "own_reads": [e.model_dump() for e in own_reads],
            "peer_writes": peer_writes,
            "downstream_reads": downstream_reads,
            "property_details": property_details,
        }

    def scene_view(self, scene: str) -> dict:
        """Nodes and edges that are active in a given scene."""
        def in_scene(scope: list[str]) -> bool:
            return not scope or scene in scope

        writes = [e for e in self.write_edges if in_scene(e.scene_scope)]
        reads = [e for e in self.read_edges if in_scene(e.scene_scope)]
        node_ids: set[str] = set()
        for e in writes:
            node_ids.add(e.target)
        for e in reads:
            node_ids.add(e.source)
        nodes = {pid: self.nodes[pid].model_dump() for pid in node_ids if pid in self.nodes}
        return {
            "scene": scene,
            "nodes": nodes,
            "write_edges": [e.model_dump() for e in writes],
            "read_edges": [e.model_dump() for e in reads],
            "active_systems": sorted({e.system for e in writes} | {e.system for e in reads}),
        }

    def entity_view(self, owner: str) -> dict:
        """All properties owned by one entity, plus edges touching them."""
        own_nodes = self.properties_owned_by(owner)
        own_ids = {n.id for n in own_nodes}
        writes = [e for e in self.write_edges if e.target in own_ids]
        reads = [e for e in self.read_edges if e.source in own_ids]
        return {
            "owner": owner,
            "nodes": [n.model_dump() for n in own_nodes],
            "write_edges": [e.model_dump() for e in writes],
            "read_edges": [e.model_dump() for e in reads],
        }

    # ------------------------------------------------------------------
    # Mutation helpers — used by MLR drill-down and DLR fill
    # ------------------------------------------------------------------

    def add_node(self, node: PropertyNode) -> None:
        if node.id in self.nodes:
            # Merge: keep existing, log if description changed
            existing = self.nodes[node.id]
            if existing.description != node.description and node.description:
                self.audit.append(f"node {node.id}: description updated by {node.declared_by}")
            return
        self.nodes[node.id] = node
        self.audit.append(f"node {node.id} added by {node.declared_by} (owner={node.owner})")
        self.version += 1

    def add_write_edge(self, edge: WriteEdge) -> None:
        # Dedup by (system, target, write_kind)
        for existing in self.write_edges:
            if (existing.system == edge.system and existing.target == edge.target
                    and existing.write_kind == edge.write_kind):
                # Merge scene_scope, keep newer trigger/formula if provided
                existing.scene_scope = sorted(set(existing.scene_scope) | set(edge.scene_scope))
                if edge.trigger and not existing.trigger:
                    existing.trigger = edge.trigger
                if edge.formula and not existing.formula:
                    existing.formula = edge.formula
                return
        self.write_edges.append(edge)
        self.audit.append(
            f"write {edge.system} → {edge.target} ({edge.write_kind.value}) added by {edge.declared_by}"
        )
        self.version += 1

    def add_read_edge(self, edge: ReadEdge) -> None:
        for existing in self.read_edges:
            if existing.system == edge.system and existing.source == edge.source:
                existing.scene_scope = sorted(set(existing.scene_scope) | set(edge.scene_scope))
                if edge.purpose and not existing.purpose:
                    existing.purpose = edge.purpose
                return
        self.read_edges.append(edge)
        self.audit.append(
            f"read {edge.system} ← {edge.source} added by {edge.declared_by}"
        )
        self.version += 1


# ---------------------------------------------------------------------------
# Basic validators (graph-structural; per-phase strictness lives in seperate files)
# ---------------------------------------------------------------------------


def validate_impact_seed(imap: ImpactMap, hlr: "GameIdentity") -> list[str]:
    """HLR-level strict-scope check. Runs after the deterministic seed build.

    Enforces:
      - systems list matches HLR game_systems exactly
      - scenes list matches HLR flat scene list exactly
      - every mechanic_spec has at least one property node
      - every mechanic_spec's system_name is in scope
    """
    from .hlr_validator import _flatten_scenes as _flatten
    errors: list[str] = []

    hlr_systems = set(hlr.get_enum("game_systems"))
    hlr_scenes = {s.scene_name for s in _flatten(hlr.scenes)}
    seed_systems = set(imap.systems)
    seed_scenes = set(imap.scenes)

    if hlr_systems != seed_systems:
        missing_in_seed = hlr_systems - seed_systems
        extra_in_seed = seed_systems - hlr_systems
        if missing_in_seed:
            errors.append(f"seed systems missing: {sorted(missing_in_seed)}")
        if extra_in_seed:
            errors.append(f"seed systems extra (not in HLR): {sorted(extra_in_seed)}")

    if hlr_scenes != seed_scenes:
        missing_in_seed = hlr_scenes - seed_scenes
        extra_in_seed = seed_scenes - hlr_scenes
        if missing_in_seed:
            errors.append(f"seed scenes missing: {sorted(missing_in_seed)}")
        if extra_in_seed:
            errors.append(f"seed scenes extra: {sorted(extra_in_seed)}")

    for spec in hlr.mechanic_specs:
        if spec.system_name not in seed_systems:
            errors.append(f"mechanic_spec '{spec.system_name}' not in scope systems")
            continue
        # At least one property node must exist for this mechanic
        found = any(n.declared_by.startswith(f"hlr_seed:{spec.system_name}") for n in imap.nodes.values())
        if not found and spec.properties:
            errors.append(
                f"mechanic_spec '{spec.system_name}' declared {len(spec.properties)} properties "
                f"but none made it into the seed — check role/owner mapping"
            )

    # Sanity: every system in scope has at least one edge (otherwise it's dead weight)
    systems_with_edges = (
        {e.system for e in imap.write_edges} |
        {e.system for e in imap.read_edges}
    )
    dead_systems = seed_systems - systems_with_edges
    if dead_systems:
        errors.append(
            f"systems in scope with no write/read edges (dead weight): {sorted(dead_systems)}"
        )

    return errors


def validate_impact_map_structural(imap: ImpactMap) -> list[str]:
    """Structural checks that apply at every phase."""
    errors: list[str] = []

    # Every write edge targets a declared node
    for e in imap.write_edges:
        if e.target not in imap.nodes:
            errors.append(f"write edge {e.system}→{e.target}: target not in nodes")
        if e.system not in imap.systems:
            errors.append(f"write edge {e.system}→{e.target}: system not in scope")
        for s in e.scene_scope:
            if s not in imap.scenes:
                errors.append(f"write edge {e.system}→{e.target}: scene '{s}' not in scope")
        if e.condition is not None:
            errors.extend(f"write {e.system}→{e.target} condition: {m}" for m in validate_expr(e.condition))
        if e.formula is not None:
            errors.extend(f"write {e.system}→{e.target} formula: {m}" for m in validate_expr(e.formula))

    # Every read edge sources an existing node
    for e in imap.read_edges:
        if e.source not in imap.nodes:
            errors.append(f"read edge {e.system}←{e.source}: source not in nodes")
        # Readers may be systems OR entity names (HUD widgets read properties) — don't enforce system scope here
        for s in e.scene_scope:
            if s not in imap.scenes:
                errors.append(f"read edge {e.system}←{e.source}: scene '{s}' not in scope")

    # Derived nodes: derivation refs must exist
    for n in imap.nodes.values():
        if n.category == Category.DERIVED and n.derivation is not None:
            for ref in expr_refs(n.derivation):
                if ref not in imap.nodes and not ref.startswith(("const.", "event.")):
                    errors.append(f"derived node {n.id}: derivation references unknown '{ref}'")
        if n.initial_value is not None:
            errors.extend(f"node {n.id} initial_value: {m}" for m in validate_expr(n.initial_value))
        if n.derivation is not None:
            errors.extend(f"node {n.id} derivation: {m}" for m in validate_expr(n.derivation))

    return errors
