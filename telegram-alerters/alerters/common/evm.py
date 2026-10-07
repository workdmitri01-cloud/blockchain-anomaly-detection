"""Minimal EVM JSON-RPC client: endpoint rotation, adaptive eth_getLogs, ERC-20 decoding."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

import requests

log = logging.getLogger(__name__)

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_RANGE_ERROR_HINTS = (
    "range",
    "too many",
    "limit",
    "exceed",
    "10000",
    "query returned more than",
    "response size",
    "timeout",
    "too large",
)


class RpcError(Exception):
    pass


class RangeTooLarge(RpcError):
    pass


def address_topic(address: str) -> str:
    """32-byte topic encoding of an address."""
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def topic_to_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


@dataclass(frozen=True)
class Transfer:
    chain: str
    token: str
    from_addr: str
    to_addr: str
    raw_amount: int
    tx_hash: str
    log_index: int
    block: int

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.tx_hash}:{self.log_index}"


def decode_transfer(chain: str, entry: dict) -> Transfer | None:
    """Decode an ERC-20 Transfer log. ERC-721 transfers (4 topics) are skipped."""
    # Explorer APIs pad unused topics with null.
    topics = [t for t in (entry.get("topics") or []) if t]
    if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
        return None
    if entry.get("removed"):
        return None
    data = entry.get("data") or "0x"
    try:
        amount = int(data[:66], 16) if data not in ("0x", "") else 0
    except ValueError:
        return None
    return Transfer(
        chain=chain,
        token=entry["address"].lower(),
        from_addr=topic_to_address(topics[1]),
        to_addr=topic_to_address(topics[2]),
        raw_amount=amount,
        tx_hash=entry["transactionHash"].lower(),
        log_index=_int(entry.get("logIndex")),
        block=_int(entry["blockNumber"]),
    )


def _int(value) -> int:
    """Parse RPC hex ("0x1a") or explorer decimal ("26" / "") numbers."""
    if value in (None, "", "0x"):
        return 0
    if isinstance(value, int):
        return value
    return int(value, 16) if str(value).startswith("0x") else int(value)


class EvmRpc:
    """JSON-RPC client that rotates between several endpoints on failure."""

    def __init__(self, urls: list[str], timeout: float = 20, session: requests.Session | None = None):
        if not urls:
            raise ValueError("No RPC urls")
        self.urls = list(urls)
        self.timeout = timeout
        self.session = session or requests.Session()
        self._idx = 0
        self._id = 0

    @property
    def current_url(self) -> str:
        return self.urls[self._idx % len(self.urls)]

    def _rotate(self) -> None:
        self._idx = (self._idx + 1) % len(self.urls)

    def call(self, method: str, params: list) -> object:
        last_err: Exception | None = None
        # Each endpoint gets one try; full rounds are retried with backoff.
        for attempt in range(len(self.urls) * 2):
            url = self.current_url
            self._id += 1
            try:
                resp = self.session.post(
                    url,
                    json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                    timeout=self.timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RpcError(f"HTTP {resp.status_code}")
                body = resp.json()
                if "error" in body and body["error"]:
                    msg = str(body["error"].get("message", body["error"]))
                    low = msg.lower()
                    if (
                        method == "eth_getLogs"
                        and "rate" not in low
                        and any(h in low for h in _RANGE_ERROR_HINTS)
                    ):
                        raise RangeTooLarge(msg)
                    raise RpcError(msg)
                return body.get("result")
            except RangeTooLarge:
                raise
            except (requests.RequestException, ValueError, RpcError) as exc:
                last_err = exc
                log.warning("RPC %s %s failed on %s: %s", method, params[:1], _redact(url), exc)
                self._rotate()
                if attempt and attempt % len(self.urls) == 0:
                    time.sleep(min(2 ** (attempt // len(self.urls)), 10))
        raise RpcError(f"{method} failed on all endpoints: {last_err}")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_logs(self, from_block: int, to_block: int, address=None, topics=None, chunk: int = 2000) -> tuple[list[dict], int]:
        """Fetch logs for [from_block, to_block] in chunks, halving the chunk on range errors.

        Returns (logs, chunk_size_that_worked) so callers can remember it.
        """
        out: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(to_block, start + chunk - 1)
            flt: dict = {"fromBlock": hex(start), "toBlock": hex(end)}
            if address:
                flt["address"] = address
            if topics:
                flt["topics"] = topics
            try:
                out.extend(self.call("eth_getLogs", [flt]) or [])
            except RangeTooLarge as exc:
                if chunk <= 1:
                    raise RpcError(f"eth_getLogs fails even for a single block: {exc}") from exc
                chunk = max(1, chunk // 2)
                log.info("eth_getLogs range too large, chunk -> %d", chunk)
                continue
            start = end + 1
        return out, chunk

    def total_supply(self, token: str) -> int | None:
        try:
            res = self.call("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"])
            return int(res, 16) if res and res != "0x" else None
        except (RpcError, ValueError):
            return None

    def is_contract(self, address: str) -> bool | None:
        try:
            code = self.call("eth_getCode", [address, "latest"])
            return bool(code and code != "0x")
        except RpcError:
            return None

    def erc20_metadata(self, token: str) -> tuple[str | None, int | None]:
        """Read symbol() and decimals() via eth_call. Best effort."""
        symbol = decimals = None
        try:
            res = self.call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
            if res and res != "0x":
                decimals = int(res, 16)
        except RpcError:
            pass
        try:
            res = self.call("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"])
            symbol = _decode_string(res)
        except RpcError:
            pass
        return symbol, decimals


def _decode_string(res: str | None) -> str | None:
    if not res or res == "0x":
        return None
    raw = bytes.fromhex(res[2:])
    try:
        if len(raw) >= 64:
            length = int.from_bytes(raw[32:64], "big")
            return raw[64 : 64 + length].decode("utf-8", "ignore") or None
        return raw.rstrip(b"\x00").decode("utf-8", "ignore") or None  # bytes32 symbols (MKR & co)
    except (ValueError, OverflowError):
        return None


def _redact(url: str) -> str:
    """Hide API keys embedded in RPC urls when logging."""
    parts = url.split("/")
    return "/".join(p if len(p) < 24 else p[:4] + "…" for p in parts)


def chunked(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
