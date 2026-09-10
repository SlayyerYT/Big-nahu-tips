"""Which coins go in the table: top-N altcoins by market cap.

BTC is excluded (not an altcoin), as are stablecoins and wrapped/staked
derivatives - their long/short data is either absent or meaningless, and they
would otherwise eat slots in a 20-row table.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
CACHE_PATH = Path(__file__).with_name("cache") / "universe.json"
CACHE_TTL_S = 24 * 3600

# Excluded by CoinGecko id. Stablecoins first, then wrapped/staked derivatives
# that track another asset and have no perp of their own.
EXCLUDED_IDS = {
    "bitcoin",
    # stablecoins
    "tether", "usd-coin", "dai", "first-digital-usd", "ethena-usde", "usds",
    "binance-usd", "trueusd", "paypal-usd", "frax", "usdd", "gho", "pyusd",
    "sky-dollar", "blackrock-usd-institutional-digital-liquidity-fund",
    "ripple-usd", "global-dollar", "usdt0", "falcon-finance", "susds",
    # wrapped / staked / liquid-restaking derivatives
    "wrapped-bitcoin", "wrapped-steth", "weth", "staked-ether", "wrapped-eeth",
    "coinbase-wrapped-btc", "binance-peg-weth", "rocket-pool-eth", "mantle-staked-ether",
    "jito-staked-sol", "lombard-staked-btc", "solv-btc", "bitcoin-avalanche-bridged-btc-b",
    "wbnb", "wrapped-avax", "binance-staked-sol", "clbtc", "wrapped-beacon-eth",
    "kelp-dao-restaked-eth", "renzo-restaked-eth", "ether-fi-staked-eth",
}

# Belt-and-braces: anything whose ticker looks like a dollar peg.
STABLE_SYMBOL_RE = re.compile(r"^(W?[A-Z]{0,3}USD[A-Z0-9]{0,3}|[A-Z]{0,4}DAI|EUR[A-Z]?)$")


def _looks_like_stable(symbol: str, coin_id: str) -> bool:
    return coin_id in EXCLUDED_IDS or bool(STABLE_SYMBOL_RE.match(symbol.upper()))


def _read_cache() -> list[str] | None:
    try:
        blob = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return list(blob["candidates"])
    except Exception:
        return None


def _cache_is_fresh() -> bool:
    try:
        blob = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return (time.time() - float(blob["fetched_at"])) < CACHE_TTL_S
    except Exception:
        return False


def _write_cache(candidates: list[str]) -> None:
    """Best-effort. Caching is an optimisation, never a reason to lose good data.

    Some hosts give the process a read-only or ephemeral filesystem. A failed
    write there must not propagate: the caller would treat it as a failed fetch
    and fall back to the stale seed list despite having just got fresh data.
    """
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"fetched_at": time.time(), "candidates": candidates}, indent=1),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("universe cache not written (%s); continuing without it", exc)


# Used only if CoinGecko is unreachable and no cache exists yet.
SEED_CANDIDATES = [
    "ETH", "BNB", "XRP", "SOL", "TRX", "DOGE", "ADA", "LINK", "SUI", "AVAX",
    "XLM", "HBAR", "BCH", "LTC", "TON", "SHIB", "DOT", "UNI", "AAVE", "NEAR",
    "APT", "ETC", "POL", "ICP", "ARB", "OP", "FIL", "ATOM", "INJ", "TAO",
]


async def get_candidates(session: aiohttp.ClientSession, force: bool = False) -> list[str]:
    """Ranked altcoin tickers, longest-list-first, for a source to resolve against.

    Returns more than TOP_N: some coins have no perpetual futures market, so the
    caller walks down this list until it has filled the table.
    """
    if not force and _cache_is_fresh():
        cached = _read_cache()
        if cached:
            return cached

    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": "100",
        "page": "1",
    }
    try:
        async with session.get(COINGECKO_MARKETS, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            rows = await resp.json()
        candidates: list[str] = []
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            coin_id = str(row.get("id", ""))
            if not symbol or _looks_like_stable(symbol, coin_id):
                continue
            if symbol not in candidates:
                candidates.append(symbol)
        if len(candidates) < 25:
            raise ValueError(f"only {len(candidates)} candidates survived filtering")
        _write_cache(candidates)
        log.info("universe refreshed from CoinGecko: %d candidates", len(candidates))
        return candidates
    except Exception as exc:
        # A 429 at refresh time must never empty the table.
        log.warning("CoinGecko refresh failed (%s); falling back to cache/seed", exc)
        return _read_cache() or list(SEED_CANDIDATES)
