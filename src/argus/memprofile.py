"""Opt-in allocation profiling — names the file and line that is retaining memory.

WHY THIS EXISTS. On 2026-08-05 an intra-session event-loop lag ramp was traced to
gen-2 garbage collection whose cost is linear in live-object count (CLAUDE.md,
"The intra-session lag ramp"). The MECHANISM was established arithmetically; the
CAUSE — what accumulates roughly 150 live objects a second — was not, and was
narrowed only by elimination: the signal loop, the market-data ingest path, the
DXLink queue backlog and the broker session were each measured clean. What was
left is the layer that cannot be exercised offline without a network.

Elimination has a floor. `tracemalloc` does not: it records the traceback at every
allocation, so a diff between two snapshots says which SOURCE LINE allocated the
memory that is still alive. That is the difference between narrowing suspects and
naming the defect.

**OFF BY DEFAULT, and that is not timidity.** `tracemalloc` roughly doubles memory
and adds real per-allocation cost, and this process already allocates ~10,000
tracked objects a second from the market-data feed. Running it always would make
the thing it measures worse. It is a mode you turn on for a session when you are
hunting, exactly like `tools.shadow --verbose`.

Design rules, each load-bearing:

- **The baseline is taken AFTER startup**, so the report shows what a RUNNING
  process accumulates rather than what importing Python costs. Same reasoning as
  `app.tune_gc`'s placement.
- **Snapshot and diff run in a worker thread.** `take_snapshot()` walks the whole
  trace table and `compare_to()` is heavy pure-Python; doing either on the event
  loop would be a multi-hundred-millisecond stall inside the tool built to
  diagnose multi-hundred-millisecond stalls.
- **It never raises.** A diagnostic that can take down the trading process is
  worse than no diagnostic. Every failure is logged and swallowed.
- **It reports the DIFF, not the total.** Absolute allocation is dominated by
  legitimate long-lived structures; growth between two points in the same session
  is the signal.
"""

from __future__ import annotations

import asyncio
import linecache
import logging
import os
import tracemalloc
from typing import Any

logger = logging.getLogger("argus.memprofile")

#: Set to enable. `1`/`true`/`on` uses `DEFAULT_FRAMES`; an integer sets the
#: traceback depth directly (deeper = more useful attribution, more overhead).
ENV_VAR = "ARGUS_TRACEMALLOC"
#: Seconds between reports. Deliberately slow — each report costs a real stall.
INTERVAL_ENV_VAR = "ARGUS_TRACEMALLOC_INTERVAL_S"

#: Frames of traceback kept per allocation. 1 names the allocating line; more is
#: needed when the allocator is a shared helper (a pydantic constructor, a queue
#: `append`) and the interesting frame is its CALLER — which is the likely case
#: here, since the suspects are all inside the SDK.
DEFAULT_FRAMES = 8
DEFAULT_INTERVAL_S = 600.0
#: Allocation sites per report. Enough to see a pattern, short enough to read in
#: a journal.
TOP_N = 15


def configured_frames(env: dict[str, str] | None = None) -> int | None:
    """Traceback depth to profile at, or None when profiling is off.

    Accepts `1`/`true`/`yes`/`on` for the default depth, or an explicit integer.
    Anything unparseable disables it WITH A WARNING rather than guessing — a
    typo'd flag silently profiling at depth 1 would produce a useless report that
    looks like a real answer.
    """
    raw = (env if env is not None else os.environ).get(ENV_VAR, "").strip().lower()
    if not raw or raw in {"0", "false", "no", "off"}:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return DEFAULT_FRAMES
    try:
        frames = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a number or boolean; profiling disabled", ENV_VAR, raw)
        return None
    if frames < 1:
        logger.warning("%s=%d must be >= 1; profiling disabled", ENV_VAR, frames)
        return None
    return frames


def configured_interval(env: dict[str, str] | None = None) -> float:
    raw = (env if env is not None else os.environ).get(INTERVAL_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %.0fs", INTERVAL_ENV_VAR, raw,
                       DEFAULT_INTERVAL_S)
        return DEFAULT_INTERVAL_S
    return value if value > 0 else DEFAULT_INTERVAL_S


