"""Hyperliquid HyperCore (L1 spot tokens) via the official, key-less info API.

POST https://api.hyperliquid.xyz/info
  userNonFundingLedgerUpdates - spot transfers / sends / USDC transfers of one account
  spotMeta, tokenDetails       - token ids, deployer, genesis balances (team discovery)

Tokens are identified by their HyperCore name ("HYPE", "PURR", ...). HyperEVM is a
regular EVM chain and is configured separately (chain `hyperevm`).
"""
from __future__ import annotations

import logging
import time
import zlib
from decimal import Decimal

import requests

from .evm import Transfer
from .explorer import AddressInfo, Explorer

log = logging.getLogger(__name__)

DEFAULT_API = "https://api.hyperliquid.xyz/info"
SCALE = 8  # amounts are decimal strings; stored as integers with 8 decimals
ZERO = "0x" + "0" * 40


def system_label(address: str) -> str | None:
    """HyperCore <-> HyperEVM bridge system addresses."""
    if address == "0x2222222222222222222222222222222222222222":
        return "HyperEVM bridge (HYPE)"
    if address.startswith("0x20000000000000000000000000000000000000"):
        return "HyperEVM bridge"
    return None


def _raw(amount) -> int:
    return int((Decimal(str(amount or 0)) * (10 ** SCALE)).to_integral_value())


class HyperCoreClient:
    def __init__(self, api_url: str = DEFAULT_API, session: requests.Session | None = None, timeout: float = 20):
        self.api = api_url
        self.session = session or requests.Session()
        self.timeout = timeout
        self._last = 0.0
        self.min_interval = 0.3  # info API: 1200 weight / min per IP
        self._meta: dict[str, dict] | None = None

    def info(self, body: dict):
        for attempt in range(4):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.post(self.api, json=body, timeout=self.timeout)
                self._last = time.time()
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 3:
                    raise
                log.warning("hyperliquid info %s failed: %s", body.get("type"), exc)
                time.sleep(2 ** attempt)
        return None

    # --- polling ---------------------------------------------------------------------
    def poll(self, chain: str, cursor: dict, tokens: list[str], watch: set[str], mode: str,
             lookback: int) -> tuple[list[Transfer], dict]:
        wanted = {t.upper() for t in tokens}
        cursor = dict(cursor)
        start_default = int((time.time() - lookback) * 1000)
        out: dict[str, Transfer] = {}
        for user in sorted(watch):
            since = int(cursor.get(user, start_default))
            rows = self.info({"type": "userNonFundingLedgerUpdates", "user": user, "startTime": since + 1}) or []
            for row in rows:
                tr = self._transfer(chain, row)
                cursor[user] = max(int(cursor.get(user, since)), int(row.get("time") or 0))
                if tr and (not wanted or tr.token.upper() in wanted):
                    out[tr.key] = tr
            cursor.setdefault(user, since)
        return sorted(out.values(), key=lambda t: t.block), cursor

    @staticmethod
    def _transfer(chain: str, row: dict) -> Transfer | None:
        d = row.get("delta") or {}
        kind = d.get("type")
        if kind in ("spotTransfer", "send"):
            token, amount = d.get("token"), d.get("amount")
        elif kind == "internalTransfer":
            token, amount = "USDC", d.get("usdc")
        else:
            return None  # deposits / withdrawals / class & sub-account moves
        if not token or not d.get("user") or not d.get("destination"):
            return None
        frm, to = d["user"].lower(), d["destination"].lower()
        usd = d.get("usdcValue")
        if usd is None and token == "USDC":
            usd = amount
        return Transfer(
            chain=chain, token=token, from_addr=frm, to_addr=to, raw_amount=_raw(amount),
            tx_hash=(row.get("hash") or "").lower(), log_index=zlib.crc32(f"{token}:{frm}:{to}".encode()),
            block=int(row.get("time") or 0) // 1000, decimals=SCALE, symbol=token,
            usd=float(usd) if usd not in (None, "") else None,
        )

    # --- metadata / discovery helpers ---------------------------------------------------
    def token_id(self, name: str) -> str | None:
        if self._meta is None:
            meta = self.info({"type": "spotMeta"}) or {}
            self._meta = {t["name"].upper(): t for t in meta.get("tokens", [])}
        tok = self._meta.get(name.upper())
        return tok.get("tokenId") if tok else None

    def token_details(self, name: str) -> dict:
        tid = self.token_id(name)
        return (self.info({"type": "tokenDetails", "tokenId": tid}) or {}) if tid else {}

    def erc20_metadata(self, token: str) -> tuple[str | None, int | None]:
        return token, SCALE

    def total_supply(self, token: str) -> int | None:
        total = self.token_details(token).get("totalSupply")
        return _raw(total) if total else None

    def is_contract(self, address: str) -> bool | None:
        return False  # HyperCore accounts are plain addresses

    def fingerprint(self, address: str) -> str | None:
        return system_label(address)


class HyperCoreExplorer(Explorer):
    """Discovery: token deployer, genesis allocations and non-circulating balances."""

    def __init__(self, client: HyperCoreClient):
        self.client = client
        self._details: dict[str, dict] = {}

    def _d(self, token: str) -> dict:
        if token not in self._details:
            self._details[token] = self.client.token_details(token)
        return self._details[token]

    def contract_creation(self, token):
        deployer = self._d(token).get("deployer")
        return (deployer.lower(), "") if deployer else None

    def first_transfers(self, chain, token, limit=1000):
        genesis = (self._d(token).get("genesis") or {}).get("userBalances") or []
        return [Transfer(chain=chain, token=token, from_addr=ZERO, to_addr=addr.lower(), raw_amount=_raw(bal),
                         tx_hash="genesis", log_index=i, block=0)
                for i, (addr, bal) in enumerate(genesis[:limit])]

    def first_transfer_logs(self, token, limit=1000):
        return []

    def top_holders(self, token, limit=50):
        rows = self._d(token).get("nonCirculatingUserBalances") or []
        rows = sorted(((a.lower(), _raw(b)) for a, b in rows), key=lambda x: -x[1])[:limit]
        return [(a, v, AddressInfo(is_contract=False, tags=["non-circulating"])) for a, v in rows]

    def address_info(self, address):
        return AddressInfo(is_contract=False, name=system_label(address))
