## Board — the main game scene controller.
## Manages zones, card visuals, player interaction, phase flow.
extends Node2D

var game_state: GameState
var card_scene: PackedScene
var selected_card: CardVisual = null

# Zone containers
@onready var p1_hand: HBoxContainer = $UI/P1Hand
@onready var p2_hand: HBoxContainer = $UI/P2Hand
@onready var p1_field: HBoxContainer = $UI/P1Field
@onready var p2_field: HBoxContainer = $UI/P2Field
@onready var p1_lp_label: Label = $UI/P1LP
@onready var p2_lp_label: Label = $UI/P2LP
@onready var phase_label: Label = $UI/PhaseLabel
@onready var turn_label: Label = $UI/TurnLabel
@onready var info_label: Label = $UI/InfoLabel
@onready var next_phase_btn: Button = $UI/NextPhaseBtn
@onready var attack_btn: Button = $UI/AttackBtn
@onready var game_over_panel: Panel = $UI/GameOverPanel
@onready var game_over_label: Label = $UI/GameOverPanel/Label

func _ready() -> void:
	# Create game state
	game_state = GameState.new()
	add_child(game_state)

	# Connect signals
	game_state.phase_changed.connect(_on_phase_changed)
	game_state.turn_changed.connect(_on_turn_changed)
	game_state.game_over.connect(_on_game_over)
	next_phase_btn.pressed.connect(_on_next_phase)
	attack_btn.pressed.connect(_on_attack)

	game_over_panel.visible = false
	attack_btn.visible = false

	# Build test decks and start
	var deck1: Array[CardData] = _build_test_deck("Player 1")
	var deck2: Array[CardData] = _build_test_deck("Player 2")
	game_state.init_game(2, 8000, [deck1, deck2])

	_refresh_all()

func _build_test_deck(owner_name: String) -> Array[CardData]:
	var deck: Array[CardData] = []
	# Simple test deck: 20 creatures, 10 spells
	for i in range(20):
		var c: CardData = CardData.new()
		c.id = "%s_creature_%d" % [owner_name, i]
		c.card_name = "Warrior %d" % (i + 1)
		c.card_type = "creature"
		c.attack = randi_range(1, 8) * 100
		c.defense = randi_range(1, 6) * 100
		c.cost = randi_range(1, 5)
		c.description = "ATK %d DEF %d" % [c.attack, c.defense]
		deck.append(c)
	for i in range(10):
		var c: CardData = CardData.new()
		c.id = "%s_spell_%d" % [owner_name, i]
		c.card_name = "Spell %d" % (i + 1)
		c.card_type = "spell"
		c.cost = randi_range(1, 3)
		c.attack = randi_range(2, 5) * 100  # damage amount
		c.description = "Deal %d damage" % c.attack
		deck.append(c)
	return deck

func _refresh_all() -> void:
	_refresh_hand(0, p1_hand, true)
	_refresh_hand(1, p2_hand, false)
	_refresh_field(0, p1_field)
	_refresh_field(1, p2_field)
	_refresh_hud()

func _refresh_hand(player: int, container: HBoxContainer, face_up: bool) -> void:
	# Clear existing
	for child in container.get_children():
		child.queue_free()

	if player >= game_state.hands.size():
		return

	for card_data in game_state.hands[player]:
		var cv: CardVisual = _create_card_visual(card_data, face_up)
		container.add_child(cv)
		if face_up and player == game_state.current_player:
			cv.card_clicked.connect(_on_hand_card_clicked)

func _refresh_field(player: int, container: HBoxContainer) -> void:
	for child in container.get_children():
		child.queue_free()

	if player >= game_state.fields.size():
		return

	for card_data in game_state.fields[player]:
		var cv: CardVisual = _create_card_visual(card_data, true)
		container.add_child(cv)
		if player == game_state.current_player:
			cv.card_clicked.connect(_on_field_card_clicked)

func _refresh_hud() -> void:
	if game_state.life_points.size() >= 2:
		p1_lp_label.text = "P1: %d LP" % game_state.life_points[0]
		p2_lp_label.text = "P2: %d LP" % game_state.life_points[1]

	phase_label.text = game_state.current_phase.to_upper()
	turn_label.text = "Turn %d — Player %d" % [game_state.turn_number, game_state.current_player + 1]

	attack_btn.visible = game_state.current_phase == "battle" and game_state.current_player == 0
	info_label.text = ""

