# Modern Kart Racer — Game Process Specification

## Mode
Single player vs AI racers. The player selects a driver and vehicle setup, then
runs a lap-based race on a wide track with item boxes, drift boosts, and a
visible countdown into active racing.

## Rendering Approach — Third-Person Chase Racing

The main gameplay view is a **third-person chase camera behind the lead player
vehicle**.

Required presentation characteristics:
- visible road width and upcoming turns
- clear horizon depth and background scenery
- visible rival vehicles ahead/alongside
- readable pickups, boost pads, ramps, and hazards
- track scale large enough that a lap does not feel toy-sized

Do not treat a top-down or scanline-only view as the default for a
Mario-Kart-like prompt unless the user explicitly asks for retro/SNES/Mode-7.

## Global FSM
```
S_MENU -> S_CHARACTER_SELECT -> S_VEHICLE_SELECT -> S_TRACK_SELECT -> S_COUNTDOWN -> S_RACING
                                                                                   |
                                                                                   v
                                                                             S_FINISHED -> S_PODIUM -> S_MENU
```

## S_MENU — Start Screen

Purpose: title / attract screen and entry into race flow.

Visual:
- title/logo area
- prompt to start
- animated background or racer showcase

Input:
- confirm -> `S_CHARACTER_SELECT`

## S_CHARACTER_SELECT — Driver Selection

Purpose: choose the playable driver archetype.

Visual:
- roster portraits or cards
- stats/handling summary
- selection cursor and confirm state

Input:
- move selection
- confirm -> `S_VEHICLE_SELECT`

## S_VEHICLE_SELECT — Vehicle Loadout

Purpose: choose kart / bike / loadout traits that affect speed, acceleration,
handling, traction, and drift feel.

Visual:
- vehicle preview
- stat deltas
- selected loadout summary

Input:
- cycle parts or presets
- confirm -> `S_TRACK_SELECT`

## S_TRACK_SELECT — Course Selection

Purpose: choose the race environment.

Visual:
- track preview card / thumbnail / minimap
- summary of track tone and hazards

Input:
- change track
- confirm -> `S_COUNTDOWN`

## S_COUNTDOWN — Pre-Race Countdown

Purpose: stage racers on the grid, lock movement briefly, then start the race.

Visual:
- full race scene visible behind countdown
- starting grid positions
- countdown values `3`, `2`, `1`, then `GO`

Rules:
- the race should not begin moving before countdown completion
- timed acceleration near the end of countdown may grant a launch boost

## S_RACING — Active Race

Purpose: the main playable race.

### Camera
- chase camera follows behind and slightly above the player vehicle
- camera exposes upcoming turns and enough road width for race decisions
- camera movement communicates speed and turning, but remains readable

### Core Controls
- accelerate
- brake / reverse
- steer left / right
- drift / hop
- use item

### Core Race Mechanics
- countdown completion unlocks racing
- launch boost from correct start timing
- drifting charges mini-turbo and releasing drift grants boost
- item boxes grant inventory
- using items causes mechanic-appropriate effects
- laps and checkpoints govern progress and position
- AI racers compete on the same track

### Track Interaction
Tracks may contain any combination of:
- boost pads
- off-road areas
- ramps / jump points
- hazards
- alternate traversal zones such as gliding, underwater driving, or adhesion /
  anti-gravity sections

### Collision And Recovery
- kart-to-kart contact should resolve without overlap
- trap / projectile hits should visibly affect control or speed
- severe mistakes should have a recovery path instead of soft-locking the race

### HUD
At minimum show:
- lap
- current position
- current item
- countdown / race state

Often also show:
- speed
- minimap
- collectible resource count
- boost / drift feedback

## S_FINISHED — Finish State

Purpose: show completion and freeze or ease out the active race.

Visual:
- finish banner or placement reveal
- final position

Transition:
- after a short pause -> `S_PODIUM`

## S_PODIUM — Results State

Purpose: show final standings and return flow.

Visual:
- race results list
- winner emphasis / celebration

Input:
- confirm -> `S_MENU`

## AI Racer Behavior

AI racers should:
1. follow the course cleanly
2. respect race states and countdown
3. take turns at speed
4. use items in plausible situations
5. remain competitive without teleporting or obviously cheating

## Critical Rules

1. Do not reduce a modern kart-racer prompt to a flat top-down checkpoint game.
2. Do not omit major race mechanics just because no template exists; synthesize
   them into canonical req artifacts.
3. Do not let build or test invent the core feature set later. Countdown, race
   flow, camera model, item flow, drift, progress, and race completion belong in
   HLR/MLR/DLR.
