"""One-time template enrichment: add write_verb + rich descriptions to mechanic template."""

import json
import sys
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent.parent / "knowledge" / "mechanic_templates" / "2d_fighter.json"

# Verb inference rules — based on property semantics
WRITE_VERBS: dict[str, str] = {
    # Health / damage — subtract
    "current_health": "subtract",
    # KO state — toggle
    "is_ko": "set",
    # Hitstop / hitstun / blockstun timers — set to value, then countdown
    "hitstop_timer": "set",
    "hitstun_timer": "set",
    "blockstun_timer": "set",
    "stun_timer": "set",
    "special_cooldown_timer": "set",
    # Velocity / push — set
    "pushback_velocity": "set",
    # Position — written by physics — set
    "position": "set",
    "velocity": "set",
    # Facing direction — set
    "facing_direction": "set",
    "is_airborne": "set",
    "is_crouching": "set",
    "is_blocking": "set",
    "block_type": "set_state",
    # Stun system
    "stun_meter": "increment",
    "is_stunned": "set",
    # Combo system
    "combo_count": "increment",
    "juggle_count": "increment",
    "combo_damage_scaling": "set",
    # Projectile
    "projectile_on_screen": "increment",
    "is_firing_projectile": "set",
    "active_hitbox": "set",
    "hit_this_frame": "set",
    # Charge
    "charge_direction": "set_state",
    "charge_frames": "increment",
    "charge_ready": "set",
    # AI
    "ai_decision_timer": "set",
    "ai_current_plan": "set_state",
    # Animation
    "current_animation": "set_state",
    "animation_frame": "increment",
    "sprite_flip_h": "set",
    # Input
    "input_buffer": "set",
    "current_action": "set_state",
    "action_frame": "increment",
    # Round
    "round_wins": "increment",
    "current_round": "increment",
    "round_timer_frames": "set",
    "round_state": "set_state",
    # Projectile state
    "owner_id": "set",
    "active": "set",
    "frames_alive": "increment",
    # HUD
    "display_value": "set",
}

