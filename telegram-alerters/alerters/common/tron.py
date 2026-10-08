"""Tron (TRC-20) via TronGrid - free; works without a key, TRONGRID_API_KEY raises limits.

Two polling modes:
  tokens   - every Transfer event of the tracked TRC-20 contracts (one call per token);
             used by the exchange bot, exchange wallets are matched locally.
  accounts - TRC-20 history of each watched account; used by the team bot.
"""
from __future__ import annotations

import logging
import os
import time

import requests

from .addr import TRON_ZERO, is_tron, tron_from_hex
from .evm import Transfer
from .explorer import AddressInfo, Explorer, ExplorerError

log = logging.getLogger(__name__)

DEFAULT_API = "https://api.trongrid.io"


def _addr(value: str | None) -> str:
    if not value:
        return TRON_ZERO
    return value if is_tron(value) else tron_from_hex(value)


class TronClient:
    def __init__(self, api_url: str = DEFAULT_API, api_key: str | None = None,
                 session: requests.Session | None = None, timeout: float = 20, max_pages: int = 25):
        self.api = api_url.rstrip("/")
        self.session = session or requests.Session()
        key = api_key if api_key is not None else os.environ.get("TRONGRID_API_KEY", "")
        self.headers = {"TRON-PRO-API-KEY": key} if key else {}
        self.timeout = timeout
        self.max_pages = max_pages
        self._last = 0.0
        # Without a key TronGrid allows only a few requests per second.
        self.min_interval = 0.12 if key else 0.35

    # --- http ------------------------------------------------------------------------
    def _req(self, method: str, path: str, **kw) -> dict:
        for attempt in range(4):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.request(method, f"{self.api}{path}", headers=self.headers,
                                            timeout=self.timeout, **kw)
                self._last = time.time()
                if resp.status_code in (429, 503) or resp.status_code >= 500:
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 3:
                    raise
                log.warning("TronGrid %s failed: %s", path, exc)
                time.sleep(2 ** attempt)
        return {}

    def _paged(self, path: str, params: dict) -> list[dict]:
        out: list[dict] = []
        params = dict(params)
        for _ in range(self.max_pages):
            body = self._req("GET", path, params=params)
            out.extend(body.get("data") or [])
            fp = (body.get("meta") or {}).get("fingerprint")
            if not fp:
                break
            params["fingerprint"] = fp
        return out

    def _constant(self, contract: str, selector: str) -> str | None:
        body = self._req("POST", "/wallet/triggerconstantcontract", json={
            "owner_address": contract, "contract_address": contract,
            "function_selector": selector, "visible": True,
        })
        res = body.get("constant_result") or []
        ok = (body.get("result") or {}).get("result", True)
        return res[0] if res and res[0] and ok else None

    # --- polling ---------------------------------------------------------------------
    def poll(self, chain: str, cursor: dict, tokens: list[str], watch: set[str], mode: str,
             lookback: int) -> tuple[list[Transfer], dict]:
        now_ms = int(time.time() * 1000)
        since = int(cursor["ts"]) if "ts" in cursor else now_ms - lookback * 1000
        transfers: list[Transfer] = []
        newest = since
        if mode == "tokens":
            for token in tokens:
                for ev in self._paged(f"/v1/contracts/{token}/events", {
                    "event_name": "Transfer", "only_confirmed": "true", "order_by": "block_timestamp,asc",
                    "min_block_timestamp": since, "limit": 200,
                }):
                    tr = self._from_event(chain, ev)
                    if tr:
                        transfers.append(tr)
                        newest = max(newest, int(ev.get("block_timestamp") or 0))
        else:
            wanted = set(tokens)
            for account in sorted(watch):
                rows = self._paged(f"/v1/accounts/{account}/transactions/trc20", {
                    "only_confirmed": "true", "order_by": "block_timestamp,asc",
                    "min_timestamp": since, "limit": 200,
                })
                per_tx: dict[str, int] = {}
                for row in rows:
                    info = row.get("token_info") or {}
                    token = info.get("address") or ""
                    if wanted and token not in wanted:
                        continue
                    tx = row.get("transaction_id", "")
                    idx = per_tx[tx] = per_tx.get(tx, -1) + 1
                    transfers.append(Transfer(
                        chain=chain, token=token, from_addr=_addr(row.get("from")), to_addr=_addr(row.get("to")),
                        raw_amount=int(row.get("value") or 0), tx_hash=tx, log_index=idx, block=0,
                        decimals=int(info["decimals"]) if info.get("decimals") is not None else None,
                        symbol=info.get("symbol"),
                    ))
                    newest = max(newest, int(row.get("block_timestamp") or 0))
        # Re-read the last millisecond next time (duplicates are dropped by alert keys).
        return transfers, {"ts": newest}

    @staticmethod
    def _from_event(chain: str, ev: dict) -> Transfer | None:
        res = ev.get("result") or {}
        frm = res.get("from") or res.get("0")
        to = res.get("to") or res.get("1")
        value = res.get("value") or res.get("2")
        if to is None or value is None:
            return None
        return Transfer(
            chain=chain, token=_addr(ev.get("contract_address")), from_addr=_addr(frm), to_addr=_addr(to),
            raw_amount=int(value), tx_hash=ev.get("transaction_id", ""), log_index=int(ev.get("event_index") or 0),
            block=int(ev.get("block_number") or 0),
        )

    # --- metadata / discovery helpers ---------------------------------------------------
    def erc20_metadata(self, token: str) -> tuple[str | None, int | None]:
        try:
            dec = self._constant(token, "decimals()")
            sym = self._constant(token, "symbol()")
        except requests.RequestException:
            return None, None
        symbol = None
        if sym and len(sym) >= 128:
            length = int(sym[64:128], 16)
            symbol = bytes.fromhex(sym[128:128 + 2 * length]).decode("utf-8", "ignore") or None
        return symbol, int(dec, 16) if dec else None

    def total_supply(self, token: str) -> int | None:
        try:
            res = self._constant(token, "totalSupply()")
        except requests.RequestException:
            return None
        return int(res, 16) if res else None

    def contract(self, address: str) -> dict:
        try:
            return self._req("POST", "/wallet/getcontract", json={"value": address, "visible": True}) or {}
        except requests.RequestException:
            return {}

    def is_contract(self, address: str) -> bool | None:
        return bool(self.contract(address).get("bytecode"))

    def fingerprint(self, address: str) -> str | None:
        try:
            if self._constant(address, "token0()"):
                return "AMM pair / pool"
            if self._constant(address, "getThreshold()"):
                return "Safe multisig"
        except requests.RequestException:
            return None
        return None


class TronExplorer(Explorer):
    """Discovery data from TronGrid (no key): creator + oldest Transfer events."""

    def __init__(self, client: TronClient):
        self.client = client

    def contract_creation(self, address):
        origin = self.client.contract(address).get("origin_address")
        return (_addr(origin), "") if origin else None

    def first_transfers(self, chain, token, limit=1000):
        try:
            events = self.client._paged(f"/v1/contracts/{token}/events", {
                "event_name": "Transfer", "only_confirmed": "true", "order_by": "block_timestamp,asc",
                "min_block_timestamp": 0, "limit": 200,
            })
        except requests.RequestException as exc:
            raise ExplorerError(str(exc)) from exc
        return [t for t in (TronClient._from_event(chain, e) for e in events[:limit]) if t]

    def first_transfer_logs(self, token, limit=1000):
        return []

    def top_holders(self, token, limit=50):
        return []  # holder lists need a TronScan API key

    def address_info(self, address):
        c = self.client.contract(address)
        if not c:
            return AddressInfo(is_contract=False)
        return AddressInfo(is_contract=bool(c.get("bytecode")), name=c.get("name") or None)

