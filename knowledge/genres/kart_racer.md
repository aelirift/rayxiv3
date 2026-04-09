# Kart Racer — Genre Knowledge

## Core Mechanics

### Mode 7 Pseudo-3D Perspective
The defining visual of SNES-era kart racers. The road is not drawn as a sprite or
image — it is **raycasted scanline by scanline** from a virtual camera looking at a
flat world plane.

```
Key parameters:
  camera_height  — how high the camera is above the road plane (e.g. 64 units)
  focal_length   — perspective strength (e.g. 200)
  horizon_y      — screen row where sky meets road (e.g. screen_h / 2)

For each screen row y > horizon_y:
  depth = camera_height * focal_length / (y - horizon_y)
  world_point = camera_pos + depth * camera_forward_vector
  color = sample_texture(world_point)
```

### Sprite Billboard Rendering
All game objects (other karts, items, obstacles) are 2D sprites scaled by depth:
```
sprite_height = focal_length / depth_to_sprite
sprite_y = horizon_y + (screen_h - horizon_y) / 2 - sprite_height / 2
```
Draw in back-to-front order (farthest first) to handle occlusion correctly.

### Lap and Checkpoint System
Progress is tracked via **checkpoints** — invisible trigger lines across the road.
```
race_progress = checkpoint_index * LARGE_NUMBER + distance_along_current_segment
position_rank = sort all karts by descending race_progress
```
Prevents shortcuts. Lap counted when checkpoint 0 is reached after all other checkpoints.

### Drift Boost
```
1. Player holds drift key + steering at speed > drift_min_speed
2. Drift charge timer starts (mini-turbo charges)
3. Release drift → boost fires with magnitude based on charge time
```

### Item Distribution by Position
Items are awarded with probability weighted by race position:
- 1st place → bananas, green shells (defensive)
- Last place → stars, blue shells, triple reds (offensive/catchup)

### Rubber Banding
AI karts adjust speed based on distance to player:
- If AI is far behind → speed multiplier increases (catches up)
- If AI is far ahead → speed multiplier decreases (lets player catch up)

## Physics Model
```
accel applied: velocity += direction * acceleration * dt
friction:      velocity *= friction^dt
turn_rate:     turn = turn_speed * (speed / max_speed)
offroad:       velocity *= offroad_friction; max_speed *= offroad_max_speed_mult
```

## HUD Elements
- Lap counter (e.g. "LAP 2 / 3")
- Position (e.g. "3rd / 5")
- Current item (icon)
- Mini-map (top-down bird's eye showing track outline + kart dots)
- Speedometer

## Common Mistakes to Avoid
- **DO NOT render road as a top-down 2D rectangle** — must use Mode 7 scanlines
- **DO NOT skip sprite depth-sorting** — draw farthest sprites first
- **DO NOT use a flat sprite for road** — road color and texture must shift with perspective
- Mini-map is HUD only, never the main view
