"""Game spec DAG — the structured design artifact.

Every node is either a container (has children) or a leaf (has a value).
Every path from root to leaf is a fully qualified address.
Every leaf traces back to a mechanic template via source_mechanic.

Levels:
  HLR creates top-level nodes (game, scenes, roles, systems)
  Template creates mid-level nodes (properties with categories)
  DLR fills leaf values

Node types:
  GameNode      — root
  SceneNode     — one per scene
  RoleNode      — fighter, projectile, hud_bar, hud_text, stage
  EntityNode    — instance of a role (ryu, ken, p1_health_bar)
  PropertyNode  — leaf: config/state/derived with value
  SystemNode    — game system with mechanics
  MechanicNode  — game-level mechanic definition
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rayxi.knowledge.mechanic_loader import ExpandedGameSchema, PropertySpec, RoleSchema
from rayxi.spec.models import (
    ActionSet,
    CollisionPair,
    GameIdentity,
    ImpactMatrix,
    MechanicDefinition,
    SceneFSM,
    SceneManifest,
    SystemInteractions,
)


@dataclass
class PropertyNode:
    """Leaf node — a property with a value (or pending value)."""
    name: str
    type: str
    category: str           # config, state, derived
    scope: str              # role_generic, instance_unique
    source_mechanic: str    # which mechanic template contributed this
    purpose: str = ""
    # Value fields — empty until DLR fills them
    value: str = ""         # for config: the actual value
    initial: str = ""       # for state: initial value
    formula: str = ""       # for derived: computation
    default: str = ""       # template default
    written_by: list[str] = field(default_factory=list)
    read_by: list[str] = field(default_factory=list)

    @property
    def is_filled(self) -> bool:
        if self.category == "config":
            return bool(self.value or self.default)
        elif self.category == "state":
            return bool(self.initial or self.default)
        elif self.category == "derived":
            return bool(self.formula)
        return False

    @property
    def address(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# System + Mechanic nodes — behavior spec, references properties by name
# ---------------------------------------------------------------------------

@dataclass
class MechanicNode:
    """A game-level rule — trigger/effect pair that a system enforces.

    References properties by "owner.property" strings (e.g. "fighter.current_health").
    Does NOT contain PropertyNodes — those live in the property DAG (EntityNode/game_properties).
    """
    name: str               # e.g. "damage_application"
    system: str             # owning system: "combat_system"
    description: str        # plain-English what it does
    trigger: str            # what starts it: "hit_this_frame == true"
    effect: str             # what it does: "subtract damage from defender.current_health"
    properties_read: list[str] = field(default_factory=list)   # owner.prop refs
    properties_written: list[str] = field(default_factory=list)


@dataclass
class SystemNode:
    """A game system — aggregated view of what it reads, writes, and enforces.

    Built by inverting PropertyNode.written_by/read_by across all entities,
    plus MechanicDefinitions from Impact Matrix. References properties by name,
    never contains them.

    Property DAG owns: data (what exists, what values).
    System DAG owns: behavior (what happens, what reads/writes what).
    """
    name: str               # e.g. "combat_system"
    description: str = ""   # from mechanic template
    processing_order: int = 0
    reads: list[str] = field(default_factory=list)    # "entity_role.property" refs
    writes: list[str] = field(default_factory=list)   # "entity_role.property" refs
    mechanics: list[MechanicNode] = field(default_factory=list)
    active_in_scenes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Entity + Scene nodes — property DAG
# ---------------------------------------------------------------------------

@dataclass
class EntityNode:
    """An instance of a role (ryu, p1_health_bar, suzaku_castle)."""
    name: str
    role: str               # which RoleNode this belongs to
    from_enum: str          # HLR enum (characters, hud_elements, etc.)
    description: str = ""   # semantic description for embedding retrieval
    godot_node_type: str = ""  # Godot class: CharacterBody2D, Area2D, ProgressBar, Label, etc.
    properties: list[PropertyNode] = field(default_factory=list)
    action_sets: list[ActionSet] = field(default_factory=list)  # what this entity can do (from MLR)

    @property
    def config_props(self) -> list[PropertyNode]:
        return [p for p in self.properties if p.category == "config"]

    @property
    def state_props(self) -> list[PropertyNode]:
        return [p for p in self.properties if p.category == "state"]

    @property
    def derived_props(self) -> list[PropertyNode]:
        return [p for p in self.properties if p.category == "derived"]

    @property
    def unfilled_count(self) -> int:
        return sum(1 for p in self.properties if not p.is_filled)

    @property
    def filled_count(self) -> int:
        return sum(1 for p in self.properties if p.is_filled)


@dataclass
class RoleSlotNode:
    """A role slot in a scene (p1_fighter, p2_fighter, active_stage)."""
    slot_name: str          # p1_fighter, p2_fighter
    bound_to_enum: str      # characters, stages
    entities: list[EntityNode] = field(default_factory=list)  # possible instances


@dataclass
class SceneNode:
    """One scene with its entities, systems, and role bindings."""
    name: str
    purpose: str
    role_slots: list[RoleSlotNode] = field(default_factory=list)
    entities: list[EntityNode] = field(default_factory=list)  # non-role entities (HUD, etc.)
    active_systems: list[str] = field(default_factory=list)
    scene_fsm: SceneFSM | None = None  # per-scene sub-states from MLR
    collision_pairs: list[CollisionPair] = field(default_factory=list)  # from MLR _collisions.json
    system_interactions: list[SystemInteractions] = field(default_factory=list)  # from MLR _interactions_{system}.json


@dataclass
class GameDAG:
    """Root of the game spec DAG."""
    game_name: str
    scenes: list[SceneNode] = field(default_factory=list)
    game_properties: list[PropertyNode] = field(default_factory=list)
    # Master entity definitions (role-level, not per-scene)
    fighter_entities: dict[str, EntityNode] = field(default_factory=dict)
    projectile_entities: dict[str, EntityNode] = field(default_factory=dict)
    # System behavior specs — built from property written_by/read_by + Impact mechanics
    systems: dict[str, SystemNode] = field(default_factory=dict)

    def game_config_value(self, name: str, default: str = "") -> str:
        """Retrieve a game-level config property value by name."""
        for p in self.game_properties:
            if p.name == name and p.category == "config":
                return p.value or p.default or default
        return default

    @property
    def total_properties(self) -> int:
        total = len(self.game_properties)
        for e in self.fighter_entities.values():
            total += len(e.properties)
        for e in self.projectile_entities.values():
            total += len(e.properties)
        for scene in self.scenes:
            for e in scene.entities:
                total += len(e.properties)
        return total

    @property
    def total_unfilled(self) -> int:
        total = sum(1 for p in self.game_properties if not p.is_filled)
        for e in self.fighter_entities.values():
            total += e.unfilled_count
        for e in self.projectile_entities.values():
            total += e.unfilled_count
        for scene in self.scenes:
            for e in scene.entities:
                total += e.unfilled_count
        return total


# ---------------------------------------------------------------------------
# Builder: Template + HLR → DAG
# ---------------------------------------------------------------------------

def _props_to_nodes(specs: list[PropertySpec]) -> list[PropertyNode]:
    """Convert PropertySpecs from the template into PropertyNodes."""
    return [
        PropertyNode(
            name=s.name,
            type=s.type,
            category=s.category,
            scope=s.scope,
            source_mechanic=s.source_mechanic,
            purpose=s.purpose,
            default=s.default,
            initial=s.initial,
            formula=s.formula,
            written_by=list(s.written_by),
            read_by=list(s.read_by),
        )
        for s in specs
    ]


# Processing order — defines the frame loop sequence (matters for correctness).
SYSTEM_PROCESSING_ORDER: dict[str, int] = {
    "input_system": 1,
    "charge_system": 2,
    "ai_system": 3,
    "movement_system": 4,
    "combat_system": 5,
    "collision_system": 6,
    "blocking_system": 7,
    "projectile_system": 8,
    "stun_system": 9,
    "combo_system": 10,
    "health_system": 11,
    "animation_system": 12,
    "round_system": 13,
}


def _build_systems(
    dag: GameDAG,
    schema: ExpandedGameSchema,
    impact: ImpactMatrix | None,
) -> None:
    """Build SystemNodes by inverting property read/write + Impact mechanics.

    Populates dag.systems in place.
    """
    # Step 1: Collect per-system reads/writes by scanning all properties
    sys_reads: dict[str, set[str]] = {}
    sys_writes: dict[str, set[str]] = {}
    all_system_names: set[str] = set()

    def _scan_props(owner_label: str, props: list[PropertyNode]) -> None:
        for p in props:
            for sys in p.read_by:
                if sys:
                    sys_reads.setdefault(sys, set()).add(f"{owner_label}.{p.name}")
                    all_system_names.add(sys)
            for sys in p.written_by:
                if sys:
                    sys_writes.setdefault(sys, set()).add(f"{owner_label}.{p.name}")
                    all_system_names.add(sys)

    _scan_props("game", dag.game_properties)
    for name, entity in dag.fighter_entities.items():
        _scan_props("fighter", entity.properties)
    for name, entity in dag.projectile_entities.items():
        _scan_props("projectile", entity.properties)

    # Step 2: Collect active_in_scenes from SceneNode.active_systems
    sys_scenes: dict[str, list[str]] = {}
    for scene in dag.scenes:
        for sys in scene.active_systems:
            sys_scenes.setdefault(sys, []).append(scene.name)
            all_system_names.add(sys)

    # Step 3: Collect descriptions from mechanic template
    sys_descriptions: dict[str, str] = {}
    if hasattr(schema, "mechanic_descriptions"):
        sys_descriptions = dict(schema.mechanic_descriptions)

    # Step 4: Build MechanicNodes from Impact Matrix
    sys_mechanic_nodes: dict[str, list[MechanicNode]] = {}
    if impact:
        for mech_def in impact.mechanics:
            node = MechanicNode(
                name=mech_def.name,
                system=mech_def.system,
                description=mech_def.description,
                trigger=mech_def.trigger,
                effect=mech_def.effect,
                properties_read=list(mech_def.properties_read),
                properties_written=list(mech_def.properties_written),
            )
            sys_mechanic_nodes.setdefault(mech_def.system, []).append(node)
            all_system_names.add(mech_def.system)

    # Step 5: Assemble SystemNodes
    for sys_name in sorted(all_system_names):
        dag.systems[sys_name] = SystemNode(
            name=sys_name,
            description=sys_descriptions.get(sys_name, ""),
            processing_order=SYSTEM_PROCESSING_ORDER.get(sys_name, 99),
            reads=sorted(sys_reads.get(sys_name, set())),
            writes=sorted(sys_writes.get(sys_name, set())),
            mechanics=sys_mechanic_nodes.get(sys_name, []),
            active_in_scenes=sys_scenes.get(sys_name, []),
        )


def build_dag(
    hlr: GameIdentity,
    schema: ExpandedGameSchema,
    manifest: SceneManifest | None = None,
    impact: ImpactMatrix | None = None,
    scene_fsms: dict[str, SceneFSM] | None = None,
    scene_collisions: dict[str, list[CollisionPair]] | None = None,
    scene_interactions: dict[str, list[SystemInteractions]] | None = None,
    entity_action_sets: dict[str, list[ActionSet]] | None = None,
) -> GameDAG:
    """Build the game DAG from HLR + expanded template schema + MLR products.

    manifest: per-scene entity/system bindings (from scene_manifest.py).
    scene_fsms: scene_name → SceneFSM (per-scene sub-states from MLR).
    scene_collisions: scene_name → CollisionPairs (from MLR _collisions.json).
    scene_interactions: scene_name → SystemInteractions list (from MLR _interactions_{system}.json).
    entity_action_sets: entity_name → ActionSets (from MLR {entity}.json).
    """
    dag = GameDAG(game_name=hlr.game_name)

    # Game-level properties
    dag.game_properties = (
        _props_to_nodes(schema.game_config) +
        _props_to_nodes(schema.game_state) +
        _props_to_nodes(schema.game_derived)
    )

    # Fighter entities — one per character, with generic + unique props
    for char in hlr.get_enum("characters"):
        generic_props = _props_to_nodes(schema.fighter_schema.properties)
        unique_props = _props_to_nodes(schema.per_character_unique.get(char, []))
        entity = EntityNode(
            name=char,
            role="fighter",
            from_enum="characters",
            godot_node_type=schema.fighter_schema.godot_base_node or "CharacterBody2D",
            properties=generic_props + unique_props,
        )
        dag.fighter_entities[char] = entity

    # Projectile entities
    for obj in hlr.get_enum("game_objects"):
        if "projectile" in obj.lower():
            entity = EntityNode(
                name=obj,
                role="projectile",
                from_enum="game_objects",
                godot_node_type=schema.projectile_schema.godot_base_node or "Area2D",
                properties=_props_to_nodes(schema.projectile_schema.properties),
            )
            dag.projectile_entities[obj] = entity

    # Scenes — from manifest if available, otherwise basic from HLR
    if manifest:
        for scene_entry in manifest.scenes:
            scene_node = SceneNode(
                name=scene_entry.scene_name,
                purpose=scene_entry.purpose,
                active_systems=list(scene_entry.active_systems),
            )

            # Role slots
            for role_name, bound_enum in scene_entry.roles.items():
                slot = RoleSlotNode(
                    slot_name=role_name,
                    bound_to_enum=bound_enum,
                )
                # Link to master entity definitions
                for char_name, entity in dag.fighter_entities.items():
                    if entity.from_enum == bound_enum:
                        slot.entities.append(entity)
                scene_node.role_slots.append(slot)

            # Non-role entities (HUD, etc.)
            for entity_ref in scene_entry.entities:
                if entity_ref.role:
                    continue  # handled above as role slot
                # Create HUD/UI entity nodes with template properties
                if entity_ref.from_enum == "hud_elements":
                    hud_entity = EntityNode(
                        name=entity_ref.entity_name,
                        role="hud",
                        from_enum=entity_ref.from_enum,
                        godot_node_type=schema.hud_bar_schema.godot_base_node or "ProgressBar",
                        properties=_props_to_nodes(schema.hud_bar_schema.properties),
                    )
                    scene_node.entities.append(hud_entity)
                elif entity_ref.from_enum == "game_objects":
                    # Check if it's a projectile (already in master list) or other
                    if entity_ref.entity_name not in dag.projectile_entities:
                        obj_entity = EntityNode(
                            name=entity_ref.entity_name,
                            role="game_object",
                            from_enum=entity_ref.from_enum,
                        )
                        scene_node.entities.append(obj_entity)
                elif entity_ref.from_enum == "stages":
                    stage_entity = EntityNode(
                        name=entity_ref.entity_name,
                        role="stage",
                        from_enum=entity_ref.from_enum,
                        godot_node_type=schema.stage_schema.godot_base_node or "Sprite2D",
                        properties=_props_to_nodes(schema.stage_schema.properties),
                    )
                    scene_node.entities.append(stage_entity)

            dag.scenes.append(scene_node)
    else:
        # Basic scene structure from HLR
        for scene in hlr.scenes:
            dag.scenes.append(SceneNode(
                name=scene.scene_name,
                purpose=scene.purpose,
            ))

    # Attach MLR products to scenes
    for scene_node in dag.scenes:
        if scene_fsms and scene_node.name in scene_fsms:
            scene_node.scene_fsm = scene_fsms[scene_node.name]
        if scene_collisions and scene_node.name in scene_collisions:
            scene_node.collision_pairs = scene_collisions[scene_node.name]
        if scene_interactions and scene_node.name in scene_interactions:
            scene_node.system_interactions = scene_interactions[scene_node.name]

    # Attach action sets to entities
    if entity_action_sets:
        for name, entity in dag.fighter_entities.items():
            if name in entity_action_sets:
                entity.action_sets = entity_action_sets[name]
        for name, entity in dag.projectile_entities.items():
            if name in entity_action_sets:
                entity.action_sets = entity_action_sets[name]

    # Build system behavior DAG (inverted from property read/write + Impact mechanics)
    _build_systems(dag, schema, impact)

    return dag


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_dag(dag: GameDAG, impact: ImpactMatrix | None = None) -> list[str]:
    """Validate the DAG for completeness and consistency."""
    errors: list[str] = []

    # Every fighter should have the same generic property count
    generic_counts = {}
    for name, entity in dag.fighter_entities.items():
        count = len([p for p in entity.properties if p.scope == "role_generic"])
        generic_counts[name] = count
    if generic_counts:
        counts = list(generic_counts.values())
        if len(set(counts)) > 1:
            errors.append(f"Inconsistent generic property counts: {generic_counts}")

    # Check for duplicate property names within an entity
    for name, entity in dag.fighter_entities.items():
        seen: dict[str, int] = {}
        for p in entity.properties:
            seen[p.name] = seen.get(p.name, 0) + 1
        dupes = {k: v for k, v in seen.items() if v > 1}
        if dupes:
            errors.append(f"Fighter '{name}' has duplicate properties: {dupes}")

    # Every property should have a type
    for name, entity in dag.fighter_entities.items():
        for p in entity.properties:
            if not p.type:
                errors.append(f"{name}.{p.name}: missing type")

    # Config/state properties should have written_by or be constants
    for name, entity in dag.fighter_entities.items():
        for p in entity.properties:
            if p.category == "state" and not p.written_by:
                errors.append(f"{name}.{p.name}: state property with no writer")

    # Derived properties should have a formula
    for name, entity in dag.fighter_entities.items():
        for p in entity.properties:
            if p.category == "derived" and not p.formula:
                errors.append(f"{name}.{p.name}: derived property with no formula")

    # Systems must have at least one read or write
    for sys_name, sys_node in dag.systems.items():
        if not sys_node.reads and not sys_node.writes:
            errors.append(f"System '{sys_name}' has no reads or writes — orphaned")

    # Reconciliation gate: Impact TRACKS ⊆ DAG properties
    if impact:
        errors.extend(_reconcile_impact_to_dag(dag, impact))

    return errors


def _reconcile_impact_to_dag(dag: GameDAG, impact: ImpactMatrix) -> list[str]:
    """Verify every property Impact says is required exists in the DAG.

    Impact (LLM) traced requirements → properties.
    Mechanic template (KB) defined property schemas.
    If they disagree, this catches it.
    """
    errors: list[str] = []

    # Collect all property names in DAG, keyed by role/owner
    dag_props: dict[str, set[str]] = {"game": {p.name for p in dag.game_properties}}
    fighter_props: set[str] = set()
    for entity in dag.fighter_entities.values():
        fighter_props.update(p.name for p in entity.properties)
    dag_props["fighter"] = fighter_props
    dag_props["character"] = fighter_props

    projectile_props: set[str] = set()
    for entity in dag.projectile_entities.values():
        projectile_props.update(p.name for p in entity.properties)
    dag_props["projectile"] = projectile_props

    for entry in impact.entries:
        for prop in entry.tracks:
            owner = prop.owner.lower()
            owner_props = dag_props.get(owner, set())
            # Fuzzy resolve: "p1_fighter" → fighter role
            if not owner_props and "fighter" in owner:
                owner_props = fighter_props
            elif not owner_props and "projectile" in owner:
                owner_props = projectile_props
            if prop.name not in owner_props:
                errors.append(
                    f"Impact→DAG gap: '{prop.owner}.{prop.name}' "
                    f"(req {entry.requirement_id}) not in template"
                )

    # Also check mechanics reference valid properties
    all_props = fighter_props | projectile_props | dag_props["game"]
    for mech in impact.mechanics:
        for ref in mech.properties_read + mech.properties_written:
            # refs are "owner.prop" format — extract prop name
            prop_name = ref.split(".")[-1] if "." in ref else ref
            if prop_name not in all_props:
                errors.append(
                    f"Impact mechanic '{mech.name}' references '{ref}' — "
                    f"property not in DAG"
                )

    return errors


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_dag_summary(dag: GameDAG) -> str:
    """Human-readable DAG summary."""
    lines = [f"Game DAG: {dag.game_name}", ""]

    lines.append(f"Game properties: {len(dag.game_properties)}")
    lines.append("")

    # Fighters
    lines.append(f"Fighters: {len(dag.fighter_entities)}")
    for name, entity in sorted(dag.fighter_entities.items()):
        generic = len([p for p in entity.properties if p.scope == "role_generic"])
        unique = len([p for p in entity.properties if p.scope == "instance_unique"])
        filled = entity.filled_count
        total = len(entity.properties)
        node_type = f" [{entity.godot_node_type}]" if entity.godot_node_type else ""
        lines.append(f"  {name}{node_type}: {total} props ({generic} generic + {unique} unique) — {filled}/{total} filled")
    lines.append("")

    # Projectiles
    if dag.projectile_entities:
        lines.append(f"Projectiles: {len(dag.projectile_entities)}")
        for name, entity in dag.projectile_entities.items():
            lines.append(f"  {name}: {len(entity.properties)} props — {entity.filled_count}/{len(entity.properties)} filled")
        lines.append("")

    # Scenes
    lines.append(f"Scenes: {len(dag.scenes)}")
    for scene in dag.scenes:
        lines.append(f"  {scene.name} — {scene.purpose}")
        if scene.scene_fsm:
            lines.append(f"    fsm: {' → '.join(scene.scene_fsm.states)}")
        if scene.active_systems:
            lines.append(f"    systems: {', '.join(scene.active_systems)}")
        if scene.collision_pairs:
            lines.append(f"    collisions: {len(scene.collision_pairs)} pairs")
        if scene.system_interactions:
            sys_names = [si.game_system for si in scene.system_interactions]
            lines.append(f"    interactions: {', '.join(sys_names)}")
        if scene.role_slots:
            for slot in scene.role_slots:
                instances = [e.name for e in slot.entities]
                lines.append(f"    role: {slot.slot_name} → [{', '.join(instances)}]")
        if scene.entities:
            for e in scene.entities:
                lines.append(f"    entity: {e.name} [{e.role}] — {len(e.properties)} props")
    lines.append("")

    # Systems
    if dag.systems:
        lines.append(f"Systems: {len(dag.systems)}")
        for sys_name in sorted(dag.systems, key=lambda s: dag.systems[s].processing_order):
            sys_node = dag.systems[sys_name]
            mech_count = len(sys_node.mechanics)
            scene_list = ", ".join(sys_node.active_in_scenes) if sys_node.active_in_scenes else "none"
            lines.append(
                f"  [{sys_node.processing_order:2d}] {sys_name}: "
                f"reads {len(sys_node.reads)}, writes {len(sys_node.writes)}, "
                f"{mech_count} mechanics, scenes: {scene_list}"
            )
        lines.append("")

    lines.append(f"TOTAL: {dag.total_properties} properties, {dag.total_unfilled} unfilled")
    return "\n".join(lines)
