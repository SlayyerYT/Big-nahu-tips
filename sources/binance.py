"""Binance USDT-perp long/short source (no API key required).

Uses globalLongShortAccountRatio - the share of *accounts* net long vs short,
across all Binance futures users. That is the metric behind phrasings like
"2.4 long accounts per short account". Binance also exposes topLongShortAccountRatio
and topLongShortPositionRatio, which return different numbers; do not swap them
in casually or the bot silently starts reporting something else.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from .base import CoinLS, Point, SourceError, nearest, prev_point

log = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"
RATIO_URL = f"{FAPI}/futures/data/globalLongShortAccountRatio"
EXCHANGE_INFO_URL = f"{FAPI}/fapi/v1/exchangeInfo"

PERIOD = "5m"
INTERVAL_S = 300
# 288 x 5m = 23.9h, delivered in a single call (limit max is 500).
LIMIT = 288

NAME = "Binance"


class BinanceSource:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._perps: set[str] | None = None

    @property
    def label(self) -> str:
        return NAME

    @property
    def venues(self) -> list[str]:
        return [NAME]

    async def _load_perps(self) -> set[str]:
        if self._perps is not None:
            return self._perps
        async with self.session.get(EXCHANGE_INFO_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            info = await resp.json()
        self._perps = {
            s["symbol"]
            for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
        }
        return self._perps

    async def _fetch_series(self, market: str) -> list[Point]:
        params = {"symbol": market, "period": PERIOD, "limit": str(LIMIT)}
        async with self.session.get(RATIO_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise SourceError(f"{market}: HTTP {resp.status}")
            rows = await resp.json()
        return [
            Point(
                t=int(r["timestamp"]) // 1000,
                long_pct=float(r["longAccount"]) * 100.0,
                short_pct=float(r["shortAccount"]) * 100.0,
            )
            for r in rows
        ]

    async def fetch(self, candidates: list[str], top_n: int, now_ts: int, prev_offset_s: int) -> list[CoinLS]:
        perps = await self._load_perps()
        wanted = [c for c in candidates if f"{c}USDT" in perps][: top_n * 2]

        sem = asyncio.Semaphore(6)

        async def one(coin: str) -> tuple[str, list[Point]]:
            async with sem:
                try:
                    return coin, await self._fetch_series(f"{coin}USDT")
                except Exception as exc:
                    log.debug("binance %s failed: %s", coin, exc)
                    return coin, []

        results = dict(await asyncio.gather(*(one(c) for c in wanted)))

        rows: list[CoinLS] = []
        for coin in wanted:
            series = results.get(coin) or []
            row = build_row(coin, series, [NAME], now_ts, prev_offset_s, INTERVAL_S)
            if row is not None:
                rows.append(row)
            if len(rows) >= top_n:
                break
        if not rows:
            raise SourceError("Binance returned no usable rows")
        return rows


def build_row(
    coin: str,
    series: list[Point],
    venues: list[str],
    now_ts: int,
    prev_offset_s: int,
    interval_s: int,
) -> CoinLS | None:
    """Turn one venue's series into a table row, or None if it has no current data.

    A symbol with no long/short data comes back as an empty list rather than an
    error, so an empty series is 'missing', never a crash.
    """
    if not series:
        return None
    series = sorted(series, key=lambda p: p.t)
    latest = series[-1]
    # Stale beyond three intervals means the venue stopped publishing.
    if now_ts - latest.t > interval_s * 3:
        return None
    tol = interval_s * 2
    return CoinLS(
        coin=coin,
        venues=list(venues),
        now=latest,
        prev=prev_point(series, latest.t, prev_offset_s, interval_s),
        h1=nearest(series, latest.t - 3600, tol),
        h24=nearest(series, latest.t - 86400, max(tol, 900)),
    )
