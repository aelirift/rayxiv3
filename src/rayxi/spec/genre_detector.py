"""Genre detection from user prompt.

Two-layer fallback:
  1. Embedding match against KB process docs (highest quality)
  2. Keyword match against prompt text (fast, deterministic)
  3. (TODO) Ask user — not implemented yet

Returns the genre slug used for template lookup (e.g., "2d_fighter", "kart_racer").
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger("rayxi.spec.genre_detector")

# Lazy-loaded embedding model
_model = None

# Embedding score threshold — below this, fall back to keyword match
EMBEDDING_THRESHOLD = 0.4

# Keyword → genre mapping
KEYWORD_TO_GENRE: dict[str, str] = {
    # Fighting games
    "fighter": "2d_fighter",
    "fighting": "2d_fighter",
    "street fighter": "2d_fighter",
    "sf2": "2d_fighter",
    "shoto": "2d_fighter",
    "punch": "2d_fighter",
    "kick": "2d_fighter",
    "hadouken": "2d_fighter",
    "combo": "2d_fighter",
    # Kart racing
    "kart": "kart_racer",
    "racing": "kart_racer",
    "race": "kart_racer",
    "mario kart": "kart_racer",
    "lap": "kart_racer",
    "drift": "kart_racer",
    # Card games
    "card game": "card_game",
    "deck": "card_game",
    "trading card": "card_game",
    "yugioh": "card_game",
    "magic the gathering": "card_game",
}


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
        _log.info("Genre detector: loaded all-MiniLM-L6-v2")
    return _model


def _embedding_detect(prompt: str, kb_dir: Path) -> tuple[float, str | None]:
    """Embed prompt against KB process docs. Returns (best_score, genre_slug or None)."""
    import numpy as np

    process_dir = kb_dir / "process"
    if not process_dir.exists():
        return 0.0, None

    # Load process docs — one per genre/game
    docs: list[tuple[str, str]] = []  # (genre_slug, text)
    for f in process_dir.glob("*.md"):
        # Extract genre from filename: street_fighter_2_process.md → 2d_fighter (look up)
        # For now, derive from the file's first heading or use file stem
        text = f.read_text(encoding="utf-8", errors="ignore")
        # Take first ~2000 chars as the doc summary for embedding
        snippet = text[:2000]
        # The "genre" mapping from process file → genre slug:
        # street_fighter_2_process.md → 2d_fighter
        # mario_kart_process.md → kart_racer
        stem = f.stem.replace("_process", "")
        # Heuristic: if "fighter" in stem → 2d_fighter; "kart" → kart_racer
        if "fighter" in stem or "fight" in stem:
            genre_slug = "2d_fighter"
        elif "kart" in stem or "race" in stem or "racing" in stem:
            genre_slug = "kart_racer"
        elif "card" in stem:
            genre_slug = "card_game"
        else:
            genre_slug = stem
        docs.append((genre_slug, snippet))

    if not docs:
        return 0.0, None

    model = _get_model()
    doc_texts = [d[1] for d in docs]
    doc_emb = model.encode(doc_texts, normalize_embeddings=True)
    prompt_emb = model.encode([prompt], normalize_embeddings=True)[0]

    sims = np.dot(doc_emb, prompt_emb)
    best_idx = int(np.argmax(sims))
    best_score = float(sims[best_idx])
    best_genre = docs[best_idx][0]

    return best_score, best_genre


def _keyword_detect(prompt: str) -> str | None:
    """Match keywords in prompt to genre. Returns genre_slug or None."""
    p = prompt.lower()
    # Sort by keyword length so longer matches win (e.g., "street fighter" before "fighter")
    for keyword in sorted(KEYWORD_TO_GENRE, key=len, reverse=True):
        if keyword in p:
            return KEYWORD_TO_GENRE[keyword]
    return None


def detect_genre(prompt: str, kb_dir: Path) -> str | None:
    """Detect game genre from user prompt.

    Two-layer fallback:
      1. Embedding match against KB process docs (>= 0.4 score)
      2. Keyword match against prompt text

    Returns genre slug (e.g., "2d_fighter") or None if no match.
    """
    # Layer 1: embedding
    score, genre = _embedding_detect(prompt, kb_dir)
    if genre and score >= EMBEDDING_THRESHOLD:
        _log.info("Genre detected via embedding: %s (score=%.3f)", genre, score)
        return genre

    # Layer 2: keyword
    genre = _keyword_detect(prompt)
    if genre:
        _log.info("Genre detected via keyword: %s", genre)
        return genre

    _log.warning(
        "Genre detection failed for prompt: '%s' (best embedding score=%.3f)",
        prompt[:60], score,
    )
    return None
