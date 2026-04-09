# Game Defaults — RayXI

Universal defaults applied to all generated games unless overridden.

## Framerate

- All games run at **60 FPS** (frames per second)
- Implemented via `clock.tick(60)` in pygame — caps the game loop at 60 iterations/sec
- 60 FPS = 16.67ms per frame budget (game logic + rendering must complete within this)
- If a frame takes longer than 16.67ms, the framerate drops — `clock.tick` is a ceiling, not a guarantee
- Game logic that depends on time (physics, animation) should use fixed timestep or delta-time to handle frame drops gracefully
