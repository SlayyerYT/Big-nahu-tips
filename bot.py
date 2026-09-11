"""Discord bot: posts a top-20 altcoin long/short table on a fixed interval.

Run the pipeline without Discord or any secrets:
    python bot.py --dry-run
Force the backup source:
    python bot.py --dry-run --source binance
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import NamedTuple

import aiohttp
from dotenv import load_dotenv

import formatter as fmt
import universe
from sources.base import CoinLS, SourceError
from sources.binance import BinanceSource
from sources.coinalyze import CoinalyzeSource
from sources.okx import OKXSizeSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("altcoin-ls")

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()
COINALYZE_KEY = os.getenv("COINALYZE_API_KEY", "").strip()
INTERVAL_MIN = int(os.getenv("POST_INTERVAL_MINUTES", "10"))
TOP_N = int(os.getenv("TOP_N", "20"))
# "mobile" fits a phone screen without sideways scrolling; "wide" adds the
# explicit SHORT column for desktop-only servers.
LAYOUT = os.getenv("LAYOUT", "mobile").strip().lower()

USER_AGENT = "altcoin-ls-bot/1.0"


class CycleResult(NamedTuple):
    """One cycle's output. Named because the SIZE column made this a 5-tuple."""

    rows: list[CoinLS]
    source_label: str
    venues: list[str]
    degraded: bool
    # None whenever the SIZE column is empty, so the footer never credits a
    # source that did not contribute a single number to this table.
    size_label: str | None


class Pipeline:
    """Fetches one cycle's worth of rows, preferring Coinalyze, falling back to Binance."""

    def __init__(self, session: aiohttp.ClientSession, force_source: str | None = None):
        self.session = session
        self.force_source = force_source
        self._okx = OKXSizeSource(session)
        self._coinalyze: CoinalyzeSource | None = None
        if COINALYZE_KEY and force_source != "binance":
            try:
                self._coinalyze = CoinalyzeSource(session, COINALYZE_KEY)
            except SourceError as exc:
                log.warning("Coinalyze unavailable: %s", exc)
        elif force_source == "coinalyze":
            raise SourceError("--source coinalyze requires COINALYZE_API_KEY to be set")
        self._binance = BinanceSource(session)
        self._primary_failures = 0

    async def _attach_sizes(self, rows: list[CoinLS], now_ts: int) -> str | None:
        """Add the size-weighted column in place. Returns the label to credit, if any.

        Deliberately soft: OKX is the only venue that can supply this column and
        there is no fallback, so any failure here must cost the SIZE column and
        nothing else. Letting it raise would trade a complete table for no table.
        """
        try:
            sizes = await self._okx.fetch_sizes([r.coin for r in rows], now_ts)
        except Exception as exc:
            log.warning("size column unavailable this cycle (%s); posting without it", exc)
            return None
        for row in rows:
            row.size = sizes.get(row.coin)
        return self._okx.label if sizes else None

    async def run(self) -> CycleResult:
        candidates = await universe.get_candidates(self.session)
        now_ts = int(time.time())
        prev_offset_s = INTERVAL_MIN * 60

        if self._coinalyze is not None and self.force_source != "binance":
            try:
                rows = await self._coinalyze.fetch(candidates, TOP_N, now_ts, prev_offset_s)
                self._primary_failures = 0
                size_label = await self._attach_sizes(rows, now_ts)
                return CycleResult(
                    rows, self._coinalyze.label, self._coinalyze.venues, False, size_label
                )
            except Exception as exc:
                self._primary_failures += 1
                if self.force_source == "coinalyze":
                    # Strict mode: never mask a broken key or symbol map behind a
                    # fallback that looks like a working bot.
                    raise
                # A primary that fails forever otherwise looks like a healthy bot
                # posting from the backup, so escalate once it stops being a blip.
                level = logging.ERROR if self._primary_failures >= 3 else logging.WARNING
                log.log(
                    level,
                    "primary source (Coinalyze) failed %d cycle(s) in a row: %s "
                    "- posting from Binance instead",
                    self._primary_failures,
                    exc,
                )

        rows = await self._binance.fetch(candidates, TOP_N, now_ts, prev_offset_s)
        # Binance is one venue where the primary is a multi-venue mean, so the
        # levels sit slightly differently. Flag it rather than let the shift read
        # as a market move.
        degraded = self._coinalyze is not None
        size_label = await self._attach_sizes(rows, now_ts)
        return CycleResult(rows, self._binance.label, self._binance.venues, degraded, size_label)


