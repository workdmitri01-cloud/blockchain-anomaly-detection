"""Solana (SPL tokens) over plain JSON-RPC - free public endpoints, optional Helius/QuickNode URL.

Solana has no cheap "all transfers of a mint" query, so both bots watch accounts:
for every watched owner (team wallet / exchange hot wallet) and token mint we find its
token accounts (getTokenAccountsByOwner), read new signatures of those accounts
(getSignaturesForAddress ``until`` the last seen one) and derive transfers from the
pre/post token balances of each transaction.
"""
from __future__ import annotations

import logging
import time
import zlib

from .evm import EvmRpc, RpcError, Transfer
from .explorer import AddressInfo, Explorer

log = logging.getLogger(__name__)

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
)
# Owner programs -> human label, used to classify token holders during discovery.
KNOWN_PROGRAMS = {
    "SMPLecH534NA9acpos4G6x7uf3LWbCAwZQE9e8ZekMu": "Squads multisig",
    "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf": "Squads multisig",
    "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m": "Streamflow vesting",
    "CChTq6PthWU82YZkbveA3WDf7s97BWhBK4Vx9bmsT743": "Bonfida token vesting",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM pool",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM pool",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM pool",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool pool",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM pool",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora AMM pool",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun bonding curve pool",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap AMM pool",
}


class SolanaClient:
    def __init__(self, urls: list[str], session=None, max_sigs: int = 50, accounts_ttl: int = 6 * 3600):
        self.rpc = EvmRpc(urls, session=session)  # plain JSON-RPC with endpoint rotation
        self.max_sigs = max_sigs
        self.accounts_ttl = accounts_ttl

    def call(self, method: str, params: list):
        return self.rpc.call(method, params)

    # --- polling ---------------------------------------------------------------------
    def _token_accounts(self, owner: str, tokens: list[str]) -> list[str]:
        filters = [{"mint": m} for m in tokens] or [{"programId": p} for p in TOKEN_PROGRAMS]
        out = []
        for flt in filters:
            res = self.call("getTokenAccountsByOwner", [owner, flt, {"encoding": "jsonParsed"}]) or {}
            out += [a["pubkey"] for a in res.get("value", [])]
        return out

    def poll(self, chain: str, cursor: dict, tokens: list[str], watch: set[str], mode: str,
             lookback: int) -> tuple[list[Transfer], dict]:
        cursor = {"accounts": dict(cursor.get("accounts", {})), "last": dict(cursor.get("last", {})),
                  "accounts_at": cursor.get("accounts_at", {})}
        now = time.time()
        wanted = set(tokens)
        seen_sigs: set[str] = set()
        transfers: dict[str, Transfer] = {}
        for owner in sorted(watch):
            if now - cursor["accounts_at"].get(owner, 0) > self.accounts_ttl:
                try:
                    cursor["accounts"][owner] = self._token_accounts(owner, tokens)
                    cursor["accounts_at"][owner] = now
                except RpcError as exc:
                    log.warning("solana: token accounts of %s failed: %s", owner, exc)
            for acc in cursor["accounts"].get(owner, []):
                params: dict = {"limit": self.max_sigs, "commitment": "finalized"}
                if cursor["last"].get(acc):
                    params["until"] = cursor["last"][acc]
                sigs = self.call("getSignaturesForAddress", [acc, params]) or []
                if not sigs:
                    continue
                cursor["last"][acc] = sigs[0]["signature"]  # newest first
                for s in reversed(sigs):
                    if s.get("err") or s["signature"] in seen_sigs:
                        continue
                    if "until" not in params and (s.get("blockTime") or 0) < now - lookback:
                        continue  # first run: no deep backfill
                    seen_sigs.add(s["signature"])
                    for tr in self._transfers(chain, s["signature"], watch):
                        if not wanted or tr.token in wanted:
                            transfers[tr.key] = tr
        return sorted(transfers.values(), key=lambda t: (t.block, t.log_index)), cursor

    def _transfers(self, chain: str, sig: str, watch: set[str]) -> list[Transfer]:
        tx = self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                "commitment": "finalized"}])
        if not tx or not tx.get("meta"):
            return []
        return transfers_from_balances(chain, sig, tx.get("slot", 0), tx["meta"], watch)

    # --- metadata / discovery helpers ---------------------------------------------------
    def erc20_metadata(self, mint: str) -> tuple[str | None, int | None]:
        try:
            res = self.call("getTokenSupply", [mint]) or {}
            return None, int(res["value"]["decimals"])
        except (RpcError, KeyError, TypeError):
            return None, None

    def total_supply(self, mint: str) -> int | None:
        try:
            return int(((self.call("getTokenSupply", [mint]) or {}).get("value") or {})["amount"])
        except (RpcError, KeyError, TypeError, ValueError):
            return None

    def owner_program(self, address: str) -> str | None:
        try:
            res = self.call("getAccountInfo", [address, {"encoding": "base64"}]) or {}
        except RpcError:
            return None
        return (res.get("value") or {}).get("owner")

    def is_contract(self, address: str) -> bool | None:
        return is_program_controlled(address, self.owner_program(address))

    def fingerprint(self, address: str) -> str | None:
        return KNOWN_PROGRAMS.get(self.owner_program(address) or "")


