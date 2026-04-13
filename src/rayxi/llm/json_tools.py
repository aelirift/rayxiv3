"""Helpers for extracting JSON payloads from LLM responses."""

from __future__ import annotations

import json
import re
from typing import Any


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_llm_wrappers(text: str) -> str:
    clean = _THINK_RE.sub("", text or "").strip()
    if not clean:
        return ""
    fence_match = _CODE_FENCE_RE.search(clean)
    if fence_match:
        inner = fence_match.group(1).strip()
        if inner:
            return inner
    return clean


def extract_json_text(text: str) -> str:
    clean = strip_llm_wrappers(text)
    if not clean:
        return ""
    try:
        json.loads(clean)
        return clean
    except Exception:
        pass

    start_index = -1
    opener = ""
    for index, ch in enumerate(clean):
        if ch in "{[":
            start_index = index
            opener = ch
            break
    if start_index < 0:
        return clean

    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for index in range(start_index, len(clean)):
        ch = clean[index]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                candidate = clean[start_index : index + 1].strip()
                try:
                    json.loads(candidate)
                    return candidate
                except Exception:
                    break
    return clean


def parse_json_response(text: str) -> Any:
    candidate = extract_json_text(text)
    if not candidate:
        raise json.JSONDecodeError("Empty JSON response", text or "", 0)
    return json.loads(candidate)
