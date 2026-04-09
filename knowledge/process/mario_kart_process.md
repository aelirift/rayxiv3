# Mario Kart — Game Process Specification

## Mode
**Single player vs 4 AI karts.** Player starts at 4th position. Race ends when all karts finish.

## Rendering Approach — Mode 7 Pseudo-3D
```
Screen layout (800×600):
  y=0..299   : SKY (gradient, top to bottom)
  y=300      : HORIZON LINE
  y=301..599 : ROAD (scanline raycasted from camera into world plane)
```

**Road scanline algorithm:**
For each screen row `y` from `horizon_y+1` to `screen_h`:
```python
row_dist = camera_height * focal_length / (y - horizon_y)
cos_a = cos(camera_angle)
sin_a = sin(camera_angle)
floor_x = camera_x + row_dist * cos_a
floor_y_w = camera_y + row_dist * sin_a
step_x = 2 * row_dist / screen_w * (-sin_a)
step_y_w = 2 * row_dist / screen_w * cos_a
floor_x -= step_x * (screen_w / 2)
floor_y_w -= step_y_w * (screen_w / 2)
for col in range(screen_w):
    color = sample_road_at(floor_x, floor_y_w)  # grass or road based on track
    draw_pixel(col, y, color)
    floor_x += step_x
    floor_y_w += step_y_w
```

**Sprite rendering:**
```python
# For each sprite in depth-sorted order (farthest first):
dx = sprite.world_x - camera_x
dy = sprite.world_y - camera_y
# Transform to camera space
inv_det = 1.0 / (plane_x * dir_y - dir_x * plane_y)
transform_x = inv_det * (dir_y * dx - dir_x * dy)
transform_y = inv_det * (-plane_y * dx + plane_x * dy)  # depth
if transform_y <= 0: continue  # behind camera
sprite_screen_x = int(screen_w / 2 * (1 + transform_x / transform_y))
sprite_height = abs(int(screen_h / transform_y))
draw_sprite_column(sprite, sprite_screen_x, sprite_height, transform_y)
```

---

## Global FSM
```
S_MENU → S_CHARACTER_SELECT → S_TRACK_SELECT → S_COUNTDOWN → S_RACING
                                                                    ↓
                                                              S_FINISHED → S_PODIUM → S_MENU
```
**No multiplayer screens. No pause during racing in initial version.**

---

## S_MENU — Start Screen

**Purpose:** Title screen. One key to start.

**Visual:**
- "MARIO KART" title text (large, centered)
- "Press SPACE to start" blinking below
- Animated background kart driving across bottom

**Inputs:** SPACE → transition to S_CHARACTER_SELECT

---

## S_CHARACTER_SELECT — Character Selection

**Purpose:** Player picks a kart character from a grid.

**Visual:**
- Grid of 8 character portraits (2 rows × 4 cols)
- Selection cursor (highlighted box)
- Character name + stat bars (speed/accel/handling) below grid
- Characters: Mario, Luigi, Peach, Toad, Yoshi, DK, Bowser, Koopa

**Inputs:**
- Arrow keys: move cursor
- SPACE/ENTER: confirm selection → S_TRACK_SELECT

**Properties initialized:** player.character_id, player.stats (from KB)

---

## S_TRACK_SELECT — Track Selection

**Purpose:** Pick which track to race on.

**Visual:**
- Track name list or mini-map thumbnails
- Highlighted selection

**Inputs:**
- UP/DOWN: move selection
- SPACE/ENTER: confirm → S_COUNTDOWN

**Properties initialized:** race.track_id, race.waypoints, race.road_half_width

---

## S_COUNTDOWN — Pre-Race Countdown

**Purpose:** 3-2-1-GO! sequence before race begins.

**Visual:**
- Mode 7 road already visible from starting position
- All karts on starting grid
- Large "3", "2", "1", "GO!" displayed center screen
- Each number shown for 1 second

