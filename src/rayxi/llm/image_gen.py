"""MiniMax image generation client.

Reuses the same API key as the text caller (`providers.minimax.api_key`) but
calls a different endpoint. MiniMax's image-gen API is at
`https://api.minimaxi.chat/v1/image_generation`.

Usage:
    caller = MiniMaxImageCaller()
    png_bytes = await caller.generate(
        prompt="anime-style fighter in idle stance",
        aspect_ratio="3:4",
    )
    Path("out.png").write_bytes(png_bytes)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

import httpx

_log = logging.getLogger("rayxi.llm.image_gen")

_CONFIG_PATH = Path("/home/aeli/projects/aelibigcoder/config/llm_config.json")
_IMAGE_API_URL = "https://api.minimaxi.chat/v1/image_generation"
_DEFAULT_MODEL = "image-01"


def _load_api_key() -> str:
    cfg = json.loads(_CONFIG_PATH.read_text())
    return cfg["providers"]["minimax"]["api_key"]


class MiniMaxImageCaller:
    """Thin async client over MiniMax /v1/image_generation."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        timeout_seconds: float = 180.0,
    ) -> None:
        self._api_key = api_key or _load_api_key()
        self._model = model
        self._timeout = timeout_seconds

    async def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str = "1:1",
        n: int = 1,
        response_format: str = "base64",
        label: str = "",
    ) -> bytes:
        """Generate an image and return raw PNG bytes.

        Args:
            prompt: the image description (include "transparent background" when needed)
            aspect_ratio: "1:1" | "3:4" | "4:3" | "9:16" | "16:9"
            n: number of images to generate (returns the first)
            response_format: "base64" or "url"
            label: optional log tag
        """
        body = {
            "model": self._model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "response_format": response_format,
            "n": n,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        tag = label or prompt[:40]
        _log.info("MiniMax image: generating '%s' (%s)", tag, aspect_ratio)

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(_IMAGE_API_URL, headers=headers, json=body)
                if resp.status_code != 200:
                    err = resp.text[:400]
                    raise RuntimeError(f"MiniMax image {resp.status_code}: {err}")
                data = resp.json()
                return self._extract_png(data, response_format)
            except Exception as exc:
                last_err = exc
                _log.warning("MiniMax image attempt %d failed: %s", attempt + 1, str(exc)[:160])
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"MiniMax image failed after 3 attempts: {last_err}")

    async def generate_many(
        self,
        specs: list[tuple[str, str, str]],
        *,
        aspect_ratio: str = "1:1",
        concurrency: int = 4,
    ) -> dict[str, bytes]:
        """Generate multiple images in parallel.

        Args:
            specs: list of (label, prompt, aspect_ratio_override or "") tuples
            aspect_ratio: default ratio if spec has empty override
            concurrency: max parallel requests

        Returns dict of label → PNG bytes.
        """
        sem = asyncio.Semaphore(concurrency)
        results: dict[str, bytes] = {}

        async def _do(label: str, prompt: str, ratio_override: str) -> None:
            async with sem:
                try:
                    png = await self.generate(
                        prompt=prompt,
                        aspect_ratio=ratio_override or aspect_ratio,
                        label=label,
                    )
                    results[label] = png
                    _log.info("MiniMax image: %s → %d bytes", label, len(png))
                except Exception as exc:
                    _log.error("MiniMax image: %s FAILED: %s", label, exc)

        await asyncio.gather(*[_do(l, p, r) for l, p, r in specs])
        return results

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _extract_png(self, data: dict, response_format: str) -> bytes:
        """Pull PNG bytes out of the JSON response, regardless of shape variant."""
        # MiniMax returns either `data.image_base64` (list) or `data` (list of urls)
        if response_format == "base64":
            # Try common shapes
            if "data" in data:
                d = data["data"]
                if isinstance(d, dict) and "image_base64" in d:
                    lst = d["image_base64"]
                    if isinstance(lst, list) and lst:
                        return base64.b64decode(lst[0])
                    if isinstance(lst, str):
                        return base64.b64decode(lst)
                if isinstance(d, list) and d:
                    first = d[0]
                    if isinstance(first, dict):
                        b64 = first.get("b64_json") or first.get("image_base64")
                        if b64:
                            return base64.b64decode(b64)
            # Some variants put it top-level
            if "image_base64" in data:
                b64 = data["image_base64"]
                if isinstance(b64, list) and b64:
                    return base64.b64decode(b64[0])
                return base64.b64decode(b64)
        elif response_format == "url":
            if "data" in data and isinstance(data["data"], dict) and "image_urls" in data["data"]:
                # Synchronous fetch of the first URL
                import httpx as _h
                urls = data["data"]["image_urls"]
                if isinstance(urls, list) and urls:
                    return _h.get(urls[0], timeout=60).content
        raise RuntimeError(f"MiniMax image: cannot extract PNG from response: {json.dumps(data)[:400]}")
