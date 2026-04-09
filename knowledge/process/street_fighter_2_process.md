# Street Fighter 2 — Game Process Specification

## Mode
**Player vs CPU only.** P1 (human) is always left side. P2 (CPU/AI) is always right side.

## Global FSM
```
S_START → S_CHAR_SELECT → S_FIGHT_INTRO → S_FIGHTING → S_ROUND_OVER
                                               ↑                ↓
                                         S_FIGHT_INTRO ← (rounds < 2 wins)
                                                               ↓
                                                        S_MATCH_OVER → S_START
```
**No other screens.** There is exactly ONE start screen. There are no lobby screens,
no mode select screens, no difficulty screens between S_START and S_FIGHT_INTRO.

---

## S_START — Start Screen

**Purpose:** Entry point. Display title, wait for player to begin.

**Visual:**
- Game title "Street Fighter II" centered
- "Press SPACE to start" blinking text below title
- Static background (dark)

**Active objects:** none (no fighters, no cursors, no data loaded)

**Inputs:**
- SPACE → transition to S_CHAR_SELECT

**Properties initialized here:** none

**DO NOT:** show character portraits, show a mode select, require multiple key presses.

---

## S_CHAR_SELECT — Character Select Screen

**Purpose:** P1 selects a character; CPU auto-selects after P1 confirms.

**Visual:**
- 3 character portrait boxes in a row: [Ryu | Ken | Chun-Li]
- P1 cursor: red highlight box over current selection (starts at Ryu, grid_x=0)
- CPU cursor: blue highlight box (starts at Ken, grid_x=1)
- Selected character name displayed below portrait grid
- P1 side label (left): "P1" with selected character name
- CPU side label (right): "CPU" with selected character name
- Background: character select themed (dark blue or black)

**Character grid (1 row, 3 columns):**
```
grid_x=0 → "ryu"
grid_x=1 → "ken"
grid_x=2 → "chun_li"
```

**P1 inputs:**
- LEFT arrow → p1_cursor.grid_x = max(0, grid_x - 1)
- RIGHT arrow → p1_cursor.grid_x = min(2, grid_x + 1)
- SPACE or ENTER → confirm selection (only if p1_cursor.confirmed == False)

**CPU behavior:**
- After P1 confirms: wait 0.8 seconds, then CPU confirms with its current grid_x
- CPU cursor does NOT move (stays at Ken by default)
- CPU selection is automatic — no input required from player

**On P1 confirm (SPACE/ENTER):**
```
p1_cursor.confirmed = True
p1_fighter.character = CHAR_GRID[p1_cursor.grid_x]   # e.g. "ryu"
p1_fighter.health    = character_data[p1_fighter.character].max_health
p1_fighter.max_health = character_data[p1_fighter.character].max_health
```
**assigns:** [p1_fighter.character, p1_fighter.health, p1_fighter.max_health]

**On CPU confirm (automatic):**
```
p2_cursor.confirmed = True
p2_fighter.character = CHAR_GRID[p2_cursor.grid_x]   # defaults to "ken"
p2_fighter.health    = character_data[p2_fighter.character].max_health
p2_fighter.max_health = character_data[p2_fighter.character].max_health
```
**assigns:** [p2_fighter.character, p2_fighter.health, p2_fighter.max_health]

**Transition trigger:** both p1_cursor.confirmed AND p2_cursor.confirmed → S_FIGHT_INTRO

**Active objects:** p1_cursor, p2_cursor, character_data (ryu/ken/chun_li), p1_fighter (data only), p2_fighter (data only)

---

## S_FIGHT_INTRO — Round Intro

**Purpose:** Announce round, position fighters, count down to FIGHT.

**Visual:**
- Stage background (randomly selected from suzaku_castle, marine_base, bison_arena)
- "Round N" banner centered (N = round_counter.current_round)
- P1 fighter sprite on left (x=200, y=floor_y, facing right)
- CPU fighter sprite on right (x=600, y=floor_y, facing left)
- Both fighters in idle/standing pose, NOT moving
- Countdown display: "3" → "2" → "1" → "FIGHT!" (1 second each)

