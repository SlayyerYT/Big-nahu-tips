"""Edge cases that only appear on a bad day: gaps, empty histories, extremes."""
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import formatter as fmt
import post_calendar
from sources import econ_calendar
from sources.base import CoinLS, Point, average_points, nearest, prev_point
from sources.binance import build_row
from sources.okx import _latest_point

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

# --- OKX size column -------------------------------------------------------
NOW_MS = NOW * 1000


def okx_rows(*pairs):
    """OKX rubik shape: [timestamp_ms, ratio] strings, newest first."""
    return [[str(ts), str(r)] for ts, r in pairs]


# A ratio is converted to shares so it can live in the same Point as everything
# else; round-tripping it back must land on the number OKX actually sent.
p = _latest_point(okx_rows((NOW_MS, "3.0")), NOW)
check("ratio 3.0 -> 75% long", p is not None and abs(p.long_pct - 75.0) < 1e-9)
check("ratio survives the round trip", abs(p.ratio - 3.0) < 1e-9)
balanced = _latest_point(okx_rows((NOW_MS, "1.0")), NOW)
check("ratio 1.0 -> 50/50", abs(balanced.long_pct - 50.0) < 1e-9)
netshort = _latest_point(okx_rows((NOW_MS, "0.878")), NOW)
check("sub-1 ratio reads as net short", netshort.long_pct < 50.0)

# Newest-first is OKX's order, but nothing enforces it, so the newest bar is
# chosen by timestamp rather than by position.
scrambled = _latest_point(okx_rows((NOW_MS - 600_000, "1.0"), (NOW_MS, "3.0")), NOW)
check("picks the newest bar, not the first", abs(scrambled.long_pct - 75.0) < 1e-9)

check("stale OKX data is refused", _latest_point(okx_rows((NOW_MS - 99_000_000, "3.0")), NOW) is None)
check("empty OKX response -> None", _latest_point([], NOW) is None)
check("zero ratio is not a reading", _latest_point(okx_rows((NOW_MS, "0")), NOW) is None)
check("negative ratio is not a reading", _latest_point(okx_rows((NOW_MS, "-2")), NOW) is None)
check("malformed OKX row is skipped", _latest_point([["nope", "x"], [str(NOW_MS), "2.0"]], NOW) is not None)

# --- size column rendering -------------------------------------------------
def sized_row(coin, long_pct, size_long=None):
    return CoinLS(
        coin, ["v"],
        Point(NOW, long_pct, 100 - long_pct),
        Point(NOW - 600, long_pct, 100 - long_pct),
        Point(NOW - 3600, long_pct, 100 - long_pct),
        Point(NOW - 86400, long_pct, 100 - long_pct),
        None if size_long is None else Point(NOW, size_long, 100 - size_long),
    )


mobile = fmt.get_layout("mobile")


def size_cell(row, layout):
    """The SIZE cell alone. Searching the whole line matches the chg column's +0.00."""
    grid = fmt.row_line(row, layout)[: fmt.table_width(layout)]
    return grid[-layout["widths"]["size"]:].strip()


check("missing size prints a blank", size_cell(sized_row("AAA", 70.0), mobile) == "-")
check("present size prints its ratio", size_cell(sized_row("AAA", 70.0, 75.0), mobile) == "3.00")

# Direction, not magnitude: the two columns come from different populations, so
# only an opposite *sign* counts as a disagreement.
check("crowd long + size short = disagreement", fmt.size_disagrees(sized_row("A", 70.0, 30.0)))
check("crowd short + size long = disagreement", fmt.size_disagrees(sized_row("A", 30.0, 70.0)))
check("same direction is not a disagreement", not fmt.size_disagrees(sized_row("A", 70.0, 60.0)))
check("no size data is not a disagreement", not fmt.size_disagrees(sized_row("A", 70.0)))


def at_ratio(ratio, crowd_long=70.0):
    """A row whose SIZE column reads exactly this long/short ratio."""
    return sized_row("A", crowd_long, 100.0 * ratio / (1.0 + ratio))


