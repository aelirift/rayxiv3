# 2D Fighter — Genre Knowledge

## Frame Data

Every move in a 2D fighter has three frame phases:

- **Startup frames**: frames before the hitbox becomes active. During startup you are
  committed but cannot deal damage yet.
- **Active frames**: frames the hitbox is live and can deal damage.
- **Recovery frames**: frames after the active window before you can act again.

### Derived values

```
total_frames    = startup + active + recovery
frame_advantage = hitstun - (startup_next + recovery_current)
```

`frame_advantage > 0` means the attacker recovers before the defender → combo possible.
`frame_advantage < 0` means the defender recovers first → punishable on block.

### Hitstun and Blockstun

- **Hitstun**: frames the defender is locked in a hit animation and cannot act.
- **Blockstun**: frames the defender is locked in a block animation (shorter than hitstun).

Combo window = hitstun_of_current_move - (startup_of_next_move + recovery_of_current_move)

If combo_window > 0, the next move connects before the defender can act → TRUE COMBO.

Example (Ryu light punch link):
  hitstun=13, startup_next=4, recovery_current=8 → window = 13-(4+8) = 1 frame (tight link)

Example (Ryu heavy punch → hadouken cancel):
  Cancelling into a special move skips recovery frames entirely → cancel combo always works
  if the special move startup is less than the hitstun of the normal.

## Input Types

### QCF — Quarter Circle Forward
```
Input sequence: down, down_forward, forward, button
Timing: must complete within ~500ms
Examples: Ryu/Ken Hadouken, Chun-Li Kikoken (uses charge instead — see below)
```

### QCB — Quarter Circle Back
```
Input sequence: down, down_back, back, button
Examples: Ryu/Ken Tatsumaki Senpukyaku
```

### DP — Dragon Punch (forward-down-downforward)
```
Input sequence: forward, down, down_forward, button
Examples: Ryu/Ken Shoryuken
Notes: commonly used for invincible uppercuts / anti-air moves
```

### Charge Move
```
Requirements:
  1. Hold charge_direction for charge_duration_ms (typically 1000ms = 60 frames)
  2. Then press release_direction + button within a short window (typically 100ms)

Examples:
  - Guile Sonic Boom: hold back 1s, then forward + punch
  - Chun-Li Kikoken:  hold back 1s, then forward + punch
  - Chun-Li Spinning Bird Kick: hold down 1s, then up + kick

CRITICAL — Jump Override for Up-Release Charge Moves:
  In a 2D fighter, pressing "up" normally makes the character jump.
  For Spinning Bird Kick (charge down → up+kick), the input system MUST check
  for a completed charge + up input BEFORE processing the jump command.
  Priority order: special_move_detection > normal_move_detection > system_inputs (jump/crouch)
  If the charge condition is satisfied when "up" is pressed, execute the special move
  and do NOT jump.

Implementation requirement:
  - Track a charge_state dict per fighter: {"direction": "down", "start_ms": 1234}
  - On each input event, check if charge_duration_ms has elapsed
  - If release_direction + button matches AND charge is satisfied → execute special
  - Clear the charge state after execution
```

### Rapid Press (Mash)
```
Requirements:
  - Press the same button N times within window_ms
  - Each press must be a fresh button-down event (not held)

Examples:
  - Chun-Li Hyakuretsu Kyaku: kick × 4 within 500ms
  - Some characters' rapid normals

Implementation requirement:
  - Track a press_buffer: list of timestamps for each button
  - On button_down: append timestamp, prune entries older than window_ms
  - If len(press_buffer) >= min_presses → trigger special, clear buffer
```

## Combo System

### Link Combo
Two moves chained where the first move's hitstun is longer than the second move's startup
plus the first move's recovery. Tight frame windows (1-3 frames).

### Cancel Combo
A normal move's recovery is cancelled (skipped) by inputting a special move during
the active or early recovery frames. The special move begins from the cancel point.
Cancel window is typically the active frames plus a few recovery frames.

### Juggle
Hitting an airborne opponent who has already been hit once. Juggle gravity reduces
hitstun each hit. Maximum juggle count is typically 1-2 additional hits.

## Blocking

```
Hold back (away from opponent) to block.
Blocking reduces damage to 0 (for high/mid attacks) or a chip amount (projectiles).
Blocking still applies blockstun — you cannot act during blockstun.
Low attacks: must crouch-block (hold down-back)
Overhead attacks: cannot be blocked crouching
Throws: cannot be blocked at all
```

## Round Structure

```
Best of N rounds (typically 3).
Round ends when: health reaches 0, or timer expires (lowest health wins).
Between rounds: positions reset, health stays depleted (no refill mid-match in SF2).
Match ends when a player wins ceil(N/2) rounds.
```

## Projectile Interaction

```
Two projectiles from opposite players colliding cancel each other out (both destroyed).
A faster or stronger projectile may beat a weaker one (implementation-dependent).
Typically both are destroyed on collision.
```

## Input Buffer

```
Store the last N directional inputs with timestamps.
On each game tick, scan the buffer for matching input sequences.
Check sequences from most-complex to least-complex (special before normal before walk).
Clear matched inputs from the buffer after execution.
Buffer size: 20 inputs, max age: 500ms per input.
```

## Character Archetypes

```
Rushdown: high walk speed, mix-up game, short-range specials (Ken)
Zoner:    projectile zoning, good anti-air, slower walk speed (Ryu)
Charge:   requires held charge; rewards patient play (Guile, Chun-Li)
Grappler: slow walk but command throws bypass blocking
```

## Physics Constants (typical SF2 values)

```
SCREEN_W         = 800
SCREEN_H         = 450
FLOOR_Y          = 380      # y-coordinate of the fighting floor
GRAVITY          = 0.8      # pixels per frame² downward acceleration
JUMP_VELOCITY    = -16      # initial upward velocity on jump
WALK_ANIMATION_FPS = 8
FPS              = 60
```
