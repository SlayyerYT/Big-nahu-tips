"""Render the coin rows as a monospace table.

Why a table and not the per-coin prose block: twenty 8-line blocks is roughly
9,000 characters, and Discord caps an embed description at 4,096 (and message
content at 2,000). The table carries the same numbers in ~1,200 characters.

Two layouts, because Discord code blocks do not wrap - anything wider than the
screen scrolls sideways, and on a phone that is unusable:

  mobile (30 chars) - fits a phone without horizontal scrolling
  wide   (45 chars) - adds SHORT and 1h, for desktop

The mobile layout drops three columns and loses almost nothing. SHORT is always
100 - LONG, so printing it costs eight characters and tells the reader nothing
they cannot already see. 1h, at a 10-minute cadence, tracks the current value so
closely that it is pure repetition. 24h went when SIZE arrived: with a 30-char
budget and no room for both, a second opinion on the present beats a third
reading of the past, and chg still carries the short-term move.

Column alignment is load-bearing - every numeric field is clamped and printed
with a fixed width so one runaway value cannot shift the whole grid. Emoji sit
at the end of the line, where their variable rendering width cannot misalign
anything.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sources.base import CoinLS

# Columns left to right. A layout picks a subset in "show"; everything else about
# a column - its width, its heading - is defined once here, so adding or dropping
# one is a change in two places rather than six.
COLUMN_ORDER = ("coin", "pct", "ratio", "delta", "h1", "h24", "size")

# Column widths in monospace characters, per layout.
# Every width is (longest possible value + 1), so a cell filled to its limit
# still leaves a separating space. "-99.99" is 6 characters, so its column is 7.
LAYOUTS = {
    "mobile": {
        "widths": {"coin": 5, "pct": 6, "ratio": 6, "delta": 7, "h1": 6, "h24": 6, "size": 6},
        # 30 characters exactly - the budget a phone can show without scrolling
        # sideways. Nothing can join this set without something else leaving it.
        "show": ("coin", "pct", "ratio", "delta", "size"),
        "show_short": False,
        "headers": {
            "coin": "COIN", "pct": "LONG", "ratio": "L/S",
            "delta": "chg", "h1": "1h", "h24": "24h", "size": "SIZE",
        },
    },
    "wide": {
        "widths": {"coin": 6, "pct": 14, "ratio": 6, "delta": 7, "h1": 6, "h24": 6, "size": 6},
        "show": ("coin", "pct", "ratio", "delta", "h1", "h24", "size"),
        "show_short": True,
        "headers": {
            "coin": "COIN", "pct": "LONG / SHORT", "ratio": "L/S",
            "delta": "chg", "h1": "1h", "h24": "24h", "size": "SIZE",
        },
    },
}
DEFAULT_LAYOUT = "mobile"

# A crowd this lopsided counts as biased.
LONG_BIAS_PCT = 55.0
SHORT_BIAS_PCT = 45.0
# Below this, a move is noise rather than a trend.
TREND_EPS_PP = 0.10

# How far the size ratio must sit from 1.00 before it counts as a lean at all.
#
# The top-trader ratio lives in a tight band around 1.00 - a live sample ran
# 0.76 to 1.20 with most coins inside 0.90-1.00 - while the account side is
# almost always well past 55% long, because retail is persistently long. Without
# a deadband, every coin reading 0.97 or 0.99 scores as a disagreement and the
# count prints near-maximum on every post, which says nothing. The band is set
# to separate a genuine lean (DOGE 0.76, LTC 0.77, ZEC 1.20) from a flat book
# (0.97, 0.99, 1.00). It is wider in ratio terms than LONG_BIAS_PCT is in share
# terms on purpose: 55% long would be a ratio of 1.22 and would discard nearly
# every real reading this metric produces.
SIZE_NEUTRAL_BAND = 0.05

MAX_RATIO = 99.99


def get_layout(name: str | None) -> dict:
    return LAYOUTS.get((name or DEFAULT_LAYOUT).lower(), LAYOUTS[DEFAULT_LAYOUT])


def columns(layout: dict) -> tuple[str, ...]:
    """The layout's columns, always in COLUMN_ORDER regardless of how "show" is written."""
    shown = set(layout["show"])
    return tuple(c for c in COLUMN_ORDER if c in shown)


def table_width(layout: dict) -> int:
    return sum(layout["widths"][c] for c in columns(layout))


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


def size_disagrees(row: CoinLS) -> bool:
    """True when the big money leans the opposite way to the crowd.

    Direction only, never magnitude: the two columns describe different
    populations, so they are not expected to match in level and a gap between
    them means nothing on its own. A size reading inside SIZE_NEUTRAL_BAND is
    treated as flat rather than as a lean in either direction.
    """
    if row.now is None or row.size is None:
        return False
    ratio = row.size.ratio
    if ratio != ratio or ratio in (float("inf"), float("-inf")):
        return False
    if abs(ratio - 1.0) <= SIZE_NEUTRAL_BAND:
        return False  # flat book, not a lean
    return (row.now.long_pct > 50.0) != (ratio > 1.0)


