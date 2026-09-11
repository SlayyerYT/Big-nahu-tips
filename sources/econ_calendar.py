"""Next week's market-moving macro events, from ForexFactory's free JSON feed.

Why this matters next to a long/short table: the crowd's positioning is most
fragile around scheduled data. A CPI print or an FOMC decision is where a 70%
long book gets liquidated, so knowing which days carry one is the difference
between reading the table and trading against it.

Named econ_calendar rather than calendar so it can never be confused with the
standard library module of that name.

Two properties of this feed drive the whole design:

*It rate-limits hard, and lies about the content type when it does.* A handful of
requests in a few minutes is enough to earn an HTTP 429 whose body is an HTML
error page. Parsed blindly that is a JSONDecodeError from a successful-looking
fetch, so status is checked before the body is ever decoded. Once a week is far
inside the limit, but GitHub runners share outbound IPs, so somebody else's job
can spend the budget first - hence the retries.

*Only "thisweek" exists.* There is no nextweek feed (it 404s), and the file is
generated per request with no Last-Modified, so there is no way to prove from
outside exactly when it rolls over to the new week. This module therefore never
trusts the label: it filters by timestamp against the window it was asked for. If
the feed has not rolled over yet, the result is zero future events - a state the
caller can detect and report honestly, rather than an empty week that would read
as "nothing happening".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import aiohttp

from .base import SourceError

log = logging.getLogger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# USD only, plus the feed's "All" bucket (global holidays and the like). The book
# this bot watches is quoted in dollars and takes its direction from US data;
# adding EUR or GBP high-impact rows roughly doubles the list for events that
# move crypto far less. To widen it, add the currency code here - nothing else
# needs changing.
WATCHED_COUNTRIES = frozenset({"USD", "All"})

# "High" is ForexFactory's own red-folder rating: CPI, NFP, FOMC, PPI and the
# like. "Holiday" is kept separately because a closed US session changes how the
# whole week trades even though it is not an impact rating.
WATCHED_IMPACTS = frozenset({"High"})
HOLIDAY_IMPACT = "Holiday"

HORIZON_DAYS = 7

# How far ahead the feed must reach before an empty result may be reported as a
# quiet week rather than as missing data. A feed that has rolled over on Sunday
# morning runs to the following Friday or Saturday, roughly six days out; one
# still serving the old week reaches under a day. Three days sits well clear of
# both, so neither a slightly short week nor a late-Sunday rollover trips it.
MIN_REACH_DAYS = 3.0

# Sized against the observed ban, not a guess: five requests inside a couple of
# minutes earned a 429 that took roughly four minutes of polling to clear. A
# retry budget shorter than that is decoration. This one spans about five
# minutes and still fits the workflow's 10-minute timeout alongside the pip
# install. If it exhausts, the run goes red and posts nothing, which is the right
# outcome - the alternative is a weekly post that silently skips itself.
RETRY_ATTEMPTS = 5
RETRY_WAIT_S = 75.0
REQUEST_TIMEOUT_S = 30


@dataclass(frozen=True)
class Event:
    """One scheduled release. Times are stored in UTC and converted at render time."""

    when: datetime
    country: str
    title: str
    impact: str
    forecast: str
    previous: str

    @property
    def is_holiday(self) -> bool:
        return self.impact == HOLIDAY_IMPACT


class WeekAhead(NamedTuple):
    """What the feed had to say about the window we asked about.

    `reach_days` is how far past `now` the feed's last event sits, and it is what
    separates the two ways of getting no events - which look identical in the
    list alone but mean opposite things:

      reach_days >= MIN_REACH_DAYS - the feed covers the week ahead and that week
                                     really is quiet: no high-impact US data.
      reach_days <  MIN_REACH_DAYS - the feed is still serving the tail of the
                                     current week, so we do not yet know what is
                                     coming and must not claim otherwise.

    "Any future row at all" is too weak a test, and fails in exactly the case
    this has to survive. The feed always keeps a few late rows - a Saturday
    print, an overnight Asian release - so a stale feed still looks alive by that
    measure. Run on a Friday it reported "quiet week ahead" while holding no data
    about the coming week whatsoever. Distance is the honest question: a feed
    that reaches six days out has the week; one that reaches half a day does not.
    """

    events: list[Event]
    reach_days: float

    @property
    def covers_the_week(self) -> bool:
        return self.reach_days >= MIN_REACH_DAYS


async def fetch_week_ahead(
    session: aiohttp.ClientSession,
    now: datetime,
    horizon_days: int = HORIZON_DAYS,
) -> WeekAhead:
    """High-impact events between now and now + horizon_days, soonest first."""
    raw = await _fetch_raw(session)
    return WeekAhead(events=_select(raw, now, horizon_days), reach_days=_reach_days(raw, now))


def _reach_days(raw: list, now: datetime) -> float:
    """How many days ahead the feed's furthest event sits. 0.0 if none are ahead."""
    furthest = 0.0
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        when = _parse_when(row.get("date"))
        if when is None:
            continue
        ahead = (when - now).total_seconds() / 86400.0
        furthest = max(furthest, ahead)
    return furthest


async def _fetch_raw(session: aiohttp.ClientSession) -> list:
    last_error = "unknown"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            async with session.get(
                FEED_URL, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
            ) as resp:
                if resp.status == 429:
                    # The body here is an HTML "Rate Limited" page. Decoding it as
                    # JSON would raise something that looks like a parse bug
                    # rather than what it is, so it never gets that far.
                    last_error = "rate limited (429)"
                    wait = _retry_after_seconds(resp.headers.get("Retry-After"))
                    if attempt < RETRY_ATTEMPTS:
                        log.warning(
                            "calendar feed rate limited, waiting %.0fs (attempt %d/%d)",
                            wait, attempt, RETRY_ATTEMPTS,
                        )
                        await asyncio.sleep(wait)
                        continue
                elif resp.status != 200:
                    last_error = f"HTTP {resp.status}"
                else:
                    # The feed serves JSON under a text content type, so the
                    # aiohttp content-type check has to be switched off.
                    return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            last_error = "timed out"
        except aiohttp.ClientError as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_WAIT_S)

    raise SourceError(f"calendar feed unavailable after {RETRY_ATTEMPTS} attempts: {last_error}")


def _select(raw: list, now: datetime, horizon_days: int) -> list[Event]:
    """Filter the feed to the events worth posting, soonest first."""
    horizon = now + timedelta(days=horizon_days)
    keep: list[Event] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        country = str(row.get("country", ""))
        impact = str(row.get("impact", ""))
        if country not in WATCHED_COUNTRIES:
            continue
        if impact not in WATCHED_IMPACTS and impact != HOLIDAY_IMPACT:
            continue
        when = _parse_when(row.get("date"))
        if when is None:
            continue
        # The window is what decides inclusion, never the feed's "thisweek" label.
        if not (now <= when <= horizon):
            continue
        keep.append(
            Event(
                when=when,
                country=country,
                title=str(row.get("title", "")).strip() or "(untitled)",
                impact=impact,
                # Absent values come through as empty strings, not nulls.
                forecast=str(row.get("forecast", "") or "").strip(),
                previous=str(row.get("previous", "") or "").strip(),
            )
        )
    keep.sort(key=lambda e: (e.when, e.title))
    return keep


def _parse_when(value) -> datetime | None:
    """Feed timestamps are ISO 8601 with a US Eastern offset; normalise to UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # an offset-less timestamp is unanchored; better dropped than guessed
    return parsed.astimezone(timezone.utc)


def _retry_after_seconds(header: str | None, default: float = RETRY_WAIT_S) -> float:
    try:
        return max(1.0, float(header))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