async def dry_run(force_source: str | None) -> int:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            pipeline = Pipeline(session, force_source)
            cycle = await pipeline.run()
        except SourceError as exc:
            log.error("%s", exc)
            return 1

    rows = cycle.rows
    layout = fmt.get_layout(LAYOUT)
    description = fmt.build_description(rows, layout)
    footer = fmt.footer_text(
        cycle.source_label, cycle.venues, INTERVAL_MIN, cycle.degraded, cycle.size_label
    )

    print()
    print(f"TOP {len(rows)} ALTCOIN LONG/SHORT")
    print(description)
    print(footer)
    print()
    print(
        f"[layout: {LAYOUT} | table width: {fmt.table_width(layout)} chars "
        f"| embed description: {len(description)} / 4096 chars]"
    )
    if len(description) > 4096:
        print("!! TOO LONG FOR ONE EMBED")
        return 1
    return 0


def build_embed(cycle: CycleResult):
    import discord

    embed = discord.Embed(
        title=f"\N{BAR CHART} TOP {len(cycle.rows)} ALTCOIN LONG/SHORT",
        description=fmt.build_description(cycle.rows, fmt.get_layout(LAYOUT)),
        colour=fmt.embed_colour(cycle.rows),
    )
    embed.set_footer(
        text=fmt.footer_text(
            cycle.source_label, cycle.venues, INTERVAL_MIN, cycle.degraded, cycle.size_label
        )
    )
    return embed


def run_bot() -> int:
    import discord
    from discord.ext import tasks

    if not DISCORD_TOKEN or not CHANNEL_ID:
        log.error("DISCORD_TOKEN and DISCORD_CHANNEL_ID must be set (copy .env.example to .env)")
        return 2
    try:
        channel_id = int(CHANNEL_ID)
    except ValueError:
        log.error("DISCORD_CHANNEL_ID must be the numeric channel ID, got %r", CHANNEL_ID)
        return 2

    # Posting needs no privileged intents; Message Content is only for reading.
    client = discord.Client(intents=discord.Intents.default())
    state: dict[str, object] = {}

    @tasks.loop(minutes=INTERVAL_MIN)
    async def post_cycle():
        pipeline: Pipeline = state["pipeline"]  # type: ignore[assignment]
        try:
            cycle = await pipeline.run()
        except Exception:
            log.exception("cycle failed; will retry next interval")
            return

        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        try:
            await channel.send(embed=build_embed(cycle))
            log.info("posted %d rows from %s", len(cycle.rows), cycle.source_label)
        except discord.Forbidden:
            log.error(
                "missing permissions in channel %s - the bot needs View Channel, "
                "Send Messages and Embed Links",
                channel_id,
            )
        except Exception:
            log.exception("failed to send message")

    @post_cycle.before_loop
    async def before():
        await client.wait_until_ready()

    @client.event
    async def on_ready():
        if "session" not in state:
            state["session"] = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
            state["pipeline"] = Pipeline(state["session"])  # type: ignore[arg-type]
        log.info("logged in as %s; posting every %d minutes", client.user, INTERVAL_MIN)
        if not post_cycle.is_running():
            post_cycle.start()

    client.run(DISCORD_TOKEN, log_handler=None)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print one table to stdout and exit; no Discord connection, no token needed",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "coinalyze", "binance"],
        default="auto",
        help=(
            "force a data source (default: auto = Coinalyze, falling back to Binance). "
            "'coinalyze' is strict: it fails loudly instead of falling back, which is "
            "how you verify a new API key actually works"
        ),
    )
    args = parser.parse_args()

    force = None if args.source == "auto" else args.source
    if args.dry_run:
        return asyncio.run(dry_run(force))
    return run_bot()


if __name__ == "__main__":
    sys.exit(main())
