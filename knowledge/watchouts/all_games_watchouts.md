# All Games — Watchouts

Missed mechanics that apply to ALL genres. Pipeline must check these during assembly.

## Instance Tracking
- **Missed:** Objects created without instance tracking, making them invisible to collision/FSM/simulation
- **Rule:** Attach instance ID to all game objects except immutable/global objects (constants, config)
- **Fix:** Every spawned object registers with a central registry on creation

## Frame Rate Awareness
- **Missed:** Game logic not tied to delta time, runs differently at different FPS
- **Rule:** All games default to 60 FPS. Physics/movement must use delta time, not raw frame count
- **Fix:** Multiply speed/acceleration by delta in every update function

## Web Export Compatibility
- **Missed:** Shader code using `return` inside `if` blocks — breaks on WebGL/ANGLE
- **Rule:** Use branchless patterns (`mix`, `step`) instead of early returns in fragment shaders
- **Fix:** Restructure shaders to compute all paths and blend with step/mix

## Asset Background Removal
- **Missed:** Sprites with solid color backgrounds used directly without transparency
- **Rule:** All character/item sprites must have transparent backgrounds
- **Fix:** Process uploaded sprite sheets — identify bg color, remove it, save as RGBA PNG

## Registry Bounds Checking
- **Missed:** Registry lookups by index crash when fewer objects exist than expected
- **Rule:** All registry.get() calls must handle None/missing gracefully
- **Fix:** Bounds check before array access, return None for missing entries
