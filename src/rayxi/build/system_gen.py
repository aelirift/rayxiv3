"""Game system script generator — produces GDScript for each game system.

Each system reads/writes specific properties on fighter/game nodes.
The mechanic template defines which properties each system uses.
This generator creates deterministic system scripts from the template.

Systems are attached to nodes in the scene and called from the scene's
_physics_process in a defined order.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .dag import GameDAG

_log = logging.getLogger("rayxi.build.system_gen")


# ---------------------------------------------------------------------------
# System processing order (matters for correctness)
# ---------------------------------------------------------------------------

SYSTEM_ORDER = [
    "input_system",
    "charge_system",
    "ai_system",
    "movement_system",
    "combat_system",
    "collision_system",
    "blocking_system",
    "projectile_system",
    "stun_system",
    "combo_system",
    "health_system",
    "animation_system",
    "round_system",
]


# ---------------------------------------------------------------------------
# Input System
# ---------------------------------------------------------------------------

INPUT_SYSTEM_GD = """\
extends Node
## Input System — reads keyboard, maps to fighter actions.
## Processing order: 1st (before everything else)

var p1_fighter: CharacterBody2D
var game_keys: Dictionary

func setup(fighter: CharacterBody2D, keys: Dictionary):
\tp1_fighter = fighter
\tgame_keys = keys

func process_input():
\tif not p1_fighter:
\t\treturn
\t
\t# Skip input during hitstop
\tif p1_fighter.hitstop_timer > 0:
\t\treturn
\t
\t# Movement input
\tvar move_dir = 0
\tif Input.is_key_pressed(OS.find_keycode_from_string(game_keys.get("left", "A"))):
\t\tmove_dir -= 1
\tif Input.is_key_pressed(OS.find_keycode_from_string(game_keys.get("right", "D"))):
\t\tmove_dir += 1
\t
\t# Jump
\tif Input.is_key_pressed(OS.find_keycode_from_string(game_keys.get("up", "W"))):
\t\tif not p1_fighter.is_airborne:
\t\t\tp1_fighter.velocity.y = p1_fighter.jump_velocity_y
\t\t\tp1_fighter.is_airborne = true
\t
\t# Crouch
\tp1_fighter.is_crouching = Input.is_key_pressed(OS.find_keycode_from_string(game_keys.get("down", "S"))) and not p1_fighter.is_airborne
\t
\t# Horizontal movement (walk)
\tif not p1_fighter.is_crouching and not p1_fighter.is_airborne:
\t\tif move_dir != 0:
\t\t\tvar spd = p1_fighter.walk_speed if move_dir == p1_fighter.facing_direction else p1_fighter.back_walk_speed
\t\t\tp1_fighter.velocity.x = move_dir * spd
\t\telse:
\t\t\tp1_fighter.velocity.x = 0
\t
\t# Attack input
\tvar attack_keys = {
\t\t"lp": game_keys.get("lp", "U"), "mp": game_keys.get("mp", "I"), "hp": game_keys.get("hp", "O"),
\t\t"lk": game_keys.get("lk", "J"), "mk": game_keys.get("mk", "K"), "hk": game_keys.get("hk", "L"),
\t}
\tfor action_name in attack_keys:
\t\tvar key = attack_keys[action_name]
\t\tif Input.is_key_pressed(OS.find_keycode_from_string(key)):
\t\t\tif p1_fighter.current_action == "none" or p1_fighter.current_action == "idle":
\t\t\t\tvar pose = "crouch_" if p1_fighter.is_crouching else ("jump_" if p1_fighter.is_airborne else "")
\t\t\t\tvar attack_map = {"lp": "light_punch", "mp": "medium_punch", "hp": "heavy_punch",
\t\t\t\t\t"lk": "light_kick", "mk": "medium_kick", "hk": "heavy_kick"}
\t\t\t\tp1_fighter.current_action = pose + attack_map[action_name]
\t\t\t\tp1_fighter.action_frame = 0
\t\t\t\tbreak
"""


# ---------------------------------------------------------------------------
# Movement System
# ---------------------------------------------------------------------------

MOVEMENT_SYSTEM_GD = """\
extends Node
## Movement System — gravity, position updates, floor/wall clamping.
## Processing order: after input

