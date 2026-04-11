"""E2E structured trace logger for the RayXI pipeline.

Captures everything from user prompt to final output:
  - Phase lifecycle (start/end with duration)
  - Every LLM call (start/end, caller, cache hit, input/output size, duration)
  - Every validation pass (phase, passed/failed, errors)
  - Pool events (acquire/release/pause, active count, memory %)
  - Build events (deterministic vs LLM, entity, success/fail)
  - Verify events (tool, target, passed, details)

Output: JSON file with full event timeline + human-readable summary.

Usage:
    from rayxi.trace import start_trace, get_trace

    trace = start_trace(user_prompt="I want to build a fighting game")
    trace.phase_start("hlr")
    call_id = trace.llm_start("hlr", "schema_expand", "ClaudeCLI", 4500)
    trace.llm_end(call_id, output_chars=800, cache_hit=False)
    trace.phase_end("hlr")
    trace.end()
    trace.save(Path(".trace/run.json"))
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("rayxi.trace")


class TraceLog:
    """E2E trace log. One per pipeline run."""

    def __init__(self, user_prompt: str) -> None:
        self.start_time = time.monotonic()
        self.start_wall = time.time()
        self.start_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.end_iso: str = ""
        self.total_duration_s: float = 0.0
        self.user_prompt = user_prompt
        self.project_name: str = ""
        self.events: list[dict] = []

        # Track in-flight LLM calls for matching start/end
        self._inflight: dict[str, dict] = {}

        # Track phase start times
        self._phase_starts: dict[str, float] = {}

        self._emit("pipeline", "pipeline_start", "",
                    user_prompt=user_prompt)
        _log.info("Trace: started at %s", self.start_iso)

    # ------------------------------------------------------------------
    # Internal event emitter
    # ------------------------------------------------------------------

    def _t(self) -> float:
        """Seconds since trace start."""
        return round(time.monotonic() - self.start_time, 3)

    def _emit(self, phase: str, event: str, label: str, **data) -> dict:
        entry = {
            "t": self._t(),
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "phase": phase,
            "event": event,
            "label": label,
        }
        entry.update(data)
        self.events.append(entry)
        return entry

    # ------------------------------------------------------------------
    # Phase lifecycle
    # ------------------------------------------------------------------

    def phase_start(self, phase: str) -> None:
        self._phase_starts[phase] = time.monotonic()
        self._emit(phase, "phase_start", "")
        _log.info("Trace: [%s] start", phase)

    def phase_end(self, phase: str, artifacts: list[str] | None = None) -> None:
        """End a phase. `artifacts` is a list of filenames (relative to output/{game}/)
        produced by the phase — each becomes a clickable button in the log viewer."""
        start = self._phase_starts.pop(phase, None)
        duration = round(time.monotonic() - start, 1) if start else 0.0
        extra: dict = {"duration_s": duration}
        if artifacts:
            extra["artifacts"] = list(artifacts)
        self._emit(phase, "phase_end", "", **extra)
        _log.info("Trace: [%s] end (%.1fs, %d artifacts)", phase, duration, len(artifacts or []))

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def llm_start(self, phase: str, label: str, caller: str, input_chars: int) -> str:
        """Log LLM call start. Returns call_id for matching with llm_end."""
        call_id = uuid.uuid4().hex[:8]
        self._inflight[call_id] = {
            "start": time.monotonic(),
            "phase": phase,
            "label": label,
            "caller": caller,
        }
        self._emit(phase, "llm_start", label,
                    call_id=call_id, caller=caller, input_chars=input_chars)
        _log.debug("Trace: [%s] llm_start %s (%s, %d chars)", phase, label, caller, input_chars)
        return call_id

    def llm_end(self, call_id: str, output_chars: int, cache_hit: bool = False,
                error: str = "") -> None:
        """Log LLM call end. Matches with llm_start via call_id."""
        info = self._inflight.pop(call_id, None)
        if info is None:
            _log.warning("Trace: llm_end for unknown call_id %s", call_id)
            return
        duration = round(time.monotonic() - info["start"], 1)
        self._emit(info["phase"], "llm_end", info["label"],
                    call_id=call_id, caller=info["caller"],
                    output_chars=output_chars, duration_s=duration,
                    cache_hit=cache_hit, error=error)
        status = "cache" if cache_hit else f"{duration}s"
        if error:
            status = f"ERROR: {error[:80]}"
        _log.debug("Trace: [%s] llm_end %s (%s, %d chars, %s)",
                    info["phase"], info["label"], info["caller"], output_chars, status)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validation(self, phase: str, validator: str, passed: bool,
                   errors: list[str] | None = None) -> None:
        self._emit(phase, "validation", validator,
                    passed=passed, error_count=len(errors or []),
                    errors=errors or [])
        status = "PASSED" if passed else f"FAILED ({len(errors or [])} errors)"
        _log.info("Trace: [%s] validation %s: %s", phase, validator, status)

    # ------------------------------------------------------------------
    # Pool events
    # ------------------------------------------------------------------

    def pool_acquire(self, label: str, active: int, memory_pct: float) -> None:
        self._emit("pool", "pool_acquire", label,
                    active=active, memory_pct=round(memory_pct, 1))

    def pool_release(self, label: str, active: int, memory_pct: float,
                     duration_s: float) -> None:
        self._emit("pool", "pool_release", label,
                    active=active, memory_pct=round(memory_pct, 1),
                    duration_s=round(duration_s, 1))

    def pool_pause(self, reason: str, active: int, memory_pct: float) -> None:
        self._emit("pool", "pool_pause", reason,
                    active=active, memory_pct=round(memory_pct, 1))
        _log.warning("Trace: pool paused — %s (active=%d, memory=%.0f%%)",
                      reason, active, memory_pct)

    def pool_batch_cooldown(self, batch_size: int, cooldown_s: float,
                            active: int) -> None:
        self._emit("pool", "pool_batch_cooldown", "",
                    batch_size=batch_size, cooldown_s=cooldown_s, active=active)

    # ------------------------------------------------------------------
    # Build events
    # ------------------------------------------------------------------

    def build_start(self, entity_name: str, method: str, scene: str = "") -> str:
        """method: 'deterministic' or 'llm'. Returns build_id."""
        build_id = uuid.uuid4().hex[:8]
        self._inflight[build_id] = {
            "start": time.monotonic(),
            "entity_name": entity_name,
            "method": method,
            "scene": scene,
        }
        self._emit("build", "build_start", entity_name,
                    build_id=build_id, method=method, scene=scene)
        _log.info("Trace: [build] %s %s/%s", method, scene, entity_name)
        return build_id

    def build_end(self, build_id: str, success: bool, output_file: str = "",
                  error: str = "") -> None:
        info = self._inflight.pop(build_id, None)
        if info is None:
            return
        duration = round(time.monotonic() - info["start"], 1)
        self._emit("build", "build_end", info["entity_name"],
                    build_id=build_id, method=info["method"],
                    scene=info["scene"], success=success,
                    duration_s=duration, output_file=output_file,
                    error=error)
        status = "OK" if success else f"FAIL: {error[:80]}"
        _log.info("Trace: [build] %s %s/%s → %s (%.1fs)",
                   info["method"], info["scene"], info["entity_name"], status, duration)

    # ------------------------------------------------------------------
    # Verify events
    # ------------------------------------------------------------------

    def verify(self, tool: str, target: str, passed: bool,
               issues: list[str] | None = None, details: dict | None = None) -> None:
        self._emit("verify", "verify", tool,
                    target=target, passed=passed,
                    issue_count=len(issues or []),
                    issues=issues or [],
                    details=details or {})
        status = "PASSED" if passed else f"{len(issues or [])} issues"
        _log.info("Trace: [verify] %s on %s: %s", tool, target, status)

    # ------------------------------------------------------------------
    # Generic event (for anything that doesn't fit above)
    # ------------------------------------------------------------------

    def event(self, phase: str, event_type: str, label: str = "", **data) -> None:
        self._emit(phase, event_type, label, **data)

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def end(self) -> None:
        self.total_duration_s = round(time.monotonic() - self.start_time, 1)
        self.end_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._emit("pipeline", "pipeline_end", "",
                    total_duration_s=self.total_duration_s)
        _log.info("Trace: ended at %s (%.1fs total)", self.end_iso, self.total_duration_s)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "user_prompt": self.user_prompt,
            "start_time": self.start_iso,
            "end_time": self.end_iso,
            "total_duration_s": self.total_duration_s,
            "event_count": len(self.events),
            "events": self.events,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        _log.info("Trace: saved to %s (%d events)", path, len(self.events))

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def format_summary(self) -> str:
        lines = [
            f"Trace Summary: {self.project_name or '(unnamed)'}",
            f"  Prompt: {self.user_prompt[:100]}{'...' if len(self.user_prompt) > 100 else ''}",
            f"  Start:  {self.start_iso}",
            f"  End:    {self.end_iso}",
            f"  Total:  {self.total_duration_s}s",
            f"  Events: {len(self.events)}",
            "",
        ]

        # Phase durations
        phase_durations: dict[str, float] = {}
        for ev in self.events:
            if ev["event"] == "phase_end":
                phase_durations[ev["phase"]] = ev.get("duration_s", 0)
        if phase_durations:
            lines.append("  Phase durations:")
            for phase, dur in phase_durations.items():
                lines.append(f"    {phase:<20} {dur:>6.1f}s")
            lines.append("")

        # LLM call stats by caller
        llm_calls: dict[str, list[dict]] = {}
        for ev in self.events:
            if ev["event"] == "llm_end":
                caller = ev.get("caller", "unknown")
                llm_calls.setdefault(caller, []).append(ev)

        if llm_calls:
            lines.append("  LLM calls by caller:")
            lines.append(f"    {'caller':<20} {'count':>5}  {'total':>7}  {'avg':>6}  {'cache':>5}")
            lines.append(f"    {'-'*20} {'-'*5}  {'-'*7}  {'-'*6}  {'-'*5}")
            for caller, calls in sorted(llm_calls.items()):
                durations = [c.get("duration_s", 0) for c in calls if not c.get("cache_hit")]
                cached = sum(1 for c in calls if c.get("cache_hit"))
                total = sum(durations)
                avg = total / len(durations) if durations else 0
                lines.append(
                    f"    {caller:<20} {len(calls):>5}  {total:>6.1f}s  {avg:>5.1f}s  {cached:>5}"
                )
            lines.append("")

        # LLM call stats by label (call type)
        llm_by_type: dict[str, list[dict]] = {}
        for ev in self.events:
            if ev["event"] == "llm_end":
                # Extract call type from label: "mlr_fsm[fighting]" → "mlr_fsm"
                label = ev.get("label", "")
                call_type = label.split("[")[0] if "[" in label else label
                llm_by_type.setdefault(call_type, []).append(ev)

        if llm_by_type:
            lines.append("  LLM calls by type:")
            lines.append(f"    {'type':<35} {'count':>5}  {'total':>7}  {'avg':>6}  {'min':>6}  {'max':>6}  {'cache':>5}")
            lines.append(f"    {'-'*35} {'-'*5}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*5}")
            for call_type, calls in sorted(llm_by_type.items()):
                durations = [c.get("duration_s", 0) for c in calls if not c.get("cache_hit")]
                cached = sum(1 for c in calls if c.get("cache_hit"))
                if durations:
                    total = sum(durations)
                    avg = total / len(durations)
                    mn = min(durations)
                    mx = max(durations)
                    lines.append(
                        f"    {call_type:<35} {len(calls):>5}  {total:>6.1f}s  {avg:>5.1f}s  {mn:>5.1f}s  {mx:>5.1f}s  {cached:>5}"
                    )
                else:
                    lines.append(
                        f"    {call_type:<35} {len(calls):>5}     0.0s    0.0s    0.0s    0.0s  {cached:>5}"
                    )
            lines.append("")

        # Pool events summary
        pauses = [ev for ev in self.events if ev["event"] == "pool_pause"]
        if pauses:
            lines.append(f"  Pool pauses: {len(pauses)}")
            for p in pauses:
                lines.append(f"    t={p['t']:.1f}s  memory={p.get('memory_pct', 0):.0f}%  "
                             f"active={p.get('active', 0)}  reason={p.get('label', '')}")
            lines.append("")

        # Build stats
        builds = [ev for ev in self.events if ev["event"] == "build_end"]
        if builds:
            det = [b for b in builds if b.get("method") == "deterministic"]
            llm = [b for b in builds if b.get("method") == "llm"]
            lines.append(f"  Builds: {len(builds)} total")
            if det:
                lines.append(f"    Deterministic: {len(det)} ({sum(1 for b in det if b.get('success'))} ok)")
            if llm:
                lines.append(f"    LLM:           {len(llm)} ({sum(1 for b in llm if b.get('success'))} ok)")
            lines.append("")

        # Verify stats
        verifies = [ev for ev in self.events if ev["event"] == "verify"]
        if verifies:
            passed = sum(1 for v in verifies if v.get("passed"))
            lines.append(f"  Verifications: {len(verifies)} total ({passed} passed, {len(verifies) - passed} failed)")
            for v in verifies:
                status = "OK" if v.get("passed") else f"FAIL ({v.get('issue_count', 0)})"
                lines.append(f"    {v.get('label', ''):<25} {v.get('target', ''):<30} {status}")
            lines.append("")

        # Errors
        errors = [ev for ev in self.events if ev.get("error")]
        if errors:
            lines.append(f"  Errors: {len(errors)}")
            for e in errors:
                lines.append(f"    t={e['t']:.1f}s  [{e['phase']}] {e.get('label', '')}: {e['error'][:100]}")
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_trace: TraceLog | None = None


def start_trace(user_prompt: str) -> TraceLog:
    """Start a new trace. Replaces any previous trace."""
    global _trace
    _trace = TraceLog(user_prompt)
    return _trace


def get_trace() -> TraceLog | None:
    """Get the current trace, or None if not started."""
    return _trace
