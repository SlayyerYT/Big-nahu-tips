"""Post next week's high-impact macro calendar to Discord, then exit.

Runs once a week, not every ten minutes, and shares nothing with post_once.py but
the webhook - different source, different shape, different cadence - so it is a
separate entry point rather than a flag on the table poster.

    python post_calendar.py              # fetch and post
    python post_calendar.py --dry-run    # render and print, send nothing

Times are rendered in Europe/Vienna, since that is where whoever reads this is
deciding when to be at a screen. The underlying feed is US Eastern; the
conversion happens here so the stored data stays in UTC.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from bot import USER_AGENT
from sources.base import SourceError
from sources.econ_calendar import Event, HORIZON_DAYS, fetch_week_ahead

log = logging.getLogger("altcoin-ls-calendar")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISPLAY_TZ = ZoneInfo(os.getenv("CALENDAR_TZ", "Europe/Vienna"))

EMBED_COLOUR = 0x5865F2
TEST_COLOUR = 0x95A5A6
MAX_DESCRIPTION = 4096

# --test reaches this far back as well as forward. The point of a test post is to
# prove the webhook delivers and the formatting is right, and an empty list
# proves neither - so the window is widened into the past, where there are known
# events to render. It is marked as a test in the embed precisely because those
# events are not upcoming ones.
TEST_LOOKBACK_DAYS = 7


def _fmt_day(day: datetime) -> str:
    return day.strftime("%a %d %b")


def _fmt_event(event: Event, tz: ZoneInfo) -> str:
    local = event.when.astimezone(tz)
    if event.is_holiday:
        return f"`{local:%H:%M}` \N{BANK} {event.title}"

    line = f"`{local:%H:%M}` **{event.title}**"
    # Forecast and previous are empty strings for events that carry no number
    # (FOMC statements, speeches). Printing "F  / P" for those is noise.
    numbers = []
    if event.forecast:
        numbers.append(f"F {event.forecast}")
    if event.previous:
        numbers.append(f"P {event.previous}")
    if numbers:
        line += "  — " + " · ".join(numbers)
    return line


def build_description(
    events: list[Event],
    window_start: datetime,
    tz: ZoneInfo = DISPLAY_TZ,
    horizon_days: int = HORIZON_DAYS,
) -> str:
    """Events grouped by local day. Plain markdown, not a code block, so it wraps.

    The table poster uses a monospace block because its columns have to line up.
    Nothing here is columnar, and a code block would force horizontal scrolling on
    a phone for no benefit.
    """
    window_end = (window_start + timedelta(days=horizon_days)).astimezone(tz)
    header = f"**{_fmt_day(window_start.astimezone(tz))} – {_fmt_day(window_end)}**"

    if not events:
        return (
            f"{header}\n\nNo high-impact US events scheduled. "
            "A quiet week for data - moves are more likely to be positioning-driven."
        )

    lines = [header, ""]
    current_day = None
    for event in events:
        local = event.when.astimezone(tz)
        day = local.date()
        if day != current_day:
            if current_day is not None:
                lines.append("")
            lines.append(f"__{_fmt_day(local)}__")
            current_day = day
        lines.append(_fmt_event(event, tz))

    description = "\n".join(lines)
    if len(description) > MAX_DESCRIPTION:
        # Never silently truncate mid-event: drop whole trailing lines and say so.
        while len(description) > MAX_DESCRIPTION - 40 and lines:
            lines.pop()
            description = "\n".join(lines)
        description += "\n\n*(list truncated)*"
    return description


def build_payload(
    events: list[Event],
    window_start: datetime,
    tz: ZoneInfo = DISPLAY_TZ,
    horizon_days: int = HORIZON_DAYS,
    test_mode: bool = False,
) -> dict:
    count = len({e.when.astimezone(tz).date() for e in events if not e.is_holiday})
    subtitle = f"{len(events)} events across {count} day(s)" if events else "no high-impact data"
    description = build_description(events, window_start, tz, horizon_days)
    if test_mode:
        # Said plainly and at the top. A test post that looks like the real thing
        # is worse than no test: these events are mostly in the past, and someone
        # scrolling past would read them as the week ahead.
        description = (
            "*Delivery test - this window includes the past week, "
            "so these are not all upcoming events.*\n\n" + description
        )
    return {
        "embeds": [
            {
                "title": (
                    "\N{SPIRAL CALENDAR PAD} MACRO WEEK AHEAD"
                    + (" — TEST" if test_mode else "")
                ),
                "description": description,
                "color": TEST_COLOUR if test_mode else EMBED_COLOUR,
                "footer": {
                    "text": (
                        f"{subtitle}  ·  times {tz.key}  ·  "
                        "high-impact US events only  ·  source: ForexFactory"
                        + ("  ·  TEST RUN" if test_mode else "")
                    )
                },
            }
        ]
    }


async def run(dry_run: bool, test_mode: bool = False) -> int:
    now = datetime.now(timezone.utc)
    # In test mode the window starts a week in the past, so there is something
    # real to render whatever day it is run on.
    window_start = now - timedelta(days=TEST_LOOKBACK_DAYS) if test_mode else now
    horizon_days = HORIZON_DAYS + TEST_LOOKBACK_DAYS if test_mode else HORIZON_DAYS

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            week = await fetch_week_ahead(session, window_start, horizon_days)
        except SourceError as exc:
            log.error("could not read the calendar: %s", exc)
            return 1

        # An empty list is only publishable if the feed actually reaches into the
        # week it claims to describe. When it does not, we have no data about the
        # week ahead, and "no high-impact events" would be a confident wrong
        # answer to the one question this post exists to answer - so the run fails
        # loudly and posts nothing rather than reassuring anybody.
        #
        # Skipped for a test post, which makes no claim about the week ahead and
        # is labelled as making none.
        if not test_mode and not week.events and not week.covers_the_week:
            log.error(
                "the calendar feed only reaches %.1f days ahead, so it has not rolled "
                "over to the week being asked about; posting nothing",
                week.reach_days,
            )
            return 1

        payload = build_payload(week.events, window_start, DISPLAY_TZ, horizon_days, test_mode)
        description = payload["embeds"][0]["description"]

        if dry_run:
            print(description)
            print()
            print(payload["embeds"][0]["footer"]["text"])
            print(f"\n[{len(week.events)} events | {len(description)} / 4096 chars]")
            return 0

        if not WEBHOOK_URL:
            log.error(
                "DISCORD_WEBHOOK_URL is not set. In GitHub Actions add it under "
                "Settings -> Secrets and variables -> Actions."
            )
            return 2

        async with session.post(WEBHOOK_URL, json=payload) as resp:
            body = await resp.text()
            if resp.status >= 300:
                # Never log the URL itself - it is the credential.
                log.error("webhook rejected the post: HTTP %s %s", resp.status, body[:400])
                return 1
        log.info("posted %d calendar events", len(week.events))
        return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render the calendar and print it; send nothing, no webhook needed",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "post a delivery test: widens the window into the past so there is "
            "something to render on any day, and labels the post as a test"
        ),
    )
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run, args.test))


if __name__ == "__main__":
    sys.exit(main())