var fighters: Array = []
var floor_y: float = 912.0
var gravity: float = 1.9
var stage_left: float = 96.0
var stage_right: float = 1824.0

func setup(fighter_list: Array, game_config: Dictionary):
\tfighters = fighter_list
\tfloor_y = game_config.get("floor_y", 912.0)
\tgravity = game_config.get("gravity", 1.9)
\tstage_left = game_config.get("stage_left_bound", 96.0)
\tstage_right = game_config.get("stage_right_bound", 1824.0)

func process_movement(delta: float):
\tfor fighter in fighters:
\t\tif fighter.hitstop_timer > 0:
\t\t\tcontinue
\t\t
\t\t# Apply gravity
\t\tif fighter.is_airborne:
\t\t\tfighter.velocity.y += gravity
\t\t
\t\t# Apply velocity
\t\tfighter.position.x += fighter.velocity.x
\t\tfighter.position.y += fighter.velocity.y
\t\t
\t\t# Floor check
\t\tif fighter.position.y >= floor_y:
\t\t\tfighter.position.y = floor_y
\t\t\tfighter.velocity.y = 0
\t\t\tfighter.is_airborne = false
\t\t
\t\t# Wall clamping
\t\tfighter.position.x = clamp(fighter.position.x, stage_left, stage_right)
\t\t
\t\t# Facing direction (always face opponent)
\t\tvar other = _get_opponent(fighter)
\t\tif other:
\t\t\tfighter.facing_direction = 1 if other.position.x > fighter.position.x else -1

func _get_opponent(fighter: CharacterBody2D) -> CharacterBody2D:
\tfor f in fighters:
\t\tif f != fighter:
\t\t\treturn f
\treturn null
"""


# ---------------------------------------------------------------------------
# Combat System
# ---------------------------------------------------------------------------

COMBAT_SYSTEM_GD = """\
extends Node
## Combat System — attack frame tracking, hit detection, damage application.
## Processing order: after movement

var fighters: Array = []

func setup(fighter_list: Array):
\tfighters = fighter_list

func process_combat(delta: float):
\tfor fighter in fighters:
\t\t# Tick hitstop
\t\tif fighter.hitstop_timer > 0:
\t\t\tfighter.hitstop_timer -= 1
\t\t\tcontinue
\t\t
\t\t# Tick hitstun
\t\tif fighter.hitstun_timer > 0:
\t\t\tfighter.hitstun_timer -= 1
\t\t\tif fighter.hitstun_timer <= 0:
\t\t\t\tfighter.current_action = "none"
\t\t\tcontinue
\t\t
\t\t# Tick blockstun
\t\tif fighter.blockstun_timer > 0:
\t\t\tfighter.blockstun_timer -= 1
\t\t\tif fighter.blockstun_timer <= 0:
\t\t\t\tfighter.is_blocking = false
\t\t\t\tfighter.current_action = "none"
\t\t\tcontinue
\t\t
\t\t# Process current attack
\t\tif fighter.current_action != "none" and fighter.current_action != "idle":
\t\t\tfighter.action_frame += 1
\t\t\tvar atk = fighter.current_action
\t\t\tvar startup = fighter.get(atk + "_startup")
\t\t\tvar active = fighter.get(atk + "_active")
\t\t\tvar recovery = fighter.get(atk + "_recovery")
\t\t\t
\t\t\tif startup == null:
\t\t\t\t# Unknown attack, cancel
\t\t\t\tfighter.current_action = "none"
\t\t\t\tcontinue
\t\t\t
\t\t\tvar total = startup + active + recovery
\t\t\t
\t\t\t# Check if in active frames → can hit
\t\t\tif fighter.action_frame > startup and fighter.action_frame <= startup + active:
\t\t\t\tfighter.hit_this_frame = true
\t\t\t\t_check_hit(fighter, atk)
\t\t\telse:
\t\t\t\tfighter.hit_this_frame = false
\t\t\t
\t\t\t# Attack done
\t\t\tif fighter.action_frame >= total:
\t\t\t\tfighter.current_action = "none"
\t\t\t\tfighter.action_frame = 0

