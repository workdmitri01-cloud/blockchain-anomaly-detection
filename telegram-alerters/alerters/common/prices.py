"""Token prices and metadata from free, key-less APIs.

Primary:  DefiLlama  https://coins.llama.fi/prices/current/{chain}:{token},...
          (returns price, decimals and symbol in one batched call)
Fallback: CoinGecko  /simple/token_price/{platform} (public tier, optional demo key)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

DEFILLAMA_URL = "https://coins.llama.fi/prices/current/{coins}"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/token_price/{platform}"


@dataclass
class TokenInfo:
    symbol: str | None = None
    decimals: int | None = None
    price: float | None = None
    fetched_at: float = 0.0


class PriceOracle:
    def __init__(self, ttl: int = 300, session: requests.Session | None = None, timeout: float = 15):
        self.ttl = ttl
        self.timeout = timeout
        self.session = session or requests.Session()
        self._cache: dict[tuple[str, str], TokenInfo] = {}
        self._fixed: dict[tuple[str, str], float] = {}
        self._coingecko_key = os.environ.get("COINGECKO_API_KEY", "")

    def set_fixed_price(self, chain: str, token: str, price: float) -> None:
        self._fixed[(chain, token.lower())] = float(price)

    def set_metadata(self, chain: str, token: str, symbol: str | None, decimals: int | None) -> None:
        info = self._cache.setdefault((chain, token.lower()), TokenInfo())
        if symbol:
            info.symbol = symbol
        if decimals is not None:
            info.decimals = decimals

    def get(self, chain: str, token: str) -> TokenInfo:
        return self._cache.setdefault((chain, token.lower()), TokenInfo())

    def refresh(self, wanted: dict[str, tuple[str, str | None]]) -> None:
        """Refresh stale prices.

        ``wanted`` is ``{"<chain>:<token>": (defillama_slug, coingecko_platform)}``.
        """
        now = time.time()
        stale = []
        for key, slugs in wanted.items():
            chain, token = key.split(":", 1)
            if (chain, token) in self._fixed:
                self.get(chain, token).price = self._fixed[(chain, token)]
                continue
            info = self.get(chain, token)
            if info.price is None or now - info.fetched_at > self.ttl:
                stale.append((chain, token, slugs))
        if not stale:
            return
        self._refresh_defillama(stale, now)
        missing = [s for s in stale if self.get(s[0], s[1]).fetched_at != now]
        if missing:
            self._refresh_coingecko(missing, now)

    def _refresh_defillama(self, stale, now: float) -> None:
        # Keep URLs reasonably short.
        for i in range(0, len(stale), 50):
            batch = stale[i : i + 50]
            coins = ",".join(f"{slugs[0]}:{token}" for _, token, slugs in batch)
            try:
                resp = self.session.get(DEFILLAMA_URL.format(coins=coins), timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json().get("coins", {})
            except (requests.RequestException, ValueError) as exc:
                log.warning("DefiLlama price request failed: %s", exc)
                continue
            for chain, token, slugs in batch:
                item = data.get(f"{slugs[0]}:{token}")
                if not item:
                    continue
                info = self.get(chain, token)
                info.price = float(item.get("price") or 0) or None
                info.symbol = info.symbol or item.get("symbol")
                if info.decimals is None and item.get("decimals") is not None:
                    info.decimals = int(item["decimals"])
                info.fetched_at = now

    def _refresh_coingecko(self, stale, now: float) -> None:
        by_platform: dict[str, list[tuple[str, str]]] = {}
        for chain, token, slugs in stale:
            if slugs[1]:
                by_platform.setdefault(slugs[1], []).append((chain, token))
        headers = {"x-cg-demo-api-key": self._coingecko_key} if self._coingecko_key else {}
        for platform, items in by_platform.items():
            try:
                resp = self.session.get(
                    COINGECKO_URL.format(platform=platform),
                    params={"contract_addresses": ",".join(t for _, t in items), "vs_currencies": "usd"},
                    headers=headers,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("CoinGecko price request failed: %s", exc)
                continue
            for chain, token in items:
                usd = (data.get(token) or {}).get("usd")
                if usd:
                    info = self.get(chain, token)
                    info.price = float(usd)
                    info.fetched_at = now
