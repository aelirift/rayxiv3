## GameState — central game state manager.
## Tracks players, turn order, phases, win conditions.
## Mechanics register hooks into phase transitions.
class_name GameState
extends Node

signal phase_changed(phase_name: String)
signal turn_changed(player_index: int)
signal game_over(winner_index: int)

# Players
var num_players: int = 2
var current_player: int = 0
var life_points: Array[int] = []

# Turn structure
var turn_number: int = 1
var current_phase: String = "draw"
var phases: Array[String] = ["draw", "main", "battle", "main2", "end"]
var phase_index: int = 0

# Game flow
var game_active: bool = false
var winner: int = -1

# Zones per player: hand, field, graveyard, deck
var hands: Array = []       # Array of Array[CardData]
var fields: Array = []      # Array of Array[CardData]
var graveyards: Array = []  # Array of Array[CardData]
var decks: Array = []       # Array of Array[CardData]

# Mechanics hooks — mechanics register callables here
var _on_phase_hooks: Dictionary = {}  # phase_name -> Array[Callable]
var _on_play_hooks: Array[Callable] = []
var _on_draw_hooks: Array[Callable] = []

func init_game(n_players: int, starting_lp: int, deck_lists: Array) -> void:
	num_players = n_players
	game_active = true
	turn_number = 1
	current_player = 0
	phase_index = 0
	current_phase = phases[0]
	winner = -1

	life_points.clear()
	hands.clear()
	fields.clear()
	graveyards.clear()
	decks.clear()

	for i in range(n_players):
		life_points.append(starting_lp)
		hands.append([])
		fields.append([])
		graveyards.append([])
		# Copy deck list
		var deck: Array[CardData] = []
		if i < deck_lists.size():
			for card in deck_lists[i]:
				deck.append(card)
			deck.shuffle()
		decks.append(deck)

	# Draw starting hands (5 cards each)
	for i in range(n_players):
		for _j in range(5):
			draw_card(i)

func draw_card(player: int) -> CardData:
	if player >= decks.size() or decks[player].size() == 0:
		return null
	var card: CardData = decks[player].pop_back()
	hands[player].append(card)
	for hook in _on_draw_hooks:
		hook.call(player, card)
	return card

func play_card(player: int, hand_index: int) -> bool:
	if player >= hands.size() or hand_index >= hands[player].size():
		return false
	var card: CardData = hands[player][hand_index]

	# Check all play hooks (mana cost, etc.) — any returning false blocks
	for hook in _on_play_hooks:
		if not hook.call(player, card):
			return false

	# Move card from hand to field
	hands[player].remove_at(hand_index)
	fields[player].append(card)
	return true

func send_to_graveyard(player: int, field_index: int) -> void:
	if player >= fields.size() or field_index >= fields[player].size():
		return
	var card: CardData = fields[player][field_index]
	fields[player].remove_at(field_index)
	graveyards[player].append(card)

func advance_phase() -> void:
	phase_index += 1
	if phase_index >= phases.size():
		# End of turn — next player
		phase_index = 0
		current_player = (current_player + 1) % num_players
		if current_player == 0:
			turn_number += 1
		# Auto draw at start of turn
		draw_card(current_player)

	current_phase = phases[phase_index]
	phase_changed.emit(current_phase)

	# Run phase hooks
	if current_phase in _on_phase_hooks:
		for hook in _on_phase_hooks[current_phase]:
			hook.call(current_player)

	# Check win conditions
	for i in range(num_players):
		if life_points[i] <= 0:
			winner = 1 - i  # other player wins
			game_active = false
			game_over.emit(winner)

func deal_damage(target_player: int, amount: int) -> void:
	if target_player < life_points.size():
		life_points[target_player] -= amount

# ── Mechanic registration ────────────────────────────────────────

func register_phase_hook(phase: String, hook: Callable) -> void:
	if phase not in _on_phase_hooks:
		_on_phase_hooks[phase] = []
	_on_phase_hooks[phase].append(hook)

func register_play_hook(hook: Callable) -> void:
	_on_play_hooks.append(hook)

func register_draw_hook(hook: Callable) -> void:
	_on_draw_hooks.append(hook)
