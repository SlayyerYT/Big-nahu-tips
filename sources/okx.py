"""OKX top-trader position ratio - the size-weighted half of the table.

Every other column in this bot counts *accounts*: what share of traders are long.
This one weights by *position size*, and the two regularly disagree - SOL on
2026-09-11 read 2.22 long by headcount and 0.88 by size, i.e. the crowd was long
while the big positions were net short. That disagreement is the entire point of
the column.

Two things about this metric are easy to get wrong:

*Population.* This is not "the same ratio, weighted". OKX computes it over its
own top-trader cohort, not over everybody, so the column differs from the LONG
and L/S columns in both weighting *and* sample. The header, legend and footer all
have to say so, or a divergence reads as pure size-weighting.

*Why a subset at all.* On a perpetual, the total notional held long always equals
the total held short - every long is somebody's short - so the size ratio across
*all* traders is identically 1.00 and carries no information. The signal exists
only inside a subset that can be net long or short. Do not "fix" this by
computing it over the whole market.

OKX is also the only venue that can supply it here: Coinalyze publishes account
shares and open interest but has no position-ratio endpoint (it 404s), and both
Binance and Bybit refuse GitHub's US runners. There is no second source. If OKX
starts blocking too, the column goes dark rather than degrading - which is why
every failure in here is soft and the rest of the table posts without it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp

from .base import Point, SourceError

log = logging.getLogger(__name__)

BASE = "https://www.okx.com"
POSITION_RATIO_PATH = "/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader"
INSTRUMENTS_PATH = "/api/v5/public/instruments"

PERIOD = "5m"
INTERVAL_S = 300

# Rubik endpoints are rate limited per IP. Twenty coins is twenty calls; a small
# semaphore keeps the burst civil without making the cycle noticeably slower.
MAX_CONCURRENCY = 5
REQUEST_TIMEOUT_S = 20

INSTRUMENTS_CACHE = Path(__file__).resolve().parent.parent / "cache" / "okx_instruments.json"
INSTRUMENTS_TTL_S = 24 * 3600

NAME = "OKX top traders"


class OKXSizeSource:
    """Supplies one extra number per coin. Never a source of table rows by itself."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._instruments: dict[str, str] | None = None

    @property
    def label(self) -> str:
        return NAME

    async def _get(self, path: str, params: dict | None = None):
        async with self.session.get(
            f"{BASE}{path}", params=params, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        ) as resp:
            if resp.status != 200:
                raise SourceError(f"OKX {path} -> HTTP {resp.status}")
            payload = await resp.json()
        # OKX signals application errors in the body with HTTP 200, so the status
        # code alone is not enough to call a request successful.
        if str(payload.get("code")) != "0":
            raise SourceError(f"OKX {path} -> code {payload.get('code')}: {payload.get('msg')}")
        return payload.get("data") or []

    async def _instrument_map(self) -> dict[str, str]:
        """base asset -> OKX instId, e.g. SOL -> SOL-USDT-SWAP."""
        if self._instruments is not None:
            return self._instruments

        cached = _read_instruments_cache()
        if cached is not None:
            self._instruments = cached
            return cached

        rows = await self._get(INSTRUMENTS_PATH, {"instType": "SWAP"})
        mapping: dict[str, str] = {}
        for row in rows:
            if row.get("settleCcy") != "USDT" or row.get("ctType") != "linear":
                continue
            if row.get("state") != "live":
                continue
            # Swaps leave baseCcy empty; the base asset is in ctValCcy. Parsing it
            # out of instId instead would break on any hyphenated ticker.
            base = str(row.get("ctValCcy", "")).upper()
            inst_id = str(row.get("instId", ""))
            if base and inst_id:
                mapping.setdefault(base, inst_id)

        if not mapping:
            raise SourceError("OKX returned no usable USDT swaps")
        _write_instruments_cache(mapping)
        self._instruments = mapping
        log.info("okx instrument map: %d coins", len(mapping))
        return mapping

    async def _one(self, coin: str, inst_id: str, now_ts: int) -> tuple[str, Point | None]:
        try:
            rows = await self._get(POSITION_RATIO_PATH, {"instId": inst_id, "period": PERIOD})
        except Exception as exc:
            log.debug("okx %s failed: %s", coin, exc)
            return coin, None
        return coin, _latest_point(rows, now_ts)

    async def fetch_sizes(self, coins: list[str], now_ts: int) -> dict[str, Point]:
        """Size-weighted points by coin. Coins OKX cannot answer for are omitted.

        A partial result is the normal case, not a failure: a coin may have no OKX
        swap, or one too new to have a top-trader history. The caller prints "-"
        for anything missing rather than dropping the row, so a thin OKX response
        can never shrink the table.
        """
        instruments = await self._instrument_map()
        wanted = [(c, instruments[c]) for c in coins if c in instruments]
        if not wanted:
            return {}

        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def guarded(coin: str, inst_id: str):
            async with sem:
                return await self._one(coin, inst_id, now_ts)

        results = await asyncio.gather(*(guarded(c, i) for c, i in wanted))
        sizes = {coin: point for coin, point in results if point is not None}
        log.info("okx size ratios: %d of %d requested coins", len(sizes), len(coins))
        return sizes


def _latest_point(rows: list, now_ts: int) -> Point | None:
    """Newest usable observation from an OKX rubik response.

    Rows arrive newest-first as [timestamp_ms, ratio] pairs. The ratio is
    long/short by position size; the rest of this codebase works in shares, so it
    is converted here - long% = 100*r/(1+r) - which keeps Point, nearest() and
    average_points() usable without special-casing this source.
    """
    best: Point | None = None
    for row in rows:
        try:
            t = int(row[0]) // 1000
            ratio = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if ratio <= 0:
            # A zero or negative ratio is not a real reading; converting it would
            # print a confident 0.00 instead of an honest blank.
            continue
        if now_ts - t > INTERVAL_S * 3:
            continue  # stale: OKX stopped publishing for this instrument
        if best is None or t > best.t:
            long_pct = 100.0 * ratio / (1.0 + ratio)
            best = Point(t=t, long_pct=long_pct, short_pct=100.0 - long_pct)
    return best


def _read_instruments_cache() -> dict[str, str] | None:
    try:
        blob = json.loads(INSTRUMENTS_CACHE.read_text(encoding="utf-8"))
        if time.time() - float(blob["fetched_at"]) > INSTRUMENTS_TTL_S:
            return None
        return dict(blob["instruments"])
    except Exception:
        return None


def _write_instruments_cache(instruments: dict[str, str]) -> None:
    """Best-effort - see the note in universe._write_cache."""
    try:
        INSTRUMENTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        INSTRUMENTS_CACHE.write_text(
            json.dumps({"fetched_at": time.time(), "instruments": instruments}, indent=1),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("okx instruments cache not written (%s); continuing without it", exc)
