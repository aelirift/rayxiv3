"""Pydantic models for the spec drill-down phases.

HLR produces GameIdentity — what the game IS.
  - Base fields guaranteed for every game.
  - Dynamic fields are genre-specific, added by schema expansion.
  - Enums declare the vocabulary downstream phases can reference.
  - game_systems enum drives MLR interaction file decomposition.

MLR produces per-scene decomposed files:
  - _fsm.json — scene sub-states and transitions
  - _collisions.json — collision pairs
  - _interactions_{system}.json — one per game_system enum value
  - {entity}.json — one per entity in the scene

DLR (not yet built) adds actual values, key bindings, frame data.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# HLR: Schema expansion
# ---------------------------------------------------------------------------


class SchemaField(BaseModel):
    field_name: str
    field_type: str
    description: str
    required: bool = True


# ---------------------------------------------------------------------------
# Shared: EnumDef
# ---------------------------------------------------------------------------


class EnumDef(BaseModel):
    name: str
    values: list[str]
    description: str = ""
    entity: bool = False  # True = values are instantiable objects in scenes. False = metadata/categories.
    value_descriptions: dict[str, str] = {}  # optional per-value descriptions (e.g. for game_systems)
    value_template_origins: dict[str, str] = {}  # debug/audit: which template system each value descended from


# ---------------------------------------------------------------------------
# Mechanic specs — structured representation of CUSTOM (non-template) systems
# so MLR/DLR/DAG/Impact can consume them without losing fidelity.
# HLR emits one per game_systems value with origin='(new)'.
# ---------------------------------------------------------------------------


class MechanicPropertySpec(BaseModel):
    role: str                    # fighter | projectile | hud | game | stage
    name: str                    # snake_case property name
    type: str                    # int | float | bool | string | Vector2
    scope: str = "instance"      # instance | player_slot | game | character_def
    purpose: str = ""            # why it exists
    written_by: list[str] = []   # systems that write it
    read_by: list[str] = []      # systems that read it
    reset_on: str = ""           # empty, or "round_start" / "match_start" / etc.


class MechanicHudEntity(BaseModel):
    name: str                    # must match an hud_elements enum value
    godot_node: str              # Control | ProgressBar | Label | etc.
    displays: str                # what it shows
    reads: list[str] = []        # property names it reads
    visual_states: str           # how user can distinguish states (e.g. "3 segments for 3 stacks")


class MechanicEffectSpec(BaseModel):
    verb: str                    # must be one of MLR's VALID_VERBS
    target: str                  # entity.property or role.property
    description: str = ""


class MechanicInteractionSpec(BaseModel):
    trigger: str                 # what starts this interaction
    condition: str = ""          # guard
    effects: list[MechanicEffectSpec] = []


class MechanicConstant(BaseModel):
    name: str
    type: str                    # int | float
    purpose: str                 # what it controls
    value_hint: str = ""         # rough guidance for DLR, optional


class MechanicSpec(BaseModel):
    """Structured definition of a custom (non-template) feature.
    Emitted by HLR for every game_systems value with value_template_origins='(new)'."""
    system_name: str
    summary: str = ""
    properties: list[MechanicPropertySpec] = []
    hud_entities: list[MechanicHudEntity] = []
    interactions: list[MechanicInteractionSpec] = []
    constants_for_dlr: list[MechanicConstant] = []


# ---------------------------------------------------------------------------
# HLR product: GameIdentity
# ---------------------------------------------------------------------------


class SceneListEntry(BaseModel):
    scene_name: str
    purpose: str
    fsm_state: str
    children: list["SceneListEntry"] = []


class GlobalFSM(BaseModel):
    states: list[str]
    transitions: list[str]


class GameIdentity(BaseModel):
    model_config = ConfigDict(extra="allow")

    game_name: str
    genre: str
    player_mode: str
    scenes: list[SceneListEntry]
    global_fsm: GlobalFSM
    global_rules: list[str]
    win_condition: str | None = None
    kb_sources: list[str] = []
    enums: list[EnumDef] = []
    mechanic_specs: list[MechanicSpec] = []  # structured specs for (new) systems

    def mechanic_spec_for(self, system_name: str) -> MechanicSpec | None:
        for m in self.mechanic_specs:
            if m.system_name == system_name:
                return m
        return None

    def get_extra(self, key: str, default: Any = None) -> Any:
        if self.model_extra:
            return self.model_extra.get(key, default)
        return default

    def extra_fields(self) -> dict[str, Any]:
        return dict(self.model_extra) if self.model_extra else {}

    def get_enum(self, name: str) -> list[str]:
        for e in self.enums:
            if e.name == name:
                return e.values
        return []

    def enum_dict(self) -> dict[str, list[str]]:
        return {e.name: e.values for e in self.enums}


# ---------------------------------------------------------------------------
# HLR Step 2: Impact Matrix — SEES / DOES / TRACKS / RUNS
# ---------------------------------------------------------------------------


class VisualImplication(BaseModel):
    """Something that must be visible. Implies an asset requirement."""
    entity: str           # which entity needs this visual
    visual: str           # description of what's seen
    asset_type: str       # "sprite", "animation", "particle", "sound", "ui_element"
    asset_id: str         # identifier for the asset (e.g. "ryu_hadouken_throw")


class InputImplication(BaseModel):
    """An input action triggered by the player."""
    input_action: str     # action name (e.g. "fire_hadouken")
    input_trigger: str    # key/sequence from global input_scheme (e.g. "QCF + punch")
    target_entity: str    # which entity/role responds
    conditions: list[str] = []  # guards (e.g. "state != stunned")


class PropertyImplication(BaseModel):
    """A property created/required by a requirement. Must justify its existence."""
    name: str             # property name
    owner: str            # entity/role that owns it
    owner_scope: str      # "instance" | "player_slot" | "character_def" | "game"
    type: str             # int, float, bool, string, Vector2, enum
    written_by: list[str] = []   # system(s) that write it
    read_by: list[str] = []      # system(s) that read it
    purpose: str = ""     # why this property exists (link to requirement)


class SystemRole(BaseModel):
    """What a system does for a specific requirement."""
    system: str           # game_system name
    responsibility: str   # what it does for this requirement


class ImpactEntry(BaseModel):
    """Full SEES/DOES/TRACKS/RUNS trace for one requirement."""
    requirement_id: str   # unique id
    requirement_text: str # human-readable
    source_type: str      # "game_system" | "special_move" | "character" | "global_rule" | "scene"
    source_ref: str       # e.g. "combat_system", "hadouken"

    sees: list[VisualImplication] = []
    does: list[InputImplication] = []
    tracks: list[PropertyImplication] = []
    runs: list[SystemRole] = []


class MechanicDefinition(BaseModel):
    """Game-level mechanic — defined once, applies to all entities of a type."""
    name: str             # e.g. "stun_mechanic"
    system: str           # which game_system owns it
    description: str      # plain-English what it does
    trigger: str          # what starts it
    effect: str           # what it does
    properties_read: list[str] = []   # properties it reads (owner.prop format)
    properties_written: list[str] = []  # properties it writes


class JustifiedProperty(BaseModel):
    """A property with full justification chain — derived from impact matrix."""
    name: str
    owner: str
    owner_scope: str
    type: str
    written_by: list[str] = []
    read_by: list[str] = []
    justified_by: list[str] = []  # requirement_ids that created this property


class ImpactMatrix(BaseModel):
    """HLR Step 2 product: full game requirement impact analysis."""
    game_name: str
    entries: list[ImpactEntry] = []
    mechanics: list[MechanicDefinition] = []

    def all_properties(self) -> list[PropertyImplication]:
        """Flatten all tracked properties from all entries."""
        props: list[PropertyImplication] = []
        for entry in self.entries:
            props.extend(entry.tracks)
        return props

    def properties_by_owner(self) -> dict[str, list[JustifiedProperty]]:
        """Merge + dedup properties grouped by owner entity."""
        merged: dict[str, dict[str, JustifiedProperty]] = {}
        for entry in self.entries:
            for prop in entry.tracks:
                owner_props = merged.setdefault(prop.owner, {})
                if prop.name in owner_props:
                    # Merge readers/writers/justifications
                    existing = owner_props[prop.name]
                    existing.written_by = list(set(existing.written_by + prop.written_by))
                    existing.read_by = list(set(existing.read_by + prop.read_by))
                    existing.justified_by = list(set(existing.justified_by + [entry.requirement_id]))
                else:
                    owner_props[prop.name] = JustifiedProperty(
                        name=prop.name,
                        owner=prop.owner,
                        owner_scope=prop.owner_scope,
                        type=prop.type,
                        written_by=list(prop.written_by),
                        read_by=list(prop.read_by),
                        justified_by=[entry.requirement_id],
                    )
        return {owner: list(props.values()) for owner, props in merged.items()}

    def all_assets(self) -> list[VisualImplication]:
        """Flatten all visual/asset implications."""
        assets: list[VisualImplication] = []
        for entry in self.entries:
            assets.extend(entry.sees)
        return assets

    def systems_for_requirement(self, req_id: str) -> list[str]:
        """Which systems does a requirement involve?"""
        for entry in self.entries:
            if entry.requirement_id == req_id:
                return [r.system for r in entry.runs]
        return []


# ---------------------------------------------------------------------------
# HLR Step 3: Scene Manifest — derived from Impact Matrix
# ---------------------------------------------------------------------------


class SceneEntityRef(BaseModel):
    """An entity that belongs in a scene, with its required properties."""
    entity_name: str
    from_enum: str        # which HLR enum (characters, hud_elements, etc.)
    role: str = ""        # runtime role (e.g. "p1_fighter") — empty if not role-bound
    reason: str = ""      # why it's in this scene


class SceneManifestEntry(BaseModel):
    """What goes in one scene — entities, systems, HUD, roles."""
    scene_name: str
    purpose: str
    active_systems: list[str] = []       # game_systems active in this scene
    entities: list[SceneEntityRef] = []  # entities present
    tracks_properties: list[str] = []    # properties that change in this scene (→ determines HUD)
    roles: dict[str, str] = {}           # role_name → bound_to_enum (e.g. p1_fighter → characters)


class SceneManifest(BaseModel):
    """HLR Step 3 product: per-scene entity/system/role binding."""
    game_name: str
    scenes: list[SceneManifestEntry] = []


# ---------------------------------------------------------------------------
# MLR products: decomposed per-scene files
# ---------------------------------------------------------------------------


class PropertyDecl(BaseModel):
    """Property declaration — name + type, NO value. DLR fills values."""
    name: str
    type: str
    description: str = ""


# --- _fsm.json ---

class SceneFSM(BaseModel):
    scene_name: str
    fsm_state: str
    states: list[str]
    transitions: list[str]


# --- _collisions.json ---

class CollisionPair(BaseModel):
    object_a: str
    object_b: str
    result: str


class SceneCollisions(BaseModel):
    scene_name: str
    collision_pairs: list[CollisionPair] = []


# --- _interactions_{system}.json ---

class Effect(BaseModel):
    verb: str  # "subtract", "add", "spawn", "destroy", "set_state", "apply", "move", etc.
    target: str  # object.property
    description: str = ""


class Interaction(BaseModel):
    trigger: str
    condition: str
    effects: list[Effect]


class SystemInteractions(BaseModel):
    """Interactions for one game_system within one scene."""
    scene_name: str
    game_system: str  # must be in HLR game_systems enum
    interactions: list[Interaction] = []


# --- {entity}.json ---

class ActionSet(BaseModel):
    owner: str
    category: str
    actions: list[str]


# Godot node types the LLM can assign at MLR time.
# Deterministic codegen uses this to pick the right template.
GODOT_NODE_TYPES = [
    # Display / UI
    "Label", "RichTextLabel", "ProgressBar", "TextureProgressBar",
    "Sprite2D", "AnimatedSprite2D", "TextureRect", "ColorRect",
    "Button", "Panel", "Control", "NinePatchRect",
    # Physics / game objects
    "Area2D", "CharacterBody2D", "StaticBody2D", "RigidBody2D",
    # Structural
    "Node2D", "Camera2D", "CanvasLayer",
]


class EntitySpec(BaseModel):
    """MLR product for one entity within a scene."""
    scene_name: str
    entity_name: str
    parent_enum: str  # which HLR enum this belongs to
    object_type: str  # "character", "hud", "effect", "transient", "background", "ui"
    godot_node_type: str = ""  # Godot class: Label, ProgressBar, Sprite2D, CharacterBody2D, etc.
    properties: list[PropertyDecl] = []
    action_sets: list[ActionSet] = []
    scene_enums: list[EnumDef] = []  # enums scoped to this entity within the scene