# Rich descriptions: objects, interactions, effects
RICH_DESCRIPTIONS: dict[str, str] = {
    "health_system": (
        "Tracks fighter HP. Objects: fighter, hud_bar. "
        "Interactions: combat_system subtracts damage from current_health on hit, "
        "round_system reads is_ko to detect round end, hud_bar reads health_percent for fill display. "
        "Effects: subtract current_health, set is_ko when health reaches zero, update display_value on hud_bar."
    ),
    "combat_system": (
        "Hit detection, damage calculation, hitstop freeze, pushback. Objects: fighter, projectile, hitbox, hurtbox. "
        "Interactions: reads action_frame and current_action to determine active hitboxes, "
        "checks hit_this_frame against opponent hurtbox, applies damage and hitstun, "
        "triggers hitstop freeze on both attacker and defender. "
        "Effects: subtract current_health, set hitstop_timer and hitstun_timer, apply pushback_velocity."
    ),
    "movement_system": (
        "Walking, jumping, crouching, facing direction, gravity. Objects: fighter, stage. "
        "Interactions: reads input direction to determine walk/jump/crouch, "
        "physics_system applies gravity and updates position, clamps to stage bounds, "
        "always faces opponent for forward/back determination. "
        "Effects: set position, set velocity, set facing_direction, set is_airborne, set is_crouching."
    ),
    "blocking_system": (
        "Stand block, crouch block, chip damage, blockstun. Objects: fighter. "
        "Interactions: detects holding back away from opponent, distinguishes stand vs crouch block, "
        "combat_system applies reduced (chip) damage when blocking, sets blockstun_timer on block. "
        "Effects: set is_blocking, set block_type, set blockstun_timer, apply chip_damage_multiplier."
    ),
    "stun_system": (
        "Stun meter accumulation, dizzy state, stun recovery. Objects: fighter. "
        "Interactions: increments stun_meter on each hit, when meter exceeds threshold sets is_stunned, "
        "stun_timer counts down dizzy duration, input_system blocks input while stunned. "
        "Effects: increment stun_meter, set is_stunned when threshold exceeded, set stun_timer."
    ),
    "combo_system": (
        "Hit counting, damage scaling, juggle tracking. Objects: fighter. "
        "Interactions: increments combo_count on consecutive hits before recovery, "
        "scales combo_damage_scaling down per hit in combo, tracks juggle_count for airborne hits. "
        "Effects: increment combo_count, set combo_damage_scaling, increment juggle_count."
    ),
    "projectile_system": (
        "Projectile spawning, travel, collision, despawn. Objects: fighter, projectile. "
        "Interactions: spawns projectile from fighter on special move input, "
        "tracks projectile_on_screen count limited by projectile_limit, "
        "collision_system detects projectile vs fighter hurtbox, despawns on hit or lifetime expiry. "
        "Effects: spawn projectile, increment projectile_on_screen, set is_firing_projectile, "
        "set owner_id, set active, increment frames_alive."
    ),
    "input_system": (
        "Input buffering, motion detection, action mapping. Objects: fighter, button. "
        "Interactions: reads keyboard or controller buttons, buffers last 20 directional inputs, "
        "detects motion patterns like quarter-circle-forward for special moves, "
        "maps button presses to current_action, blocked during hitstop and hitstun. "
        "Effects: set input_buffer, set current_action, increment action_frame."
    ),
    "animation_system": (
        "State-to-animation mapping, frame timing, sprite management. Objects: fighter, sprite. "
        "Interactions: reads current_action and current_health to pick animation, "
        "advances animation_frame each tick, flips sprite_flip_h based on facing_direction, "
        "displays hit reactions, blocks, KO animations. "
        "Effects: set current_animation, increment animation_frame, set sprite_flip_h."
    ),
    "round_system": (
        "Round management, timer countdown, win tracking, scene transitions. Objects: game, fighter, hud_text. "
        "Interactions: counts down round_timer_frames each tick, reads is_ko to detect KO, "
        "compares health on time-up to determine winner, increments round_wins, "
        "transitions between pre_round, fighting, ko, time_up, round_over states. "
        "Effects: set round_timer_frames, set round_state, increment round_wins, increment current_round."
    ),
    "charge_system": (
        "Charge input tracking for charge-type special moves. Objects: fighter. "
        "Interactions: detects holding back or down direction, accumulates charge_frames, "
        "sets charge_ready when duration reached, releases on opposite direction + button. "
        "Effects: set charge_direction, increment charge_frames, set charge_ready."
    ),
    "special_move_system": (
        "Special move detection, execution, cooldowns. Objects: fighter, projectile. Instance-unique per character. "
        "Interactions: reads input_buffer for motion patterns (qcf, qcb, dp, charge), "
        "checks special_cooldown_timer, executes special move with startup/active/recovery frames, "
        "spawns projectile if move is_projectile. "
        "Effects: set current_action, set special_cooldown_timer, spawn projectile."
    ),
    "ai_system": (
        "CPU opponent decision making. Objects: fighter. "
        "Interactions: reads opponent position and current_action, "
        "decides walk/attack/block/jump based on ai_aggression and ai_difficulty, "
        "delays reactions by ai_reaction_frames to feel human. "
        "Effects: set ai_decision_timer, set ai_current_plan, set current_action."
    ),
    "collision_system": (
        "Hitbox/hurtbox overlap detection, push-out. Objects: fighter, projectile, hitbox, hurtbox. "
        "Interactions: checks active_hitbox of attacker against hurtbox of defender, "
        "uses stand_hurtbox/crouch_hurtbox/air_hurtbox based on fighter state, "
        "pushes fighters apart on overlap to prevent intersection. "
        "Effects: set hit_this_frame, set active_hitbox, set position (push-out)."
    ),
}


def update_template():
    template = json.loads(TEMPLATE_PATH.read_text())

    # Update mechanic descriptions
    for mech_name, mech in template.get("mechanics", {}).items():
        if mech_name in RICH_DESCRIPTIONS:
            mech["description"] = RICH_DESCRIPTIONS[mech_name]

    # Add write_verb to every property that has written_by
    write_verb_count = 0
    for mech_name, mech in template.get("mechanics", {}).items():
        for role_key, role_data in mech.items():
            if not role_key.startswith("contributes_to_"):
                continue
            if not isinstance(role_data, dict):
                continue
            for category in ["config", "state", "derived"]:
                props = role_data.get(category, [])
                for p in props:
                    if not isinstance(p, dict):
                        continue
                    if p.get("written_by") and "write_verb" not in p:
                        verb = WRITE_VERBS.get(p["name"], "set")
                        p["write_verb"] = verb
                        write_verb_count += 1

    # Also handle normal_attack_template and per_special_move_template (parameterized properties)
    # These are state properties written by combat_system / special_move_system
    for mech_name, mech in template.get("mechanics", {}).items():
        for tmpl_key in ["normal_attack_template", "per_special_move_template"]:
            tmpl = mech.get(tmpl_key)
            if not tmpl:
                continue
            for p in tmpl.get("per_attack", []) + tmpl.get("config", []):
                if not isinstance(p, dict):
                    continue
                # These attack properties are config, not written at runtime — skip
                # But mark them with write_verb=set for completeness
                if "write_verb" not in p:
                    p["write_verb"] = "set"
                    write_verb_count += 1

    TEMPLATE_PATH.write_text(json.dumps(template, indent=2) + "\n")
    print(f"Updated template:")
    print(f"  - {len(RICH_DESCRIPTIONS)} mechanic descriptions enriched")
    print(f"  - {write_verb_count} properties tagged with write_verb")


if __name__ == "__main__":
    update_template()
