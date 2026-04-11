"""One-time HLIS (High-Level Impacted Systems) grouping.

LLM call that groups the HLT systems into related clusters and writes
the groups back into the HLT file. Run once per template.

Usage: uv run python scripts/group_hlis.py 2d_fighter
"""

import asyncio
import json
import sys
from pathlib import Path

DST_DIR = Path(__file__).parent.parent / "knowledge" / "mechanic_templates"


HLIS_PROMPT = """\
You are a game architecture analyst. Given a list of game systems with descriptions, \
group them into clusters of tightly-coupled systems. Each cluster represents systems \
that interact closely (read/write each other's properties, depend on each other, or \
share concerns).

Output a JSON object:
{
  "hlis_groups": [
    {
      "name": "string — group name",
      "systems": ["system1", "system2", ...],
      "rationale": "string — why these systems belong together"
    }
  ]
}

Rules:
- Every system MUST be in exactly one group.
- Standalone systems (not coupled to others) form their own single-member group.
- Group sizes can vary — some groups have 1, some have 5+.
- Output ONLY the JSON. No markdown.
"""


async def group_hlis(template_name: str):
    from rayxi.llm.callers import build_callers, build_router

    hlt_path = DST_DIR / f"{template_name}_hlt.json"
    if not hlt_path.exists():
        print(f"Error: {hlt_path} not found")
        return

    hlt = json.loads(hlt_path.read_text())
    systems = hlt.get("systems", {})

    if "hlis_groups" in hlt:
        print(f"HLT already has hlis_groups — skipping. Delete the field to regenerate.")
        return

    # Build the prompt
    sys_list = "\n".join(
        f"- {name}: {info.get('description', '')}"
        for name, info in sorted(systems.items())
    )
    user_prompt = f"## Systems to group ({len(systems)} total)\n{sys_list}"

    callers = build_callers()
    router = build_router(callers)
    caller = router.primary
    print(f"Calling {type(caller).__name__} with {len(systems)} systems...")

    raw = await caller(HLIS_PROMPT, user_prompt, json_mode=True, label="hlis_grouping")
    parsed = json.loads(raw)
    groups = parsed.get("hlis_groups", [])

    # Validate: every system in exactly one group
    grouped_systems: set[str] = set()
    duplicates: list[str] = []
    for g in groups:
        for s in g.get("systems", []):
            if s in grouped_systems:
                duplicates.append(s)
            grouped_systems.add(s)

    missing = set(systems.keys()) - grouped_systems
    extras = grouped_systems - set(systems.keys())

    print(f"\nProduced {len(groups)} groups:")
    for g in groups:
        print(f"  {g['name']}: {g['systems']}")
        print(f"    {g.get('rationale', '')}")

    if missing:
        print(f"\nWARNING: missing systems: {missing}")
    if extras:
        print(f"\nWARNING: extra/invented systems: {extras}")
    if duplicates:
        print(f"\nWARNING: duplicates: {duplicates}")

    if missing or extras or duplicates:
        print("\nFix issues before saving. Aborting.")
        return

    # Save
    hlt["hlis_groups"] = groups
    hlt_path.write_text(json.dumps(hlt, indent=2) + "\n")
    print(f"\nSaved {len(groups)} HLIS groups to {hlt_path.name}")


if __name__ == "__main__":
    template = sys.argv[1] if len(sys.argv) > 1 else "2d_fighter"
    asyncio.run(group_hlis(template))