_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def is_on_curve(address: str) -> bool:
    """Ed25519 point check: wallets are on-curve, PDAs (program-derived addresses) are not."""
    from .addr import b58decode

    raw = b58decode(address)
    if len(raw) != 32:
        return False
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    x2 = (y2 - 1) * pow(_D * y2 + 1, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def is_program_controlled(address: str, owner_program: str | None) -> bool:
    """True for program-owned accounts and PDAs (pools, vaults, vesting...), False for wallets."""
    if owner_program and owner_program != SYSTEM_PROGRAM:
        return True
    return not is_on_curve(address)


def transfers_from_balances(chain: str, sig: str, slot: int, meta: dict, watch: set[str]) -> list[Transfer]:
    """Net token flows per (owner, mint) in one transaction -> transfers touching watched owners."""
    deltas: dict[tuple[str, str], int] = {}
    decimals: dict[str, int] = {}
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(key) or []:
            owner, mint = b.get("owner"), b.get("mint")
            if not owner or not mint:
                continue
            amt = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            decimals[mint] = int((b.get("uiTokenAmount") or {}).get("decimals") or 0)
            deltas[(owner, mint)] = deltas.get((owner, mint), 0) + sign * amt
    out = []
    for mint in {m for _, m in deltas}:
        senders = sorted(((o, -d) for (o, m), d in deltas.items() if m == mint and d < 0), key=lambda x: -x[1])
        receivers = sorted(((o, d) for (o, m), d in deltas.items() if m == mint and d > 0), key=lambda x: -x[1])
        for owner, amount in senders + receivers:
            if owner not in watch:
                continue
            outflow = (owner, amount) in senders
            others = receivers if outflow else senders
            counter = others[0][0] if others else ("burn" if outflow else "mint")
            frm, to = (owner, counter) if outflow else (counter, owner)
            out.append(Transfer(
                chain=chain, token=mint, from_addr=frm, to_addr=to, raw_amount=amount, tx_hash=sig,
                log_index=zlib.crc32(f"{mint}:{frm}:{to}".encode()), block=slot, decimals=decimals[mint],
            ))
    return out


class SolanaExplorer(Explorer):
    """Discovery from RPC only: mint authority + largest holders (getTokenLargestAccounts)."""

    def __init__(self, client: SolanaClient):
        self.client = client

    def contract_creation(self, mint):
        try:
            res = self.client.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}
            info = ((res.get("value") or {}).get("data") or {}).get("parsed", {}).get("info", {})
        except (RpcError, AttributeError):
            return None
        auth = info.get("mintAuthority")
        return (auth, "") if auth else None

    def first_transfer_logs(self, token, limit=1000):
        return []

    def top_holders(self, mint, limit=50):
        try:
            largest = (self.client.call("getTokenLargestAccounts", [mint]) or {}).get("value") or []
            if not largest:
                return []
            accs = self.client.call("getMultipleAccounts", [[a["address"] for a in largest],
                                                             {"encoding": "jsonParsed"}]) or {}
        except RpcError:
            return []
        holders: dict[str, int] = {}
        for item, acc in zip(largest, accs.get("value") or []):
            owner = (((acc or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
            if owner:
                holders[owner] = holders.get(owner, 0) + int(item.get("amount") or 0)
        out = []
        for owner, amount in sorted(holders.items(), key=lambda x: -x[1])[:limit]:
            prog = self.client.owner_program(owner)
            name = KNOWN_PROGRAMS.get(prog or "")
            out.append((owner, amount, AddressInfo(is_contract=is_program_controlled(owner, prog), name=name)))
        return out

    def address_info(self, address):
        prog = self.client.owner_program(address)
        return AddressInfo(is_contract=is_program_controlled(address, prog), name=KNOWN_PROGRAMS.get(prog or ""))
