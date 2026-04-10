"""LLM callers for RayXI v3.

Claude CLI is the default for complex calls.
MiniMax handles simple/mechanical calls (collisions, simple entities).
Fallback order: Claude CLI → MiniMax → Kimi → GLM.

CallerRouter maps call_type labels to the right caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

import httpx

from .pool import PoolSlot, get_pool
from .protocol import LLMCaller

_CONFIG_PATH = Path("/home/aeli/projects/aelibigcoder/config/llm_config.json")
_KIMI_CRED_PATH = Path.home() / ".kimi/credentials/kimi-code.json"
_KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
_KIMI_AUTH_URL = "https://auth.kimi.com/api/oauth/token"

_kimi_token_cache: dict = {}
_kimi_token_cache_time: float = 0
_log = logging.getLogger("rayxi.llm.callers")


def _load_config() -> dict:
    return json.loads(_CONFIG_PATH.read_text())


def _get_kimi_token() -> str:
    global _kimi_token_cache, _kimi_token_cache_time
    now = time.time()
    if _kimi_token_cache and now - _kimi_token_cache_time < 30:
        return _kimi_token_cache["access_token"]
    creds = json.loads(_KIMI_CRED_PATH.read_text())
    if creds.get("expires_at", 0) - now < 300:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": _KIMI_CLIENT_ID,
        }).encode()
        req = urllib.request.Request(
            _KIMI_AUTH_URL, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            new_creds = json.loads(resp.read())
        new_creds["expires_at"] = time.time() + new_creds.get("expires_in", 900)
        _KIMI_CRED_PATH.write_text(json.dumps(new_creds))
        _KIMI_CRED_PATH.chmod(0o600)
        creds = new_creds
    _kimi_token_cache = creds
    _kimi_token_cache_time = now
    return creds["access_token"]


class GlmCaller:
    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        base = cfg.get("api_base_url", "https://api.z.ai/api/anthropic/v1")
        if "/anthropic/" in base:
            self._url = "https://api.z.ai/api/paas/v4/chat/completions"
        elif base.endswith("/chat/completions"):
            self._url = base
        else:
            self._url = f"{base}/chat/completions"

    async def __call__(self, system: str, prompt: str, *, json_mode: bool = False, label: str = "") -> str:
        body: dict = {
            "model": self._cfg.get("model", "GLM-5.1"),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": self._cfg.get("max_tokens", 32768),
            "temperature": self._cfg.get("temperature", 0.7),
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        slot_label = f"GLM/{label}" if label else "GLM"
        async with PoolSlot(get_pool(), slot_label):
            async with httpx.AsyncClient(timeout=self._cfg.get("timeout_seconds", 600)) as client:
                resp = await client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._cfg['api_key']}", "Content-Type": "application/json"},
                    json=body,
                )
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"GLM error {resp.status_code}: {json.dumps(data)[:200]}")
        text = data["choices"][0]["message"]["content"]
        if not text:
            raise RuntimeError(f"GLM empty response: {json.dumps(data)[:300]}")
        return text


class MiniMaxCaller:
    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg

    async def __call__(self, system: str, prompt: str, *, json_mode: bool = False, label: str = "") -> str:
        body: dict = {
            "model": self._cfg["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": 196000,
            "temperature": self._cfg.get("temperature", 0.7),
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        slot_label = f"MiniMax/{label}" if label else "MiniMax"
        async with PoolSlot(get_pool(), slot_label):
            async with httpx.AsyncClient(timeout=self._cfg.get("timeout_seconds", 120)) as client:
                resp = await client.post(
                    self._cfg["api_base_url"],
                    headers={"Authorization": f"Bearer {self._cfg['api_key']}", "Content-Type": "application/json"},
                    json=body,
                )
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"MiniMax error {resp.status_code}: {json.dumps(data)[:200]}")
        choices = data.get("choices")
        if not choices or not choices[0].get("message", {}).get("content"):
            raise RuntimeError(f"MiniMax empty response: {json.dumps(data)[:300]}")
        text = choices[0]["message"]["content"]
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        # Extract JSON from markdown code fences if present
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, flags=re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()
        if not text:
            raise RuntimeError("MiniMax empty response after think-strip")
        return text


class KimiCaller:
    async def __call__(self, system: str, prompt: str, *, json_mode: bool = False, label: str = "") -> str:
        token = _get_kimi_token()
        body: dict = {
            "model": "kimi-for-coding",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": 32768,
            "temperature": 0.7,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        slot_label = f"Kimi/{label}" if label else "Kimi"
        async with PoolSlot(get_pool(), slot_label):
            async with httpx.AsyncClient(timeout=600) as client:
                resp = await client.post(
                    "https://api.kimi.com/coding/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "User-Agent": "claude-code/2.0",
                    },
                    json=body,
                )
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"Kimi error {resp.status_code}: {json.dumps(data)[:200]}")
        return data["choices"][0]["message"]["content"]


class ClaudeCLICaller:
    """Claude Code CLI via `claude --print` subprocess. No conversation context."""

    async def __call__(self, system: str, prompt: str, *, json_mode: bool = False, label: str = "") -> str:
        full_prompt = f"{system}\n\n---\n\n{prompt}"
        if json_mode:
            full_prompt += "\n\nIMPORTANT: Respond with ONLY a valid JSON object. No markdown fences, no explanation, no text before or after the JSON."

        slot_label = f"ClaudeCLI/{label}" if label else "ClaudeCLI"
        _log.info("%s: sending %d chars", slot_label, len(full_prompt))

        async with PoolSlot(get_pool(), slot_label):
            proc = await asyncio.create_subprocess_exec(
                "claude", "--print", "--output-format", "text",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(full_prompt.encode())

        if proc.returncode != 0:
            err = stderr.decode()[:300]
            raise RuntimeError(f"Claude CLI error (exit {proc.returncode}): {err}")

        text = stdout.decode().strip()
        if not text:
            raise RuntimeError("Claude CLI empty response")

        # Strip markdown fences if present
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, flags=re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()

        _log.info("%s: got %d chars back", slot_label, len(text))
        return text


class FallbackCaller:
    def __init__(self, callers: list) -> None:
        self._callers = callers

    async def __call__(self, system: str, prompt: str, *, json_mode: bool = False, label: str = "") -> str:
        last_exc: Exception | None = None
        for caller in self._callers:
            try:
                return await caller(system, prompt, json_mode=json_mode, label=label)
            except Exception as exc:
                err_str = str(exc).lower()
                is_transient = any(p in err_str for p in (
                    "rate limit", "usage limit", "quota", "insufficient balance",
                    "1302", "1234", "1113", "403", "429", "empty response", "timeout",
                ))
                if is_transient or "timeout" in type(exc).__name__.lower():
                    _log.warning("FallbackCaller: %s failed (%s), trying next…", type(caller).__name__, str(exc)[:120])
                    last_exc = exc
                else:
                    raise
        raise RuntimeError(f"All callers failed. Last error: {last_exc}")


# ---------------------------------------------------------------------------
# Caller routing — maps call types to the right LLM
# ---------------------------------------------------------------------------

# Entity object_types that are simple enough for MiniMax
_SIMPLE_ENTITY_TYPES = {"hud", "background", "ui"}

# Call types routed to the fast/simple caller (MiniMax)
_SIMPLE_CALL_TYPES = {
    "mlr_collisions",
    "mlr_entity_hud",
    "mlr_entity_background",
    "mlr_entity_ui",
    "dlr_entity_hud",
    "dlr_entity_background",
    "dlr_entity_ui",
}


def call_type_for_entity(phase: str, object_type: str) -> str:
    """Build the call_type string for an entity call.

    phase: "mlr" or "dlr"
    object_type: from EntitySpec.object_type (character, hud, background, etc.)
    """
    return f"{phase}_entity_{object_type}"


def is_simple_call(call_type: str) -> bool:
    return call_type in _SIMPLE_CALL_TYPES


class CallerRouter:
    """Routes call_type labels to the right LLM caller.

    primary: Claude CLI (complex calls — FSM, interactions, characters)
    fast:    MiniMax (simple calls — collisions, HUD, backgrounds)

    If fast caller is unavailable, everything goes to primary.
    """

    def __init__(self, primary: LLMCaller, fast: LLMCaller | None = None) -> None:
        self._primary = primary
        self._fast = fast

    def get(self, call_type: str) -> LLMCaller:
        if self._fast and is_simple_call(call_type):
            return self._fast
        return self._primary

    @property
    def primary(self) -> LLMCaller:
        return self._primary

    @property
    def fast(self) -> LLMCaller | None:
        return self._fast


def build_callers() -> dict[str, LLMCaller]:
    """Build callers. Claude CLI is default. Fallback: Claude → MiniMax → Kimi → GLM."""
    cfg = _load_config()
    providers = cfg.get("providers", {})
    callers: dict[str, LLMCaller] = {}

    # Claude CLI — default if available
    if shutil.which("claude"):
        callers["claude"] = ClaudeCLICaller()

    if "minimax" in providers:
        callers["minimax"] = MiniMaxCaller(providers["minimax"])
    if _KIMI_CRED_PATH.exists():
        callers["kimi"] = KimiCaller()
    if "glm" in providers:
        callers["glm"] = GlmCaller(providers["glm"])

    # Fallback chain: Claude → MiniMax → Kimi → GLM
    chain = [c for k in ("claude", "minimax", "kimi", "glm") if (c := callers.get(k)) is not None]
    if len(chain) > 1:
        callers["default"] = FallbackCaller(chain)
    elif chain:
        callers["default"] = chain[0]

    return callers


def build_router(callers: dict[str, LLMCaller]) -> CallerRouter:
    """Build a CallerRouter from the callers dict.

    primary = claude or default
    fast = minimax (if available)
    """
    primary = callers.get("claude") or callers["default"]
    fast = callers.get("minimax")
    router = CallerRouter(primary, fast)
    _log.info(
        "Router: primary=%s, fast=%s",
        type(primary).__name__,
        type(fast).__name__ if fast else "None (all calls → primary)",
    )
    return router
