"""HLR Step 3 — Scene Manifest.

Derives per-scene entity/system lists from the Impact Matrix.
For each scene, determines:
  - Which entities are present (and why)
  - Which systems are active
  - Which properties change (→ determines HUD needs)
  - Which roles exist (runtime bindings like p1_fighter → characters)

Uses one LLM call per scene to determine relevance from the impact matrix.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from rayxi.knowledge import KnowledgeBase, KnowledgeContext
from rayxi.llm.protocol import LLMCaller
from rayxi.trace import get_trace

from .models import (
    GameIdentity,
    ImpactMatrix,
    SceneEntityRef,
    SceneManifest,
    SceneManifestEntry,
    SceneListEntry,
)

_log = logging.getLogger("rayxi.spec.scene_manifest")
_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "manifest"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_key(system: str, prompt: str) -> str:
    return hashlib.sha256((system + prompt).encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _cache_put(key: str, data: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(data, encoding="utf-8")


async def _call_llm(caller: LLMCaller, system: str, prompt: str, label: str) -> str:
    """Call LLM with cache + retry + trace."""
    trace = get_trace()
    caller_name = type(caller).__name__
    key = _cache_key(system, prompt)
    cached = _cache_get(key)
    if cached is not None:
        _log.info("%s — cache hit (%s)", label, key)
        if trace:
            cid = trace.llm_start("scene_manifest", label, caller_name, len(system) + len(prompt))
            trace.llm_end(cid, output_chars=len(cached), cache_hit=True)
        return cached

    last_err = None
    for attempt in range(3):
        cid = trace.llm_start("scene_manifest", label, caller_name, len(system) + len(prompt)) if trace else ""
        try:
            raw = await caller(system, prompt, json_mode=True, label=label)
            json.loads(raw)
            _cache_put(key, raw)
            if trace:
                trace.llm_end(cid, output_chars=len(raw))
            return raw
        except (json.JSONDecodeError, RuntimeError, Exception) as exc:
            last_err = exc
            if trace:
                trace.llm_end(cid, output_chars=0, error=str(exc)[:120])
            _log.warning("%s attempt %d failed: %s", label, attempt + 1, str(exc)[:120])
    raise RuntimeError(f"{label} failed after 3 attempts: {last_err}")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

MANIFEST_SYSTEM_PROMPT = """\
You are a game design architect. Determine exactly which entities, systems, and \
roles belong in ONE scene of a game.

You will receive:
1. The game's HLR (scenes, enums, systems)
2. The impact matrix summary (which properties each entity has, which systems use them)
3. The specific scene to analyze

For this scene, determine:
- **entities**: Which entities are PRESENT in this scene? Only include entities that \
  serve a function here (displayed, interactive, tracking state). Each entity must come \
  from an HLR enum.
- **active_systems**: Which game_systems are ACTIVE in this scene? Only systems that \
  have work to do. A start_screen doesn't need combat_system. \
  IMPORTANT: You MUST only use system names from the game_systems enum in the HLR. \
  Do NOT invent new system names.
- **tracks_properties**: Which properties CHANGE in this scene? (Not just displayed — \
  actually modified.) This determines which HUD elements are needed.
- **roles**: Runtime role bindings. If this scene uses a role like "p1_fighter" that \
  gets filled by a character from an enum at runtime, declare it here. \
  Format: {"role_name": "bound_to_enum_name"}.

The generic heuristic for entity inclusion:
- An entity belongs if the scene's behavior REQUIRES it (displayed, interacted with, \
  or its state changes here).
- A HUD element belongs if it displays a property that CHANGES in this scene.
- A HUD element does NOT belong if it displays a property that's static/irrelevant here.

Output a JSON object:
{
  "scene_name": "string",
  "purpose": "string",
  "active_systems": ["string — game_system names"],
  "entities": [
    {"entity_name": "string", "from_enum": "string — HLR enum name", "role": "string or empty", "reason": "string — why it's here"}
  ],
  "tracks_properties": ["string — owner.property that changes in this scene"],
  "roles": {"role_name": "bound_to_enum_name"}
}