func _check_hit(attacker: CharacterBody2D, attack_name: String):
\tvar defender = _get_opponent(attacker)
\tif not defender:
\t\treturn
\t
\t# Simple distance-based hit check (placeholder for hitbox/hurtbox)
\tvar dist = abs(attacker.position.x - defender.position.x)
\tvar hit_range = 120.0  # placeholder
\t
\tif dist <= hit_range:
\t\tvar damage = attacker.get(attack_name + "_damage")
\t\tif damage == null:
\t\t\tdamage = 30  # default
\t\t
\t\t# Apply damage
\t\tdefender.current_health -= damage
\t\tif defender.current_health < 0:
\t\t\tdefender.current_health = 0
\t\t\tdefender.is_ko = true
\t\t
\t\t# Hitstop
\t\tattacker.hitstop_timer = 8
\t\tdefender.hitstop_timer = 8
\t\t
\t\t# Hitstun
\t\tvar hitstun = attacker.get(attack_name + "_hitstun")
\t\tdefender.hitstun_timer = hitstun if hitstun else 15
\t\t
\t\t# Pushback
\t\tvar push_dir = 1 if attacker.position.x < defender.position.x else -1
\t\tdefender.pushback_velocity = push_dir * 8.0
\t\tdefender.velocity.x = defender.pushback_velocity

func _get_opponent(fighter: CharacterBody2D) -> CharacterBody2D:
\tfor f in fighters:
\t\tif f != fighter:
\t\t\treturn f
\treturn null
"""


# ---------------------------------------------------------------------------
# Round System
# ---------------------------------------------------------------------------

ROUND_SYSTEM_GD = """\
extends Node
## Round System — timer, KO detection, round flow.
## Processing order: last (after all combat resolution)

signal round_over(winner_name: String, reason: String)

var fighters: Array = []
var round_timer_frames: int = 5940  # 99s × 60fps
var round_state: String = "fighting"  # pre_round, fighting, ko, time_up, round_over
var round_timer_label: Label = null
var p1_health_bar: ProgressBar = null
var p2_health_bar: ProgressBar = null

func setup(fighter_list: Array, timer_label: Label = null, p1_bar: ProgressBar = null, p2_bar: ProgressBar = null):
\tfighters = fighter_list
\tround_timer_label = timer_label
\tp1_health_bar = p1_bar
\tp2_health_bar = p2_bar

func process_round(delta: float):
\tif round_state != "fighting":
\t\treturn
\t
\t# Tick timer
\tround_timer_frames -= 1
\tif round_timer_label:
\t\tround_timer_label.text = str(round_timer_frames / 60)
\t
\t# Update health bars
\tif fighters.size() >= 1 and p1_health_bar:
\t\tp1_health_bar.value = (float(fighters[0].current_health) / float(fighters[0].max_health)) * 100.0
\tif fighters.size() >= 2 and p2_health_bar:
\t\tp2_health_bar.value = (float(fighters[1].current_health) / float(fighters[1].max_health)) * 100.0
\t
\t# KO check
\tfor fighter in fighters:
\t\tif fighter.is_ko:
\t\t\tround_state = "ko"
\t\t\tvar winner = _get_opponent(fighter)
\t\t\tvar winner_name = winner.name if winner else "unknown"
\t\t\temit_signal("round_over", winner_name, "KO")
\t\t\treturn
\t
\t# Time up
\tif round_timer_frames <= 0:
\t\tround_state = "time_up"
\t\t# Winner is whoever has more health
\t\tif fighters.size() >= 2:
\t\t\tvar winner_name = fighters[0].name if fighters[0].current_health >= fighters[1].current_health else fighters[1].name
\t\t\temit_signal("round_over", winner_name, "TIME")