**Duration:** 3 seconds. Then auto-transition to S_RACING.

**DO NOT:** allow player input to move kart. Start engine animation only.

---

## S_RACING — Active Race

**Purpose:** Main race loop.

**Camera:** Always tracks player kart. camera_x = player.world_x, camera_y = player.world_y, camera_angle = player.angle.

**Road rendering:** Mode 7 scanline algorithm (see above). All 300 scanlines per frame.

**HUD (overlaid after road/sprites drawn):**
- TOP-LEFT: current lap (e.g. "LAP 2/3")
- TOP-RIGHT: current position (e.g. "3rd")  
- BOTTOM-LEFT: current item (icon)
- BOTTOM-CENTER: speedometer
- BOTTOM-RIGHT: mini-map (bird's eye view of track + kart dots)

**Player controls:**
- UP/W: accelerate
- DOWN/S: brake/reverse
- LEFT/A: steer left
- RIGHT/D: steer right
- LSHIFT: drift (hold while turning at speed)
- SPACE: use item

**Race end:** When player completes `max_laps` laps → transition to S_FINISHED.

**Collision:**
- Kart-kart: push apart by collision_push_force
- Kart-item: pick up item box, apply item effect
- Kart-obstacle (banana/shell): spin_out effect (kart spins 360° over 60 frames, loses speed)

**Position tracking:**
```
race_progress = checkpoint_index * 10000 + (1.0 - dist_to_next_cp / segment_len) * 10000
position = rank among all karts by descending race_progress
```

**Item drop rule:** Item box respawns 5 seconds after being collected.

---

## S_FINISHED — Finish Screen

**Purpose:** Show "FINISH!" after player crosses line.

**Visual:** Large "FINISH!" banner over frozen road view. Final position shown.

**Duration:** 2 seconds, then → S_PODIUM.

---

## S_PODIUM — Results Screen

**Purpose:** Show race results.

**Visual:**
- Static background
- "RESULTS" header
- List of all kart names + their finish times/positions (1st through 5th)

**Inputs:** SPACE/ENTER → S_MENU

---

## Mode 7 Implementation Notes

**Track polygon check (on/off road):**
```python
def on_road(world_x, world_y, waypoints, half_width):
    # Find nearest segment, compute perpendicular distance
    min_dist = float('inf')
    for i in range(len(waypoints) - 1):
        seg_dist = point_to_segment_dist(world_x, world_y, waypoints[i], waypoints[i+1])
        min_dist = min(min_dist, seg_dist)
    return min_dist <= half_width
```

**Sky rendering:**
```python
for y in range(horizon_y):
    t = y / horizon_y
    r = lerp(sky_top[0], sky_bottom[0], t)
    g = lerp(sky_top[1], sky_bottom[1], t)
    b = lerp(sky_top[2], sky_bottom[2], t)
    pygame.draw.line(surface, (r, g, b), (0, y), (screen_w, y))
```

**Rumble strips:** world positions within `road_half_width` to `road_half_width + rumble_width` draw in rumble_color.

**Performance tip:** Use `pygame.surfarray` for bulk pixel writes during scanline phase instead of individual draw_pixel calls. Or use a numpy array and blit at end of scanline loop.

---

## AI Kart Behavior

Each AI kart follows the track waypoints:
1. Target next waypoint
2. Steer toward it using angle error * steering_kp
3. Apply rubber-banding: if far behind player, multiply max_speed by rubber_band_mult
4. Use items automatically after random delay (30–60 frames)

---

## CRITICAL RENDERING RULES

1. **DO NOT use pygame 2D top-down view** — always Mode 7 scanline rendering
2. **DO NOT skip the scanline loop** — road must be perspective-projected
3. All karts, items, and obstacles are sprites scaled by `focal_length / depth_z`
4. Player kart is NOT drawn as a sprite — it's the camera. Draw kart shadow/front only.
5. Mini-map shows top-down view of track + all kart positions (2D, in corner HUD only)
