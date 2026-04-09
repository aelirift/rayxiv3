# Kart Racer — Watchouts

## Road Detection
- **Missed:** Used centerline distance for road detection — inaccurate on curves, missed curbs
- **Rule:** Road detection must sample the track texture pixel color, not calculate distance from centerline
- **Fix:** Sample pixel at kart position, check if gray (road) or green (grass)

## Track Tiling
- **Missed:** Track texture wraps/tiles infinitely, player sees repeated tracks
- **Rule:** Track must not tile. Show grass/void outside track bounds
- **Fix:** Clamp UV coordinates in shader, render grass for out-of-bounds areas

## Camera Scale vs Track Size
- **Missed:** Camera height too high — entire track visible, lanes look tiny
- **Rule:** Camera height must be tuned so road fills ~60% of screen width
- **Fix:** cam_h=8 for 934x964 track at 3x scale gave correct proportions

## Kart Sprite Scaling
- **Missed:** Sprite too large (4x) or too small — doesn't match road width
- **Rule:** Kart sprite should be ~1/8 of road width
- **Fix:** 0.6x scale for SNES sprites on 800x600 screen with cam_h=8

## Countdown Must Complete
- **Missed:** Countdown used wall clock time but test loop ran faster than real time — countdown never finished
- **Rule:** Countdown must use engine delta time, not wall clock
- **Fix:** Accumulate delta in countdown, transition states at thresholds

## AI Karts Need CharacterData
- **Missed:** Only player 0 had CharacterData registered, AI karts crashed on physics lookup
- **Rule:** Every kart (player + AI) must have all required subsystem instances registered
- **Fix:** Create CharacterData, KartPhysics for each kart during race init