Output ONLY the JSON. No markdown, no explanation.
"""


# ---------------------------------------------------------------------------
# Per-scene LLM call
# ---------------------------------------------------------------------------

def _build_matrix_summary(matrix: ImpactMatrix) -> str:
    """Compact summary of the impact matrix for the manifest prompt."""
    lines = []
    by_owner = matrix.properties_by_owner()
    for owner, props in sorted(by_owner.items()):
        prop_names = [f"{p.name}({p.type})" for p in sorted(props, key=lambda p: p.name)]
        lines.append(f"  {owner}: {', '.join(prop_names)}")

    systems_involved: dict[str, set[str]] = {}
    for entry in matrix.entries:
        for r in entry.runs:
            systems_involved.setdefault(r.system, set()).add(entry.source_ref)

    sys_lines = []
    for system, refs in sorted(systems_involved.items()):
        sys_lines.append(f"  {system}: involved in {', '.join(sorted(refs))}")

    return (
        "## Entity Properties\n" + "\n".join(lines) + "\n\n"
        "## System Involvement\n" + "\n".join(sys_lines)
    )


def _flatten_scenes(scenes: list[SceneListEntry]) -> list[SceneListEntry]:
    flat: list[SceneListEntry] = []
    for s in scenes:
        flat.append(s)
        if s.children:
            flat.extend(_flatten_scenes(s.children))
    return flat


async def _build_scene_manifest(
    scene: SceneListEntry,
    hlr: GameIdentity,
    matrix: ImpactMatrix,
    caller: LLMCaller,
    base_context: str,
) -> SceneManifestEntry:
    """Build manifest for one scene."""
    prompt = (
        f"{base_context}\n\n"
        f"## Scene to Analyze\n"
        f"Name: {scene.scene_name}\n"
        f"Purpose: {scene.purpose}\n"
        f"FSM State: {scene.fsm_state}\n"
    )

    label = f"manifest[{scene.scene_name}]"
    raw = await _call_llm(caller, MANIFEST_SYSTEM_PROMPT, prompt, label)
    parsed = json.loads(raw)

    return SceneManifestEntry.model_validate(parsed)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run_scene_manifest(
    hlr: GameIdentity,
    matrix: ImpactMatrix,
    caller: LLMCaller,
) -> SceneManifest:
    """Run HLR Step 3: derive scene manifest from impact matrix.

    One LLM call per scene, all in parallel.
    """
    trace = get_trace()
    if trace:
        trace.phase_start("scene_manifest")

    # Build shared context
    hlr_summary = json.dumps({
        "game_name": hlr.game_name,
        "genre": hlr.genre,
        "scenes": [{"name": s.scene_name, "purpose": s.purpose, "fsm_state": s.fsm_state}
                    for s in _flatten_scenes(hlr.scenes)],
        "game_systems": hlr.get_enum("game_systems"),
        "enums": hlr.enum_dict(),
    }, indent=2)

    matrix_summary = _build_matrix_summary(matrix)

    base_context = (
        f"## Game HLR\n```json\n{hlr_summary}\n```\n\n"
        f"## Impact Matrix\n{matrix_summary}"
    )

    all_scenes = _flatten_scenes(hlr.scenes)
    _log.info("Scene Manifest: launching %d scene analyses in parallel", len(all_scenes))

    tasks = [
        _build_scene_manifest(scene, hlr, matrix, caller, base_context)
        for scene in all_scenes
    ]
    entries = await asyncio.gather(*tasks)

    # Enforce scoping: strip system names the LLM invented outside HLR enum
    allowed_systems = set(hlr.get_enum("game_systems"))
    if allowed_systems:
        for entry in entries:
            rejected = [s for s in entry.active_systems if s not in allowed_systems]
            if rejected:
                _log.warning(
                    "[%s] Dropping invalid systems not in game_systems enum: %s",
                    entry.scene_name, rejected,
                )
            entry.active_systems = [s for s in entry.active_systems if s in allowed_systems]

    manifest = SceneManifest(
        game_name=hlr.game_name,
        scenes=list(entries),
    )

    # Log summary
    total_entities = sum(len(s.entities) for s in manifest.scenes)
    total_systems = sum(len(s.active_systems) for s in manifest.scenes)
    _log.info("Scene Manifest: %d scenes, %d total entity placements, %d total system activations",
               len(manifest.scenes), total_entities, total_systems)

    if trace:
        trace.event("scene_manifest", "manifest_summary",
                     scenes=len(manifest.scenes),
                     total_entities=total_entities,
                     total_systems=total_systems)
        trace.phase_end("scene_manifest")

    return manifest


def validate_scene_manifest(
    hlr: GameIdentity,
    matrix: ImpactMatrix,
    manifest: SceneManifest,
) -> list[str]:
    """Validate scene manifest against HLR and impact matrix."""
    trace = get_trace()
    errors: list[str] = []
    known_systems = set(hlr.get_enum("game_systems"))
    known_entities: set[str] = set()
    for enum_def in hlr.enums:
        known_entities.update(enum_def.values)

    for scene in manifest.scenes:
        # Systems must be in HLR
        for sys in scene.active_systems:
            if sys not in known_systems:
                errors.append(f"[{scene.scene_name}] system '{sys}' not in HLR game_systems")

        # Entities must trace to HLR enums
        for entity in scene.entities:
            if entity.from_enum not in hlr.enum_dict():
                errors.append(
                    f"[{scene.scene_name}] entity '{entity.entity_name}' "
                    f"references unknown enum '{entity.from_enum}'"
                )
            elif entity.entity_name not in hlr.get_enum(entity.from_enum):
                # Allow role references
                if not entity.role:
                    errors.append(
                        f"[{scene.scene_name}] entity '{entity.entity_name}' "
                        f"not in enum '{entity.from_enum}'"
                    )

        # Must have at least one system
        if not scene.active_systems:
            errors.append(f"[{scene.scene_name}] no active systems")

        # Must have at least one entity
        if not scene.entities:
            errors.append(f"[{scene.scene_name}] no entities")

    if trace:
        trace.validation("scene_manifest", "manifest_validator",
                          passed=len(errors) == 0, errors=errors)

    return errors


def format_scene_manifest(manifest: SceneManifest) -> str:
    """Human-readable scene manifest."""
    lines = [f"Scene Manifest: {manifest.game_name}", ""]

    for scene in manifest.scenes:
        lines.append(f"  {scene.scene_name} — {scene.purpose}")
        if scene.roles:
            for role, enum in scene.roles.items():
                lines.append(f"    role: {role} → {enum}")
        lines.append(f"    systems: {', '.join(scene.active_systems)}")
        lines.append(f"    entities ({len(scene.entities)}):")
        for e in scene.entities:
            role_str = f" as {e.role}" if e.role else ""
            lines.append(f"      {e.entity_name} [{e.from_enum}]{role_str} — {e.reason}")
        if scene.tracks_properties:
            lines.append(f"    tracks: {', '.join(scene.tracks_properties[:15])}")
        lines.append("")

    return "\n".join(lines)
