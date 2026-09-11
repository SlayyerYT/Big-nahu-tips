"""Post one table to a Discord webhook, then exit.

This is the serverless path: instead of a bot process holding a gateway
connection open forever, a scheduler (GitHub Actions) runs this every 10 minutes
and it posts through a webhook. No host, no token, no uptime to babysit.

It works because nothing is stored between runs. Every column - including the
change since the last post - is derived from the market history fetched in this
run, so a cold start produces exactly the same table a long-running process would.

    python post_once.py              # fetch and post
    python post_once.py --dry-run    # render and print, send nothing
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

import aiohttp

import formatter as fmt
from bot import INTERVAL_MIN, LAYOUT, USER_AGENT, CycleResult, Pipeline
from sources.base import SourceError

log = logging.getLogger("altcoin-ls-post")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def build_payload(cycle: CycleResult) -> dict:
    layout = fmt.get_layout(LAYOUT)
    footer = fmt.footer_text(
        cycle.source_label, cycle.venues, INTERVAL_MIN, cycle.degraded, cycle.size_label
    )
    return {
        "embeds": [
            {
                "title": f"\N{BAR CHART} TOP {len(cycle.rows)} ALTCOIN LONG/SHORT",
                "description": fmt.build_description(cycle.rows, layout),
                "color": fmt.embed_colour(cycle.rows),
                "footer": {"text": footer},
            }
        ]
    }


async def run(dry_run: bool) -> int:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            cycle = await Pipeline(session).run()
        except SourceError as exc:
            log.error("could not build a table: %s", exc)
            return 1

        rows = cycle.rows
        payload = build_payload(cycle)
        description = payload["embeds"][0]["description"]

        if dry_run:
            print(description)
            print(payload["embeds"][0]["footer"]["text"])
            print(f"\n[{len(rows)} rows | {len(description)} / 4096 chars | would POST to webhook]")
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
        log.info("posted %d rows from %s", len(rows), cycle.source_label)
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
        help="render the table and print it; send nothing, no webhook needed",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
