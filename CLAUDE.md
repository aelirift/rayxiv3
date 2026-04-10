# RayXI Project Instructions

## Workflow Rules

- **ALWAYS discuss approach before coding.** Do not implement fixes or changes without first explaining the problem, proposing options, and getting explicit user consent. Even if the fix seems obvious.
- **Fix pipeline, not games.** Always trace game bugs to pipeline root cause. Never patch generated game code directly.
- **Design, not patches.** Redesign pipeline phases properly. Code should be clean by design, not patched.
- **Test one thing at a time.** Implement one change, test it, confirm it works, then move on. Do not batch multiple changes without testing.

## Default Test Case

SF2 with only Ryu as mirror match (Ryu vs Ryu CPU). Run `python run_to_dag.py` to test pipeline steps 1-6 (before build).

## Pipeline Order

1. HLR (LLM — interprets user prompt)
2. KB Template (deterministic — loads genre template)
3. Impact Matrix (deterministic — from template)
4. Scene Manifest (LLM — what goes where)
5. MLR (hybrid — FSM/collisions from LLM, interactions/entities deterministic)
6. Build DAG (deterministic — merges everything)
7. DLR Fill (KB deterministic + LLM for gaps)
8. GDScript Gen (deterministic)
9. Godot Project Gen (deterministic)

## LLM Call Policy

Minimize LLM calls. If something can be derived deterministically from the template, use code. LLM is only for: user intent interpretation (HLR), game flow knowledge (FSM), and filling values KB doesn't cover (DLR).