func _get_opponent(fighter: CharacterBody2D) -> CharacterBody2D:
\tfor f in fighters:
\t\tif f != fighter:
\t\t\treturn f
\treturn null
"""


# ---------------------------------------------------------------------------
# AI System
# ---------------------------------------------------------------------------

AI_SYSTEM_GD = """\
extends Node
## AI System — CPU opponent random actions.
## Processing order: after input (replaces input for CPU fighters)

var cpu_fighter: CharacterBody2D
var ai_timer: int = 0

func setup(fighter: CharacterBody2D):
\tcpu_fighter = fighter

func process_ai():
\tif not cpu_fighter or not cpu_fighter.is_cpu:
\t\treturn
\t
\t# Skip during hitstop/hitstun
\tif cpu_fighter.hitstop_timer > 0 or cpu_fighter.hitstun_timer > 0:
\t\treturn
\t
\tai_timer -= 1
\tif ai_timer > 0:
\t\treturn
\t
\t# Random decision every N frames based on reaction time
\tai_timer = cpu_fighter.ai_reaction_frames + randi_range(0, 20)
\t
\t# Random action
\tvar roll = randf()
\tif roll < cpu_fighter.ai_aggression * 0.6:
\t\t# Attack
\t\tvar attacks = ["light_punch", "medium_punch", "heavy_punch",
\t\t\t"light_kick", "medium_kick", "heavy_kick"]
\t\tcpu_fighter.current_action = attacks[randi() % attacks.size()]
\t\tcpu_fighter.action_frame = 0
\telif roll < 0.7:
\t\t# Walk toward opponent
\t\tcpu_fighter.velocity.x = cpu_fighter.walk_speed * cpu_fighter.facing_direction
\telif roll < 0.85:
\t\t# Block (walk backward)
\t\tcpu_fighter.velocity.x = -cpu_fighter.back_walk_speed * cpu_fighter.facing_direction
\t\tcpu_fighter.is_blocking = true
\telse:
\t\t# Idle
\t\tcpu_fighter.velocity.x = 0
\t\tcpu_fighter.is_blocking = false
"""


# ---------------------------------------------------------------------------
# System map
# ---------------------------------------------------------------------------

SYSTEM_SCRIPTS = {
    "input_system": INPUT_SYSTEM_GD,
    "movement_system": MOVEMENT_SYSTEM_GD,
    "combat_system": COMBAT_SYSTEM_GD,
    "round_system": ROUND_SYSTEM_GD,
    "ai_system": AI_SYSTEM_GD,
}


def generate_system_scripts(
    dag: GameDAG,
    active_systems: list[str],
    output_dir: Path,
) -> list[Path]:
    """Generate system .gd scripts for the given active systems."""
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []

    for system in active_systems:
        if system in SYSTEM_SCRIPTS:
            path = output_dir / f"{system}.gd"
            path.write_text(SYSTEM_SCRIPTS[system], encoding="utf-8")
            generated.append(path)
            _log.info("System: %s → %s", system, path)
        else:
            # Stub for systems without full implementation
            path = output_dir / f"{system}.gd"
            path.write_text(
                f"extends Node\n## {system} — stub (not yet implemented)\n\n"
                f"func process_{system.replace('_system', '')}(delta: float):\n\tpass\n",
                encoding="utf-8",
            )
            generated.append(path)
            _log.info("System (stub): %s → %s", system, path)

    return generated


def generate_fighting_scene_script(
    dag: GameDAG,
    active_systems: list[str],
    output_dir: Path,
) -> Path:
    """Generate the fighting scene script that wires up all systems."""
    # Build system setup + process calls
    setup_lines: list[str] = []
    process_lines: list[str] = []

    # Determine which systems we have full implementations for
    has_input = "input_system" in active_systems
    has_movement = "movement_system" in active_systems
    has_combat = "combat_system" in active_systems
    has_round = "round_system" in active_systems
    has_ai = "ai_system" in active_systems

    # Read config from DAG instead of hardcoding
    p1_keys = dag.game_config_value("p1_keys", '{"left":"A","right":"D","up":"W","down":"S","lp":"U","mp":"I","hp":"O","lk":"J","mk":"K","hk":"L"}')
    floor_y = dag.game_config_value("floor_y", "912")
    gravity = dag.game_config_value("gravity", "1.9")
    stage_left = dag.game_config_value("stage_left_bound", "96")
    stage_right = dag.game_config_value("stage_right_bound", "1824")
    movement_config = f'{{"floor_y": {floor_y}.0, "gravity": {gravity}, "stage_left_bound": {stage_left}.0, "stage_right_bound": {stage_right}.0}}'

    script = f"""extends Node2D