**Inputs:** none accepted during intro

**Properties initialized here:**
```
round_timer.value = 99          # assigns: [round_timer.value]
p1_fighter.x = 200              # assigns: [p1_fighter.x]
p1_fighter.y = floor_y          # assigns: [p1_fighter.y]
p1_fighter.vx = 0               # assigns: [p1_fighter.vx, p1_fighter.vy]
p1_fighter.health = max_health  # reset to full for new round
p2_fighter.x = 600
p2_fighter.y = floor_y
p2_fighter.health = max_health  # reset to full for new round
p1_fighter.state = "idle"
p2_fighter.state = "idle"
combo_buffer cleared for both fighters
stun_meter reset to 0 for both fighters
```

**Transition trigger:** countdown reaches 0 → S_FIGHTING

**Active objects:** p1_fighter, p2_fighter, stage, round_counter, health_bar x2, round_timer

---

## S_FIGHTING — Fight

**Purpose:** Core gameplay. Fighters battle until health=0 or timer=0.

**Duration:** 99 seconds (round_timer.value counts down, 1 decrement per second)

**Visual:**
- Stage background
- Both fighters rendered at their current positions with current animation state
- P1 health bar: top-left, depletes right→left
- CPU health bar: top-right, depletes left→right
- Round timer: top-center, counting down from 90
- Round win indicators (stars/pips) below each health bar

### P1 Controls (keyboard):
| Key | Action |
|-----|--------|
| A | walk left (back direction when facing right) |
| D | walk right (forward direction when facing right) |
| W | jump |
| S | crouch |
| S + D simultaneously | diagonal: down_fwd |
| S + A simultaneously | diagonal: down_back |
| W + D simultaneously | diagonal: up_fwd |
| W + A simultaneously | diagonal: up_back |
| U | light punch |
| I | medium punch |
| O | heavy punch |
| J | light kick |
| K | medium kick |
| L | heavy kick |
| S + attack key | crouching attack (while crouching) |
| W + attack key (airborne) | jump attack |
| A (hold while attack incoming) | standing block |
| S + A (hold while attack incoming) | crouch block |

**Diagonal input detection:** Every frame, check simultaneous key states. If S and D both held → `down_fwd`. If S and A both held → `down_back`. If W and D both held → `up_fwd`. If W and A both held → `up_back`. Single key press takes priority: W alone = `up`, not diagonal.

**Facing direction:** "back" and "forward" are always relative to which side the fighter is on. P1 on left: A=back, D=forward. If P1 crosses to right side, A=forward, D=back. Input system must resolve relative directions per-frame based on `fighter.facing`.

**Input buffer:** last 20 inputs recorded with frame timestamps. Window = 500ms.
Special move detection runs BEFORE normal attack processing AND before blocking check.

### Blocking:
- **Standing block:** Hold A (back direction) while in `idle` or `walking` state AND an attack is about to connect.
- **Crouch block:** Hold S+A (down_back) while in `crouching` state AND an attack is about to connect.
- Blocking must be checked AFTER special move detection but BEFORE damage application.
- **Blocked hit effects:** damage × 0.25 (25% of full damage), causes `block_stun` (not `hit_stun`), pushes defender backward by 20px.
- **What can be blocked:** standing normals (standing or crouch block), crouching normals (crouch block only — standing block misses), projectiles (standing block only), jump attacks (standing or crouch block).
- **Cannot be blocked:** throws (must tech or accept damage), dizzy state (no block possible while stunned).
- **Crouch block required for low attacks:** if `attack.hits_low == true`, only crouch block works. Standing block does NOT reduce damage for low attacks.

