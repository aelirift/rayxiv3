## CardVisual — renders a single card on screen.
## Handles hover, click, drag interactions.
class_name CardVisual
extends Control

signal card_clicked(card_visual: CardVisual)

var card_data: CardData = null
var face_up: bool = true
var selected: bool = false
var hovering: bool = false

@onready var bg: ColorRect = $BG
@onready var name_label: Label = $NameLabel
@onready var type_label: Label = $TypeLabel
@onready var stats_label: Label = $StatsLabel
@onready var desc_label: Label = $DescLabel
@onready var cost_label: Label = $CostLabel
@onready var art_rect: TextureRect = $ArtRect

const CARD_W: int = 100
const CARD_H: int = 140

func _ready() -> void:
	custom_minimum_size = Vector2(CARD_W, CARD_H)
	mouse_entered.connect(_on_hover)
	mouse_exited.connect(_on_unhover)

func setup(data: CardData) -> void:
	card_data = data
	_update_display()

func _update_display() -> void:
	if card_data == null:
		return

	if not face_up:
		bg.color = Color(0.2, 0.15, 0.4)
		name_label.text = ""
		type_label.text = ""
		stats_label.text = ""
		desc_label.text = ""
		cost_label.text = ""
		return

	# Card type colors
	match card_data.card_type:
		"creature":
			bg.color = Color(0.75, 0.7, 0.55)
		"spell":
			bg.color = Color(0.3, 0.6, 0.5)
		"trap":
			bg.color = Color(0.6, 0.3, 0.5)
		"land":
			bg.color = Color(0.4, 0.55, 0.3)
		_:
			bg.color = Color(0.5, 0.5, 0.5)

	name_label.text = card_data.card_name
	type_label.text = card_data.card_type.to_upper()
	desc_label.text = card_data.description

	if card_data.card_type == "creature":
		stats_label.text = "%d/%d" % [card_data.attack, card_data.defense]
	else:
		stats_label.text = ""

	if card_data.cost > 0:
		cost_label.text = str(card_data.cost)
	else:
		cost_label.text = ""

	# Load art if available
	if card_data.art_path != "" and FileAccess.file_exists(card_data.art_path):
		art_rect.texture = load(card_data.art_path) as Texture2D

func _on_hover() -> void:
	hovering = true
	if not selected:
		modulate = Color(1.2, 1.2, 1.0)
		position.y -= 10

func _on_unhover() -> void:
	hovering = false
	if not selected:
		modulate = Color.WHITE
		position.y += 10

func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb: InputEventMouseButton = event as InputEventMouseButton
		if mb.pressed and mb.button_index == MOUSE_BUTTON_LEFT:
			card_clicked.emit(self)
