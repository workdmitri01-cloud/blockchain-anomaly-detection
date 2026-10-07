"""Block-explorer clients used for wallet discovery (contract creator, early transfers, holders).

Backends, tried in order per chain:

* Blockscout   - free, no key (eth / base / optimism / arbitrum / polygon / gnosis ...)
                 REST v2 + Etherscan-compatible ``/api`` endpoints
* Etherscan V2 - one free key for all chains (``ETHERSCAN_API_KEY``), or any
                 Etherscan-compatible API without a key, e.g. Routescan for Avalanche
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import requests

from .evm import TRANSFER_TOPIC

log = logging.getLogger(__name__)


@dataclass
class AddressInfo:
    is_contract: bool | None = None
    name: str | None = None  # contract name ("GnosisSafeProxy", "UniswapV2Pair") or public tag
    tags: list[str] = field(default_factory=list)


class ExplorerError(Exception):
    pass


class Explorer:
    """Interface. Every method may raise ExplorerError or return empty results."""

    def contract_creation(self, address: str) -> tuple[str, str] | None:
        """(creator, creation_tx_hash)"""
        raise NotImplementedError

    def first_transfer_logs(self, token: str, limit: int = 1000) -> list[dict]:
        """Oldest Transfer logs of the token (RPC log format)."""
        raise NotImplementedError

    def top_holders(self, token: str, limit: int = 50) -> list[tuple[str, int, AddressInfo]]:
        raise NotImplementedError

    def address_info(self, address: str) -> AddressInfo:
        raise NotImplementedError


class _Http:
    def __init__(self, session: requests.Session | None, timeout: float):
        self.session = session or requests.Session()
        self.timeout = timeout
        self._last = 0.0
        self.min_interval = 0.25  # stay under free-tier rate limits

    def get(self, url: str, params: dict | None = None) -> object:
        for attempt in range(3):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self._last = time.time()
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise ExplorerError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError, ExplorerError) as exc:
                log.debug("explorer GET %s failed: %s", url, exc)
                if attempt == 2:
                    raise ExplorerError(str(exc)) from exc
                time.sleep(1.5 * (attempt + 1))
        return None


def _etherscan_result(body) -> object:
    if not isinstance(body, dict):
        raise ExplorerError("bad response")
    result = body.get("result")
    if str(body.get("status")) == "0" and not isinstance(result, list):
        # "No records found" is a valid empty answer; anything else is an error.
        if "no " in str(body.get("message", "")).lower() or "no " in str(result).lower():
            return []
        raise ExplorerError(f"{body.get('message')}: {result}")
    return result


class BlockscoutExplorer(Explorer):
    def __init__(self, base_url: str, session: requests.Session | None = None, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.http = _Http(session, timeout)

    def contract_creation(self, address):
        data = self.http.get(f"{self.base}/api/v2/addresses/{address}")
        if not data:
            return None
        creator = data.get("creator_address_hash")
        tx = data.get("creation_transaction_hash") or data.get("creation_tx_hash")
        return (creator.lower(), (tx or "").lower()) if creator else None

    def first_transfer_logs(self, token, limit=1000):
        body = self.http.get(
            f"{self.base}/api",
            {"module": "logs", "action": "getLogs", "fromBlock": 0, "toBlock": "latest",
             "address": token, "topic0": TRANSFER_TOPIC},
        )
        return list(_etherscan_result(body) or [])[:limit]

    def top_holders(self, token, limit=50):
        out: list[tuple[str, int, AddressInfo]] = []
        params: dict | None = None
        while len(out) < limit:
            data = self.http.get(f"{self.base}/api/v2/tokens/{token}/holders", params)
            if not data:
                break
            for item in data.get("items", []):
                out.append((item["address"]["hash"].lower(), int(item.get("value") or 0), self._info(item["address"])))
            params = data.get("next_page_params")
            if not params:
                break
        return out[:limit]

    def address_info(self, address):
        data = self.http.get(f"{self.base}/api/v2/addresses/{address}")
        return self._info(data or {})

    @staticmethod
    def _info(a: dict) -> AddressInfo:
        tags = [t.get("label") or t.get("display_name") or t.get("name") for t in a.get("public_tags") or []]
        tags += [t.get("name") for t in ((a.get("metadata") or {}).get("tags") or [])]
        name = a.get("name")
        impl = [i.get("name") for i in a.get("implementations") or [] if i.get("name")]
        if impl and name and name not in impl:
            name = f"{name} / {impl[0]}"
        elif impl and not name:
            name = impl[0]
        return AddressInfo(is_contract=a.get("is_contract"), name=name, tags=[t for t in tags if t])


class EtherscanExplorer(Explorer):
    """Etherscan V2 (multichain, one key) or any Etherscan-compatible API (Routescan...)."""

    def __init__(self, api_url: str, chain_id: int, api_key: str | None = None,
                 session: requests.Session | None = None, timeout: float = 30):
        self.url = api_url
        self.chain_id = chain_id
        self.api_key = api_key if api_key is not None else os.environ.get("ETHERSCAN_API_KEY", "")
        self.http = _Http(session, timeout)
        self.http.min_interval = 0.35  # free tier: 3-5 req/s

    def _q(self, **params) -> object:
        params = {"chainid": self.chain_id, **params}
        if self.api_key:
            params["apikey"] = self.api_key
        return _etherscan_result(self.http.get(self.url, params))

    def contract_creation(self, address):
        res = self._q(module="contract", action="getcontractcreation", contractaddresses=address)
        if not res:
            return None
        return res[0]["contractCreator"].lower(), res[0]["txHash"].lower()

    def first_transfer_logs(self, token, limit=1000):
        res = self._q(module="logs", action="getLogs", fromBlock=0, toBlock="latest", address=token,
                      topic0=TRANSFER_TOPIC, page=1, offset=min(limit, 1000))
        return list(res or [])

    def top_holders(self, token, limit=50):
        return []  # token holder lists are a paid Etherscan feature

    def address_info(self, address):
        res = self._q(module="contract", action="getsourcecode", address=address)
        item = (res or [{}])[0] if isinstance(res, list) else {}
        name = item.get("ContractName") or None
        if not name:
            # No verified source: could be an EOA or an unverified contract.
            return AddressInfo(is_contract=None)
        return AddressInfo(is_contract=True, name=name)


class MultiExplorer(Explorer):
    """Tries each backend until one answers."""

    def __init__(self, backends: list[Explorer]):
        self.backends = backends

    def _try(self, method: str, *args, empty=None):
        for b in self.backends:
            try:
                res = getattr(b, method)(*args)
            except ExplorerError as exc:
                log.warning("%s.%s failed: %s", type(b).__name__, method, exc)
                continue
            if res:
                return res
        return empty

    def contract_creation(self, address):
        return self._try("contract_creation", address)

    def first_transfer_logs(self, token, limit=1000):
        return self._try("first_transfer_logs", token, limit, empty=[])

    def top_holders(self, token, limit=50):
        return self._try("top_holders", token, limit, empty=[])

    def address_info(self, address):
        merged = AddressInfo()
        for b in self.backends:
            try:
                info = b.address_info(address)
            except ExplorerError:
                continue
            merged.is_contract = merged.is_contract if merged.is_contract is not None else info.is_contract
            merged.name = merged.name or info.name
            merged.tags += [t for t in info.tags if t not in merged.tags]
            if merged.is_contract is not None and (merged.name or merged.is_contract is False):
                break
        return merged


def explorer_for(chain) -> MultiExplorer:
    backends: list[Explorer] = []
    if getattr(chain, "blockscout", None):
        backends.append(BlockscoutExplorer(chain.blockscout))
    api = getattr(chain, "etherscan_api", None)
    # etherscan.io itself refuses key-less calls; Routescan & co. work without a key.
    if api and (os.environ.get("ETHERSCAN_API_KEY") or "etherscan.io" not in api):
        backends.append(EtherscanExplorer(api, chain.chain_id))
    return MultiExplorer(backends)