## Fighting scene — wires up game systems and runs the game loop.
## Generated by RayXI

# System nodes
{"var input_sys: Node" if has_input else ""}
{"var movement_sys: Node" if has_movement else ""}
{"var combat_sys: Node" if has_combat else ""}
{"var round_sys: Node" if has_round else ""}
{"var ai_sys: Node" if has_ai else ""}

# Entity references
var p1_fighter: CharacterBody2D
var p2_fighter: CharacterBody2D
var fighters: Array = []

func _ready():
\t# Get entity references
\tp1_fighter = get_node_or_null("p1_fighter")
\tp2_fighter = get_node_or_null("p2_fighter")
\tif p1_fighter:
\t\tfighters.append(p1_fighter)
\tif p2_fighter:
\t\tfighters.append(p2_fighter)
\t\tp2_fighter.is_cpu = true
\t\tp2_fighter.facing_direction = -1
\t
\t# Initialize health from config
\tfor fighter in fighters:
\t\tfighter.current_health = fighter.max_health
\t
\t# Load and setup systems
{_gen_system_load("input_sys", "input_system", has_input)}
{_gen_system_load("movement_sys", "movement_system", has_movement)}
{_gen_system_load("combat_sys", "combat_system", has_combat)}
{_gen_system_load("round_sys", "round_system", has_round)}
{_gen_system_load("ai_sys", "ai_system", has_ai)}
\t
\t# Setup systems with entity references
{chr(9) + 'input_sys.setup(p1_fighter, ' + p1_keys + ')' if has_input else ''}
{chr(9) + 'movement_sys.setup(fighters, ' + movement_config + ')' if has_movement else ''}
{'\tcombat_sys.setup(fighters)' if has_combat else ''}
{'\tai_sys.setup(p2_fighter)' if has_ai else ''}
\t
\t# Round system with HUD references
{_gen_round_setup(has_round)}
\t
\tprint("Fighting scene ready — ", fighters.size(), " fighters")

func _physics_process(delta):
\t# Process systems in order
{'\tinput_sys.process_input()' if has_input else ''}
{'\tai_sys.process_ai()' if has_ai else ''}
{'\tmovement_sys.process_movement(delta)' if has_movement else ''}
{'\tcombat_sys.process_combat(delta)' if has_combat else ''}
{'\tround_sys.process_round(delta)' if has_round else ''}
"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "fighting.gd"
    path.write_text(script, encoding="utf-8")
    return path


def _gen_system_load(var_name: str, system_name: str, active: bool) -> str:
    if not active:
        return ""
    return (
        f"\t{var_name} = preload(\"res://scripts/systems/{system_name}.gd\").new()\n"
        f"\tadd_child({var_name})"
    )


def _gen_round_setup(has_round: bool) -> str:
    if not has_round:
        return ""
    return (
        "\tvar timer_label = get_node_or_null(\"round_timer_display\")\n"
        "\tvar p1_bar = get_node_or_null(\"p1_health_bar\")\n"
        "\tvar p2_bar = get_node_or_null(\"p2_health_bar\")\n"
        "\tround_sys.setup(fighters, timer_label, p1_bar, p2_bar)"
    )
