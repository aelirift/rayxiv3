"""Memory-aware process pool for LLM subprocess callers.

Batch-admits subprocesses with memory pacing:
  - Hard cap: 98 concurrent subprocesses.
  - Batch admission with adaptive sizing:
      active < 50 → batch 3–5, cooldown 2.5s
      active >= 50 → batch 1–2, cooldown 5s
  - Memory ceiling: 80%. No new procs above this.
    Resumes below 70% (hysteresis prevents start-stop thrashing).
  - Collects per-label duration stats (caller type, phase, etc.).

Usage:
    from rayxi.llm.pool import get_pool

    async with get_pool(label="ClaudeCLI/HLR"):
        result = await some_llm_call()

    # After a run, inspect stats:
    get_pool().print_stats()
"""

from __future__ import annotations

import asyncio
import logging
import time

from rayxi.trace import get_trace

_log = logging.getLogger("rayxi.llm.pool")

_MAX_PROCS = 98
_BATCH_SIZE_LOW = 5       # batch size when active < _ACTIVE_THRESHOLD
_BATCH_SIZE_HIGH = 2      # batch size when active >= _ACTIVE_THRESHOLD
_ACTIVE_THRESHOLD = 50    # switch from large to small batches
_COOLDOWN_LOW_S = 2.5     # pause between batches (low load)
_COOLDOWN_HIGH_S = 5.0    # pause between batches (high load)
_MEM_CEILING_PCT = 80.0   # hard stop — no new procs above this
_MEM_RESUME_PCT = 70.0    # resume admitting below this
_MEM_POLL_S = 3           # how often to recheck when paused on memory


def _memory_usage_percent() -> float:
    """Return memory usage as a percentage (0-100)."""
    try:
        with open("/proc/meminfo") as f:
            total = available = 0
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1])
                if total and available:
                    break
            if total == 0:
                return 0.0
            return ((total - available) / total) * 100
    except OSError:
        return 0.0  # non-Linux: assume fine


class _ProcStats:
    """Tracks per-label timing stats."""

    def __init__(self) -> None:
        # label → list of durations (seconds)
        self._durations: dict[str, list[float]] = {}

    def record(self, label: str, duration: float) -> None:
        self._durations.setdefault(label, []).append(duration)

    def summary(self) -> dict[str, dict[str, float]]:
        """Return {label: {count, total_s, avg_s, min_s, max_s}}."""
        out: dict[str, dict[str, float]] = {}
        for label, durations in sorted(self._durations.items()):
            out[label] = {
                "count": len(durations),
                "total_s": round(sum(durations), 1),
                "avg_s": round(sum(durations) / len(durations), 1),
                "min_s": round(min(durations), 1),
                "max_s": round(max(durations), 1),
            }
        return out

    def format(self) -> str:
        lines = ["Pool stats by label:"]
        lines.append(f"  {'label':<40} {'count':>5}  {'total':>7}  {'avg':>6}  {'min':>6}  {'max':>6}")
        lines.append(f"  {'-'*40} {'-'*5}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*6}")
        for label, s in self.summary().items():
            lines.append(
                f"  {label:<40} {s['count']:>5}  {s['total_s']:>6.1f}s  {s['avg_s']:>5.1f}s  {s['min_s']:>5.1f}s  {s['max_s']:>5.1f}s"
            )
        return "\n".join(lines)

    def reset(self) -> None:
        self._durations.clear()


class ProcessPool:
    """Memory-paced subprocess pool with batch admission and stats tracking."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(_MAX_PROCS)
        self._admission = asyncio.Lock()
        self._active = 0
        self._batch_admitted = 0
        self.stats = _ProcStats()

    @property
    def active(self) -> int:
        return self._active

    def _batch_size(self) -> int:
        return _BATCH_SIZE_LOW if self._active < _ACTIVE_THRESHOLD else _BATCH_SIZE_HIGH

    def _cooldown(self) -> float:
        return _COOLDOWN_LOW_S if self._active < _ACTIVE_THRESHOLD else _COOLDOWN_HIGH_S

    async def acquire(self, label: str = "") -> float:
        """Acquire a slot. Returns the wall-clock start time for stats."""
        # Hard cap — blocks if 98 already active
        await self._semaphore.acquire()

        # Serialize admission decisions so batching works
        async with self._admission:
            # Hard stop while memory is above ceiling
            usage = _memory_usage_percent()
            while usage >= _MEM_CEILING_PCT:
                _log.warning(
                    "Pool: memory %.0f%% >= ceiling %d%%, %d active — "
                    "paused until %.0f%%",
                    usage, _MEM_CEILING_PCT, self._active, _MEM_RESUME_PCT,
                )
                trace = get_trace()
                if trace:
                    trace.pool_pause(f"memory_ceiling_{usage:.0f}pct",
                                     self._active, usage)
                await asyncio.sleep(_MEM_POLL_S)
                usage = _memory_usage_percent()
                if usage < _MEM_RESUME_PCT:
                    _log.info("Pool: memory %.0f%% < resume %d%% — resuming",
                              usage, _MEM_RESUME_PCT)
                    break

            self._active += 1
            self._batch_admitted += 1
            batch_sz = self._batch_size()
            _log.debug(
                "Pool: acquired [%s] (memory %.0f%%, %d active, batch %d/%d)",
                label, usage, self._active, self._batch_admitted, batch_sz,
            )
            trace = get_trace()
            if trace:
                trace.pool_acquire(label, self._active, usage)

            # After a full batch, cooldown to let memory settle
            if self._batch_admitted >= batch_sz:
                self._batch_admitted = 0
                cd = self._cooldown()
                _log.debug(
                    "Pool: batch full — cooling down %.1fs before next batch",
                    cd,
                )
                if trace:
                    trace.pool_batch_cooldown(batch_sz, cd, self._active)
                await asyncio.sleep(cd)

        return time.monotonic()

    def release(self, label: str = "", start_time: float = 0.0) -> None:
        self._active -= 1
        self._semaphore.release()
        duration = time.monotonic() - start_time if start_time > 0 else 0.0
        if start_time > 0:
            self.stats.record(label or "unlabeled", duration)
            _log.debug("Pool: released [%s] after %.1fs (%d active)",
                        label, duration, self._active)
        else:
            _log.debug("Pool: released (%d active)", self._active)
        trace = get_trace()
        if trace:
            trace.pool_release(label, self._active,
                               _memory_usage_percent(), duration)

    def print_stats(self) -> None:
        _log.info("\n%s", self.stats.format())

    async def __aenter__(self) -> ProcessPool:
        await self.acquire()
        return self

    async def __aexit__(self, *args) -> None:
        self.release()


class PoolSlot:
    """Context manager that carries a label and tracks timing.

    Usage:
        async with PoolSlot(get_pool(), "ClaudeCLI/MLR-fsm"):
            ...
    """

    def __init__(self, pool: ProcessPool, label: str = "") -> None:
        self._pool = pool
        self._label = label
        self._start: float = 0.0

    async def __aenter__(self) -> PoolSlot:
        self._start = await self._pool.acquire(self._label)
        return self

    async def __aexit__(self, *args) -> None:
        self._pool.release(self._label, self._start)


# Singleton — shared across all callers in one process
_pool: ProcessPool | None = None


def get_pool() -> ProcessPool:
    """Return the global ProcessPool singleton."""
    global _pool
    if _pool is None:
        _pool = ProcessPool()
    return _pool