### Crouching Attacks:
- Input: S held + any attack key while in `crouching` state.
- Produces a crouching normal with `hits_low = true` for kicks, `hits_low = false` for punches.
- Crouching medium kick and crouching heavy kick typically hit low and can trip (cause knockdown).
- Hitbox is lower than standing attacks — hits opponents in crouch.
- Cannot special-cancel crouching heavy kick (no cancel window for heavy kicks when crouching).

### Jump Attacks:
- Input: any attack key pressed while fighter `state == jumping`.
- Produces an aerial normal attack with an aerial hitbox — different from standing/crouching hitbox.
- Jump attacks CANNOT be blocked while crouching (they hit overhead).
- Jump attacks CAN be traded (both fighters' hitboxes connect simultaneously).
- Jump attacks enable cross-ups: jumping OVER the opponent and hitting their back — opponent must block the opposite direction.

### Special Move Input Sequences:
- **QCF + punch** (down→down_fwd→fwd + punch): Hadouken (Ryu), Ken_Hadouken (Ken), Kikoken (Chun-Li charge variant)
- **DP + punch** (fwd→down→down_fwd + punch): Shoryuken (Ryu/Ken)
- **QCB + kick** (down→down_back→back + kick): Tatsumaki (Ryu/Ken)
- **Charge back 1s + fwd + punch**: Kikoken (Chun-Li)
- **Charge down 1s + up + kick**: Spinning Bird Kick (Chun-Li) — NOTE: up+kick must NOT trigger jump when charge is complete
- **4+ kicks in 500ms**: Hyakuretsu Kyaku (Chun-Li)

### CPU (P2) AI Behavior:
- Walk toward P1 if distance > 150px
- If distance ≤ 80px: attack with light punch or light kick (random)
- Jump occasionally (random 2% chance per frame)
- No special moves (keep AI simple for testing)
- CPU faces P1 at all times (auto-facing)

### Hitstop (freeze frames on impact):
When an attack connects (hit OR block), both fighters freeze for a fixed number of frames.
During hitstop: no movement, no new inputs processed, animation freezes.
After hitstop expires: hitstun/blockstun begins, fighter resumes animation.

| Attack strength | Hitstop frames |
|----------------|---------------|
| light           | 5             |
| medium          | 8             |
| heavy           | 12            |
| special move    | 10            |
| projectile hit  | 6             |

Implementation: maintain a `hitstop_timer` counter (in frames) on each fighter.
When a hit lands: set `both_fighters.hitstop_timer = hitstop_frames`.
Each frame: decrement `hitstop_timer`. While > 0, skip all update logic for that fighter.

### Stun / Dizzy System:
Each fighter has a `stun_meter` (int, starts at 0 each round).
Every hit (blocked or not) adds the move's `stun` value to `stun_meter`.

When `stun_meter >= character_data[fighter.character].stun_threshold`:
- Fighter enters `stunned` state
- Stars/birds visual spawned above fighter's head
- `stun_meter` resets to 0
- `stun_timer` set to `DIZZY_DURATION_FRAMES = 300` (5 seconds at 60fps)
- During stunned: NO inputs accepted, fighter cannot move or block
- Each frame: decrement `stun_timer`
- When `stun_timer <= 0`: fighter returns to `idle` state

`stun_meter` also decays passively: subtract 1 per frame when fighter is in `idle` state.
This prevents infinite dizzy stacking between hits.

### Collision Detection (every frame):
1. **hitbox vs opponent hurtbox:** if owner differs and hitbox.hit_connected==False → apply damage + hitstun
2. **projectile vs opponent hurtbox:** apply damage + destroy projectile
3. **projectile vs projectile:** both destroyed (projectile clash)
4. **fighter vs wall:** clamp x to [left_wall+20, right_wall-20]
5. **fighter vs fighter overlap:** push both apart horizontally

### Win Conditions (checked every frame):
- `p1_fighter.health <= 0` → CPU wins round → S_ROUND_OVER
- `p2_fighter.health <= 0` → P1 wins round → S_ROUND_OVER
- `round_timer.value <= 0` → fighter with higher health wins round → S_ROUND_OVER

**Transition trigger:** any win condition met → S_ROUND_OVER

**Active objects:** p1_fighter, p2_fighter, p1_hurtbox, p2_hurtbox, hitboxes (transient), projectiles (transient), p1_health_bar, p2_health_bar, round_timer, round_counter, stage

---

## S_ROUND_OVER — Round End

**Purpose:** Display round result, update win counts, decide next state.

**Duration:** 2.5 seconds (no input accepted)

**Visual:**
- Stage and fighters remain visible
- "K.O." text if health depleted, "TIME" if timer expired
- Winner's name briefly displayed
- Round win indicators updated (add win pip for winner)

**Logic:**
```
winner.wins += 1
round_counter updated
round_counter.current_round += 1

if winner.wins >= 2:
    → S_MATCH_OVER
else:
    → S_FIGHT_INTRO   (next round, fighters reset)
```

**Properties reset for next round (on → S_FIGHT_INTRO):**
- `p1_fighter.health = p1_fighter.max_health`
- `p2_fighter.health = p2_fighter.max_health`
- fighter positions reset (200 / 600)
- stun meters cleared
- combo buffers cleared
- all transient objects (hitboxes, projectiles) destroyed

**Active objects:** p1_fighter, p2_fighter, p1_health_bar, p2_health_bar, round_counter, stage

---

## S_MATCH_OVER — Match End

**Purpose:** Display match winner, return to start.

**Duration:** until player presses SPACE

**Visual:**
- "Player Wins!" (if P1 won) or "CPU Wins!" (if CPU won)
- Final round score displayed
- "Press SPACE to continue"

**Inputs:**
- SPACE → full game reset → S_START

**Reset on exit:**
- All fighter state cleared (health, wins, character assignment)
- Both cursors reset (confirmed=False, grid_x back to defaults)
- round_counter reset (p1_wins=0, p2_wins=0, current_round=1)

---

## Fighter State Machine (per fighter)

```
idle ←→ walking
idle → jumping → idle
idle → crouching → idle
idle/walking/crouching → attacking → idle
idle/walking/crouching → special_move → idle
any → hit_stun → idle/knockdown
blocking → block_stun → idle
knockdown → idle
stunned → idle
idle/walking → defeated  (terminal, when health=0 confirmed)
idle/walking → victory   (terminal, when opponent defeated)
```

**State rules:**
- `defeated` and `victory` are terminal for the fighter (match S_ROUND_OVER transition)
- Fighter cannot attack while in hit_stun, block_stun, stunned, knockdown, jumping (unless jump attack)
- Jumping fighter cannot block

---

## Key Invariants

1. `p1_fighter.character` and `p2_fighter.character` MUST be non-empty strings before S_FIGHT_INTRO begins
2. `round_timer.value` MUST be set to 99 at the start of every S_FIGHT_INTRO
3. Fighter health MUST be reset to max_health at the start of every S_FIGHT_INTRO
4. Only ONE active session of S_FIGHTING at any time (no nested fights)
5. **Input processing order every frame (strictly in this order):**
   a. Decrement `hitstop_timer` — if > 0, skip all input/update for this fighter this frame
   b. If `state == stunned`: decrement `stun_timer` — if > 0, skip input; if == 0, set state = idle
   c. Check special move combos in input buffer (highest priority)
   d. Check blocking (back direction held + incoming attack)
   e. Check crouching attacks (S held + attack key, state == crouching)
   f. Check jump attacks (attack key while state == jumping)
   g. Check normal attacks
   h. Check movement (walk/jump/crouch)
6. Charge state persists across frames — do not reset charge on every frame, only on action or direction release
7. `stun_meter` and `hitstop_timer` reset to 0 at the start of every S_FIGHT_INTRO
8. Blocking is only valid when: fighter is in idle/walking/crouching, back direction held, and opponent's attack hitbox overlaps or is imminent
9. Low attacks (crouching kicks with `hits_low=true`) are NOT reduced by standing block — only crouch block works
