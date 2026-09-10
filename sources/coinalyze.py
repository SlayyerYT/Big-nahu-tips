"""Coinalyze multi-venue long/short source (requires a free API key).

Coinalyze symbols are per-market, not aggregated (e.g. SOLUSDT_PERP.A is one
exchange). To reproduce the cross-exchange figure the website shows, we resolve
each coin to its perp on several venues and take an unweighted mean of the long
share. That is the closest approximation available: per-venue account counts,
which a weighted mean would need, are not exposed by any public API.

Quota: 40 API calls/minute/key, and *each symbol* in a request counts as one
call. 20 coins x 3 venues = 60 symbols per cycle = 6 calls/min at a 10-minute
cadence. Comfortably inside the limit.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import aiohttp

from .base import CoinLS, Point, SourceError, average_points, nearest, prev_point

log = logging.getLogger(__name__)

BASE = "https://api.coinalyze.net/v1"
INTERVAL = "5min"
INTERVAL_S = 300
MAX_SYMBOLS_PER_REQUEST = 20
LOOKBACK_S = 25 * 3600

# Venue preference. Deep, liquid books first - a thin venue's account ratio is noise.
PREFERRED_EXCHANGES = ["binance", "bybit", "okx"]
MAX_VENUES_PER_COIN = 3

MARKETS_CACHE = Path(__file__).resolve().parent.parent / "cache" / "coinalyze_markets.json"
MARKETS_TTL_S = 24 * 3600

NAME = "Coinalyze"


class CoinalyzeSource:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        if not api_key:
            raise SourceError("no Coinalyze API key configured")
        self.session = session
        self.api_key = api_key
        self._symbol_map: dict[str, list[str]] | None = None
        self._venue_names: list[str] = []

    @property
    def label(self) -> str:
        return NAME

    @property
    def venues(self) -> list[str]:
        return self._venue_names or [e.capitalize() for e in PREFERRED_EXCHANGES]

    async def _get(self, path: str, params: dict | None = None):
        params = dict(params or {})
        params["api_key"] = self.api_key
        async with self.session.get(
            f"{BASE}{path}", params=params, timeout=aiohttp.ClientTimeout(total=45)
        ) as resp:
            if resp.status == 401:
                raise SourceError("Coinalyze rejected the API key (401)")
            if resp.status == 429:
                retry = resp.headers.get("Retry-After", "?")
                raise SourceError(f"Coinalyze rate limited (429), retry after {retry}s")
            if resp.status != 200:
                raise SourceError(f"Coinalyze {path} -> HTTP {resp.status}")
            return await resp.json()

    async def _exchange_codes(self) -> dict[str, str]:
        """Map exchange code -> lowercase exchange name."""
        rows = await self._get("/exchanges")
        return {str(r["code"]): str(r.get("name", "")).lower() for r in rows}

    async def _build_symbol_map(self) -> dict[str, list[str]]:
        """base asset -> list of Coinalyze perp symbols, best venues first."""
        if self._symbol_map is not None:
            return self._symbol_map

        cached = _read_markets_cache()
        if cached is not None:
            self._symbol_map, self._venue_names = cached
            return self._symbol_map

        codes = await self._exchange_codes()
        markets = await self._get("/future-markets")

        # exchange code -> preference rank, for the venues we care about
        rank: dict[str, int] = {}
        names: dict[str, str] = {}
        for code, name in codes.items():
            for i, pref in enumerate(PREFERRED_EXCHANGES):
                if pref in name:
                    rank[code] = i
                    names[code] = pref
                    break

        by_coin: dict[str, list[tuple[int, str]]] = {}
        used_venues: set[str] = set()
        for m in markets:
            if not m.get("is_perpetual"):
                continue
            # A market without this flag returns an empty history, not an error.
            if not m.get("has_long_short_ratio_data"):
                continue
            if m.get("quote_asset") != "USDT" or m.get("margined") != "STABLE":
                continue
            code = str(m.get("exchange", ""))
            if code not in rank:
                continue
            base = str(m.get("base_asset", "")).upper()
            if not base:
                continue
            by_coin.setdefault(base, []).append((rank[code], str(m["symbol"])))
            used_venues.add(names[code])

        symbol_map = {
            coin: [s for _, s in sorted(entries)][:MAX_VENUES_PER_COIN]
            for coin, entries in by_coin.items()
        }
        venue_names = [p.capitalize() for p in PREFERRED_EXCHANGES if p in used_venues]
        _write_markets_cache(symbol_map, venue_names)
        self._symbol_map = symbol_map
        self._venue_names = venue_names
        log.info("coinalyze symbol map: %d coins across %s", len(symbol_map), venue_names)
        return symbol_map

    async def fetch(
        self, candidates: list[str], top_n: int, now_ts: int, prev_offset_s: int
    ) -> list[CoinLS]:
        symbol_map = await self._build_symbol_map()

        chosen = [c for c in candidates if symbol_map.get(c)][:top_n]
        if not chosen:
            raise SourceError("no candidate coin resolved to a Coinalyze market")

        symbols: list[str] = []
        for coin in chosen:
            symbols.extend(symbol_map[coin])

        series_by_symbol: dict[str, list[Point]] = {}
        for batch in _chunks(symbols, MAX_SYMBOLS_PER_REQUEST):
            rows = await self._get(
                "/long-short-ratio-history",
                {
                    "symbols": ",".join(batch),
                    "interval": INTERVAL,
                    "from": str(now_ts - LOOKBACK_S),
                    "to": str(now_ts),
                },
            )
            for entry in rows:
                hist = entry.get("history") or []
                series_by_symbol[str(entry["symbol"])] = sorted(
                    (
                        Point(t=int(h["t"]), long_pct=float(h["l"]), short_pct=float(h["s"]))
                        for h in hist
                    ),
                    key=lambda p: p.t,
                )

        out: list[CoinLS] = []
        for coin in chosen:
            row = self._aggregate(coin, symbol_map[coin], series_by_symbol, now_ts, prev_offset_s)
            if row is not None:
                out.append(row)
        if not out:
            raise SourceError("Coinalyze returned no usable rows")
        return out

    def _aggregate(
        self,
        coin: str,
        symbols: list[str],
        series_by_symbol: dict[str, list[Point]],
        now_ts: int,
        prev_offset_s: int,
    ) -> CoinLS | None:
        live = {s: series_by_symbol.get(s) or [] for s in symbols}
        live = {s: v for s, v in live.items() if v}
        if not live:
            return None

        latest_t = max(v[-1].t for v in live.values())
        if now_ts - latest_t > INTERVAL_S * 3:
            return None

        tol = INTERVAL_S * 2

        def at(offset: int, tolerance: int) -> Point | None:
            return average_points(
                nearest(v, latest_t - offset, tolerance) for v in live.values()
            )

        now_pt = average_points(nearest(v, latest_t, tol) for v in live.values())
        if now_pt is None:
            return None

        return CoinLS(
            coin=coin,
            venues=[f"{len(live)} venues"],
            now=now_pt,
            prev=average_points(
                prev_point(v, latest_t, prev_offset_s, INTERVAL_S) for v in live.values()
            ),
            h1=at(3600, tol),
            h24=at(86400, max(tol, 900)),
        )


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _read_markets_cache() -> tuple[dict[str, list[str]], list[str]] | None:
    try:
        blob = json.loads(MARKETS_CACHE.read_text(encoding="utf-8"))
        if time.time() - float(blob["fetched_at"]) > MARKETS_TTL_S:
            return None
        return blob["symbol_map"], blob.get("venues", [])
    except Exception:
        return None


def _write_markets_cache(symbol_map: dict[str, list[str]], venues: list[str]) -> None:
    """Best-effort - see the note in universe._write_cache.

    On a read-only host an unguarded write here would abort _build_symbol_map and
    take the whole primary source down with it, every cycle.
    """
    try:
        MARKETS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MARKETS_CACHE.write_text(
            json.dumps(
                {"fetched_at": time.time(), "symbol_map": symbol_map, "venues": venues}, indent=1
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("markets cache not written (%s); continuing without it", exc)