def format_growth(stats: list[Any], *, top_n: int = TOP_N) -> list[str]:
    """Render a `compare_to()` result as readable lines. Pure, so it is testable.

    Only GROWTH is shown. A site that shrank is not evidence about a leak, and
    including both halves doubles the length of something meant to be read on a
    phone at 15:00.
    """
    lines: list[str] = []
    for stat in stats[:top_n]:
        if stat.size_diff <= 0:
            continue
        frame = stat.traceback[0]
        source = linecache.getline(frame.filename, frame.lineno).strip()
        lines.append(
            f"  {stat.size_diff / 1024:9.1f} KiB  {stat.count_diff:+8,} objs  "
            f"{frame.filename}:{frame.lineno}"
            + (f"\n      {source}" if source else "")
        )
    return lines


class MemoryProfiler:
    """Periodic allocation-growth reports. Inert unless `ARGUS_TRACEMALLOC` is set."""

    def __init__(
        self,
        *,
        frames: int | None = None,
        interval_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._frames = frames if frames is not None else configured_frames(env)
        self._interval = interval_s if interval_s is not None else configured_interval(env)
        self._baseline: tracemalloc.Snapshot | None = None
        self._task: asyncio.Task[None] | None = None
        self._reports = 0

    @property
    def enabled(self) -> bool:
        return self._frames is not None

    def start(self) -> None:
        """Begin tracing and schedule periodic reports. A no-op when disabled.

        Called at the END of `Application.startup()`, so the baseline excludes
        every import, the config, the DB connection and the control server —
        which are long-lived by definition and would otherwise dominate the
        report forever.
        """
        if not self.enabled:
            return
        try:
            tracemalloc.start(self._frames or DEFAULT_FRAMES)
            self._baseline = tracemalloc.take_snapshot()
        except Exception:  # noqa: BLE001 - a diagnostic must never break startup
            logger.exception("could not start tracemalloc; profiling disabled")
            self._frames = None
            return
        logger.warning(
            "ALLOCATION PROFILING ON (%s): %d frames, report every %.0fs. This adds "
            "real memory and CPU overhead — do not leave it on.",
            ENV_VAR, self._frames, self._interval,
        )
        self._task = asyncio.create_task(self._run(), name="memprofile")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.exception("memory profiler failed during shutdown")
            self._task = None
        if self.enabled and tracemalloc.is_tracing():
            # One final report: the most informative moment is the end of a
            # session, when accumulation has had all day to show itself.
            await self.report(final=True)
            tracemalloc.stop()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.report()

    async def report(self, *, final: bool = False) -> None:
        """Log the top allocation sites by growth since the baseline.

        Snapshot AND diff run in a worker thread: `take_snapshot()` walks the whole
        trace table and `compare_to()` is heavy pure-Python, so doing either on the
        loop would be exactly the stall this exists to find.
        """
        if not self.enabled or self._baseline is None:
            return
        try:
            lines, total_kib = await asyncio.to_thread(self._diff)
        except Exception:  # noqa: BLE001 - never raise out of a diagnostic
            logger.exception("allocation report failed")
            return
        self._reports += 1
        header = "FINAL allocation report" if final else f"allocation report #{self._reports}"
        if not lines:
            logger.info("%s: no growth since baseline", header)
            return
        logger.warning(
            "%s — top %d sites by GROWTH since startup (total +%.1f KiB):\n%s",
            header, len(lines), total_kib, "\n".join(lines),
        )

    def _diff(self) -> tuple[list[str], float]:
        """Runs in a worker thread. Returns (rendered lines, total growth KiB)."""
        assert self._baseline is not None
        current = tracemalloc.take_snapshot()
        stats = current.compare_to(self._baseline, "lineno")
        total = sum(s.size_diff for s in stats if s.size_diff > 0) / 1024
        return format_growth(stats), total