def _trend_arrow(delta_pp: float | None) -> str:
    if delta_pp is None:
        return ""  # empty, not a space: no trailing whitespace on the line
    if delta_pp > TREND_EPS_PP:
        return "\N{NORTH EAST ARROW}"
    if delta_pp < -TREND_EPS_PP:
        return "\N{SOUTH EAST ARROW}"
    return "\N{RIGHTWARDS ARROW}"


def _left_aligned(column: str, layout: dict) -> bool:
    """Only the ticker, and the wide layout's "70.5 / 29.2" cell, read better left."""
    return column == "coin" or (column == "pct" and layout["show_short"])


def _cell(text: str, column: str, layout: dict) -> str:
    width = layout["widths"][column]
    return f"{text:<{width}}" if _left_aligned(column, layout) else f"{text:>{width}}"


def header_line(layout: dict) -> str:
    return "".join(_cell(layout["headers"][c], c, layout) for c in columns(layout))


def row_line(row: CoinLS, layout: dict) -> str:
    now = row.now
    assert now is not None  # rows without a current point are dropped upstream

    if layout["show_short"]:
        pct_text = f"{now.long_pct:.1f} / {now.short_pct:.1f}"
    else:
        pct_text = f"{now.long_pct:.1f}"

    values = {
        "coin": _fmt_coin(row.coin, layout["widths"]["coin"]),
        "pct": pct_text,
        "ratio": _fmt_ratio(now.ratio),
        "delta": _fmt_delta(row.delta_pp),
        "h1": _fmt_ratio(row.h1.ratio if row.h1 else None),
        "h24": _fmt_ratio(row.h24.ratio if row.h24 else None),
        # Blank, not a zero, whenever OKX had nothing for this coin: a missing
        # size reading must never be mistaken for a balanced one.
        "size": _fmt_ratio(row.size.ratio if row.size else None),
    }
    line = "".join(_cell(values[c], c, layout) for c in columns(layout))
    return f"{line} {_bias_emoji(now.long_pct)}{_trend_arrow(row.delta_pp)}"


def summary_lines(rows: list[CoinLS], layout: dict) -> list[str]:
    """Footer rows under the table: the crowd's position, then how often size disagrees.

    The second line is the one worth reading. Counting the coins where headcount
    and position size point opposite ways puts a number on the thing the SIZE
    column exists to show, and it costs no table width - which the first line,
    already near the 30-char budget, has none of to spare.
    """
    live = [r for r in rows if r.now is not None]
    if not live:
        return []

    avg_long = sum(r.now.long_pct for r in live) / len(live)
    crowd_long = sum(1 for r in live if r.now.long_pct >= LONG_BIAS_PCT)
    if layout["show_short"]:
        lines = [f"{crowd_long}/{len(live)} coins crowd-long  |  avg {avg_long:.1f}% long"]
    else:
        lines = [f"{crowd_long}/{len(live)} long | avg {avg_long:.1f}%"]

    # Only coins OKX actually answered for can agree or disagree; counting the
    # rest as agreement would quietly understate the split.
    sized = [r for r in live if r.size is not None]
    if sized:
        split = sum(1 for r in sized if size_disagrees(r))
        if layout["show_short"]:
            lines.append(f"size disagrees with headcount on {split} of {len(sized)}")
        else:
            lines.append(f"{split}/{len(sized)} disagree by size")
    return lines


def build_table(rows: list[CoinLS], layout: dict | None = None) -> str:
    layout = layout or get_layout(None)
    rule = "-" * table_width(layout)
    lines = [header_line(layout), rule]
    lines.extend(row_line(r, layout) for r in rows)
    summary = summary_lines(rows, layout)
    if summary:
        lines.append(rule)
        lines.extend(summary)
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
    # SIZE is explained separately and at length because it is the one column that
    # is not what the others are: different weighting, different venue and a
    # different population. Left unexplained it reads as "L/S but better", and a
    # reader would take a divergence for a size effect when part of it is simply
    # a narrower sample of traders.
    if any(r.size is not None for r in rows):
        legend += (
            "\n`SIZE` OKX **top traders only**, weighted by position size rather than "
            "counted per account - so it answers \"which way is the big money leaning\", "
            "not \"which way is the crowd leaning\". A different venue and a different "
            "group of traders than the other columns, so the two are not directly "
            "comparable in level - only in direction. (Across *all* traders this number "
            "is always 1.00: every long is somebody's short.) A reading within "
            f"{SIZE_NEUTRAL_BAND:.2f} of 1.00 counts as flat, not as a lean."
        )
    return f"```\n{table}\n```\n{legend}"


def footer_text(
    source_label: str,
    venues: list[str],
    interval_minutes: int,
    degraded: bool,
    size_label: str | None = None,
) -> str:
    # Skip the venue list when it just repeats the source name (single-venue source).
    show_venues = venues and venues != [source_label]
    venue_note = f" ({', '.join(venues)})" if show_venues else ""
    stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    # Provenance is per column, not per table: the SIZE column comes from a
    # different venue than everything else, so a single "Source:" line would be
    # false as soon as that column is populated.
    source_note = f"Source: {source_label}{venue_note}"
    if size_label:
        source_note += f", SIZE: {size_label}"
    parts = [source_note, stamp, f"next update in {interval_minutes}m"]
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
