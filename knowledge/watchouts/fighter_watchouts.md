# 2D Fighter — Watchouts

## Dynamic Hitboxes for Projectiles
- **Missed:** Fireballs/projectiles spawned without hitboxes — pass through opponents
- **Rule:** Every projectile must dynamically generate a hitbox on spawn, sized to the projectile sprite
- **Fix:** Projectile factory creates hitbox rect matching sprite bounds, registers with collision system

## Character Select Timing
- **Missed:** Character select screen transitions instantly — no delay, no lock-in animation
- **Rule:** Character select needs: hover state → lock-in confirmation → transition delay (1-2 seconds) → fight screen
- **Fix:** FSM for select screen: BROWSING → LOCKED → TRANSITIONING → FIGHT

## Move Cooldowns
- **Missed:** Special moves (fireballs) can be spammed every frame — no cooldown
- **Rule:** Each special move needs a cooldown timer (e.g., 30-60 frames between uses)
- **Fix:** Track last_used_frame per move, gate on cooldown elapsed

## Hitbox Visibility
- **Missed:** Character limbs too small to see hitbox coverage — unclear where attacks land
- **Rule:** Sprites must be large enough that attack animations are visually readable
- **Fix:** Character sprites should be 30-40% of screen height. Debug mode shows hitbox overlays

## Sprite Frame Consistency
- **Missed:** AI-generated animation frames look different from each other — inconsistent character design
- **Rule:** All frames for one character must come from the same source (one sprite sheet or one Minimax batch)
- **Fix:** Generate all poses in a single sprite sheet prompt, or use code-driven transforms from one base image
