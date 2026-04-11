# RayXI Project Instructions

## Workflow Rules

- **ALWAYS discuss approach before coding.** Do not implement fixes or changes without first explaining the problem, proposing options, and getting explicit user consent. Even if the fix seems obvious.
- **Analyze the whole picture before designing.** Never jump to the first design that fixes the surface symptom. Read the relevant code, understand the flow, identify the class of bug, and only then propose a fix that addresses the root cause. Surface-level "this line is broken, here's a patch" is not what I want.
- **Verify freely, execute with permission.** You do not need permission to read, grep, run diagnostic commands, or inspect state. You need explicit approval to edit code.
- **Fix pipeline, not games.** Always trace game bugs to pipeline root cause. Never patch generated game code directly.
- **Design, not patches.** Redesign pipeline phases properly. Code should be clean by design, not patched.
- **Test one thing at a time.** Implement one change, test it, confirm it works, then move on. Do not batch multiple changes without testing.

## Core Architectural Invariants

- **The impact map captures ALL detail impacts.** Every property, value domain (enum), cross-system handoff, trigger condition, and formula must live in the impact map. If a system needs to read or write a value, that value must appear in a `PropertyNode`. If a property has a constrained value set, its `enum_values` must be declared. If systems A and B communicate, both ends of the contract must be explicit in the impact map slice. LLMs generate code from slices — they NEVER invent state, property names, or cross-system contracts. When the LLM needs something that isn't in the slice, the answer is to extend the impact map (MLR/DLR), not to let codegen improvise.
- **Token usage is never a concern.** Do not make bandaid fixes, skip features, truncate context, or drop prompts to save tokens. Correctness and design integrity always win. Expand slices, inline full descriptions, include every relevant edge — if it helps the LLM produce correct code, include it.
- **Genre-agnostic core, game-specific names only from data.** Function names in generated code (`setup`, `process`, `process_damage_event`) are always generic. Any reference to a specific role, entity, property, or value must come from the impact map, HLT, or HLR — never hardcoded in pipeline code. If the pipeline needs to mention "fighter" or "current_action", it reads them from `imap.nodes[*].owner` / `imap.nodes[*].name`, not from a string literal.

## LLM Provider Priority

- `primary:   Kimi`     — all complex codegen, HLR, MLR, DLR, system generation
- `secondary: MiniMax`  — simple/mechanical calls (collisions, HUD, backgrounds)
- `tertiary:  GLM`      — last-resort fallback (rate-limited)

Claude CLI is NOT in the pipeline. Do not add subprocess-based LLM callers.

## Default Test Case

SF2 with only Ryu as mirror match (Ryu vs Ryu CPU). Run `python run_to_impact.py` to generate spec artifacts, then `python scripts/build_game.py sf2_rage 2d_fighter` to build.

## Pipeline Order

1. HLR (LLM — interprets user prompt)
2. KB Template (deterministic — loads genre template)
3. Impact Seed (deterministic — from HLR + template)
4. MLR drill-down (LLM — per-system refinement of impact map, fills enums + formulas)
5. DLR fill (LLM — typed constants + initial values)
6. codegen_runner (per-system: template_python / typed_walker / LLM)
7. scene_gen (deterministic — scenes/{name}.gd from imap.ordered_systems + pools)
8. hud_gen (LLM — custom widgets from mechanic_specs)
9. mechanic_patcher (deterministic — injects imap fighter props into character .gd)
10. Godot import + export

## LLM Call Policy

Every LLM call routes through the router. The primary (Kimi) handles complex codegen; the secondary (MiniMax) handles simple calls via `_SIMPLE_CALL_TYPES`. Use `router.primary` or `router.get("<call_type>")` — never instantiate callers directly.
