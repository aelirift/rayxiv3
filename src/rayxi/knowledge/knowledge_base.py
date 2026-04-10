"""KnowledgeBase — two-tier game design knowledge retrieval.

Tier 1 (exact): JSON files in knowledge/games/ — looked up by game name.
Tier 2 (keyword): Markdown files in knowledge/genres/, knowledge/watchouts/,
                  and knowledge/design_principles/ — matched by genre keyword.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_GENRE_KEYWORDS: dict[str, list[str]] = {
    "2d_fighter": [
        "fighter", "fighting", "vs", "versus", "punch", "kick", "kombat",
        "street fighter", "mortal kombat", "tekken", "marvel vs capcom",
        "2d fight", "arcade fighter", "capcom",
    ],
    "kart_racer": [
        "kart", "racing", "racer", "mario kart", "drift", "race",
    ],
    "shmup": ["invaders", "shooter", "shmup", "bullet hell", "space", "alien", "shoot em up", "galaga", "gradius"],
    "puzzle": ["tetris", "puzzle", "block", "falling block", "match", "connect"],
    "platformer": ["platform", "mario", "jump", "run", "side scroll", "metroid"],
    "snake": ["snake", "worm"],
}

_GAME_ALIASES: dict[str, str] = {
    "street fighter 2": "street_fighter_2",
    "street fighter ii": "street_fighter_2",
    "sf2": "street_fighter_2",
    "street fighter": "street_fighter_2",
    "marvel vs capcom": "street_fighter_2",
    "mvc": "street_fighter_2",
    "mortal kombat": "street_fighter_2",
    "mario kart": "mario_kart",
}


@dataclass
class KnowledgeContext:
    game_data: dict = field(default_factory=dict)
    genre_docs: list[str] = field(default_factory=list)
    watchout_docs: list[str] = field(default_factory=list)
    process_doc: str = ""
    source_names: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.game_data and not self.genre_docs and not self.process_doc

    def to_prompt_text(self) -> str:
        parts: list[str] = []
        if self.process_doc:
            parts.append(
                f"=== Game Process Specification (authoritative — follow exactly) ===\n{self.process_doc.strip()}"
            )
        if self.game_data:
            game_name = self.game_data.get("_meta", {}).get("game", "unknown")
            parts.append(
                f"=== Game Reference Data ({game_name}) ===\n```json\n{json.dumps(self.game_data, indent=2)}\n```"
            )
        for doc in self.genre_docs:
            parts.append(f"=== Genre Knowledge ===\n{doc.strip()}")
        for doc in self.watchout_docs:
            parts.append(f"=== Watchouts (avoid these mistakes) ===\n{doc.strip()}")
        return "\n\n".join(parts)


class KnowledgeBase:
    def __init__(self, knowledge_dir: Path) -> None:
        self._root = Path(knowledge_dir)

    def retrieve_context(self, concept: str) -> KnowledgeContext:
        game_name = self._detect_game_name(concept)
        genre = self._detect_genre(concept)

        game_data = self._load_json(self._root / "games" / f"{game_name}.json") if game_name else {}
        genre_docs = self._load_genre_docs(genre) if genre else []
        watchout_docs = self._load_watchout_docs(genre)
        process_doc = self._load_text(self._root / "process" / f"{game_name}_process.md") if game_name else ""

        sources: list[str] = []
        if game_data:
            sources.append(f"games/{game_name}.json")
        if genre_docs:
            sources.append(f"genres/{genre}.md")
        if watchout_docs:
            sources.append("watchouts/")
        if process_doc:
            sources.append(f"process/{game_name}_process.md")

        return KnowledgeContext(
            game_data=game_data,
            genre_docs=genre_docs,
            watchout_docs=watchout_docs,
            process_doc=process_doc,
            source_names=sources,
        )

    def _detect_genre(self, concept: str) -> str:
        lower = concept.lower()
        for genre, keywords in _GENRE_KEYWORDS.items():
            if any(kw in lower for kw in keywords):
                return genre
        return ""

    def _detect_game_name(self, concept: str) -> str:
        lower = concept.lower()
        for alias in sorted(_GAME_ALIASES, key=len, reverse=True):
            if alias in lower:
                return _GAME_ALIASES[alias]
        normalised = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")
        for f in (self._root / "games").glob("*.json"):
            if f.stem == normalised or normalised.startswith(f.stem):
                return f.stem
        return ""

    def _load_json(self, path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _load_text(self, path: Path) -> str:
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""
        return ""

    def _load_genre_docs(self, genre: str) -> list[str]:
        docs: list[str] = []
        genre_file = self._root / "genres" / f"{genre}.md"
        if genre_file.exists():
            docs.append(genre_file.read_text(encoding="utf-8"))
        principles_dir = self._root / "design_principles"
        if principles_dir.exists():
            for f in sorted(principles_dir.glob("*.md")):
                text = f.read_text(encoding="utf-8")
                if len(text) < 4000:
                    docs.append(text)
        return docs

    def _load_watchout_docs(self, genre: str) -> list[str]:
        docs: list[str] = []
        watchouts_dir = self._root / "watchouts"
        if not watchouts_dir.exists():
            return docs
        # Always load all_games_watchouts
        all_watchouts = watchouts_dir / "all_games_watchouts.md"
        if all_watchouts.exists():
            docs.append(all_watchouts.read_text(encoding="utf-8"))
        # Load genre-specific watchouts
        if genre:
            genre_map = {"2d_fighter": "fighter_watchouts.md", "kart_racer": "kart_racer_watchouts.md"}
            watchout_file = watchouts_dir / genre_map.get(genre, f"{genre}_watchouts.md")
            if watchout_file.exists():
                docs.append(watchout_file.read_text(encoding="utf-8"))
        return docs