# The normal case for this metric, and the one a bare >50% comparison gets wrong:
# the top-trader ratio sits near 1.00 nearly all the time, so without a deadband
# every flat book scores as a disagreement and the count pins at maximum forever.
check("size 0.99 is flat, not a disagreement", not fmt.size_disagrees(at_ratio(0.99)))
check("size 0.97 is flat, not a disagreement", not fmt.size_disagrees(at_ratio(0.97)))
check("size exactly 1.00 is flat", not fmt.size_disagrees(at_ratio(1.0)))
check("size 1.01 is flat even against a short crowd", not fmt.size_disagrees(at_ratio(1.01, 30.0)))
# ...but a real lean still has to register, in both directions.
check("size 0.76 is a real lean", fmt.size_disagrees(at_ratio(0.76)))
check("size 1.20 against a short crowd is a real lean", fmt.size_disagrees(at_ratio(1.20, 42.0)))
check("a lean agreeing with the crowd still does not count", not fmt.size_disagrees(at_ratio(1.20)))
check("inf size ratio is not a disagreement", not fmt.size_disagrees(sized_row("A", 70.0, 100.0)))

split_rows = [sized_row("A", 70.0, 30.0), sized_row("B", 70.0, 60.0), sized_row("C", 70.0)]
lines = fmt.summary_lines(split_rows, mobile)
# The denominator counts only the coins OKX answered for - C must not be scored
# as agreement just because it has no reading.
check("split count ignores coins without size data", lines[-1].startswith("1/2"))
check("summary fits the mobile budget", max(len(ln) for ln in lines) <= 32)
check("no size data at all -> no split line", len(fmt.summary_lines([sized_row("A", 70.0)], mobile)) == 1)

# --- provenance ------------------------------------------------------------
plain = fmt.footer_text("Coinalyze", ["Binance", "Bybit"], 10, False, None)
credited = fmt.footer_text("Coinalyze", ["Binance", "Bybit"], 10, False, "OKX top traders")
check("footer stays silent about an empty size column", "OKX" not in plain)
check("footer credits the size source separately", "SIZE: OKX top traders" in credited)
legend_with = fmt.build_description([sized_row("A", 70.0, 75.0)], mobile)
legend_without = fmt.build_description([sized_row("A", 70.0)], mobile)
check("SIZE is explained when present", "top traders only" in legend_with)
check("SIZE is not explained when absent", "top traders only" not in legend_without)

# The mobile budget is the reason 24h was dropped; prove the swap actually held.
check("mobile still fits after adding SIZE", fmt.table_width(mobile) <= MOBILE_BUDGET)
check("mobile shows SIZE", "SIZE" in fmt.header_line(mobile))
check("mobile dropped 24h", "24h" not in fmt.header_line(mobile))
check("wide keeps both 24h and SIZE", {"24h", "SIZE"} <= set(fmt.header_line(fmt.get_layout("wide")).split()))

# --- economic calendar -----------------------------------------------------
CAL_NOW = datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)  # a Sunday, 08:00 Vienna


def feed_row(offset_hours, title="CPI m/m", country="USD", impact="High",
             forecast="0.3%", previous="0.2%"):
    when = CAL_NOW + timedelta(hours=offset_hours)
    return {
        "title": title, "country": country, "impact": impact,
        # The real feed serves US Eastern offsets, not UTC.
        "date": when.astimezone(timezone(timedelta(hours=-4))).isoformat(),
        "forecast": forecast, "previous": previous,
    }


week = econ_calendar._select([feed_row(30), feed_row(-30, title="Old CPI")], CAL_NOW, 7)
check("past events are excluded", [e.title for e in week] == ["CPI m/m"])
check("timestamps normalise to UTC", week[0].when.tzinfo is timezone.utc)

# The window, not the feed's "thisweek" label, decides what is in.
check("beyond the horizon is excluded", econ_calendar._select([feed_row(24 * 8)], CAL_NOW, 7) == [])
check("exactly at the horizon is included", len(econ_calendar._select([feed_row(24 * 7)], CAL_NOW, 7)) == 1)
check("exactly at now is included", len(econ_calendar._select([feed_row(0)], CAL_NOW, 7)) == 1)

check("low impact is dropped", econ_calendar._select([feed_row(10, impact="Low")], CAL_NOW, 7) == [])
check("other currencies are dropped", econ_calendar._select([feed_row(10, country="GBP")], CAL_NOW, 7) == [])
holiday = econ_calendar._select([feed_row(10, title="Bank Holiday", impact="Holiday")], CAL_NOW, 7)
check("holidays are kept", len(holiday) == 1 and holiday[0].is_holiday)

check("malformed rows are skipped", econ_calendar._select(["nope", None, feed_row(5)], CAL_NOW, 7) != [])
check("unparseable date is skipped", econ_calendar._select([feed_row(5) | {"date": "soon"}], CAL_NOW, 7) == [])
check("offset-less date is skipped", econ_calendar._select([feed_row(5) | {"date": "2026-09-14T12:00:00"}], CAL_NOW, 7) == [])
check("events come back soonest first",
      [e.title for e in econ_calendar._select([feed_row(40, title="B"), feed_row(20, title="A")], CAL_NOW, 7)] == ["A", "B"])

