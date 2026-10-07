"""Bot #1: ERC-20 transfers to / from team wallets (manual list + automatic discovery)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..common.config import (
    BaseBotConfig,
    ChainConfig,
    TelegramConfig,
    load_chains,
    load_yaml,
    parse_telegram,
    parse_tokens,
)
from ..common.evm import TRANSFER_TOPIC, address_topic, chunked
from ..common.explorer import AddressInfo, Explorer, explorer_for
from ..common.labels import AddressBook
from ..common.runner import Alert, BaseAlerter, Valued
from ..common.telegram import esc, fmt_amount, fmt_usd, short
from .discovery import BURN, DiscoveryParams, classify, discover

log = logging.getLogger(__name__)


@dataclass
class TeamWallet:
    address: str
    name: str
    chains: list[str] | None = None  # None = every enabled chain
    auto: bool = False
    depth: int = 0


@dataclass
class AutoDiscoveryConfig:
    enabled: bool = True
    min_score: float = 3
    min_share_pct: float = 0.5
    top_holders: int = 50
    max_depth: int = 2
    refresh_hours: float = 24
    notify: bool = True  # Telegram summary when new wallets are found
    # Live "follow the money": a big outflow from a team wallet to a new
    # (non-exchange, non-DeFi) address makes that address a team wallet too.
    follow_outflows: bool = True
    follow_min_usd: float = 50_000
    follow_min_supply_pct: float = 0.5
    follow_max_depth: int = 2


@dataclass
class TeamBotConfig(BaseBotConfig):
    wallets: list[TeamWallet] = field(default_factory=list)
    exclude_wallets: set[str] = field(default_factory=set)
    auto_discovery: AutoDiscoveryConfig = field(default_factory=lambda: AutoDiscoveryConfig(enabled=False))
    # true: alert on ANY ERC-20 moving through team wallets, not only `tokens`.
    track_all_tokens: bool = False
    min_usd: float = 0.0
    # With track_all_tokens, skip tokens without a market price (airdrop spam).
    skip_unpriced: bool = True
    # Transfers between two team wallets.
    alert_internal: bool = True
    known_addresses: dict[str, str] = field(default_factory=dict)


def load_config(path: str, dry_run: bool = False) -> TeamBotConfig:
    raw = load_yaml(path)
    tokens = parse_tokens(raw.get("tokens"))
    wallets = []
    for w in raw.get("wallets") or []:
        chains = w.get("chains") or w.get("chain")
        if isinstance(chains, str):
            chains = None if chains in ("*", "all") else [chains]
        wallets.append(TeamWallet(address=w["address"].lower(), name=w.get("name") or short(w["address"]), chains=chains))
    ad_raw = raw.get("auto_discovery")
    ad = AutoDiscoveryConfig(**ad_raw) if isinstance(ad_raw, dict) else AutoDiscoveryConfig(enabled=ad_raw is not False)
    if not wallets and not ad.enabled:
        raise ValueError("team wallets bot: 'wallets' is empty and auto_discovery is disabled")
    if ad.enabled and not tokens:
        raise ValueError("team wallets bot: auto_discovery needs 'tokens' to look for team wallets")
    networks = set(raw.get("networks") or [])
    networks |= {t.chain for t in tokens}
    for w in wallets:
        networks |= set(w.chains or [])
    if not networks:
        raise ValueError("team wallets bot: no networks - set 'networks' or token/wallet chains")
    telegram = (
        TelegramConfig(bot_token="", chat_id="")
        if dry_run and not (raw.get("telegram") or {}).get("bot_token")
        else parse_telegram(raw.get("telegram"), "team wallets bot")
    )
    cfg = TeamBotConfig(
        telegram=telegram,
        chains=load_chains(raw.get("chains"), networks),
        tokens=tokens,
        state_file=raw.get("state_file", "state/team_wallets.json"),
        poll_interval=int(raw.get("poll_interval", 20)),
        labels_file=raw.get("labels_file"),
        wallets=wallets,
        exclude_wallets={a.lower() for a in raw.get("exclude_wallets") or []},
        auto_discovery=ad,
        track_all_tokens=bool(raw.get("track_all_tokens", False)),
        min_usd=float(raw.get("min_usd", 0) or 0),
        skip_unpriced=bool(raw.get("skip_unpriced", True)),
        alert_internal=bool(raw.get("alert_internal", True)),
        known_addresses={k.lower(): v for k, v in (raw.get("known_addresses") or {}).items()},
    )
    if not cfg.track_all_tokens and not cfg.tokens:
        raise ValueError("team wallets bot: set 'tokens' or enable 'track_all_tokens'")
    return cfg


class TeamWalletsAlerter(BaseAlerter):
    name = "team-wallets"
    cfg: TeamBotConfig

    def __init__(self, *args, book: AddressBook | None = None, explorers: dict[str, Explorer] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.book = book if book is not None else AddressBook.from_file(self.cfg.labels_file)
        self.explorers = explorers if explorers is not None else {
            n: explorer_for(c) for n, c in self.cfg.chains.items()
        }

    # --- wallet registry: manual + auto-discovered ------------------------------------
    @property
    def _auto(self) -> dict[str, dict[str, dict]]:
        return self.state.extra.setdefault("auto_wallets", {})

    def team_wallets(self, chain: str) -> dict[str, TeamWallet]:
        out: dict[str, TeamWallet] = {}
        if self.cfg.auto_discovery.enabled:
            for addr, w in self._auto.get(chain, {}).items():
                out[addr] = TeamWallet(addr, w["name"], [chain], auto=True, depth=w.get("depth", 0))
        for w in self.cfg.wallets:  # manual entries win (names, depth 0)
            if w.chains is None or chain in w.chains:
                out[w.address] = w
        for addr in self.cfg.exclude_wallets:
            out.pop(addr, None)
        return out

    def _add_auto(self, chain: str, addr: str, name: str, reasons: list[str], depth: int, token: str) -> bool:
        if addr in self.cfg.exclude_wallets or addr in self._auto.get(chain, {}):
            return False
        self._auto.setdefault(chain, {})[addr] = {
            "name": name, "reasons": reasons, "depth": depth, "token": token, "found_at": int(time.time()),
        }
        return True

    # --- discovery ---------------------------------------------------------------------
    def before_scan(self) -> None:
        ad = self.cfg.auto_discovery
        if not ad.enabled:
            return
        done = self.state.extra.setdefault("discovery_at", {})
        for t in self.cfg.tokens:
            key = f"{t.chain}:{t.address}"
            if time.time() - done.get(key, 0) < ad.refresh_hours * 3600:
                continue
            try:
                self.run_discovery(t.chain, t.address)
            except Exception:  # noqa: BLE001 - never block alerts because of discovery
                log.exception("discovery failed for %s", key)
            done[key] = time.time()
            self.state.save()

    def run_discovery(self, chain: str, token: str) -> list:
        ad = self.cfg.auto_discovery
        symbol = self.oracle.get(chain, token).symbol
        team, low = discover(
            chain, token, symbol, self.rpcs.get(chain), self.explorers[chain], self.book,
            DiscoveryParams(min_score=ad.min_score, min_share_pct=ad.min_share_pct,
                            top_holders=ad.top_holders, max_depth=ad.max_depth),
        )
        manual = {w.address for w in self.cfg.wallets}
        new = [c for c in team if c.address not in manual
               and self._add_auto(chain, c.address, f"🤖 {c.title}", c.reasons, 0, token)]
        if new and ad.notify:
            self.sender.send(self._discovery_text(chain, token, symbol, new, low))
        return new

    def _discovery_text(self, chain_name, token, symbol, new, low) -> str:
        chain = self.cfg.chains[chain_name]
        lines = [f"🤖 <b>Автопоиск командных кошельков {esc(symbol or short(token))}</b> ({esc(chain_name)})",
                 f"Добавлено в отслеживание: <b>{len(new)}</b>\n"]
        for c in new[:20]:
            lines.append(f'• <a href="{chain.address_url(c.address)}">{esc(c.title)}</a> — score {c.score:g}\n'
                         f"   <i>{esc('; '.join(c.reasons))}</i>")
        if len(new) > 20:
            lines.append(f"… и ещё {len(new) - 20}")
        if low:
            lines.append(f"\nНизкая уверенность (не отслеживаются): {len(low)} — "
                         + ", ".join(f'<a href="{chain.address_url(c.address)}">{short(c.address)}</a>' for c in low[:8]))
        lines.append("\nЛишний адрес — добавьте его в <code>exclude_wallets</code>.")
        return "\n".join(lines)

    def _follow(self, chain: ChainConfig, v: Valued, parent: TeamWallet) -> bool:
        """Big outflow from a team wallet to a fresh address -> start tracking that address."""
        ad = self.cfg.auto_discovery
        tr = v.transfer
        dst = tr.to_addr
        if not (ad.enabled and ad.follow_outflows) or parent.depth >= ad.follow_max_depth:
            return False
        if dst in BURN or dst in self.book or dst in self.cfg.exclude_wallets or dst == tr.token:
            return False
        supply = self._supply(chain.name, tr.token)
        by_usd = v.usd is not None and v.usd >= ad.follow_min_usd
        by_share = bool(supply) and tr.raw_amount * 100 >= supply * ad.follow_min_supply_pct
        if not (by_usd or by_share):
            return False
        rpc = self.rpcs.get(chain.name)
        is_contract = rpc.is_contract(dst) if rpc else None
        if is_contract is not False:
            # Contracts are followed only if they look like team infra (Safe, vesting...).
            try:
                info = self.explorers[chain.name].address_info(dst)
            except Exception:  # noqa: BLE001
                info = AddressInfo()
            if classify(info) != "team":
                return False
        return self._add_auto(chain.name, dst, f"🤖 от {parent.name.removeprefix('🤖 ')}",
                              [f"крупный перевод с командного кошелька {short(parent.address)}"],
                              parent.depth + 1, tr.token)

    def _supply(self, chain: str, token: str) -> int | None:
        cache = self.state.extra.setdefault("supply", {})
        key = f"{chain}:{token}"
        if key not in cache and self.rpcs.get(chain):
            cache[key] = self.rpcs[chain].total_supply(token)
        return cache.get(key)

    # --- scanning ------------------------------------------------------------------------
    def log_filters(self, chain: ChainConfig) -> list[dict]:
        wallets = [address_topic(a) for a in self.team_wallets(chain.name)]
        tokens = [t.address for t in self.cfg.tokens_for(chain.name)]
        if not wallets or (not tokens and not self.cfg.track_all_tokens):
            return []
        address = None if self.cfg.track_all_tokens else tokens
        filters = []
        # Topic positions are AND-ed, so "from OR to" needs two queries.
        for part in chunked(wallets, 100):
            filters.append({"address": address, "topics": [TRANSFER_TOPIC, part]})
            filters.append({"address": address, "topics": [TRANSFER_TOPIC, None, part]})
        return filters

    def _min_usd(self, chain: str, token: str) -> float:
        for t in self.cfg.tokens_for(chain):
            if t.address == token and t.min_usd is not None:
                return t.min_usd
        return self.cfg.min_usd

    def describe(self, chain: ChainConfig, addr: str, team: dict[str, TeamWallet]) -> str:
        link = f'<a href="{chain.address_url(addr)}">{short(addr)}</a>'
        if addr in team:
            return f"👥 <b>{esc(team[addr].name)}</b> ({link})"
        if addr in self.cfg.known_addresses:
            return f"{esc(self.cfg.known_addresses[addr])} ({link})"
        label = self.book.get(addr)
        if label:
            return f"🏦 <b>{esc(label.entity)}</b> [{esc(label.name)}] ({link})"
        if addr == "0x" + "0" * 40:
            return "🪙 mint / burn (0x0)"
        return link

    def handle(self, chain: ChainConfig, transfers: list[Valued]) -> list[Alert]:
        team = self.team_wallets(chain.name)
        configured = {t.address for t in self.cfg.tokens_for(chain.name)}
        alerts = []
        for v in transfers:
            tr = v.transfer
            src, dst = tr.from_addr in team, tr.to_addr in team
            if not (src or dst):
                continue
            if src and dst and not self.cfg.alert_internal:
                continue
            if tr.token not in configured:
                if not self.cfg.track_all_tokens:
                    continue
                if v.usd is None and self.cfg.skip_unpriced:
                    continue
            followed = src and not dst and tr.token in configured and self._follow(chain, v, team[tr.from_addr])
            min_usd = self._min_usd(chain.name, tr.token)
            if min_usd and v.usd is not None and v.usd < min_usd and not followed:
                continue
            if src and dst:
                head = "🔁 <b>Перевод между командными кошельками</b>"
            elif src:
                head = "🔴 <b>Исходящий перевод с командного кошелька</b>"
            else:
                head = "🟢 <b>Входящий перевод на командный кошелёк</b>"
            counter = tr.to_addr if src else tr.from_addr
            cex = self.book.get(counter) if counter not in team else None
            if cex:
                head += f"\n⚠️ {'Депозит на биржу' if src else 'Вывод с биржи'} <b>{esc(cex.entity)}</b>"
            text = (
                f"{head}\n\n"
                f"💰 <b>{fmt_amount(v.amount)} {esc(v.symbol)}</b> (~{fmt_usd(v.usd)})\n"
                f"⛓ {esc(chain.name)} · блок {tr.block}\n"
                f"От: {self.describe(chain, tr.from_addr, team)}\n"
                f"Кому: {self.describe(chain, tr.to_addr, team)}\n"
            )
            if followed:
                text += "🆕 Получатель автоматически добавлен в командные кошельки\n"
                team = self.team_wallets(chain.name)
            text += f'🔗 <a href="{chain.tx_url(tr.tx_hash)}">Транзакция</a>'
            alerts.append(Alert(key=f"team:{tr.key}", text=text))
        return alerts
