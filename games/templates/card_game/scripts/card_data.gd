## CardData — data container for a single card definition.
## This is pure data — no rendering, no game logic.
class_name CardData
extends Resource

@export var id: String = ""
@export var card_name: String = ""
@export var card_type: String = "creature"  # creature, spell, trap, land, etc.
@export var description: String = ""

# Stats — used by combat mechanic
@export var attack: int = 0
@export var defense: int = 0

# Cost — used by mana/resource mechanic
@export var cost: int = 0
@export var cost_type: String = "mana"  # which resource this costs

# Art
@export var art_path: String = ""

# Custom properties — mechanics can read these
@export var properties: Dictionary = {}
