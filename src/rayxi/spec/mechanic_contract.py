"""Req-owned mechanic contract models shared by coverage, build, and tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class CoverageSignals(BaseModel):
    system_names: list[str] = Field(default_factory=list)
    role_names: list[str] = Field(default_factory=list)
    property_ids: list[str] = Field(default_factory=list)
    scene_names: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    trace_keywords: list[str] = Field(default_factory=list)
    test_keywords: list[str] = Field(default_factory=list)


class MechanicVerification(BaseModel):
    trace_any: list[str] = Field(default_factory=list)
    trace_all: list[str] = Field(default_factory=list)
    trace_any_global: list[str] = Field(default_factory=list)
    trace_all_global: list[str] = Field(default_factory=list)
    trace_none: list[str] = Field(default_factory=list)
    trace_none_global: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MechanicTestAction(BaseModel):
    action: str
    description: str = ""
    keys: str | None = None
    control_intent: str = ""
    method: str = "press"
    wait_ms: int = 600
    hold_ms: int = 0
    verify_change: bool = False
    diff_threshold: float | None = None
    navigate_query: str | None = None
    url_query: str | None = None
    sequence: list[dict[str, Any]] = Field(default_factory=list)
    verification: MechanicVerification = Field(default_factory=MechanicVerification)


class MechanicBehavior(BaseModel):
    id: str
    name: str
    summary: str = ""
    required_for_basic_play: bool = True
    priority: int = 100
    source: str = "deterministic"
    system_names: list[str] = Field(default_factory=list)
    role_names: list[str] = Field(default_factory=list)
    property_ids: list[str] = Field(default_factory=list)
    scene_names: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    actions: list[MechanicTestAction] = Field(default_factory=list)


class MechanicFeature(BaseModel):
    id: str
    name: str
    summary: str = ""
    source: str = "hlr_system"
    required_for_basic_play: bool = True
    signals: CoverageSignals = Field(default_factory=CoverageSignals)
    behaviors: list[MechanicBehavior] = Field(default_factory=list)


class MechanicManifest(BaseModel):
    game_name: str
    prompt: str
    features: list[MechanicFeature] = Field(default_factory=list)
    generated_by: str = "deterministic"
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
