"""Shared data model for long/short ratio sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Point:
    """One long/short observation. Percentages are 0-100."""

    t: int  # unix seconds, start of the interval
    long_pct: float
    short_pct: float

    @property
    def ratio(self) -> float:
        if self.short_pct <= 0:
            return float("inf")
        return self.long_pct / self.short_pct


@dataclass
class CoinLS:
    """Everything one table row needs, all taken from a single source."""

    coin: str
    venues: list[str] = field(default_factory=list)
    now: Point | None = None
    prev: Point | None = None  # ~1 posting interval ago
    h1: Point | None = None
    h24: Point | None = None

    @property
    def delta_pp(self) -> float | None:
        """Change in long share, in percentage points, vs the previous cycle."""
        if self.now is None or self.prev is None:
            return None
        return self.now.long_pct - self.prev.long_pct


def nearest(series: Sequence[Point], target_t: int, tolerance_s: int) -> Point | None:
    """Pick the observation closest to target_t, or None if the gap is too big.

    Indexing backwards by a fixed offset (series[-3] for "10 minutes ago") breaks
    silently whenever a venue drops a bar, shifting every column. Match on the
    timestamp instead and refuse a point that is too far from what was asked for.
    """
    if not series:
        return None
    best = min(series, key=lambda p: abs(p.t - target_t))
    if abs(best.t - target_t) > tolerance_s:
        return None
    return best


def prev_point(
    series: Sequence[Point], latest_t: int, prev_offset_s: int, interval_s: int
) -> Point | None:
    """The bar one posting interval back, or None if there isn't a distinct one.

    Without the identity guard, a sparse series resolves 'one interval ago' to the
    current bar - it is within tolerance of the target - and the column reports a
    change of +0.00 instead of staying blank. The guard, not a narrowed tolerance,
    is what prevents that: squeezing the tolerance below one interval would reject
    a legitimate neighbouring bar whenever the cadence and the bar grid don't line
    up, blanking dPP for every coin.
    """
    point = nearest(series, latest_t - prev_offset_s, interval_s)
    if point is None or point.t == latest_t:
        return None
    return point


def average_points(points: Iterable[Point | None]) -> Point | None:
    """Unweighted mean of the long share across venues.

    We have no per-venue account counts, so an unweighted mean is the best
    available approximation of a cross-exchange aggregate. The ratio is derived
    from the mean shares (mean-of-ratios would not equal long/short).
    """
    valid = [p for p in points if p is not None]
    if not valid:
        return None
    longs = sum(p.long_pct for p in valid) / len(valid)
    shorts = sum(p.short_pct for p in valid) / len(valid)
    t = max(p.t for p in valid)
    return Point(t=t, long_pct=longs, short_pct=shorts)


class SourceError(RuntimeError):
    """Raised when a source cannot produce usable data for this cycle."""
