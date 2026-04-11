"""KB retrieval via semantic embedding.

Returns relevant high/mid-level KB chunks for a user prompt.
Excludes low-level files (game JSON frame data, mechanic templates).
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger("rayxi.spec.kb_retrieval")

# Lazy-loaded model
_model = None

# HIGH/MID LEVEL KB file patterns — what HLR should see
HIGH_LEVEL_PATTERNS = [
    "genres/*.md",       # Genre theory and mechanics
    "process/*.md",      # Game process / FSM specs
    "watchouts/*.md",    # Common pitfalls
    "design_principles/*.md",  # Universal defaults
]


def _get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
            _log.info("KB retrieval: loaded all-MiniLM-L6-v2")
        except ImportError:
            _log.warning(
                "KB retrieval: sentence_transformers not installed; "
                "returning empty chunk list. Install with `pip install sentence-transformers`."
            )
            return None
    return _model


def _chunk_text(text: str, chunk_size: int = 800) -> list[str]:
    """Split text into chunks of ~N chars, breaking on paragraph boundaries."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) > chunk_size and current:
            chunks.append(current.strip())
            current = p
        else:
            current = current + "\n\n" + p if current else p
    if current:
        chunks.append(current.strip())
    return chunks


def _load_chunks(kb_dir: Path) -> list[tuple[str, str]]:
    """Load all high/mid-level KB chunks. Returns [(source_label, text), ...]."""
    chunks: list[tuple[str, str]] = []
    for pattern in HIGH_LEVEL_PATTERNS:
        for f in kb_dir.glob(pattern):
            content = f.read_text()
            for i, chunk in enumerate(_chunk_text(content)):
                label = f"{f.relative_to(kb_dir)}#chunk{i}"
                chunks.append((label, chunk))
    return chunks


def retrieve_relevant_chunks(
    prompt: str,
    kb_dir: Path,
    top_k: int = 10,
    min_score: float = 0.25,
) -> list[tuple[str, str, float]]:
    """Retrieve top-K KB chunks relevant to the prompt via embedding similarity.

    Returns list of (source_label, chunk_text, similarity_score).
    Filters out chunks below min_score.
    """
    import numpy as np

    chunks = _load_chunks(kb_dir)
    if not chunks:
        return []

    model = _get_model()
    if model is None:
        return []  # no embedding model available
    chunk_texts = [c[1] for c in chunks]
    chunk_emb = model.encode(chunk_texts, normalize_embeddings=True)
    prompt_emb = model.encode([prompt], normalize_embeddings=True)[0]

    sims = np.dot(chunk_emb, prompt_emb)
    ranked = sorted(zip(chunks, sims), key=lambda x: -x[1])

    results: list[tuple[str, str, float]] = []
    for (label, text), score in ranked[:top_k]:
        if float(score) >= min_score:
            results.append((label, text, float(score)))

    _log.info(
        "KB retrieval: %d chunks above %s threshold (top_k=%d)",
        len(results), min_score, top_k,
    )
    return results
