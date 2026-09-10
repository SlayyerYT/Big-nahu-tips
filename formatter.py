"""Render the coin rows as a monospace table.

Why a table and not the per-coin prose block: twenty 8-line blocks is roughly
9,000 characters, and Discord caps an embed description at 4,096 (and message
content at 2,000). The table carries the same numbers in ~1,200 characters.

Two layouts, because Discord code blocks do not wrap - anything wider than the
screen scrolls sideways, and on a phone that is unusable:

  mobile (30 chars) - fits a phone without horizontal scrolling
  wide   (45 chars) - adds SHORT and 1h, for desktop

The mobile layout drops two columns and loses almost nothing. SHORT is always
100 - LONG, so printing it costs eight characters and tells the reader nothing
they cannot already see. 1h, at a 10-minute cadence, tracks the current value so
closely that 24h carries the actual history.

Column alignment is load-bearing - every numeric field is clamped and printed
with a fixed width so one runaway value cannot shift the whole grid. Emoji sit
at the end of the line, where their variable rendering width cannot misalign
anything.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sources.base import CoinLS

# Column widths in monospace characters, per layout.
# Every width is (longest possible value + 1), so a cell filled to its limit
# still leaves a separating space. "-99.99" is 6 characters, so its column is 7.
LAYOUTS = {
    "mobile": {
        "coin": 5,
        "pct": 6,
        "ratio": 6,
        "delta": 7,
        "h1": 6,
        "h24": 6,
        "show_short": False,
        # 1h is dropped: at a 10-minute cadence it tracks the current value almost
        # exactly, so it is the cheapest column to lose to the width budget.
        "show_h1": False,
        "headers": ("COIN", "LONG", "L/S", "chg", "1h", "24h"),
    },
    "wide": {
        "coin": 6,
        "pct": 14,
        "ratio": 6,
        "delta": 7,
        "h1": 6,
        "h24": 6,
        "show_short": True,
        "show_h1": True,
        "headers": ("COIN", "LONG / SHORT", "L/S", "chg", "1h", "24h"),
    },
}
DEFAULT_LAYOUT = "mobile"

# A crowd this lopsided counts as biased.
LONG_BIAS_PCT = 55.0
SHORT_BIAS_PCT = 45.0
# Below this, a move is noise rather than a trend.
TREND_EPS_PP = 0.10

MAX_RATIO = 99.99


def get_layout(name: str | None) -> dict:
    return LAYOUTS.get((name or DEFAULT_LAYOUT).lower(), LAYOUTS[DEFAULT_LAYOUT])


def table_width(layout: dict) -> int:
    width = layout["coin"] + layout["pct"] + layout["ratio"] + layout["delta"] + layout["h24"]
    if layout["show_h1"]:
        width += layout["h1"]
    return width


def _fmt_coin(ticker: str, width: int) -> str:
    """Fit a ticker to the column, marking any truncation.

    A silently clipped ticker reads as a different, real coin - "LONGNAME" cut to
    "LONG" is misleading in a way that a trailing dot is not.
    """
    limit = width - 1
    if len(ticker) <= limit:
        return ticker
    return ticker[: limit - 1] + "."


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return "-"
    if value != value or value in (float("inf"), float("-inf")):
        return "-"
    return f"{min(value, MAX_RATIO):.2f}"


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "-"
    clamped = max(-MAX_RATIO, min(MAX_RATIO, value))
    return f"{clamped:+.2f}"


def _bias_emoji(long_pct: float) -> str:
    if long_pct >= LONG_BIAS_PCT:
        return "\N{LARGE GREEN CIRCLE}"
    if long_pct <= SHORT_BIAS_PCT:
        return "\N{LARGE RED CIRCLE}"
    return "\N{MEDIUM WHITE CIRCLE}"


def _trend_arrow(delta_pp: float | None) -> str:
    if delta_pp is None:
        return ""  # empty, not a space: no trailing whitespace on the line
    if delta_pp > TREND_EPS_PP:
        return "\N{NORTH EAST ARROW}"
    if delta_pp < -TREND_EPS_PP:
        return "\N{SOUTH EAST ARROW}"
    return "\N{RIGHTWARDS ARROW}"


def header_line(layout: dict) -> str:
    coin, pct, ratio, delta, h1, h24 = layout["headers"]
    # The percentage column is left-aligned only in the wide layout, where the
    # cell holds "70.5 / 29.2"; a lone number reads better right-aligned.
    pct_cell = f"{pct:<{layout['pct']}}" if layout["show_short"] else f"{pct:>{layout['pct']}}"
    h1_cell = f"{h1:>{layout['h1']}}" if layout["show_h1"] else ""
    return (
        f"{coin:<{layout['coin']}}"
        f"{pct_cell}"
        f"{ratio:>{layout['ratio']}}"
        f"{delta:>{layout['delta']}}"
        f"{h1_cell}"
        f"{h24:>{layout['h24']}}"
    )


def row_line(row: CoinLS, layout: dict) -> str:
    now = row.now
    assert now is not None  # rows without a current point are dropped upstream

    if layout["show_short"]:
        pct = f"{now.long_pct:.1f} / {now.short_pct:.1f}"
        pct_cell = f"{pct:<{layout['pct']}}"
    else:
        pct_cell = f"{now.long_pct:.1f}".rjust(layout["pct"])

    h1_cell = (
        f"{_fmt_ratio(row.h1.ratio if row.h1 else None):>{layout['h1']}}"
        if layout["show_h1"]
        else ""
    )
    line = (
        f"{_fmt_coin(row.coin, layout['coin']):<{layout['coin']}}"
        f"{pct_cell}"
        f"{_fmt_ratio(now.ratio):>{layout['ratio']}}"
        f"{_fmt_delta(row.delta_pp):>{layout['delta']}}"
        f"{h1_cell}"
        f"{_fmt_ratio(row.h24.ratio if row.h24 else None):>{layout['h24']}}"
    )
    return f"{line} {_bias_emoji(now.long_pct)}{_trend_arrow(row.delta_pp)}"


def summary_line(rows: list[CoinLS], layout: dict) -> str:
    live = [r for r in rows if r.now is not None]
    if not live:
        return ""
    avg_long = sum(r.now.long_pct for r in live) / len(live)
    crowd_long = sum(1 for r in live if r.now.long_pct >= LONG_BIAS_PCT)
    if layout["show_short"]:
        return f"{crowd_long}/{len(live)} coins crowd-long  |  avg {avg_long:.1f}% long"
    return f"{crowd_long}/{len(live)} long | avg {avg_long:.1f}%"


def build_table(rows: list[CoinLS], layout: dict | None = None) -> str:
    layout = layout or get_layout(None)
    rule = "-" * table_width(layout)
    lines = [header_line(layout), rule]
    lines.extend(row_line(r, layout) for r in rows)
    summary = summary_line(rows, layout)
    if summary:
        lines.append(rule)
        lines.append(summary)
    return "\n".join(lines)


def build_description(rows: list[CoinLS], layout: dict | None = None) -> str:
    """The embed description: the table in a code block, plus a legend.

    The legend sits outside the code block so it wraps to the screen instead of
    forcing the whole table wider.
    """
    layout = layout or get_layout(None)
    table = build_table(rows, layout)
    if layout["show_short"]:
        legend = (
            "`L/S` long accounts per short account  ·  "
            "`chg` change in long share since the last post, in percentage points"
        )
    else:
        legend = (
            "`LONG` % of accounts long · `L/S` longs per short · "
            "`chg` change since last post (pp)"
        )
    return f"```\n{table}\n```\n{legend}"


def footer_text(source_label: str, venues: list[str], interval_minutes: int, degraded: bool) -> str:
    # Skip the venue list when it just repeats the source name (single-venue source).
    show_venues = venues and venues != [source_label]
    venue_note = f" ({', '.join(venues)})" if show_venues else ""
    stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    parts = [f"Source: {source_label}{venue_note}", stamp, f"next update in {interval_minutes}m"]
    if degraded:
        parts.append("BACKUP SOURCE - levels differ from the primary")
    return "  ·  ".join(parts)


def embed_colour(rows: list[CoinLS]) -> int:
    live = [r for r in rows if r.now is not None]
    if not live:
        return 0x2B2D31
    avg_long = sum(r.now.long_pct for r in live) / len(live)
    if avg_long >= LONG_BIAS_PCT:
        return 0x2ECC71
    if avg_long <= SHORT_BIAS_PCT:
        return 0xE74C3C
    return 0x95A5A6
