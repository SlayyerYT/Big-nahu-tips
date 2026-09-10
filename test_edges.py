"""Edge cases that only appear on a bad day: gaps, empty histories, extremes."""
import sys

import formatter as fmt
from sources.base import CoinLS, Point, average_points, nearest, prev_point
from sources.binance import build_row

FAILS = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(name)


NOW = 1_800_000_000
S = 300


def series(n, start_long=70.0, step=0.0, skip=()):
    out = []
    for i in range(n):
        t = NOW - (n - 1 - i) * S
        if i in skip:
            continue
        lng = start_long + step * i
        out.append(Point(t=t, long_pct=lng, short_pct=100 - lng))
    return out


# --- nearest() -------------------------------------------------------------
s = series(20)
check("nearest exact hit", nearest(s, NOW - 600, 600).t == NOW - 600)
check("nearest rejects out-of-tolerance", nearest(s, NOW - 99999, 600) is None)
check("nearest on empty series", nearest([], NOW, 600) is None)

# A missing bar must not silently shift the column: the 10-min-ago bar is gone,
# so the nearest point is 15 min ago, still inside a 2-interval tolerance.
gapped = series(20, skip=(17,))
picked = nearest(gapped, NOW - 600, 600)
check("gap falls back to a nearby bar", picked is not None and picked.t == NOW - 900)
check("gap beyond tolerance returns None", nearest(gapped, NOW - 600, 200) is None)

# --- prev_point ------------------------------------------------------------
# The identity guard, not a narrow tolerance, is what stops a now-vs-now compare.
one_bar = [Point(t=NOW, long_pct=70, short_pct=30)]
check("prev refuses to compare now against now", prev_point(one_bar, NOW, 600, S) is None)
check("prev finds a real earlier bar", prev_point(series(20), NOW, 600, S).t == NOW - 600)
# A 5-minute cadence must still populate the column (the old tolerance broke this).
check("prev works at a 5-minute cadence", prev_point(series(20), NOW, 300, S).t == NOW - 300)
check("prev works at a 30-minute cadence", prev_point(series(20), NOW, 1800, S).t == NOW - 1800)

# --- build_row -------------------------------------------------------------
check("empty series -> None (no crash)", build_row("X", [], ["v"], NOW, 600, S) is None)

stale = [Point(t=NOW - 100000, long_pct=70, short_pct=30)]
check("stale series -> None", build_row("X", stale, ["v"], NOW, 600, S) is None)

row = build_row("SOL", series(300, 70.0, 0.01), ["Binance"], NOW, 600, S)
check("full series builds a row", row is not None and row.now is not None)
check("24h column resolves", row.h24 is not None)
check("delta is populated", row.delta_pp is not None)

row1 = build_row("NEW", one_bar, ["Binance"], NOW, 600, S)
check("single-bar row exists", row1 is not None)
check("single-bar delta is None", row1.delta_pp is None)
check("single-bar 24h is None", row1.h24 is None)

# --- average_points --------------------------------------------------------
check("average of none -> None", average_points([None, None]) is None)
avg = average_points([Point(NOW, 60, 40), None, Point(NOW, 80, 20)])
check("average skips missing venues", avg is not None and abs(avg.long_pct - 70) < 1e-9)
check("averaged ratio derives from mean shares", abs(avg.ratio - 70 / 30) < 1e-9)

# --- value formatting ------------------------------------------------------
check("None ratio renders", fmt._fmt_ratio(None) == "-")
check("inf ratio clamps", fmt._fmt_ratio(float("inf")) == "-")
check("huge ratio clamps", fmt._fmt_ratio(9e9) == "99.99")
check("zero shorts -> inf -> dash", fmt._fmt_ratio(Point(NOW, 100.0, 0.0).ratio) == "-")
check("None delta renders", fmt._fmt_delta(None) == "-")
check("delta keeps sign", fmt._fmt_delta(-0.14) == "-0.14")
check("runaway delta clamps", fmt._fmt_delta(-1e6) == "-99.99")
check("short ticker untouched", fmt._fmt_coin("SOL", 5) == "SOL")
check("exact-fit ticker untouched", fmt._fmt_coin("AVAX", 5) == "AVAX")
check("long ticker marked, not silently clipped", fmt._fmt_coin("LONGNAME", 5) == "LON.")

# --- layout ----------------------------------------------------------------
# Deliberately awkward rows: every cell filled to its limit, plus missing values.
awkward = [
    CoinLS("SOL", ["v"], Point(NOW, 70.53, 29.21), Point(NOW - 600, 70.67, 29.07),
           Point(NOW - 3600, 70.5, 29.24), Point(NOW - 86400, 70.57, 29.17)),
    CoinLS("A", ["v"], Point(NOW, 100.0, 0.0), None, None, None),
    CoinLS("LONGNAME", ["v"], Point(NOW, 5.0, 95.0),
           Point(NOW - 600, 99.0, 1.0), None, Point(NOW - 86400, 50.0, 50.0)),
    CoinLS("ZEC", ["v"], Point(NOW, 33.0, 67.0), Point(NOW - 600, 32.83, 67.17), None, None),
]

for name in ("mobile", "wide"):
    layout = fmt.get_layout(name)
    table = fmt.build_table(awkward, layout)
    expected = fmt.table_width(layout)
    body = [ln for ln in table.split("\n") if ln and not ln.startswith("-")]
    # Strip the trailing " <emoji><arrow>" to leave just the numeric grid.
    grid = [ln.rstrip().rsplit(" ", 1)[0] for ln in body[1:-1]]
    print(f"\n--- {name} ({expected} chars wide) ---")
    print(table + "\n")
    check(f"[{name}] header width matches the grid", len(fmt.header_line(layout)) == expected)
    check(f"[{name}] no row overflows the grid", max(len(g) for g in grid) <= expected)
    # Columns colliding is the bug a monospace table gets reported for: every
    # row must still split into the same number of whitespace-separated fields.
    field_counts = {len(ln.split()) for ln in body[1:-1]}
    check(f"[{name}] columns never collide {field_counts}", len(field_counts) == 1)
    check(f"[{name}] truncation is marked", "LONGNAME" not in table)

# The whole point of the mobile layout: Discord's mobile code block shows roughly
# 32 monospace characters before it starts scrolling sideways.
MOBILE_BUDGET = 32
mobile = fmt.get_layout("mobile")
mobile_w = fmt.table_width(mobile)
check(f"mobile table fits a phone ({mobile_w} <= {MOBILE_BUDGET})", mobile_w <= MOBILE_BUDGET)
mobile_lines = fmt.build_table(awkward, mobile).split("\n")
longest = max(len(ln) for ln in mobile_lines)
check(f"no mobile line overflows incl. emoji ({longest})", longest <= MOBILE_BUDGET + 4)
check("mobile drops the redundant SHORT column", " / " not in mobile_lines[2])
check("wide keeps the SHORT column", " / " in fmt.build_table(awkward, fmt.get_layout("wide")))
check("unknown layout name falls back to the default", fmt.get_layout("nonsense") is mobile)

# --- size guard ------------------------------------------------------------
big = [CoinLS(f"CN{i:02d}", ["v"], Point(NOW, 55.5, 44.5), Point(NOW - 600, 55.0, 45.0),
              Point(NOW - 3600, 54.0, 46.0), Point(NOW - 86400, 53.0, 47.0)) for i in range(20)]
for name in ("mobile", "wide"):
    desc = fmt.build_description(big, fmt.get_layout(name))
    check(f"[{name}] 20 rows fit an embed ({len(desc)} chars)", len(desc) <= 4096)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all edge cases pass")