func _create_card_visual(data: CardData, face_up: bool) -> CardVisual:
	var cv: CardVisual = CardVisual.new()

	# Build the visual nodes manually (no scene file needed)
	var bg: ColorRect = ColorRect.new()
	bg.name = "BG"
	bg.custom_minimum_size = Vector2(100, 140)
	bg.size = Vector2(100, 140)
	cv.add_child(bg)

	var name_l: Label = Label.new()
	name_l.name = "NameLabel"
	name_l.position = Vector2(4, 2)
	name_l.size = Vector2(92, 20)
	name_l.add_theme_font_size_override("font_size", 11)
	cv.add_child(name_l)

	var type_l: Label = Label.new()
	type_l.name = "TypeLabel"
	type_l.position = Vector2(4, 72)
	type_l.size = Vector2(60, 16)
	type_l.add_theme_font_size_override("font_size", 9)
	type_l.add_theme_color_override("font_color", Color(0.6, 0.6, 0.6))
	cv.add_child(type_l)

	var stats_l: Label = Label.new()
	stats_l.name = "StatsLabel"
	stats_l.position = Vector2(4, 118)
	stats_l.size = Vector2(92, 20)
	stats_l.add_theme_font_size_override("font_size", 14)
	stats_l.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	cv.add_child(stats_l)

	var desc_l: Label = Label.new()
	desc_l.name = "DescLabel"
	desc_l.position = Vector2(4, 88)
	desc_l.size = Vector2(92, 28)
	desc_l.add_theme_font_size_override("font_size", 9)
	desc_l.autowrap_mode = TextServer.AUTOWRAP_WORD
	cv.add_child(desc_l)

	var cost_l: Label = Label.new()
	cost_l.name = "CostLabel"
	cost_l.position = Vector2(80, 2)
	cost_l.size = Vector2(16, 20)
	cost_l.add_theme_font_size_override("font_size", 14)
	cost_l.add_theme_color_override("font_color", Color(0.2, 0.4, 0.8))
	cv.add_child(cost_l)

	var art_r: TextureRect = TextureRect.new()
	art_r.name = "ArtRect"
	art_r.position = Vector2(8, 22)
	art_r.size = Vector2(84, 48)
	cv.add_child(art_r)

	cv.custom_minimum_size = Vector2(100, 140)
	cv.face_up = face_up
	cv.setup(data)
	return cv

func _on_hand_card_clicked(cv: CardVisual) -> void:
	if game_state.current_phase not in ["main", "main2"]:
		info_label.text = "Can only play cards in Main phase"
		return

	# Find index in hand
	var idx: int = -1
	for i in range(game_state.hands[game_state.current_player].size()):
		if game_state.hands[game_state.current_player][i] == cv.card_data:
			idx = i
			break

	if idx >= 0:
		if game_state.play_card(game_state.current_player, idx):
			info_label.text = "Played: %s" % cv.card_data.card_name
			_refresh_all()
		else:
			info_label.text = "Can't play: %s" % cv.card_data.card_name

func _on_field_card_clicked(cv: CardVisual) -> void:
	selected_card = cv
	info_label.text = "Selected: %s" % cv.card_data.card_name

func _on_next_phase() -> void:
	if not game_state.game_active:
		return
	game_state.advance_phase()
	selected_card = null
	_refresh_all()

func _on_attack() -> void:
	if selected_card == null:
		info_label.text = "Select a creature on your field first"
		return
	if selected_card.card_data.card_type != "creature":
		info_label.text = "Only creatures can attack"
		return

	var opponent: int = 1 - game_state.current_player
	if game_state.fields[opponent].size() > 0:
		# Attack first opponent creature
		var target: CardData = game_state.fields[opponent][0]
		if selected_card.card_data.attack > target.defense:
			var overflow: int = selected_card.card_data.attack - target.defense
			game_state.send_to_graveyard(opponent, 0)
			game_state.deal_damage(opponent, overflow)
			info_label.text = "%s destroyed %s! %d damage" % [selected_card.card_data.card_name, target.card_name, overflow]
		elif selected_card.card_data.attack == target.defense:
			game_state.send_to_graveyard(opponent, 0)
			# Find attacker on field and destroy it too
			for i in range(game_state.fields[game_state.current_player].size()):
				if game_state.fields[game_state.current_player][i] == selected_card.card_data:
					game_state.send_to_graveyard(game_state.current_player, i)
					break
			info_label.text = "Both destroyed!"
		else:
			var recoil: int = target.defense - selected_card.card_data.attack
			game_state.deal_damage(game_state.current_player, recoil)
			for i in range(game_state.fields[game_state.current_player].size()):
				if game_state.fields[game_state.current_player][i] == selected_card.card_data:
					game_state.send_to_graveyard(game_state.current_player, i)
					break
			info_label.text = "%s was destroyed! %d recoil" % [selected_card.card_data.card_name, recoil]
	else:
		# Direct attack
		game_state.deal_damage(opponent, selected_card.card_data.attack)
		info_label.text = "Direct attack! %d damage" % selected_card.card_data.attack

	selected_card = null
	_refresh_all()

func _on_phase_changed(_phase: String) -> void:
	_refresh_all()

func _on_turn_changed(_player: int) -> void:
	_refresh_all()

func _on_game_over(winner_idx: int) -> void:
	game_over_panel.visible = true
	game_over_label.text = "Player %d Wins!" % (winner_idx + 1)
