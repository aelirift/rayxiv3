"""HLR system → template system mapping via semantic embeddings.

The HLR LLM may invent system names that differ from the template's names
(e.g., HLR says 'hitstop_system' but template uses 'combat_system').
This module uses embedding similarity to map HLR systems to template systems
based on their descriptions.

Game-agnostic — works for any genre template that has system descriptions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rayxi.knowledge.mechanic_loader import ExpandedGameSchema
    from .models import GameIdentity

_log = logging.getLogger("rayxi.spec.system_mapper")

# Lazy-loaded embedding model
_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
            _log.info("System mapper: loaded all-MiniLM-L6-v2")
        except ImportError:
            _log.warning(
                "System mapper: sentence_transformers not installed; falling "
                "back to exact-name matching only."
            )
            return None
    return _model


def map_hlr_to_template(
    hlr: "GameIdentity",
    schema: "ExpandedGameSchema",
    similarity_threshold: float = 0.5,
) -> dict[str, str]:
    """Map each HLR game_system to its closest template mechanic via embedding similarity.

    Returns:
        dict: {hlr_system_name: template_system_name}
              If similarity < threshold, maps to itself (treated as new system).
    """
    import numpy as np

    hlr_systems = hlr.get_enum("game_systems")
    if not hlr_systems:
        return {}

    # Get HLR system descriptions + template-origin tags
    hlr_descs: dict[str, str] = {}
    hlr_origins: dict[str, str] = {}
    for enum in hlr.enums:
        if enum.name == "game_systems":
            hlr_descs = dict(enum.value_descriptions or {})
            hlr_origins = dict(enum.value_template_origins or {})
            break

    template_descs = dict(schema.mechanic_descriptions)
    if not template_descs:
        # No template descriptions — can't map, return identity
        return {s: s for s in hlr_systems}

    model = _get_model()
    if model is None:
        # Embedding unavailable: exact-name mapping only; unknowns map to self.
        return {s: (s if s in template_descs else s) for s in hlr_systems}

    # Build rich text per system: "name: description"
    template_names = list(template_descs.keys())
    template_texts = [f"{name}: {desc}" for name, desc in template_descs.items()]
    template_emb = model.encode(template_texts, normalize_embeddings=True)

    mapping: dict[str, str] = {}
    unmapped: list[str] = []

    for hlr_sys in hlr_systems:
        # HLR-declared (new) systems are custom — do NOT map to any template system.
        # These have mechanic_specs and are scaffolded separately.
        if hlr_origins.get(hlr_sys) == "(new)":
            mapping[hlr_sys] = hlr_sys
            _log.info("System mapping: %s is (new), skipping template match", hlr_sys)
            continue

        # If exact name match, use it directly
        if hlr_sys in template_descs:
            mapping[hlr_sys] = hlr_sys
            continue

        # Otherwise embed and match
        hlr_desc = hlr_descs.get(hlr_sys, hlr_sys)
        hlr_text = f"{hlr_sys}: {hlr_desc}"
        hlr_emb = model.encode([hlr_text], normalize_embeddings=True)[0]
        sims = np.dot(template_emb, hlr_emb)
        best_idx = int(np.argmax(sims))
        best_score = float(sims[best_idx])
        best_name = template_names[best_idx]

        if best_score >= similarity_threshold:
            mapping[hlr_sys] = best_name
            _log.info(
                "System mapping: %s → %s (score=%.3f)", hlr_sys, best_name, best_score
            )
        else:
            mapping[hlr_sys] = hlr_sys  # unmapped, treat as new system
            unmapped.append(f"{hlr_sys} (best={best_name}@{best_score:.3f})")

    if unmapped:
        _log.warning(
            "System mapper: %d HLR systems unmapped (below %s threshold): %s",
            len(unmapped), similarity_threshold, unmapped,
        )

    return mapping
