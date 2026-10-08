"""Automatic discovery of team wallets for a token, from public on-chain data only.

Signals (each adds to a score; wallets with score >= min_score are tracked):

  +4  deployer of the token contract
  +4  received tokens from the initial mint (Transfer from 0x0)
  +3  received a large share in the initial distribution directly from deployer/minter
  +2  ... one hop further (team -> vesting -> sub-wallet)
  +2  contract that looks like team infrastructure: Safe multisig, vesting, timelock,
      treasury, DAO / governor (by verified name or explorer tag)
  +2  explorer public tag mentions the token / project name
  +1  top holder with >= min_share of supply (+1 more if also in the distribution tree)

Excluded: CEX wallets (address book), DEX pools / routers / bridges / staking and other
DeFi contracts (by name), airdrop distributors, burn addresses, the token contract itself.

Data: Blockscout (free, no key) and/or Etherscan-compatible APIs for the contract creator,
oldest Transfer logs and top holders; RPC for totalSupply / eth_getCode.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..common.addr import TRON_ZERO, norm
from ..common.explorer import AddressInfo, Explorer
from ..common.labels import AddressBook
from ..common.telegram import short

log = logging.getLogger(__name__)

ZERO = "0x" + "0" * 40
# "From" addresses that mean a mint (EVM 0x0, Tron's zero address, Solana mint pseudo-party).
MINT_FROM = {ZERO, TRON_ZERO, "mint"}
BURN = {
    ZERO,
    TRON_ZERO,
    "mint",
    "burn",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000042069420694206942069",
    "0x0000000000000000000000000000000000000001",
}

# DeFi / infra contracts whose balance or inflows are NOT team activity.
NON_TEAM_RE = re.compile(
    r"(pair|pool|router|swap|bridge|gateway|portal|aggregat|exchange|staking|farm|masterchef|"
    r"uniswap|pancake|sushi|curve|balancer|1inch|permit2|multicall|wrapped|weth|lending|"
    r"comptroller|ctoken|atoken|merkle|airdrop|claim|distributor|launchpad|presale|"
    r"factory|position|nonfungible|liquidity|locker for lp)",
    re.I,
)
# Contracts that typically hold team / treasury funds.
TEAM_RE = re.compile(
    r"(safe|multisig|multi-sig|gnosis|vest|timelock|time lock|token ?lock|treasury|team|"
    r"foundation|dao\b|governor|reserve|ecosystem|escrow|allocation|advisors?|investors?|"
    r"marketing|operations|development|non-circulating)",
    re.I,
)


@dataclass
class Candidate:
    address: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    depth: int = 0
    info: AddressInfo | None = None

    def add(self, points: float, reason: str) -> None:
        if reason not in self.reasons:
            self.score += points
            self.reasons.append(reason)

    @property
    def title(self) -> str:
        name = self.info.name if self.info and self.info.name else None
        return f"{name} {short(self.address)}" if name else short(self.address)


@dataclass
class DiscoveryParams:
    min_score: float = 3
    min_share_pct: float = 0.5  # % of supply that counts as a "large" allocation / holder
    top_holders: int = 50
    max_depth: int = 2
    max_lookups: int = 40  # explorer address lookups per token


def classify(info: AddressInfo | None) -> str:
    """'team' | 'non_team' | 'unknown' from a contract name / tags."""
    if not info:
        return "unknown"
    text = " ".join([info.name or "", *info.tags])
    if not text.strip():
        return "unknown"
    if TEAM_RE.search(text) and not re.search(r"(pair|pool|router|bridge)", text, re.I):
        return "team"
    if NON_TEAM_RE.search(text):
        return "non_team"
    return "unknown"


def discover(chain: str, token: str, symbol: str | None, rpc, explorer: Explorer, book: AddressBook,
             params: DiscoveryParams | None = None) -> tuple[list[Candidate], list[Candidate]]:
    """Returns (team_wallets, low_confidence_candidates)."""
    p = params or DiscoveryParams()
    token = norm(token)
    cands: dict[str, Candidate] = {}

    def cand(addr: str) -> Candidate:
        return cands.setdefault(addr, Candidate(addr))

    supply = rpc.total_supply(token) if rpc else None
    holders = explorer.top_holders(token, p.top_holders) if p.top_holders else []
    if not supply and holders:
        supply = sum(v for _, v, _ in holders) or None
    transfers = list(explorer.first_transfers(chain, token))
    transfers.sort(key=lambda t: (t.block, t.log_index))
    if not supply:
        supply = sum(t.raw_amount for t in transfers if t.from_addr in MINT_FROM) or None
    big = (supply * p.min_share_pct / 100) if supply else 0

    # 1. Deployer
    creation = explorer.contract_creation(token)
    deployer = creation[0] if creation else None
    if deployer:
        cand(deployer).add(4, "деплоер контракта токена")

    if not deployer and not transfers and not holders:
        log.warning("discovery %s:%s: explorer returned no data (network / API key / unsupported chain?)", chain, token)

    # 2. Mint + initial distribution tree (oldest transfers, chronological)
    depth: dict[str, int] = {deployer: 0} if deployer else {}
    for tr in transfers:
        if tr.from_addr in MINT_FROM:
            if tr.to_addr not in BURN and tr.raw_amount >= big:
                cand(tr.to_addr).add(4, "получил токены при минте")
                depth.setdefault(tr.to_addr, 0)
            continue
        d = depth.get(tr.from_addr)
        if d is None or d >= p.max_depth or tr.to_addr in BURN or tr.to_addr == token:
            continue
        if tr.raw_amount < big or tr.to_addr in book:
            continue
        pct = f" ({tr.raw_amount * 100 / supply:.1f}% supply)" if supply else ""
        if d == 0:
            cand(tr.to_addr).add(3, f"начальное распределение от {short(tr.from_addr)}{pct}")
        else:
            cand(tr.to_addr).add(2, f"распределение 2-го уровня от {short(tr.from_addr)}{pct}")
        if tr.to_addr not in depth:
            depth[tr.to_addr] = d + 1
            cands[tr.to_addr].depth = d + 1

    # 3. Top holders
    holder_info: dict[str, AddressInfo] = {}
    for rank, (addr, value, info) in enumerate(holders, 1):
        holder_info[addr] = info
        if not supply or value < big or addr in BURN or addr == token or addr in book:
            continue
        in_tree = addr in cands
        c = cand(addr)
        c.add(1, f"топ-холдер #{rank} ({value * 100 / supply:.1f}% supply)")
        if in_tree:
            c.add(1, "до сих пор держит аллокацию")

    # 4. Classify by contract name / tags, drop exchanges and DeFi contracts
    team, low = [], []
    sym = (symbol or "").lower()
    for c in sorted(cands.values(), key=lambda c: -c.score)[: p.max_lookups]:
        if c.address in book or c.address in BURN or c.address == token:
            continue
        info = holder_info.get(c.address)
        if info is None or (info.is_contract and not info.name):
            try:
                info = explorer.address_info(c.address)
            except Exception:  # noqa: BLE001 - discovery must never crash the bot
                info = AddressInfo()
        if info.is_contract is None and rpc:
            info.is_contract = rpc.is_contract(c.address)
        if info.is_contract and not info.name and rpc and hasattr(rpc, "fingerprint"):
            # No verified name (typical on BSC without a paid explorer): probe the contract.
            info.name = rpc.fingerprint(c.address)
        c.info = info
        kind = classify(info)
        if kind == "non_team":
            continue
        if c.address == deployer and info.is_contract and kind != "team":
            # Token created by a factory / launchpad contract - not a team wallet.
            continue
        if kind == "team":
            c.add(2, f"контракт: {info.name or ', '.join(info.tags)}")
        if sym and len(sym) >= 3 and any(sym in t.lower() for t in info.tags):
            c.add(2, "публичный тег обозревателя с названием проекта")
        (team if c.score >= p.min_score else low).append(c)
    team.sort(key=lambda c: -c.score)
    low.sort(key=lambda c: -c.score)
    log.info("discovery %s:%s -> %d team wallets, %d low-confidence", chain, token, len(team), len(low))
    return team, low