# The distinction the whole design rests on: an empty week because the week is
# quiet, versus an empty week because the feed still holds the old week's data.
# Measured by how far the feed reaches, because "has any future row" is too weak
# - a stale feed always keeps a few late rows and passed that test while holding
# nothing about the week ahead.
stale = [feed_row(-30), feed_row(-60), feed_row(14, impact="Low")]  # tail of the old week
fresh = [feed_row(-30), feed_row(24 * 6, impact="Low")]             # rolled over
check("stale feed is not trusted for an empty week",
      not econ_calendar.WeekAhead([], econ_calendar._reach_days(stale, CAL_NOW)).covers_the_week)
check("rolled-over feed is trusted",
      econ_calendar.WeekAhead([], econ_calendar._reach_days(fresh, CAL_NOW)).covers_the_week)
check("a feed with no future rows reaches zero", econ_calendar._reach_days([feed_row(-30)], CAL_NOW) == 0.0)
check("reach is measured in days", abs(econ_calendar._reach_days([feed_row(48)], CAL_NOW) - 2.0) < 1e-6)

# --- calendar rendering ----------------------------------------------------
VIENNA = ZoneInfo("Europe/Vienna")
events = econ_calendar._select(
    [feed_row(8, title="CPI m/m"), feed_row(9, title="Core CPI m/m"),
     feed_row(56, title="FOMC Statement", forecast="", previous=""),
     feed_row(57, title="Bank Holiday", impact="Holiday", forecast="", previous="")],
    CAL_NOW, 7,
)
desc = post_calendar.build_description(events, CAL_NOW, VIENNA)
check("every event is rendered", all(t in desc for t in ("CPI m/m", "FOMC Statement", "Bank Holiday")))
check("forecast and previous appear", "F 0.3%" in desc and "P 0.2%" in desc)
# FOMC statements carry no numbers; an empty "F  / P" would be noise.
check("no empty forecast block", "F  " not in desc and "— ·" not in desc)
check("days are grouped", desc.count("__") == 2 * 2)  # two distinct local days
check("description fits an embed", len(desc) <= 4096)

quiet = post_calendar.build_description([], CAL_NOW, VIENNA)
check("a genuinely quiet week says so", "No high-impact US events" in quiet)
check("quiet week never claims events", "F " not in quiet)

payload = post_calendar.build_payload(events, CAL_NOW, VIENNA)
check("payload carries one embed", len(payload["embeds"]) == 1)
check("footer names the timezone", "Europe/Vienna" in payload["embeds"][0]["footer"]["text"])
check("footer names the source", "ForexFactory" in payload["embeds"][0]["footer"]["text"])

# Test mode must be unmistakable: a test post that reads like the real thing is
# worse than none, since its events are mostly in the past.
test_payload = post_calendar.build_payload(events, CAL_NOW, VIENNA, 14, True)
real_payload = post_calendar.build_payload(events, CAL_NOW, VIENNA, 7, False)
check("test post is marked in the title", "TEST" in test_payload["embeds"][0]["title"])
check("test post warns in the body", "Delivery test" in test_payload["embeds"][0]["description"])
check("test post is marked in the footer", "TEST RUN" in test_payload["embeds"][0]["footer"]["text"])
check("test post uses a different colour", test_payload["embeds"][0]["color"] != real_payload["embeds"][0]["color"])
check("the real post carries no test marking",
      "TEST" not in real_payload["embeds"][0]["title"]
      and "TEST" not in real_payload["embeds"][0]["footer"]["text"]
      and "Delivery test" not in real_payload["embeds"][0]["description"])
# The widened window has to show up in the rendered date range, not just the flag.
check("test window spans two weeks", "27 Sep" in post_calendar.build_description(events, CAL_NOW, VIENNA, 14))

# A packed week must degrade by dropping whole lines, never mid-event.
flood = econ_calendar._select([feed_row(1 + i * 0.01, title=f"Event number {i}") for i in range(400)], CAL_NOW, 7)
flooded = post_calendar.build_description(flood, CAL_NOW, VIENNA)
check(f"overlong calendar is truncated ({len(flooded)} chars)", len(flooded) <= 4096)
check("truncation is announced", "truncated" in flooded)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all edge cases pass")
