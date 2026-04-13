# Kart Racer — Genre Knowledge

## Core View And Course Feel

Modern kart racers are usually presented from a **third-person chase camera**
behind the player vehicle, not a top-down view. The player should read:
- the road surface immediately ahead
- upcoming turns and hazards
- rival vehicles in front and to the sides
- scenery and horizon depth that make speed legible

Tracks should feel **wide and long enough to race on**, with multiple rivals,
clear racing lines, and enough room for drifting, passing, and item play.

## Core Mechanics

### Countdown And Launch
Most kart racers start with a visible countdown before input fully unlocks.
Well-timed acceleration at the start can grant a **launch boost**.

### Drift And Mini-Turbo
Drifting is a defining mechanic:
1. Driver turns while holding a drift input at sufficient speed
2. Vehicle enters a lateral slide state
3. Charge builds over time
4. Releasing drift pays out a short speed boost

### Item Boxes And Inventory
Courses place collectible item boxes on the racing line. Picking one up fills an
inventory slot, and using it creates one of several effect types:
- self speed boost
- projectile
- trap / hazard
- shield / orbiting defense
- temporary invincibility
- area debuff

### Boost Pads, Ramps, And Trick States
Track geometry is an active mechanic source:
- **boost pads** add speed on contact
- **ramps / jumps** create a brief airborne state
- airborne transitions can trigger **trick / landing boosts**

### Coins Or Speed Resources
Many modern kart racers include a collectible resource that affects speed,
positioning, or race economy. If used, it must be shown clearly in the HUD and
have a visible gameplay effect.

### Off-Road, Recovery, And Hazards
The course surface should matter:
- off-road reduces speed and/or handling
- hazards spin out, bounce, or slow the driver
- falling off the course or severe mistakes should have a recovery mechanic

### Alternate Traversal Modes
Later-era kart racers often include at least one traversal variant beyond flat
road driving:
- gliding / air sections
- underwater handling
- adhesion / anti-gravity / wall-riding sections

Not every build needs every variant, but a prompt asking for a later-style
Mario-Kart-like experience should not collapse into a flat road-only prototype.

## AI And Race Management

AI rivals should:
- follow the course
- handle turns and drift opportunities
- use items
- respect the same race states as the player

Race management should include:
- lap and checkpoint progression
- current position / standings
- race finish logic
- post-race result state

## HUD Expectations

At minimum, kart race HUDs usually communicate:
- current lap
- current position
- current item
- speed or pace feedback
- countdown / start state

Often they also include:
- minimap
- collectible resource counter
- boost / drift feedback

## Common Mistakes To Avoid

- Do not default the main race view to top-down when the prompt implies a
  modern kart racer.
- Do not make tracks toy-sized or narrower than the vehicles themselves.
- Do not treat the camera as cosmetic; chase-camera readability is part of the
  mechanic contract.
- Do not stop at a checkpoint racer with drift and one item if the prompt asks
  for a Mario-Kart-like experience.
- If no template exists, synthesize the missing mechanics into canonical
  HLR/MLR/DLR artifacts instead of silently omitting them.
